#!/usr/bin/env python3
"""Build a single-file aed[.exe] using PyInstaller.

Usage:
    python build/build.py

Result:
    dist/aed.exe   (Windows)
    dist/aed       (Linux)

Requirements (build host only - end users don't need anything):
    pip install pyinstaller
"""

import os
import shutil
import subprocess
import sys


def main() -> int:
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    spec = os.path.join(repo, "build", "aed.spec")

    if shutil.which("pyinstaller") is None:
        print("[!] PyInstaller not found. install with:  pip install pyinstaller",
              file=sys.stderr)
        return 1

    cmd = [
        "pyinstaller", spec,
        "--clean",
        "--distpath", os.path.join(repo, "dist"),
        "--workpath", os.path.join(repo, "build", "_work"),
        "--noconfirm",
    ]
    print("[i] running:", " ".join(cmd))
    rc = subprocess.call(cmd, cwd=repo)
    if rc != 0:
        return rc

    name = "aed.exe" if sys.platform == "win32" else "aed"
    out = os.path.join(repo, "dist", name)
    if os.path.exists(out):
        size = os.path.getsize(out)
        print(f"[+] built: {out} ({size/1024/1024:.1f} MiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
