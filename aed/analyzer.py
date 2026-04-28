"""MBR / GPT / FAT / exFAT / NTFS detection for raw images."""

import os
import struct
from dataclasses import dataclass
from typing import List, Optional

from .util import human_bytes


SECTOR = 512


@dataclass
class Partition:
    index: int
    start: int           # byte offset within image
    size: int            # bytes
    type_hint: str       # FAT12/FAT16/FAT32/EXFAT/NTFS/RAW/...
    label: str = ""

    def describe(self) -> str:
        return (
            f"  #{self.index}  off={self.start:>14}  "
            f"size={human_bytes(self.size):>10}  {self.type_hint:<6}  {self.label}"
        )


def _read(fh, off: int, n: int) -> bytes:
    fh.seek(off)
    return fh.read(n)


def _detect_fs(fh, off: int) -> str:
    """Inspect the partition boot sector to guess the filesystem."""
    head = _read(fh, off, 512)
    if len(head) < 512:
        return "RAW"
    if head[3:11] == b"NTFS    ":
        return "NTFS"
    if head[3:11] == b"EXFAT   ":
        return "EXFAT"
    # FAT detection: filesystem type field
    if head[54:62].rstrip() in (b"FAT12", b"FAT16", b"FAT"):
        return head[54:62].rstrip().decode("ascii", "ignore") or "FAT"
    if head[82:90].rstrip() == b"FAT32":
        return "FAT32"
    return "RAW"


def _parse_mbr(fh, total: int) -> List[Partition]:
    sec0 = _read(fh, 0, 512)
    if len(sec0) < 512 or sec0[510:512] != b"\x55\xaa":
        return []
    parts: List[Partition] = []
    for i in range(4):
        e = sec0[446 + i * 16: 446 + (i + 1) * 16]
        ptype = e[4]
        lba = struct.unpack_from("<I", e, 8)[0]
        sectors = struct.unpack_from("<I", e, 12)[0]
        if ptype == 0 or sectors == 0:
            continue
        start = lba * SECTOR
        size = sectors * SECTOR
        if start + size > total:
            size = max(0, total - start)
        parts.append(
            Partition(
                index=i + 1,
                start=start,
                size=size,
                type_hint=_detect_fs(fh, start),
            )
        )
    return parts


def _parse_gpt(fh, total: int) -> List[Partition]:
    hdr = _read(fh, SECTOR, SECTOR)
    if hdr[:8] != b"EFI PART":
        return []
    part_lba = struct.unpack_from("<Q", hdr, 72)[0]
    n_entries = struct.unpack_from("<I", hdr, 80)[0]
    entry_size = struct.unpack_from("<I", hdr, 84)[0]
    parts: List[Partition] = []
    for i in range(n_entries):
        e = _read(fh, part_lba * SECTOR + i * entry_size, entry_size)
        if not e or e[:16] == b"\x00" * 16:
            continue
        first = struct.unpack_from("<Q", e, 32)[0]
        last = struct.unpack_from("<Q", e, 40)[0]
        name = e[56: 56 + 72].decode("utf-16-le", "ignore").rstrip("\x00")
        start = first * SECTOR
        size = (last - first + 1) * SECTOR
        parts.append(
            Partition(
                index=i + 1,
                start=start,
                size=size,
                type_hint=_detect_fs(fh, start),
                label=name,
            )
        )
    return parts


def analyze(image_path: str) -> List[Partition]:
    """Return a list of partitions in the image."""
    total = os.path.getsize(image_path)
    with open(image_path, "rb") as fh:
        # Check superfloppy (no MBR / GPT, FS at offset 0)
        fs = _detect_fs(fh, 0)
        if fs != "RAW":
            return [Partition(index=0, start=0, size=total, type_hint=fs,
                              label="superfloppy")]
        gpt = _parse_gpt(fh, total)
        if gpt:
            return gpt
        return _parse_mbr(fh, total)


def print_partitions(image_path: str, parts: List[Partition]) -> None:
    print(f"[+] 이미지: {image_path}  ({human_bytes(os.path.getsize(image_path))})")
    if not parts:
        print("    파티션 테이블을 찾지 못했습니다 (시그니처 카빙 모드 사용 권장)")
        return
    print("    번호   오프셋          크기         타입    레이블")
    print("    " + "-" * 64)
    for p in parts:
        print(p.describe())
