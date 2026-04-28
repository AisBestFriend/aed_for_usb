"""Cross-platform removable-storage discovery.

Windows: enumerate physical disks via PowerShell (Get-Disk + Get-PhysicalDisk).
Linux:  enumerate via lsblk JSON output.
"""

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List

from .util import IS_WINDOWS, IS_LINUX, human_bytes


@dataclass
class Device:
    path: str            # e.g. \\.\PhysicalDrive2  or /dev/sdb
    model: str = ""
    size: int = 0
    bus: str = ""        # USB / SATA / NVMe ...
    removable: bool = False
    partitions: List[str] = field(default_factory=list)

    def describe(self) -> str:
        flag = "[USB]" if self.removable or self.bus.upper() == "USB" else "     "
        return (
            f"{flag} {self.path:<24} {human_bytes(self.size):>12}  "
            f"{self.bus:<6}  {self.model}"
        )


def _run(cmd: list, timeout: int = 30) -> str:
    return subprocess.check_output(
        cmd, stderr=subprocess.STDOUT, timeout=timeout, text=True
    )


def _scan_windows() -> List[Device]:
    ps = (
        "Get-Disk | ForEach-Object { "
        "  $d = $_; "
        "  $pd = Get-PhysicalDisk -DeviceNumber $d.Number -ErrorAction SilentlyContinue; "
        "  [PSCustomObject]@{"
        "    Number=$d.Number; "
        "    Size=$d.Size; "
        "    Model=$d.Model; "
        "    Bus=$d.BusType; "
        "    Removable=($pd.MediaType -eq 'Removable' -or $d.BusType -eq 'USB')"
        "  } "
        "} | ConvertTo-Json -Compress -Depth 3"
    )
    out = _run(["powershell", "-NoProfile", "-Command", ps])
    if not out.strip():
        return []
    data = json.loads(out)
    if isinstance(data, dict):
        data = [data]
    devs: List[Device] = []
    for d in data:
        devs.append(
            Device(
                path=fr"\\.\PhysicalDrive{d['Number']}",
                model=(d.get("Model") or "").strip(),
                size=int(d.get("Size") or 0),
                bus=str(d.get("Bus") or ""),
                removable=bool(d.get("Removable")),
            )
        )
    return devs


def _scan_linux() -> List[Device]:
    if not shutil.which("lsblk"):
        return []
    out = _run(
        ["lsblk", "-b", "-J", "-o", "NAME,PATH,SIZE,MODEL,TRAN,RM,TYPE"]
    )
    data = json.loads(out)
    devs: List[Device] = []
    for d in data.get("blockdevices", []):
        if d.get("type") != "disk":
            continue
        parts = [
            c.get("path", "")
            for c in d.get("children", [])
            if c.get("type") == "part"
        ]
        devs.append(
            Device(
                path=d.get("path") or f"/dev/{d['name']}",
                model=(d.get("model") or "").strip(),
                size=int(d.get("size") or 0),
                bus=(d.get("tran") or "").upper(),
                removable=str(d.get("rm")) in ("1", "True", "true"),
                partitions=parts,
            )
        )
    return devs


def scan() -> List[Device]:
    if IS_WINDOWS:
        return _scan_windows()
    if IS_LINUX:
        return _scan_linux()
    return []


def print_table(devices: List[Device]) -> None:
    if not devices:
        print("(no block devices found - run as Administrator/root)")
        return
    print("    DEVICE                       SIZE     BUS     MODEL")
    print("    " + "-" * 70)
    for d in devices:
        print("   ", d.describe())
    print(
        "\n[i] [USB] = removable / USB-bus device. "
        "Always image a USB before recovery."
    )
