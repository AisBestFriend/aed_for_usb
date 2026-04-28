"""Signature-based file carving.

Reads through an image (or raw device) sector by sector, looks for known file
headers, and writes recovered files out. Works even when the filesystem is
completely destroyed - we never look at the FS, only the byte stream.
"""

import os
import struct
from dataclasses import dataclass
from typing import Callable, List, Optional

from .util import Progress, human_bytes


CHUNK = 4 * 1024 * 1024
SECTOR_ALIGN = 512


@dataclass
class Signature:
    name: str
    ext: str
    header: bytes
    footer: Optional[bytes] = None
    max_size: int = 50 * 1024 * 1024
    finder: Optional[Callable] = None      # custom (buf, pos) -> Optional[int]


def _zip_or_office(buf: bytes, start: int) -> Optional[int]:
    """Find end-of-central-directory record."""
    eocd = b"PK\x05\x06"
    end = buf.rfind(eocd)
    if end == -1 or end < start:
        return None
    if end + 22 > len(buf):
        return None
    comment_len = struct.unpack_from("<H", buf, end + 20)[0]
    return end + 22 + comment_len


def _mp4_finder(buf: bytes, start: int) -> Optional[int]:
    """Walk top-level boxes from start until size==0 or buffer ends."""
    pos = start
    while pos + 8 <= len(buf):
        size = struct.unpack_from(">I", buf, pos)[0]
        if size == 0:
            return len(buf)
        if size == 1:
            if pos + 16 > len(buf):
                return None
            big = struct.unpack_from(">Q", buf, pos + 8)[0]
            if big < 16:
                return None
            pos += big
        else:
            if size < 8:
                return None
            pos += size
        if pos - start > 2 * 1024 * 1024 * 1024:
            return None
    return None


SIGS: List[Signature] = [
    Signature("JPEG", "jpg", b"\xff\xd8\xff", footer=b"\xff\xd9"),
    Signature("PNG", "png", b"\x89PNG\r\n\x1a\n",
              footer=b"IEND\xaeB`\x82"),
    Signature("GIF87a", "gif", b"GIF87a", footer=b"\x00\x3b"),
    Signature("GIF89a", "gif", b"GIF89a", footer=b"\x00\x3b"),
    Signature("BMP", "bmp", b"BM", max_size=20 * 1024 * 1024),
    Signature("PDF", "pdf", b"%PDF-", footer=b"%%EOF"),
    Signature("ZIP/Office", "zip", b"PK\x03\x04", finder=_zip_or_office,
              max_size=200 * 1024 * 1024),
    Signature("RAR4", "rar", b"Rar!\x1a\x07\x00"),
    Signature("RAR5", "rar", b"Rar!\x1a\x07\x01\x00"),
    Signature("7z", "7z", b"7z\xbc\xaf\x27\x1c"),
    Signature("MP3-ID3", "mp3", b"ID3"),
    Signature("WAV", "wav", b"RIFF"),
    Signature("MP4/MOV", "mp4", b"\x00\x00\x00", finder=_mp4_finder,
              max_size=2 * 1024 * 1024 * 1024),
    Signature("DOC/XLS/PPT", "ole", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    Signature("HWP", "hwp", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    Signature("EXE/DLL", "exe", b"MZ"),
    Signature("SQLite", "sqlite", b"SQLite format 3\x00"),
]


def _matches_mp4(buf: bytes, pos: int) -> bool:
    """MP4 signature: 4-byte size then 'ftyp'."""
    return pos + 8 <= len(buf) and buf[pos + 4: pos + 8] == b"ftyp"


def _detect_at(buf: bytes, pos: int) -> Optional[Signature]:
    for sig in SIGS:
        if sig.name.startswith("MP4"):
            if _matches_mp4(buf, pos):
                return sig
            continue
        if buf.startswith(sig.header, pos):
            return sig
    return None


def carve(
    src: str,
    out_dir: str,
    align: int = SECTOR_ALIGN,
    max_files: int = 0,
) -> int:
    """Scan `src` and write recovered files to `out_dir`. Returns count.

    `src` may be a disk image OR a raw device path.
    """
    os.makedirs(out_dir, exist_ok=True)
    total = _size_of(src)
    prog = Progress(max(total, 1), label="carve")
    found = 0
    overlap = 1 * 1024 * 1024              # carry-over so big files survive
    buf = b""
    base = 0

    with _open_any(src) as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            buf += chunk
            pos = 0
            scan_end = len(buf) - overlap if len(buf) > overlap else len(buf)
            while pos < scan_end:
                if pos % align != 0:
                    pos += align - (pos % align)
                    continue
                sig = _detect_at(buf, pos)
                if sig is None:
                    pos += align
                    continue
                end = _find_end(buf, pos, sig)
                if end is None or end <= pos:
                    pos += align
                    continue
                data = buf[pos:end]
                fname = f"{base + pos:012x}_{sig.ext}.{sig.ext}"
                with open(os.path.join(out_dir, fname), "wb") as fo:
                    fo.write(data)
                found += 1
                print(
                    f"    [+] {sig.name:<11} @ off={base + pos:>12}  "
                    f"size={human_bytes(len(data)):>10}  -> {fname}"
                )
                pos = end
                if max_files and found >= max_files:
                    prog.finish()
                    print(f"[+] reached max_files={max_files}")
                    return found
            keep = max(0, len(buf) - scan_end)
            base += len(buf) - keep
            buf = buf[-keep:] if keep else b""
            prog.update(min(base + len(buf), total))

    prog.finish()
    print(f"[+] carving complete: {found} files -> {out_dir}")
    return found


def _find_end(buf: bytes, pos: int, sig: Signature) -> Optional[int]:
    if sig.finder is not None:
        return sig.finder(buf, pos)
    if sig.footer:
        end = buf.find(sig.footer, pos + len(sig.header))
        if end == -1:
            return None
        return end + len(sig.footer)
    # Header-only signature: emit a fixed window so we get *something*.
    return min(pos + sig.max_size, len(buf))


def _size_of(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        # raw device - we don't know precisely without ioctl; report 0
        return 0


def _open_any(path: str):
    """Open file or raw device for sequential read."""
    return open(path, "rb")
