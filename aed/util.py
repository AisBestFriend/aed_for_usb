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
        print("[i] requesting Administrator rights via UAC ...", file=sys.stderr)
        rc = _relaunch_windows_elevated()
        if rc == 0:
            # Hand off to the elevated child; this process is done.
            sys.exit(0)
        print(
            "[!] UAC was declined. Raw disk access requires Administrator.\n"
            "    Right-click the program and choose 'Run as administrator'.",
            file=sys.stderr,
        )
        sys.exit(2)

    msg = (
        "[!] Raw disk access requires Administrator (Windows) or root (Linux).\n"
        "    Windows : right-click -> 'Run as administrator'.\n"
        "    Linux   : re-run with sudo."
    )
    print(msg, file=sys.stderr)
    sys.exit(2)


# Backwards-compatible alias used elsewhere.
require_admin = ensure_admin


def explain_permission_error(err: OSError) -> str:
    """Translate a raw-disk OSError into actionable Korean+English advice."""
    code = getattr(err, "winerror", None) or err.errno
    if IS_WINDOWS:
        if code in (5,):       # ERROR_ACCESS_DENIED
            return (
                "ACCESS DENIED. Run the program as Administrator "
                "(right-click -> 'Run as administrator')."
            )
        if code in (32,):      # ERROR_SHARING_VIOLATION
            return (
                "The volume is locked by Windows. Close any File Explorer / "
                "antivirus window pointing at the USB and retry."
            )
        if code in (21,):      # ERROR_NOT_READY
            return "Device not ready. Replug the USB and retry."
        if code in (1117,):    # ERROR_IO_DEVICE
            return "Hardware I/O error - the drive is failing. Imaging will skip bad sectors."
    if IS_LINUX:
        if code in (13,):
            return "Permission denied. Re-run with sudo."
        if code in (16,):
            return "Device busy. Unmount it (umount) and retry."
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
            f"{human_bytes(int(rate))}/s  ETA {eta:6.1f}s {extra}   "
        )
        sys.stdout.flush()

    def finish(self) -> None:
        self.update(self.total)
        sys.stdout.write("\n")
        sys.stdout.flush()
