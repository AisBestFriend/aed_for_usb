"""Pure-python raw disk imager with bad-sector handling.

Reads from \\.\PhysicalDriveN (Windows) or /dev/sdX (Linux) and writes a sparse
image to disk. Bad regions are retried, then skipped; their offsets are logged
to <image>.aedlog so the run can be resumed.

This works even when the filesystem is corrupt and the OS can't mount the
device, because we never touch the filesystem - only sectors.
"""

import os
import sys
import time
import ctypes
from dataclasses import dataclass
from typing import Optional

from .util import IS_WINDOWS, Progress, human_bytes, explain_permission_error


SECTOR = 512
DEFAULT_BLOCK = 1 * 1024 * 1024           # 1 MiB happy-path read
RETRY_BLOCK = 64 * 1024                    # 64 KiB retry granularity
SKIP_BLOCK = 4 * 1024                      # 4 KiB final skip granularity


@dataclass
class ImageStats:
    total: int = 0
    good: int = 0
    bad: int = 0
    retried: int = 0


# ---------------------------------------------------------------------------
# Windows raw-disk access via ctypes.
#
# CRITICAL: all kernel32 calls below MUST declare argtypes/restype. Without
# them ctypes marshals a Python int as a 32-bit C int, which truncates the
# 64-bit device HANDLE on 64-bit Windows and makes every call fail with
# ERROR_INVALID_HANDLE. We use a kernel32 bound with use_last_error=True so
# ctypes.get_last_error() reflects the real Win32 error.
# ---------------------------------------------------------------------------

if IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    HANDLE = ctypes.c_void_p
    DWORD = ctypes.c_uint32
    BOOL = ctypes.c_int
    LPVOID = ctypes.c_void_p

    _k32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, DWORD, DWORD, LPVOID, DWORD, DWORD, HANDLE,
    ]
    _k32.CreateFileW.restype = HANDLE

    _k32.DeviceIoControl.argtypes = [
        HANDLE, DWORD, LPVOID, DWORD, LPVOID, DWORD,
        ctypes.POINTER(DWORD), LPVOID,
    ]
    _k32.DeviceIoControl.restype = BOOL

    _k32.SetFilePointerEx.argtypes = [
        HANDLE, ctypes.c_int64, ctypes.POINTER(ctypes.c_int64), DWORD,
    ]
    _k32.SetFilePointerEx.restype = BOOL

    _k32.ReadFile.argtypes = [
        HANDLE, LPVOID, DWORD, ctypes.POINTER(DWORD), LPVOID,
    ]
    _k32.ReadFile.restype = BOOL

    _k32.CloseHandle.argtypes = [HANDLE]
    _k32.CloseHandle.restype = BOOL


def _device_size_windows(handle) -> int:
    """Return device byte length, trying two IOCTLs. 0 if both fail."""
    # IOCTL_DISK_GET_LENGTH_INFO -> GET_LENGTH_INFORMATION { LARGE_INTEGER }
    IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
    out = ctypes.create_string_buffer(8)
    ret = DWORD(0)
    if _k32.DeviceIoControl(handle, IOCTL_DISK_GET_LENGTH_INFO,
                            None, 0, out, 8, ctypes.byref(ret), None):
        return int.from_bytes(out.raw[:8], "little")

    # Fallback: IOCTL_DISK_GET_DRIVE_GEOMETRY_EX -> DISK_GEOMETRY_EX
    #   DISK_GEOMETRY (24 bytes) then LARGE_INTEGER DiskSize at offset 24.
    IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
    geo = ctypes.create_string_buffer(32)
    ret2 = DWORD(0)
    if _k32.DeviceIoControl(handle, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX,
                            None, 0, geo, 32, ctypes.byref(ret2), None):
        return int.from_bytes(geo.raw[24:32], "little")
    return 0


def _open_windows_raw(path: str):
    """Open \\.\PhysicalDriveN with FILE_SHARE_READ|WRITE for raw read."""
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    # NOTE: deliberately NOT using FILE_FLAG_NO_BUFFERING - it would require the
    # user-space buffer to be sector-aligned in memory, which
    # create_string_buffer does not guarantee, causing ERROR_INVALID_PARAMETER.
    FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
    INVALID = ctypes.c_void_p(-1).value

    h = _k32.CreateFileW(
        path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_SEQUENTIAL_SCAN,
        None,
    )
    if h is None or h == 0 or (h & 0xFFFFFFFFFFFFFFFF) == (INVALID & 0xFFFFFFFFFFFFFFFF):
        err = ctypes.get_last_error()
        raise OSError(0, f"CreateFileW failed for {path}", None, err)
    return h


def _read_windows(handle, offset: int, length: int) -> bytes:
    new_pos = ctypes.c_int64(0)
    if not _k32.SetFilePointerEx(handle, ctypes.c_int64(offset),
                                 ctypes.byref(new_pos), 0):  # FILE_BEGIN
        err = ctypes.get_last_error()
        raise OSError(0, "SetFilePointerEx failed", None, err)
    buf = ctypes.create_string_buffer(length)
    n = DWORD(0)
    if not _k32.ReadFile(handle, buf, length, ctypes.byref(n), None):
        err = ctypes.get_last_error()
        raise OSError(0, "ReadFile failed", None, err)
    return buf.raw[: n.value]


def _close_windows(handle) -> None:
    _k32.CloseHandle(handle)


def _device_size_posix(fd) -> int:
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.lseek(fd, 0, os.SEEK_SET)


class SizeUnknownError(RuntimeError):
    """Raised when the device size cannot be determined by any means."""


def _readable_at(read_fn, off: int) -> bool:
    """True if at least one sector in a small window around `off` reads OK.

    Reading a few nearby points avoids treating an isolated bad sector as the
    end of the device during capacity probing.
    """
    for delta in (0, SECTOR, 64 * 1024, 256 * 1024):
        try:
            d = read_fn(off + delta, SECTOR)
            if d and len(d) == SECTOR:
                return True
        except OSError:
            continue
    return False


def _discover_size(read_fn, cap: int = 4 * 1024 ** 4) -> int:
    """Find readable capacity by probing, when the OS won't report it.

    Returns 0 only if NOTHING is readable in the first ~16 MiB (controller
    likely dead). A bad sector 0 alone does not disqualify the device - we
    look for any readable anchor first. Then we exponentially grow a probe
    offset until a read fails and binary-search the boundary. `cap` is a
    4 TiB sanity limit.
    """
    # An isolated bad sector 0 must not make us declare the drive dead.
    anchors = [0, SECTOR, 4096, 64 * 1024, 1024 * 1024, 16 * 1024 * 1024]
    if not any(_readable_at(read_fn, a) for a in anchors):
        return 0

    last_good = SECTOR
    probe = 1024 * 1024
    while probe <= cap:
        if _readable_at(read_fn, probe):
            last_good = probe
            probe *= 2
        else:
            break
    lo, hi = last_good, min(probe, cap)
    while lo + SECTOR < hi:
        mid = (lo + hi) // 2
        mid -= mid % SECTOR
        if mid <= lo:
            break
        if _readable_at(read_fn, mid):
            lo = mid
        else:
            hi = mid
    return lo + SECTOR



def image_device(
    src: str,
    dst: str,
    block: int = DEFAULT_BLOCK,
    max_retries: int = 2,
    resume: bool = True,
    size_hint: int = 0,
    total_override: int = 0,
) -> ImageStats:
    """Read `src` raw device into sparse file `dst`.

    Strategy: try big blocks for speed; on read error fall back to RETRY_BLOCK
    then SKIP_BLOCK. Bad ranges are zero-filled in the image and logged to
    `dst + ".aedlog"` so a later pass can re-attempt them.

    `size_hint` is the device size as reported by the scanner (Get-Disk /
    lsblk). It is used as a fallback when the size IOCTL/seek fails.
    """
    log_path = dst + ".aedlog"
    bad_ranges = []                               # list of [start, length]
    resume_from = 0
    if resume and os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("# next="):
                    try:
                        resume_from = int(line.split("=", 1)[1])
                    except ValueError:
                        pass
                elif line and not line.startswith("#"):
                    s, n = line.split()
                    bad_ranges.append([int(s), int(n)])

    try:
        if IS_WINDOWS:
            h = _open_windows_raw(src)
            try:
                total = _device_size_windows(h)
            except OSError:
                total = 0
            read_fn = lambda off, ln: _read_windows(h, off, ln)
            close_fn = lambda: _close_windows(h)
        else:
            fd = os.open(src, os.O_RDONLY)
            try:
                total = _device_size_posix(fd)
            except OSError:
                total = 0
            read_fn = lambda off, ln: (os.lseek(fd, off, os.SEEK_SET), os.read(fd, ln))[1]
            close_fn = lambda: os.close(fd)
    except OSError as e:
        raise RuntimeError(
            f"{src} 을(를) 열 수 없습니다: {explain_permission_error(e)}"
        ) from e

    # 1) explicit override (user typed the capacity) wins.
    if total_override > 0:
        total = total_override
    # 2) scanner-reported size (Get-Disk / lsblk).
    elif total <= 0 and size_hint > 0:
        total = size_hint

    # 3) OS and scanner both gave nothing (e.g. failing USB reports 0 B).
    #    Probe the device by reading to discover the real capacity.
    if total <= 0:
        print("    [i] OS 가 용량을 보고하지 않습니다. 직접 읽어서 용량을 탐지합니다 ...")
        total = _discover_size(read_fn)
        if total > 0:
            print(f"    [+] 탐지된 읽기 가능 용량: {human_bytes(total)}")

    # Round down to a sector boundary - raw reads must be sector-aligned.
    total -= total % SECTOR

    if total <= 0:
        close_fn()
        raise SizeUnknownError(
            f"{src} 의 용량을 확인할 수 없고, 0번 섹터조차 읽지 못했습니다.\n"
            "    이는 USB 컨트롤러가 응답하지 않는 상태(심각한 하드웨어 고장)일 "
            "가능성이 높습니다.\n"
            "    USB 를 다른 포트/PC 에 꽂아보고, 그래도 0바이트로 보이면 "
            "소프트웨어로는 복구가 어렵습니다(칩-오프 등 전문업체 영역)."
        )

    flags = "r+b" if (resume and os.path.exists(dst)) else "wb"
    out = open(dst, flags)
    if flags == "wb":
        # pre-allocate sparse file
        out.truncate(total)

    stats = ImageStats(total=total)
    prog = Progress(total, label=f"이미징 {os.path.basename(src)}")
    offset = resume_from

    # Early-abort guard: if the drive yields nothing at all in the first chunk
    # of the run, don't grind through the whole (possibly forced) size with
    # retries on every block - bail out and report it as unreadable.
    EARLY_ABORT_AFTER = 32 * 1024 * 1024
    aborted_early = False

    def _try_read(off: int, length: int) -> Optional[bytes]:
        for attempt in range(max_retries + 1):
            try:
                data = read_fn(off, length)
                if not data:
                    return b""
                return data
            except OSError:
                stats.retried += 1
                time.sleep(0.05 * (attempt + 1))
        return None

    try:
        while offset < total:
            length = min(block, total - offset)
            data = _try_read(offset, length)
            if data is not None and len(data) == length:
                out.seek(offset)
                out.write(data)
                stats.good += length
                offset += length
                prog.update(offset, f"정상={human_bytes(stats.good)} "
                                    f"불량={human_bytes(stats.bad)}")
                continue

            # Slow-path: bisect down to small blocks, mark bad regions.
            sub = offset
            end = offset + length
            while sub < end:
                sublen = min(RETRY_BLOCK, end - sub)
                d2 = _try_read(sub, sublen)
                if d2 is not None and len(d2) == sublen:
                    out.seek(sub)
                    out.write(d2)
                    stats.good += sublen
                    sub += sublen
                else:
                    # Final granularity skip
                    finer = sub
                    finer_end = sub + sublen
                    while finer < finer_end:
                        flen = min(SKIP_BLOCK, finer_end - finer)
                        d3 = _try_read(finer, flen)
                        if d3 is not None and len(d3) == flen:
                            out.seek(finer)
                            out.write(d3)
                            stats.good += flen
                        else:
                            out.seek(finer)
                            out.write(b"\x00" * flen)
                            stats.bad += flen
                            bad_ranges.append([finer, flen])
                        finer += flen
                    sub += sublen
            offset = end
            prog.update(offset, f"정상={human_bytes(stats.good)} "
                                f"불량={human_bytes(stats.bad)}")

            if (stats.good == 0
                    and (offset - resume_from) >= EARLY_ABORT_AFTER):
                aborted_early = True
                break
    finally:
        prog.finish()
        out.flush()
        out.close()
        close_fn()
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(f"# src={src}\n")
            fh.write(f"# dst={dst}\n")
            fh.write(f"# total={total}\n")
            fh.write(f"# good={stats.good}\n")
            fh.write(f"# bad={stats.bad}\n")
            fh.write(f"# retried={stats.retried}\n")
            fh.write(f"# next={offset}\n")
            for s, n in bad_ranges:
                fh.write(f"{s} {n}\n")

    if aborted_early:
        print(f"\n    [!] 처음 {human_bytes(EARLY_ABORT_AFTER)} 에서 단 한 바이트도 "
              "읽지 못해 이미징을 조기 중단했습니다.")

    return stats


def print_summary(src: str, dst: str, stats: ImageStats) -> None:
    print()
    print(f"[+] 이미징 완료: {dst}")
    print(f"    원본 장치 :  {src}")
    print(f"    용량      :  {human_bytes(stats.total)}")
    print(f"    정상 영역 :  {human_bytes(stats.good)}")
    print(f"    불량 영역 :  {human_bytes(stats.bad)}")
    print(f"    재시도수  :  {stats.retried}")
    print(f"    로그 파일 :  {dst}.aedlog")
    if stats.bad:
        print(
            "[!] 읽지 못한 섹터가 있습니다. `aed image` 를 다시 실행하면 "
            "로그를 보고 이어서 재시도합니다."
        )
