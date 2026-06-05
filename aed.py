#!/usr/bin/env python3
"""AED for USB - USB 안전 복구 도구.

사용법:
  aed                              # 대화형 마법사 (권장)
  aed scan                         # 디스크 목록 표시
  aed health                       # 디스크 건강 진단 (불량 USB 표시)
  aed image  <장치> <이미지파일>    # 배드 섹터를 건너뛰며 이미지 생성
  aed analyze <이미지파일>          # 이미지의 MBR/GPT 파티션 표시
  aed recover <이미지파일> <폴더>   # 이미지에서 FAT 살아있는 파일 복구
  aed mount-copy <이미지> <폴더>    # OS 마운트 후 파일 복사 (NTFS/exFAT)
  aed carve   <이미지|장치> <폴더>  # 시그니처 기반 카빙 (FS 깨졌을 때)

알아두기:
  - 관리자(Windows) / root(Linux) 권한이 필요합니다. Windows 는 자동으로
    UAC 권한 요청 창을 띄웁니다.
  - 원본 USB 는 절대 쓰지 않습니다. 이미지 파일에만 기록합니다.
  - 파일 탐색기에서 USB 를 못 열어도(RAW / 깨진 FS) 디스크 관리에서 보이기만
    하면 이 도구로 이미징 + 복구 가능합니다.
"""

import sys
import os

from aed import scanner, health, imager, analyzer, recover, carver, wizard
from aed.util import ensure_admin, human_bytes, init_console


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
    if len(args) < 2:
        print("사용법: aed image <장치경로> <저장할 이미지 파일> [--size 32G]")
        return 1
    ensure_admin()
    src, dst = args[0], args[1]
    total_override = 0
    rest = args[2:]
    if len(rest) >= 2 and rest[0] == "--size":
        total_override = _parse_size(rest[1])
    # Look up the scanner-reported size as a fallback for size detection.
    size_hint = 0
    try:
        for d in scanner.scan():
            if d.path.lower() == src.lower():
                size_hint = d.size
                break
    except Exception:
        pass
    stats = imager.image_device(src, dst, size_hint=size_hint,
                                total_override=total_override)
    imager.print_summary(src, dst, stats)
    return 0


def _parse_size(text: str) -> int:
    text = text.strip().lower().replace(" ", "")
    mult = 1
    for suffix, factor in (("gib", 1024 ** 3), ("gb", 1000 ** 3), ("g", 1024 ** 3),
                           ("mib", 1024 ** 2), ("mb", 1000 ** 2), ("m", 1024 ** 2),
                           ("tib", 1024 ** 4), ("tb", 1000 ** 4), ("t", 1024 ** 4)):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            mult = factor
            break
    try:
        return int(float(text) * mult)
    except ValueError:
        return 0


def cmd_analyze(args):
    if len(args) != 1:
        print("사용법: aed analyze <이미지 파일>")
        return 1
    parts = analyzer.analyze(args[0])
    analyzer.print_partitions(args[0], parts)
    return 0


def cmd_recover(args):
    if len(args) < 2:
        print("사용법: aed recover <이미지> <복구폴더> [--offset N] [--include-deleted]")
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
            print(f"알 수 없는 옵션: {rest[i]}")
            return 1
    recover.fat_recover(img, out, part_offset=offset,
                        include_deleted=include_deleted)
    return 0


def cmd_mount_copy(args):
    if len(args) != 2:
        print("사용법: aed mount-copy <이미지> <복구폴더>")
        return 1
    ensure_admin()
    n = recover.os_mount_and_copy(args[0], args[1])
    print(f"[+] {n} 개 파일을 복사했습니다")
    return 0


def cmd_carve(args):
    if len(args) < 2:
        print("사용법: aed carve <이미지|장치> <복구폴더> [--max N]")
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
    init_console()
    argv = argv or sys.argv[1:]
    interactive = not argv      # no args = launched by double-click
    rc = 0
    try:
        if not argv:
            rc = cmd_wizard([])
        elif argv[0] in ("-h", "--help", "help", "도움말"):
            rc = _usage()
        else:
            cmd = COMMANDS.get(argv[0])
            if cmd is None:
                print(f"알 수 없는 명령: {argv[0]}\n")
                rc = _usage()
            else:
                rc = cmd(argv[1:])
    except KeyboardInterrupt:
        print("\n[!] 사용자가 중단했습니다")
        rc = 130
    except RuntimeError as e:
        print(f"\n[!] {e}", file=sys.stderr)
        rc = 3
    except Exception as e:
        print(f"\n[!] 예기치 못한 오류: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        rc = 4
    if interactive:
        _pause_on_exit()
    return rc


def _pause_on_exit() -> None:
    """Keep the console window open so the user can read the error message
    when aed.exe was launched by double-click."""
    try:
        # only pause when stdin is a real console (i.e. interactive launch)
        if sys.stdin and sys.stdin.isatty():
            input("\n[엔터 키를 누르면 종료됩니다] ")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
