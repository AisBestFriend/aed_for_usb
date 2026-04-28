import os
import sys
import time
import ctypes
import platform
import subprocess


IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

# Set when this process is itself the elevated re-launch, so we don't loop.
_ELEVATED_ENV_FLAG = "AED_ELEVATED"


def init_console() -> None:
    """Make sure stdout/stderr can print Korean on a Windows console."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def is_admin() -> bool:
    """Return True when running with privileges required for raw disk access."""
    if IS_WINDOWS:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def _relaunch_windows_elevated() -> int:
    """Re-launch the current process via UAC. Returns child exit code."""
    SW_SHOWNORMAL = 1
    if getattr(sys, "frozen", False):
        # PyInstaller single-file exe: re-launch the exe directly.
        exe = sys.executable
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        exe = sys.executable
        params = subprocess.list2cmdline([os.path.abspath(sys.argv[0])] + sys.argv[1:])

    # Mark child so it doesn't try to elevate again.
    os.environ[_ELEVATED_ENV_FLAG] = "1"

    ShellExecuteW = ctypes.windll.shell32.ShellExecuteW
    ShellExecuteW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
    ]
    ShellExecuteW.restype = ctypes.c_void_p
    rc = ShellExecuteW(None, "runas", exe, params, None, SW_SHOWNORMAL)
    # Per docs, return value > 32 means success.
    if int(rc) <= 32:
        return -1
    return 0


def ensure_admin(auto_elevate: bool = True) -> None:
    """Ensure we're running with raw-disk privileges.

    On Windows, if we're not elevated and `auto_elevate` is True, re-launch
    ourselves via UAC and exit. The new (elevated) process gets the same args.
    On Linux, just instruct the user to re-run with sudo.
    """
    if is_admin():
        return

    already_tried = os.environ.get(_ELEVATED_ENV_FLAG) == "1"

    if IS_WINDOWS and auto_elevate and not already_tried:
        print("[i] 관리자 권한이 필요합니다. UAC 창을 띄웁니다 ...", file=sys.stderr)
        rc = _relaunch_windows_elevated()
        if rc == 0:
            # Hand off to the elevated child; this process is done.
            sys.exit(0)
        print(
            "[!] UAC 가 거부되었습니다. 디스크 직접 접근에는 관리자 권한이 필요합니다.\n"
            "    aed.exe 를 마우스 우클릭 -> '관리자 권한으로 실행' 으로 다시 시작하세요.",
            file=sys.stderr,
        )
        sys.exit(2)

    msg = (
        "[!] 디스크 직접 접근에는 관리자(Windows) / root(Linux) 권한이 필요합니다.\n"
        "    Windows : 우클릭 -> '관리자 권한으로 실행'\n"
        "    Linux   : sudo 로 다시 실행"
    )
    print(msg, file=sys.stderr)
    sys.exit(2)


# Backwards-compatible alias used elsewhere.
require_admin = ensure_admin


def explain_permission_error(err: OSError) -> str:
    """디스크 raw access OSError 를 사용자 안내 문구로 변환."""
    code = getattr(err, "winerror", None) or err.errno
    if IS_WINDOWS:
        if code in (5,):       # ERROR_ACCESS_DENIED
            return ("권한이 거부되었습니다. 관리자 권한으로 다시 실행하세요 "
                    "(우클릭 -> '관리자 권한으로 실행').")
        if code in (32,):      # ERROR_SHARING_VIOLATION
            return ("USB 가 다른 프로그램(파일 탐색기, 백신 등)에 잠겨 있습니다. "
                    "해당 창을 모두 닫고 다시 시도하세요.")
        if code in (21,):      # ERROR_NOT_READY
            return "장치가 준비되지 않았습니다. USB 를 다시 꽂고 시도하세요."
        if code in (1117,):    # ERROR_IO_DEVICE
            return ("하드웨어 I/O 오류 - 드라이브가 손상 중입니다. "
                    "이미저가 배드 섹터를 자동으로 건너뜁니다.")
    if IS_LINUX:
        if code in (13,):
            return "권한 거부. sudo 로 다시 실행하세요."
        if code in (16,):
            return "장치 사용 중. umount 후 다시 시도하세요."
    return str(err)


def human_bytes(n: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    f = float(n)
    for u in units:
        if f < 1024.0 or u == units[-1]:
            return f"{f:.2f} {u}"
        f /= 1024.0
    return f"{f:.2f} {units[-1]}"


class Progress:
    """Lightweight progress reporter that writes a single carriage-return line."""

    def __init__(self, total: int, label: str = "progress"):
        self.total = max(total, 1)
        self.label = label
        self.start = time.time()
        self.last_emit = 0.0

    def update(self, done: int, extra: str = "") -> None:
        now = time.time()
        if now - self.last_emit < 0.2 and done < self.total:
            return
        self.last_emit = now
        elapsed = now - self.start
        rate = done / elapsed if elapsed > 0 else 0.0
        pct = 100.0 * done / self.total
        eta = (self.total - done) / rate if rate > 0 else 0.0
        sys.stdout.write(
            f"\r{self.label}: {pct:6.2f}%  "
            f"{human_bytes(done)} / {human_bytes(self.total)}  "
            f"{human_bytes(int(rate))}/s  남은시간 {eta:6.1f}초 {extra}   "
        )
        sys.stdout.flush()

    def finish(self) -> None:
        self.update(self.total)
        sys.stdout.write("\n")
        sys.stdout.flush()
