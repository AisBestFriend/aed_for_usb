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


def _device_size_windows(handle) -> int:
    # IOCTL_DISK_GET_LENGTH_INFO = 0x7405C
    IOCTL = 0x0007405C
    out = ctypes.create_string_buffer(8)
    ret = ctypes.c_uint32(0)
    ok = ctypes.windll.kernel32.DeviceIoControl(
        handle, IOCTL, None, 0, out, 8, ctypes.byref(ret), None
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "IOCTL_DISK_GET_LENGTH_INFO failed")
    return int.from_bytes(out.raw[:8], "little")


def _open_windows_raw(path: str):
    """Open \\.\PhysicalDriveN with FILE_SHARE_READ|WRITE for raw read."""
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_FLAG_NO_BUFFERING = 0x20000000
    FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
    INVALID = ctypes.c_void_p(-1).value

    CreateFileW = ctypes.windll.kernel32.CreateFileW
    CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    CreateFileW.restype = ctypes.c_void_p
    h = CreateFileW(
        path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_SEQUENTIAL_SCAN | FILE_FLAG_NO_BUFFERING,
        None,
    )
    if h is None or h == INVALID:
        err = ctypes.get_last_error()
        raise OSError(err, f"CreateFileW failed for {path} (err={err})")
    return h


def _read_windows(handle, offset: int, length: int) -> bytes:
    SetFilePointerEx = ctypes.windll.kernel32.SetFilePointerEx
    SetFilePointerEx.argtypes = [
        ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p, ctypes.c_uint32
    ]
    ReadFile = ctypes.windll.kernel32.ReadFile
    ReadFile.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    if not SetFilePointerEx(handle, ctypes.c_int64(offset), None, 0):
        raise OSError(ctypes.get_last_error(), "SetFilePointerEx failed")
    buf = ctypes.create_string_buffer(length)
    n = ctypes.c_uint32(0)
    if not ReadFile(handle, buf, length, ctypes.byref(n), None):
        raise OSError(ctypes.get_last_error(), "ReadFile failed")
    return buf.raw[: n.value]


def _close_windows(handle) -> None:
    ctypes.windll.kernel32.CloseHandle(handle)


def _device_size_posix(fd) -> int:
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.lseek(fd, 0, os.SEEK_SET)


def image_device(
    src: str,
    dst: str,
    block: int = DEFAULT_BLOCK,
    max_retries: int = 2,
    resume: bool = True,
) -> ImageStats:
    """Read `src` raw device into sparse file `dst`.

    Strategy: try big blocks for speed; on read error fall back to RETRY_BLOCK
    then SKIP_BLOCK. Bad ranges are zero-filled in the image and logged to
    `dst + ".aedlog"` so a later pass can re-attempt them.
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
            total = _device_size_posix(fd)
            read_fn = lambda off, ln: (os.lseek(fd, off, os.SEEK_SET), os.read(fd, ln))[1]
            close_fn = lambda: os.close(fd)
    except OSError as e:
        raise RuntimeError(
            f"Could not open {src}: {explain_permission_error(e)}"
        ) from e

    if total <= 0:
        close_fn()
        raise RuntimeError(
            f"{src} 의 용량을 확인할 수 없습니다. OS 가 장치를 인식하는지 확인하세요."
        )

    flags = "r+b" if (resume and os.path.exists(dst)) else "wb"
    out = open(dst, flags)
    if flags == "wb":
        # pre-allocate sparse file
        out.truncate(total)

    stats = ImageStats(total=total)
    prog = Progress(total, label=f"이미징 {os.path.basename(src)}")
    offset = resume_from

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
