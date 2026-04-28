"""USB health / failure diagnostics.

We classify a device as suspicious / bad based on:
  * inability to read first sector
  * read latency on probe sectors
  * whether the partition table or boot sector is recognizable
  * SMART/health status reported by the OS (Windows: Get-PhysicalDisk)
"""

import os
import time
import json
import struct
import subprocess
from dataclasses import dataclass
from typing import List, Optional

from .util import IS_WINDOWS, IS_LINUX, human_bytes
from .imager import _open_windows_raw, _read_windows, _close_windows


@dataclass
class Health:
    path: str
    ok: bool
    label: str             # "OK" / "WARN" / "BAD" / "UNREADABLE"
    notes: List[str]
    latency_ms: Optional[float] = None
    smart: str = ""

    def describe(self) -> str:
        flag = {"OK": "[ 정상  ]", "WARN": "[ 주의  ]",
                "BAD": "[ 불량  ]", "UNREADABLE": "[읽기실패]"}[self.label]
        lat = f"{self.latency_ms:6.1f}ms" if self.latency_ms is not None else "  --  "
        return f"{flag} {self.path:<24} 응답={lat}  SMART={self.smart or '-'}  " \
               f"{'; '.join(self.notes)}"


def _smart_windows() -> dict:
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-PhysicalDisk | Select-Object DeviceId,HealthStatus,OperationalStatus | "
             "ConvertTo-Json -Compress"],
            text=True, stderr=subprocess.STDOUT,
        )
        data = json.loads(out) if out.strip() else []
        if isinstance(data, dict):
            data = [data]
        return {str(d.get("DeviceId")): d for d in data}
    except Exception:
        return {}


def _probe_offsets(size_hint: int) -> List[int]:
    if size_hint <= 0:
        return [0, 1024 * 1024]
    return [
        0,
        size_hint // 4,
        size_hint // 2,
        max(0, size_hint - 4096),
    ]


def diagnose(path: str, size_hint: int = 0) -> Health:
    """Probe a device with small reads and classify its health."""
    notes: List[str] = []
    smart = ""
    if IS_WINDOWS:
        # path looks like \\.\PhysicalDrive2 -> deviceId = "2"
        try:
            dev_id = path.rsplit("PhysicalDrive", 1)[1]
        except IndexError:
            dev_id = ""
        s = _smart_windows().get(dev_id, {})
        smart = (s.get("HealthStatus") or "") + (
            f"/{s.get('OperationalStatus')}" if s.get("OperationalStatus") else ""
        )

    latency = None
    try:
        if IS_WINDOWS:
            h = _open_windows_raw(path)
            try:
                t0 = time.time()
                first = _read_windows(h, 0, 4096)
                latency = (time.time() - t0) * 1000.0
                if not first:
                    notes.append("zero-byte read on sector 0")
                _check_boot_signature(first, notes)
                for off in _probe_offsets(size_hint)[1:]:
                    try:
                        _read_windows(h, off, 4096)
                    except OSError as e:
                        notes.append(f"읽기 오류 @ {human_bytes(off)}: {e}")
            finally:
                _close_windows(h)
        else:
            fd = os.open(path, os.O_RDONLY)
            try:
                t0 = time.time()
                first = os.read(fd, 4096)
                latency = (time.time() - t0) * 1000.0
                _check_boot_signature(first, notes)
                for off in _probe_offsets(size_hint)[1:]:
                    try:
                        os.lseek(fd, off, os.SEEK_SET)
                        os.read(fd, 4096)
                    except OSError as e:
                        notes.append(f"읽기 오류 @ {human_bytes(off)}: {e}")
            finally:
                os.close(fd)
    except OSError as e:
        return Health(
            path=path, ok=False, label="UNREADABLE",
            notes=[f"열기 실패: {e}"], smart=smart,
        )

    label = "OK"
    if smart and smart.split("/")[0].lower() not in ("", "healthy"):
        notes.append(f"SMART={smart}")
        label = "WARN"
    if latency is not None and latency > 250:
        notes.append(f"응답 지연({latency:.0f}ms)")
        label = "WARN"
    if any("읽기 오류" in n or "read error" in n for n in notes):
        label = "BAD"
    if any("부트 서명" in n or "boot signature" in n.lower() for n in notes) and label == "OK":
        label = "WARN"

    return Health(path=path, ok=(label == "OK"), label=label,
                  notes=notes, latency_ms=latency, smart=smart)


def _check_boot_signature(sec0: bytes, notes: List[str]) -> None:
    if len(sec0) < 512:
        notes.append("0번 섹터 읽기 분량 부족")
        return
    if sec0[510:512] != b"\x55\xaa":
        notes.append("MBR 부트 서명(0x55AA) 없음")
    # quick FS-type hint
    if sec0[3:11] in (b"NTFS    ", b"EXFAT   "):
        return
    if sec0[54:62].rstrip() in (b"FAT12", b"FAT16", b"FAT") or \
       sec0[82:90].rstrip() == b"FAT32":
        return


def diagnose_all(devices) -> List[Health]:
    results = []
    for d in devices:
        results.append(diagnose(d.path, d.size))
    return results


def print_health(results: List[Health]) -> None:
    if not results:
        print("(진단할 장치가 없습니다)")
        return
    print("    상태       장치 경로                응답         SMART      비고")
    print("    " + "-" * 80)
    for r in results:
        print("   ", r.describe())
