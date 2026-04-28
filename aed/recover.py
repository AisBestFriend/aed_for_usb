"""Filesystem-aware recovery from a disk image.

Strategy:
1. If the image (or a chosen partition slice) parses as FAT12/16/32, walk it
   in pure Python and copy every file we can read - including entries marked
   deleted (first byte 0xE5) when --include-deleted is set.
2. Otherwise we fall back to the OS:
     - Windows : mount the image read-only with `Mount-DiskImage` (PowerShell)
                 then robocopy /B every file off the mounted volume.
     - Linux   : losetup + mount -o ro,loop and cp -a.
   Both leave the source image untouched.
3. If the OS refuses to mount, the user should run `aed carve` instead.
"""

import os
import sys
import struct
import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterator, List, Optional

from .util import IS_WINDOWS, IS_LINUX, human_bytes


# ---------------------------------------------------------------------------
# Pure-python FAT walker (FAT12 / FAT16 / FAT32)
# ---------------------------------------------------------------------------


@dataclass
class FatLayout:
    sector_size: int
    cluster_size: int
    fat_offset: int
    root_offset: int
    data_offset: int
    fat_type: str            # "FAT12" / "FAT16" / "FAT32"
    fat_size: int
    total_clusters: int
    root_cluster: int = 0    # FAT32 only


def _parse_fat_bpb(buf: bytes, base_offset: int) -> Optional[FatLayout]:
    if len(buf) < 512 or buf[510:512] != b"\x55\xaa":
        return None
    bytes_per_sec = struct.unpack_from("<H", buf, 11)[0]
    sec_per_clus = buf[13]
    rsvd_sec = struct.unpack_from("<H", buf, 14)[0]
    n_fats = buf[16]
    root_ent = struct.unpack_from("<H", buf, 17)[0]
    tot_sec_16 = struct.unpack_from("<H", buf, 19)[0]
    fat_sz_16 = struct.unpack_from("<H", buf, 22)[0]
    tot_sec_32 = struct.unpack_from("<I", buf, 32)[0]
    fat_sz_32 = struct.unpack_from("<I", buf, 36)[0]
    if bytes_per_sec not in (512, 1024, 2048, 4096):
        return None
    if sec_per_clus == 0 or n_fats == 0:
        return None
    fat_size = fat_sz_16 if fat_sz_16 else fat_sz_32
    tot_sec = tot_sec_16 if tot_sec_16 else tot_sec_32
    if fat_size == 0 or tot_sec == 0:
        return None
    root_dir_sec = ((root_ent * 32) + (bytes_per_sec - 1)) // bytes_per_sec
    data_sec = tot_sec - (rsvd_sec + n_fats * fat_size + root_dir_sec)
    total_clusters = data_sec // sec_per_clus
    if total_clusters < 4085:
        fat_type = "FAT12"
    elif total_clusters < 65525:
        fat_type = "FAT16"
    else:
        fat_type = "FAT32"
    root_cluster = struct.unpack_from("<I", buf, 44)[0] if fat_type == "FAT32" else 0
    fat_offset = base_offset + rsvd_sec * bytes_per_sec
    root_offset = (
        base_offset + (rsvd_sec + n_fats * fat_size) * bytes_per_sec
    )
    data_offset = root_offset + (root_dir_sec * bytes_per_sec if fat_type != "FAT32" else 0)
    return FatLayout(
        sector_size=bytes_per_sec,
        cluster_size=sec_per_clus * bytes_per_sec,
        fat_offset=fat_offset,
        root_offset=root_offset,
        data_offset=data_offset,
        fat_type=fat_type,
        fat_size=fat_size * bytes_per_sec,
        total_clusters=total_clusters,
        root_cluster=root_cluster,
    )


def _next_cluster(fat: bytes, layout: FatLayout, cluster: int) -> int:
    if layout.fat_type == "FAT12":
        idx = cluster + (cluster // 2)
        if idx + 1 >= len(fat):
            return 0x0FFFFFFF
        v = fat[idx] | (fat[idx + 1] << 8)
        return (v >> 4) if (cluster & 1) else (v & 0x0FFF)
    if layout.fat_type == "FAT16":
        idx = cluster * 2
        if idx + 2 > len(fat):
            return 0xFFFF
        return struct.unpack_from("<H", fat, idx)[0]
    idx = cluster * 4
    if idx + 4 > len(fat):
        return 0x0FFFFFFF
    return struct.unpack_from("<I", fat, idx)[0] & 0x0FFFFFFF


def _is_eoc(layout: FatLayout, c: int) -> bool:
    if layout.fat_type == "FAT12":
        return c >= 0x0FF8
    if layout.fat_type == "FAT16":
        return c >= 0xFFF8
    return c >= 0x0FFFFFF8


def _decode_short_name(entry: bytes) -> str:
    name = entry[:8].decode("cp437", "ignore").rstrip(" ")
    ext = entry[8:11].decode("cp437", "ignore").rstrip(" ")
    return f"{name}.{ext}" if ext else name


def _walk_fat_dir(
    img,
    layout: FatLayout,
    fat: bytes,
    cluster: int,
    is_root_fixed: bool,
    fixed_off: int = 0,
    fixed_size: int = 0,
    path: str = "",
    include_deleted: bool = False,
) -> Iterator[dict]:
    """Yield {name,size,start_cluster,deleted,is_dir,path} for every entry."""
    chunks: List[bytes] = []
    if is_root_fixed:
        img.seek(fixed_off)
        chunks.append(img.read(fixed_size))
    else:
        c = cluster
        seen = set()
        while c >= 2 and not _is_eoc(layout, c) and c not in seen:
            seen.add(c)
            off = layout.data_offset + (c - 2) * layout.cluster_size
            img.seek(off)
            chunks.append(img.read(layout.cluster_size))
            c = _next_cluster(fat, layout, c)

    lfn_buffer: List[str] = []
    for chunk in chunks:
        for i in range(0, len(chunk), 32):
            e = chunk[i:i + 32]
            if len(e) < 32 or e[0] == 0x00:
                # 0x00 marks end of directory; bail out unless we're scanning
                # for deleted entries beyond it.
                if not include_deleted:
                    return
                continue
            attr = e[11]
            if attr == 0x0F:
                # LFN entry - collect Unicode chars
                seq = e[0]
                chars = (e[1:11] + e[14:26] + e[28:32]).decode(
                    "utf-16-le", "ignore"
                )
                chars = chars.split("\x00", 1)[0]
                if seq & 0x40:
                    lfn_buffer = [chars]
                else:
                    lfn_buffer.insert(0, chars)
                continue
            deleted = e[0] == 0xE5
            if deleted and not include_deleted:
                lfn_buffer = []
                continue
            if attr & 0x08:        # volume label
                lfn_buffer = []
                continue
            short = _decode_short_name(e)
            if deleted:
                short = "_" + short[1:]    # restore leading char as '_'
            long_name = "".join(lfn_buffer) if lfn_buffer else short
            lfn_buffer = []
            if long_name in (".", ".."):
                continue
            start = (
                struct.unpack_from("<H", e, 26)[0]
                | (struct.unpack_from("<H", e, 20)[0] << 16)
            )
            size = struct.unpack_from("<I", e, 28)[0]
            is_dir = bool(attr & 0x10)
            yield {
                "name": long_name,
                "size": size,
                "start_cluster": start,
                "deleted": deleted,
                "is_dir": is_dir,
                "path": path,
            }
            if is_dir and start >= 2 and not deleted:
                yield from _walk_fat_dir(
                    img, layout, fat, start, False,
                    path=os.path.join(path, long_name),
                    include_deleted=include_deleted,
                )


def _read_clusters(img, layout: FatLayout, fat: bytes, start: int, size: int) -> bytes:
    out = bytearray()
    c = start
    seen = set()
    while c >= 2 and not _is_eoc(layout, c) and c not in seen and len(out) < size:
        seen.add(c)
        off = layout.data_offset + (c - 2) * layout.cluster_size
        img.seek(off)
        out += img.read(layout.cluster_size)
        c = _next_cluster(fat, layout, c)
    return bytes(out[:size])


def fat_recover(
    image_path: str,
    out_dir: str,
    part_offset: int = 0,
    include_deleted: bool = False,
) -> int:
    """Walk a FAT volume in the image and copy files. Returns count."""
    os.makedirs(out_dir, exist_ok=True)
    with open(image_path, "rb") as img:
        img.seek(part_offset)
        bpb = img.read(512)
        layout = _parse_fat_bpb(bpb, part_offset)
        if not layout:
            print("[!] not a FAT volume at this offset")
            return 0
        print(
            f"[+] {layout.fat_type}  cluster={layout.cluster_size}B  "
            f"clusters={layout.total_clusters}"
        )
        img.seek(layout.fat_offset)
        fat = img.read(layout.fat_size)

        if layout.fat_type == "FAT32":
            entries = _walk_fat_dir(
                img, layout, fat, layout.root_cluster, False,
                include_deleted=include_deleted,
            )
        else:
            root_size = layout.data_offset - layout.root_offset
            entries = _walk_fat_dir(
                img, layout, fat, 0, True,
                fixed_off=layout.root_offset, fixed_size=root_size,
                include_deleted=include_deleted,
            )

        count = 0
        for ent in entries:
            if ent["is_dir"]:
                d = os.path.join(out_dir, ent["path"], ent["name"])
                os.makedirs(d, exist_ok=True)
                continue
            data = _read_clusters(img, layout, fat, ent["start_cluster"], ent["size"])
            sub = os.path.join(out_dir, ent["path"])
            os.makedirs(sub, exist_ok=True)
            tag = "deleted_" if ent["deleted"] else ""
            safe = tag + _safe_filename(ent["name"])
            with open(os.path.join(sub, safe), "wb") as fh:
                fh.write(data)
            count += 1
            print(
                f"    [{ 'DEL' if ent['deleted'] else 'OK ' }] "
                f"{os.path.join(ent['path'], safe)}  "
                f"({human_bytes(len(data))})"
            )
        print(f"[+] recovered {count} files into {out_dir}")
        return count


def _safe_filename(name: str) -> str:
    bad = '<>:"/\\|?*\0'
    return "".join(("_" if c in bad else c) for c in name).strip() or "unnamed"


# ---------------------------------------------------------------------------
# OS-mediated mount + copy fallback (NTFS / exFAT, etc.)
# ---------------------------------------------------------------------------


def os_mount_and_copy(image_path: str, out_dir: str) -> int:
    """Mount image read-only, copy files, then dismount."""
    os.makedirs(out_dir, exist_ok=True)
    if IS_WINDOWS:
        return _windows_mount_copy(image_path, out_dir)
    if IS_LINUX:
        return _linux_mount_copy(image_path, out_dir)
    print("[!] unsupported OS for mount-based recovery")
    return 0


def _windows_mount_copy(image_path: str, out_dir: str) -> int:
    img = os.path.abspath(image_path)
    ps_mount = (
        f"$r = Mount-DiskImage -ImagePath '{img}' -Access ReadOnly -PassThru;"
        f"($r | Get-Volume).DriveLetter"
    )
    try:
        letter = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_mount],
            text=True, stderr=subprocess.STDOUT,
        ).strip().splitlines()[-1]
    except subprocess.CalledProcessError as e:
        print("[!] Mount-DiskImage failed:", e.output)
        return 0
    if not letter:
        print("[!] mount succeeded but no drive letter assigned")
        return 0
    src = f"{letter}:\\"
    print(f"[+] mounted at {src} (read-only). copying...")
    try:
        # /B = backup mode (read locked files), /E recurse, /R:1 retry once
        rc = subprocess.call(
            ["robocopy", src, out_dir, "/E", "/B", "/R:1", "/W:1", "/NFL", "/NDL"]
        )
        # robocopy returns <8 on success
        ok = rc < 8
    finally:
        subprocess.call(
            ["powershell", "-NoProfile", "-Command",
             f"Dismount-DiskImage -ImagePath '{img}' | Out-Null"]
        )
    if not ok:
        print(f"[!] robocopy returned {rc}; some files may be missing")
    return _count_files(out_dir)


def _linux_mount_copy(image_path: str, out_dir: str) -> int:
    if not (shutil.which("mount") and shutil.which("losetup")):
        print("[!] mount/losetup not available")
        return 0
    mnt = out_dir + ".mnt"
    os.makedirs(mnt, exist_ok=True)
    loop = subprocess.check_output(
        ["losetup", "--show", "-r", "-f", "-P", image_path], text=True
    ).strip()
    try:
        # try every partition node
        candidates = [loop] + [f"{loop}p{i}" for i in range(1, 9)]
        mounted = None
        for c in candidates:
            if not os.path.exists(c):
                continue
            if subprocess.call(["mount", "-o", "ro,loop", c, mnt]) == 0:
                mounted = c
                break
        if not mounted:
            print("[!] could not mount any partition")
            return 0
        subprocess.call(["cp", "-a", mnt + "/.", out_dir])
        subprocess.call(["umount", mnt])
    finally:
        subprocess.call(["losetup", "-d", loop])
        try:
            os.rmdir(mnt)
        except OSError:
            pass
    return _count_files(out_dir)


def _count_files(root: str) -> int:
    n = 0
    for _, _, files in os.walk(root):
        n += len(files)
    return n
