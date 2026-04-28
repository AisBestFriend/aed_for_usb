"""Interactive wizard.

Walks the user through:
  1. Detect failing USBs
  2. Pick the source device
  3. Image it (with bad-sector handling, live progress)
  4. Choose what to recover (whole volume, a folder, or a glob like *.jpg)
  5. Choose where to write recovered files
  6. Run recovery and show progress
"""

import os
import sys
import fnmatch
from typing import Iterable, List, Optional

from . import scanner, health, imager, analyzer, recover, carver
from .util import human_bytes, IS_WINDOWS, require_admin


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
        print("    invalid choice, try again.")


def _ask_yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{d}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def run() -> int:
    print("=" * 70)
    print("  AED for USB - safe USB recovery wizard")
    print("=" * 70)

    require_admin()

    # ---- 1. detect ------------------------------------------------------
    print("\n[1/5] scanning for storage devices ...")
    devs = scanner.scan()
    if not devs:
        print("    no devices detected. is the USB plugged in?")
        return 1
    scanner.print_table(devs)

    print("\n[1/5] running health probe (this is read-only) ...")
    results = health.diagnose_all(devs)
    health.print_health(results)

    suspect = [
        i for i, r in enumerate(results)
        if r.label in ("BAD", "WARN", "UNREADABLE")
    ]
    if suspect:
        print(
            f"\n[!] {len(suspect)} device(s) flagged as suspect "
            "(BAD/WARN/UNREADABLE) - those are the likely recovery targets."
        )
    else:
        print("\n[i] no obvious failures detected. you can still recover any device.")

    # ---- 2. pick source -------------------------------------------------
    options = [f"{d.path}  ({human_bytes(d.size)}, {d.bus}, {d.model})"
               for d in devs]
    idx = _ask_choice("\n[2/5] pick the SOURCE device", options)
    src = devs[idx]
    print(f"    -> {src.path}")

    # ---- 3. image -------------------------------------------------------
    default_img = os.path.join(
        os.getcwd(),
        f"usb_{os.path.basename(src.path).replace(chr(92), '_')}.img",
    )
    img_path = _ask("\n[3/5] image file path to write to", default=default_img)
    if os.path.exists(img_path):
        if _ask_yes(f"    '{img_path}' exists. resume?", default=True):
            resume = True
        else:
            os.remove(img_path)
            log = img_path + ".aedlog"
            if os.path.exists(log):
                os.remove(log)
            resume = False
    else:
        resume = False

    print(f"    imaging (this may take a while; bad sectors will be skipped + retried)")
    stats = imager.image_device(src.path, img_path, resume=resume)
    imager.print_summary(src.path, img_path, stats)

    # ---- 4. plan recovery ----------------------------------------------
    parts = analyzer.analyze(img_path)
    analyzer.print_partitions(img_path, parts)

    print("\n[4/5] choose recovery STRATEGY")
    strategies = [
        "Filesystem walk  - copy live files (FAT12/16/32, fast & accurate)",
        "Mount + copy     - let the OS read it (NTFS/exFAT)",
        "Signature carve  - extract by file headers (use if FS is destroyed)",
    ]
    strat = _ask_choice("strategy", strategies)

    out_root = _ask(
        "[4/5] recovered files target DIRECTORY",
        default=os.path.join(os.getcwd(), "recovered"),
    )
    os.makedirs(out_root, exist_ok=True)

    # ---- 5. execute -----------------------------------------------------
    print("\n[5/5] running recovery ...")
    if strat == 0:
        return _do_fs_walk(img_path, parts, out_root)
    if strat == 1:
        return _do_os_mount(img_path, out_root)
    return _do_carve(img_path, out_root)


def _do_fs_walk(img_path: str, parts, out_root: str) -> int:
    if not parts:
        if not _ask_yes("no partitions detected. try treating image as a "
                        "single FAT volume?", default=True):
            return 1
        offset = 0
    else:
        opts = [p.describe() for p in parts]
        idx = _ask_choice("which partition to recover", opts)
        offset = parts[idx].start

    # filtering
    print("\nrecovery scope:")
    print("  1) ALL files")
    print("  2) only one folder (path inside the volume, e.g. 'DCIM/Camera')")
    print("  3) only files matching a pattern (e.g. '*.jpg', '*.docx')")
    print("  4) include DELETED files too")
    raw = input("scope (default 1; comma-combine, e.g. '3,4'): ").strip() or "1"
    flags = {x.strip() for x in raw.split(",") if x.strip()}

    folder_filter = ""
    pattern = ""
    include_deleted = "4" in flags
    if "2" in flags:
        folder_filter = _ask("folder path inside the volume").strip("/\\")
    if "3" in flags:
        pattern = _ask("filename glob pattern", default="*.*")

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

    # Re-implement walk with filters
    with open(img_path, "rb") as img:
        img.seek(offset)
        bpb = img.read(512)
        layout = recover._parse_fat_bpb(bpb, offset)
        if not layout:
            print("[!] not a FAT volume at this offset; try strategy 2 or 3.")
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
            print(f"    [{ 'DEL' if ent['deleted'] else 'OK ' }] "
                  f"{full_rel}  ({human_bytes(len(data))})")
        print(f"\n[+] recovered {n} matching files into {out_dir}")
    return 0


def _do_os_mount(img_path: str, out_dir: str) -> int:
    n = recover.os_mount_and_copy(img_path, out_dir)
    print(f"[+] copied {n} files via OS mount.")
    return 0


def _do_carve(img_path: str, out_dir: str) -> int:
    raw = _ask("max files to recover (0 = unlimited)", default="0")
    try:
        max_files = int(raw)
    except ValueError:
        max_files = 0
    carver.carve(img_path, out_dir, max_files=max_files)
    return 0
