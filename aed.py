#!/usr/bin/env python3
"""AED for USB - safe USB recovery toolkit.

Usage:
  aed                              # interactive wizard (recommended)
  aed scan                         # list block devices
  aed health                       # probe devices, flag failing USBs
  aed image  <DEV> <OUT.img>       # image a device, skipping bad sectors
  aed analyze <IMG>                # show MBR/GPT partitions in image
  aed recover <IMG> <OUTDIR>       # walk FAT in image, copy live files
  aed mount-copy <IMG> <OUTDIR>    # OS-mount image read-only and copy
  aed carve   <IMG|DEV> <OUTDIR>   # signature-based carving (no FS needed)

Notes:
  - Requires Administrator (Windows) / root (Linux). On Windows the program
    auto-requests UAC elevation.
  - Operations on the SOURCE device are read-only. The image is the only file
    that ever gets written to.
  - Even when File Explorer cannot open the USB (RAW / corrupt FS), this tool
    can still image and recover from it as long as Disk Management sees it.
"""

import sys
import os

from aed import scanner, health, imager, analyzer, recover, carver, wizard
from aed.util import ensure_admin, human_bytes


def _usage() -> int:
    print(__doc__)
    return 1


def cmd_scan(_args):
    ensure_admin()
    scanner.print_table(scanner.scan())
    return 0


def cmd_health(_args):
    ensure_admin()
    devs = scanner.scan()
    health.print_health(health.diagnose_all(devs))
    return 0


def cmd_image(args):
    if len(args) != 2:
        print("usage: aed image <DEVICE> <OUT.img>")
        return 1
    ensure_admin()
    src, dst = args
    stats = imager.image_device(src, dst)
    imager.print_summary(src, dst, stats)
    return 0


def cmd_analyze(args):
    if len(args) != 1:
        print("usage: aed analyze <IMAGE>")
        return 1
    parts = analyzer.analyze(args[0])
    analyzer.print_partitions(args[0], parts)
    return 0


def cmd_recover(args):
    if len(args) < 2:
        print("usage: aed recover <IMAGE> <OUTDIR> [--offset N] [--include-deleted]")
        return 1
    img, out = args[0], args[1]
    rest = args[2:]
    offset = 0
    include_deleted = False
    i = 0
    while i < len(rest):
        if rest[i] == "--offset":
            offset = int(rest[i + 1])
            i += 2
        elif rest[i] == "--include-deleted":
            include_deleted = True
            i += 1
        else:
            print(f"unknown option: {rest[i]}")
            return 1
    recover.fat_recover(img, out, part_offset=offset,
                        include_deleted=include_deleted)
    return 0


def cmd_mount_copy(args):
    if len(args) != 2:
        print("usage: aed mount-copy <IMAGE> <OUTDIR>")
        return 1
    ensure_admin()
    n = recover.os_mount_and_copy(args[0], args[1])
    print(f"[+] {n} files copied")
    return 0


def cmd_carve(args):
    if len(args) < 2:
        print("usage: aed carve <IMAGE|DEVICE> <OUTDIR> [--max N]")
        return 1
    src, out = args[0], args[1]
    max_files = 0
    if len(args) >= 4 and args[2] == "--max":
        max_files = int(args[3])
    if src.startswith(r"\\.") or src.startswith("/dev/"):
        ensure_admin()
    carver.carve(src, out, max_files=max_files)
    return 0


def cmd_wizard(_args):
    return wizard.run()


COMMANDS = {
    "scan": cmd_scan,
    "health": cmd_health,
    "image": cmd_image,
    "analyze": cmd_analyze,
    "recover": cmd_recover,
    "mount-copy": cmd_mount_copy,
    "carve": cmd_carve,
    "wizard": cmd_wizard,
}


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        return cmd_wizard([])
    if argv[0] in ("-h", "--help", "help"):
        return _usage()
    cmd = COMMANDS.get(argv[0])
    if cmd is None:
        print(f"unknown command: {argv[0]}\n")
        return _usage()
    try:
        return cmd(argv[1:])
    except KeyboardInterrupt:
        print("\n[!] interrupted")
        return 130
    except RuntimeError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
