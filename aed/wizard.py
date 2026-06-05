"""대화형 마법사.

사용자를 다음 흐름으로 안내합니다:
  1. 불량 USB 자동 탐지
  2. 복구할 USB 선택
  3. 안전한 이미지 생성 (배드 섹터 자동 처리, 실시간 진행 표시)
  4. 복구 범위 선택 (전체 / 폴더 / 패턴 / 삭제 파일 포함)
  5. 대상 폴더 선택 후 복구 실행
"""

import os
import sys
import fnmatch
from typing import Iterable, List, Optional

from . import scanner, health, imager, analyzer, recover, carver
from .util import (
    human_bytes, IS_WINDOWS, require_admin,
    desktop_dir, open_in_file_manager,
)


IMAGE_FOLDER_NAME = "임시usb이미지"
RECOVERY_FOLDER_NAME = "usb복구폴더"


def _default_image_dir() -> str:
    return os.path.join(desktop_dir(), IMAGE_FOLDER_NAME)


def _default_recovery_dir() -> str:
    return os.path.join(desktop_dir(), RECOVERY_FOLDER_NAME)


def _safe_devname(path: str) -> str:
    return os.path.basename(path).replace("\\", "_").replace("/", "_") or "device"


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def _ask_choice(prompt: str, options: List[str]) -> int:
    while True:
        for i, opt in enumerate(options, 1):
            print(f"  {i}) {opt}")
        raw = input(f"{prompt} (1-{len(options)}): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print("    잘못된 입력입니다. 다시 시도하세요.")


def _ask_yes(prompt: str, default: bool = True) -> bool:
    d = "예/아니오 (Y/n)" if default else "예/아니오 (y/N)"
    raw = input(f"{prompt} [{d}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "예", "ㅇ", "y.")


def run() -> int:
    print("=" * 70)
    print("  AED for USB - USB 안전 복구 마법사")
    print("=" * 70)

    require_admin()

    # ---- 1. 장치 탐지 ----------------------------------------------------
    print("\n[1/5] 디스크를 검색합니다 ...")
    devs = scanner.scan()
    if not devs:
        print("    검색된 디스크가 없습니다. USB 가 연결되어 있는지 확인하세요.")
        return 1
    scanner.print_table(devs)

    print("\n[1/5] USB 건강 상태를 진단합니다 (읽기만 수행, 안전) ...")
    results = health.diagnose_all(devs)
    health.print_health(results)

    suspect = [
        i for i, r in enumerate(results)
        if r.label in ("BAD", "WARN", "UNREADABLE")
    ]
    if suspect:
        print(
            f"\n[!] {len(suspect)} 개 장치가 의심 상태(불량/주의/읽기실패)로 감지되었습니다. "
            "이런 장치가 보통 복구 대상입니다."
        )
    else:
        print("\n[i] 명백한 결함은 발견되지 않았습니다. 원하는 장치를 선택해 진행하세요.")

    # ---- 2. 소스 선택 ----------------------------------------------------
    options = [f"{d.path}  ({human_bytes(d.size)}, {d.bus}, {d.model})"
               for d in devs]
    idx = _ask_choice("\n[2/5] 복구할 USB(원본 장치)를 선택하세요", options)
    src = devs[idx]
    print(f"    -> {src.path} 선택됨")

    # ---- 3. 이미지 생성 --------------------------------------------------
    default_img_dir = _default_image_dir()
    os.makedirs(default_img_dir, exist_ok=True)
    default_img = os.path.join(default_img_dir, f"usb_{_safe_devname(src.path)}.img")

    print("\n[3/5] 디스크 이미지를 저장할 위치")
    print(f"    기본 경로 : {default_img}")
    print("    [TIP] 잘 모르시면 그냥 [Enter] 키를 누르세요. 바탕화면의 "
          f"'{IMAGE_FOLDER_NAME}' 폴더에 자동으로 저장됩니다.")
    img_path = _ask("이미지 파일 경로", default=default_img)
    if os.path.exists(img_path):
        if _ask_yes(f"    '{img_path}' 가 이미 존재합니다. 이어받기 하시겠습니까?",
                    default=True):
            resume = True
        else:
            os.remove(img_path)
            log = img_path + ".aedlog"
            if os.path.exists(log):
                os.remove(log)
            resume = False
    else:
        resume = False

    if src.size <= 0:
        print("    [!] 이 장치는 OS 가 용량을 0바이트로 보고합니다(고장 USB 의 흔한 증상).")
        print("        먼저 직접 읽어서 용량 자동 탐지를 시도하고, 안 되면 직접 입력하게 됩니다.")

    print("    이미징 시작 - 시간이 걸릴 수 있습니다. 배드 섹터는 자동 재시도/스킵합니다.")
    stats = _image_with_size_fallback(src, img_path, resume)
    if stats is None:
        return 1
    imager.print_summary(src.path, img_path, stats)

    if stats.good <= 0:
        print()
        print("=" * 70)
        print("  [결론] 이 USB 에서 단 한 바이트도 읽지 못했습니다.")
        print("=" * 70)
        print("  USB 컨트롤러가 응답하지 않는 하드웨어 고장으로 보입니다.")
        print("  이 경우 어떤 소프트웨어로도 복구가 불가능하며, NAND 칩을 직접")
        print("  읽는 '칩-오프' 같은 물리 복구(데이터 복구 전문업체)가 필요합니다.")
        print("  - 다른 USB 포트 / 다른 PC 에 꽂아 한 번 더 시도해 보세요.")
        print("  - 인식이 들쑥날쑥하면, 인식되는 순간 곧바로 다시 실행해 보세요.")
        return 1

    if stats.bad > 0:
        readable_pct = 100.0 * stats.good / max(stats.total, 1)
        print(f"    [i] 전체의 {readable_pct:.1f}% 를 읽었습니다. 읽은 부분에서 "
              "최대한 복구를 진행합니다.")

    # ---- 4. 복구 전략 ----------------------------------------------------
    parts = analyzer.analyze(img_path)
    analyzer.print_partitions(img_path, parts)

    print("\n[4/5] 복구 방식을 선택하세요")
    strategies = [
        "파일시스템 워크   - 살아있는 파일 그대로 복구 (FAT12/16/32, 가장 정확)",
        "OS 마운트 후 복사  - NTFS / exFAT 처럼 OS 가 읽을 수 있을 때",
        "시그니처 카빙     - 파일시스템이 망가졌을 때 헤더로 추출 (마지막 수단)",
    ]
    strat = _ask_choice("복구 방식", strategies)

    default_recovery = _default_recovery_dir()
    print("\n[4/5] 복구된 파일을 저장할 폴더")
    print(f"    기본 경로 : {default_recovery}")
    print("    [TIP] 잘 모르시면 그냥 [Enter] 키를 누르세요. 바탕화면의 "
          f"'{RECOVERY_FOLDER_NAME}' 폴더에 자동으로 저장됩니다.")
    out_root = _ask("복구 폴더", default=default_recovery)
    os.makedirs(out_root, exist_ok=True)

    # ---- 5. 실행 ---------------------------------------------------------
    print("\n[5/5] 복구를 시작합니다 ...")
    if strat == 0:
        rc = _do_fs_walk(img_path, parts, out_root)
    elif strat == 1:
        rc = _do_os_mount(img_path, out_root)
    else:
        rc = _do_carve(img_path, out_root)

    _print_final_summary(img_path, out_root)
    return rc


def _parse_capacity(text: str) -> int:
    """'32G', '16gb', '32000000000', '7.5 GiB' -> bytes. 0 if unparseable."""
    text = text.strip().lower().replace(" ", "")
    if not text:
        return 0
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


def _image_with_size_fallback(src, img_path: str, resume: bool):
    """Run imaging; if size can't be detected, let the user enter it and retry.

    Returns ImageStats, or None if the user gives up.
    """
    try:
        return imager.image_device(src.path, img_path, resume=resume,
                                   size_hint=src.size)
    except imager.SizeUnknownError as e:
        print(f"\n[!] {e}")
        print("\n  USB 표면/포장에 적힌 용량을 알고 계시면 직접 입력해 강제로 시도할 수 있습니다.")
        print("  예: 32G, 16GB, 64g  (모르면 그냥 [Enter] -> 복구 중단)")
        raw = input("  USB 실제 용량 입력: ").strip()
        cap = _parse_capacity(raw)
        if cap <= 0:
            print("  [i] 용량 입력이 없어 복구를 중단합니다.")
            return None
        print(f"  [+] {human_bytes(cap)} 로 강제 이미징을 시도합니다 "
              "(읽을 수 없는 부분은 자동 스킵).")
        try:
            return imager.image_device(src.path, img_path, resume=resume,
                                       total_override=cap)
        except imager.SizeUnknownError:
            print("  [!] 그래도 0번 섹터를 읽지 못했습니다. 하드웨어 고장으로 보입니다.")
            return None


def _print_final_summary(img_path: str, out_root: str) -> None:
    img_path = os.path.abspath(img_path)
    out_root = os.path.abspath(out_root)
    n_files = 0
    total_bytes = 0
    for root, _dirs, files in os.walk(out_root):
        for f in files:
            try:
                total_bytes += os.path.getsize(os.path.join(root, f))
                n_files += 1
            except OSError:
                pass

    print()
    print("=" * 70)
    print("  복구 완료 안내")
    print("=" * 70)
    print(f"  복구된 파일 위치 :  {out_root}")
    print(f"  복구된 파일 수   :  {n_files} 개  ({human_bytes(total_bytes)})")
    print(f"  디스크 이미지    :  {img_path}")
    try:
        img_size = os.path.getsize(img_path)
        print(f"                      ({human_bytes(img_size)})")
    except OSError:
        pass
    print()
    print("  [안내]")
    print("   - 복구된 파일이 정상인지 확인하신 뒤,")
    print(f"     디스크 이미지(.img) 는 용량을 차지하므로 더 이상 필요 없으면 삭제하셔도 됩니다.")
    print("   - 복구가 부족하면 같은 이미지로 다른 방식(시그니처 카빙 등)을 다시 시도할 수 있습니다.")
    print("=" * 70)

    if _ask_yes("\n복구된 폴더를 지금 열어볼까요?", default=True):
        if open_in_file_manager(out_root):
            print(f"[+] 탐색기에서 열었습니다: {out_root}")
        else:
            print(f"[!] 자동으로 열지 못했습니다. 위 경로를 직접 열어 확인하세요.")


def _do_fs_walk(img_path: str, parts, out_root: str) -> int:
    if not parts:
        if not _ask_yes("파티션을 찾지 못했습니다. 이미지 전체를 단일 FAT 볼륨으로 시도하시겠습니까?",
                        default=True):
            return 1
        offset = 0
    else:
        opts = [p.describe() for p in parts]
        idx = _ask_choice("복구할 파티션을 선택하세요", opts)
        offset = parts[idx].start

    # 필터링 옵션
    print("\n복구 범위 선택:")
    print("  1) 전체 파일")
    print("  2) 특정 폴더만 (볼륨 안 경로, 예: 'DCIM/Camera')")
    print("  3) 패턴 일치만 (예: '*.jpg', '*.docx')")
    print("  4) 삭제된 파일도 포함")
    raw = input("선택 (기본 1; 콤마로 조합 가능, 예 '3,4'): ").strip() or "1"
    flags = {x.strip() for x in raw.split(",") if x.strip()}

    folder_filter = ""
    pattern = ""
    include_deleted = "4" in flags
    if "2" in flags:
        folder_filter = _ask("볼륨 내부 폴더 경로").strip("/\\")
    if "3" in flags:
        pattern = _ask("파일명 패턴", default="*.*")

    return _filtered_fat_recover(
        img_path, offset, out_root,
        folder_filter=folder_filter,
        pattern=pattern,
        include_deleted=include_deleted,
    )


def _filtered_fat_recover(
    img_path: str, offset: int, out_dir: str,
    folder_filter: str = "", pattern: str = "",
    include_deleted: bool = False,
) -> int:
    if not folder_filter and not pattern:
        recover.fat_recover(img_path, out_dir, part_offset=offset,
                            include_deleted=include_deleted)
        return 0

    with open(img_path, "rb") as img:
        img.seek(offset)
        bpb = img.read(512)
        layout = recover._parse_fat_bpb(bpb, offset)
        if not layout:
            print("[!] 이 위치에 FAT 볼륨이 없습니다. 다른 방식(2번/3번)을 시도하세요.")
            return 1
        img.seek(layout.fat_offset)
        fat = img.read(layout.fat_size)
        if layout.fat_type == "FAT32":
            entries = recover._walk_fat_dir(
                img, layout, fat, layout.root_cluster, False,
                include_deleted=include_deleted,
            )
        else:
            root_size = layout.data_offset - layout.root_offset
            entries = recover._walk_fat_dir(
                img, layout, fat, 0, True,
                fixed_off=layout.root_offset, fixed_size=root_size,
                include_deleted=include_deleted,
            )
        n = 0
        for ent in entries:
            if ent["is_dir"]:
                continue
            full_rel = os.path.join(ent["path"], ent["name"])
            if folder_filter and not full_rel.lower().startswith(
                folder_filter.lower().replace("\\", "/").rstrip("/")
            ):
                continue
            if pattern and not fnmatch.fnmatch(ent["name"].lower(), pattern.lower()):
                continue
            data = recover._read_clusters(
                img, layout, fat, ent["start_cluster"], ent["size"]
            )
            sub = os.path.join(out_dir, ent["path"])
            os.makedirs(sub, exist_ok=True)
            tag = "deleted_" if ent["deleted"] else ""
            safe = tag + recover._safe_filename(ent["name"])
            with open(os.path.join(sub, safe), "wb") as fh:
                fh.write(data)
            n += 1
            print(f"    [{ '삭제됨' if ent['deleted'] else '정상' }] "
                  f"{full_rel}  ({human_bytes(len(data))})")
        print(f"\n[+] 조건과 일치하는 {n} 개 파일을 {out_dir} 로 복구했습니다")
    return 0


def _do_os_mount(img_path: str, out_dir: str) -> int:
    n = recover.os_mount_and_copy(img_path, out_dir)
    print(f"[+] OS 마운트 방식으로 {n} 개 파일을 복사했습니다.")
    return 0


def _do_carve(img_path: str, out_dir: str) -> int:
    raw = _ask("최대 추출 파일 수 (0 = 무제한)", default="0")
    try:
        max_files = int(raw)
    except ValueError:
        max_files = 0
    carver.carve(img_path, out_dir, max_files=max_files)
    return 0
