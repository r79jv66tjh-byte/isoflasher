"""Datenträger ermitteln (Get-Disk/Get-Partition/Get-Volume) und Sperren prüfen.

Übernommen aus DiskFormatter, aber strenger: Ziel dürfen NUR Wechseldatenträger (USB, SD, MMC)
sein – interne Platten sind immer gesperrt, es gibt keinen Schalter dafür.

Harte Sperren:
  * Datenträger mit IsSystem/IsBoot, mit System-/Boot-Partition,
  * Datenträger mit dem Systemlaufwerk (%SystemDrive%) oder dem Windows-Ordner,
  * Datenträger mit einer Auslagerungsdatei,
  * Datenträger, von dem IsoFlasher/Python läuft,
  * offline/schreibgeschützt, interne Platten (kein USB/SD/MMC, kein Wechseldatenträger).

Nur für automatische Tests: Die Umgebungsvariable ISOFLASHER_TEST_DISKS enthält eine mit ';'
getrennte Liste von VHD/VHDX-Dateipfaden. Ein Datenträger mit Bustyp „File Backed Virtual“, dessen
Location exakt einer dieser Dateien entspricht, gilt dann als Wechseldatenträger. Echte Platten
können darüber nie freigeschaltet werden.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

from . import ps

REMOVABLE_BUS = {"USB", "SD", "MMC"}
TEST_BUS = "File Backed Virtual"
_BUS_NUM = {"1": "SCSI", "2": "ATAPI", "3": "ATA", "4": "1394", "5": "SSA", "6": "Fibre Channel",
            "7": "USB", "8": "RAID", "9": "iSCSI", "10": "SAS", "11": "SATA", "12": "SD",
            "13": "MMC", "14": "Virtual", "15": "File Backed Virtual", "16": "Storage Spaces",
            "17": "NVMe"}


def fmt_size(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1000
    return f"{n:.1f} TB"


def test_disk_paths() -> set[str]:
    raw = os.environ.get("ISOFLASHER_TEST_DISKS", "")
    return {p.strip().lower() for p in raw.split(";")
            if p.strip().lower().endswith((".vhd", ".vhdx"))}


@dataclass
class Volume:
    letter: str
    label: str
    filesystem: str
    size: int
    free: int
    drive_type: str
    path: str
    disk_number: int | None = None
    partition_number: int | None = None

    def to_json(self) -> dict:
        return {"letter": self.letter or None, "label": self.label, "filesystem": self.filesystem,
                "size_bytes": self.size, "size": fmt_size(self.size), "free_bytes": self.free,
                "drive_type": self.drive_type, "partition_number": self.partition_number}


@dataclass
class Disk:
    number: int
    model: str
    serial: str
    bus_type: str
    size: int
    is_system: bool = False
    is_boot: bool = False
    is_offline: bool = False
    is_readonly: bool = False
    partition_style: str = ""
    location: str = ""
    unique_id: str = ""
    sector_size: int = 512
    volumes: list[Volume] = field(default_factory=list)
    hard_locks: list[str] = field(default_factory=list)

    @property
    def is_test_disk(self) -> bool:
        return self.bus_type == TEST_BUS and self.location.lower() in test_disk_paths()

    @property
    def removable(self) -> bool:
        return (self.bus_type in REMOVABLE_BUS or self.is_test_disk
                or any(v.drive_type == "Removable" for v in self.volumes))

    def blocks(self) -> list[str]:
        """Alle Gründe, warum dieser Datenträger kein Ziel sein darf (leer = erlaubt)."""
        r = list(self.hard_locks)
        if self.is_offline:
            r.append("Datenträger ist offline")
        if self.is_readonly:
            r.append("Datenträger ist schreibgeschützt")
        if not self.removable:
            r.append("Kein Wechseldatenträger (nur USB-Sticks und SD-Karten sind erlaubt)")
        if self.size <= 0:
            r.append("Größe unbekannt / kein Medium eingelegt")
        return r

    @property
    def letters(self) -> list[str]:
        return [v.letter for v in self.volumes if v.letter]

    @property
    def title(self) -> str:
        let = ", ".join(f"{x}:" for x in self.letters)
        return (f"Datenträger {self.number}: {self.model or '?'} ({fmt_size(self.size)}, {self.bus_type})"
                + (f" [{let}]" if let else ""))

    def uid(self) -> str:
        return f"{self.unique_id}|{self.serial}|{self.size}"

    def to_json(self) -> dict:
        b = self.blocks()
        return {"number": self.number, "model": self.model, "serial": self.serial,
                "bus_type": self.bus_type, "size_bytes": self.size, "size": fmt_size(self.size),
                "removable": self.removable, "test_disk": self.is_test_disk,
                "partition_style": self.partition_style, "selectable": not b, "locked_reasons": b,
                "volumes": [v.to_json() for v in self.volumes]}


@dataclass
class Snapshot:
    disks: list[Disk]

    def disk(self, number: int) -> Disk | None:
        return next((d for d in self.disks if d.number == number), None)

    def disk_of_letter(self, letter: str) -> Disk | None:
        letter = letter.upper()
        return next((d for d in self.disks if letter in d.letters), None)


_QUERY = r"""
$d = @(Get-Disk | ForEach-Object { [pscustomobject]@{
  Number=$_.Number; Model=$_.Model; FriendlyName=$_.FriendlyName; Serial=$_.SerialNumber;
  BusType=[string]$_.BusType; Size=$_.Size; IsSystem=$_.IsSystem; IsBoot=$_.IsBoot;
  IsOffline=$_.IsOffline; IsReadOnly=$_.IsReadOnly; PartitionStyle=[string]$_.PartitionStyle;
  Location=$_.Location; UniqueId=$_.UniqueId; SectorSize=$_.LogicalSectorSize } })
$p = @(Get-Partition -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{
  DiskNumber=$_.DiskNumber; PartitionNumber=$_.PartitionNumber; DriveLetter=[string]$_.DriveLetter;
  IsBoot=$_.IsBoot; IsSystem=$_.IsSystem; AccessPaths=@($_.AccessPaths) } })
$v = @(Get-Volume | ForEach-Object { [pscustomobject]@{
  DriveLetter=[string]$_.DriveLetter; Label=$_.FileSystemLabel; FileSystem=[string]$_.FileSystemType;
  Size=$_.Size; Free=$_.SizeRemaining; DriveType=[string]$_.DriveType; Path=$_.Path } })
$pf = @(Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
[pscustomobject]@{disks=$d; partitions=$p; volumes=$v; pagefiles=$pf} | ConvertTo-Json -Depth 4 -Compress
"""


def _letter(x) -> str:
    s = str(x or "").strip().replace("\x00", "")
    return s[0].upper() if s and s[0].isalpha() else ""


def _bus(x) -> str:
    s = str(x or "").strip()
    return _BUS_NUM.get(s, s or "?")


def own_letters() -> set[str]:
    """Laufwerke, von denen IsoFlasher und Python laufen."""
    out = set()
    for p in (__file__, sys.executable, os.getcwd()):
        drive = os.path.splitdrive(os.path.abspath(p))[0]
        if len(drive) == 2 and drive[1] == ":":
            out.add(drive[0].upper())
    return out


def build_snapshot(raw: dict, system_letters: set[str], own: set[str] | None = None) -> Snapshot:
    disks: dict[int, Disk] = {}
    for r in ps.as_list(raw.get("disks")):
        n = int(r["Number"])
        disks[n] = Disk(
            number=n, model=(r.get("Model") or r.get("FriendlyName") or "").strip(),
            serial=(r.get("Serial") or "").strip(), bus_type=_bus(r.get("BusType")),
            size=int(r.get("Size") or 0), is_system=bool(r.get("IsSystem")),
            is_boot=bool(r.get("IsBoot")), is_offline=bool(r.get("IsOffline")),
            is_readonly=bool(r.get("IsReadOnly")), partition_style=str(r.get("PartitionStyle") or ""),
            location=str(r.get("Location") or ""), unique_id=str(r.get("UniqueId") or ""),
            sector_size=int(r.get("SectorSize") or 512))

    by_path: dict[str, tuple[int, int]] = {}
    by_letter: dict[str, tuple[int, int]] = {}
    for p in ps.as_list(raw.get("partitions")):
        dn, pn = int(p["DiskNumber"]), int(p["PartitionNumber"])
        d = disks.get(dn)
        if d is not None and (p.get("IsBoot") or p.get("IsSystem")):
            _lock(d, "Enthält die System-/Boot-Partition")
        for ap in ps.as_list(p.get("AccessPaths")):
            if str(ap).lower().startswith("\\\\?\\volume"):
                by_path[str(ap).lower().rstrip("\\")] = (dn, pn)
        if _letter(p.get("DriveLetter")):
            by_letter[_letter(p.get("DriveLetter"))] = (dn, pn)

    for r in ps.as_list(raw.get("volumes")):
        v = Volume(letter=_letter(r.get("DriveLetter")), label=str(r.get("Label") or ""),
                   filesystem=str(r.get("FileSystem") or ""), size=int(r.get("Size") or 0),
                   free=int(r.get("Free") or 0), drive_type=str(r.get("DriveType") or ""),
                   path=str(r.get("Path") or ""))
        hit = by_path.get(v.path.lower().rstrip("\\")) or (by_letter.get(v.letter) if v.letter else None)
        if hit and hit[0] in disks:
            v.disk_number, v.partition_number = hit
            disks[hit[0]].volumes.append(v)

    page_letters = {_letter(p) for p in ps.as_list(raw.get("pagefiles")) if _letter(p)}
    own = own_letters() if own is None else own
    for d in disks.values():
        if d.is_system:
            _lock(d, "Systemdatenträger (IsSystem)")
        if d.is_boot:
            _lock(d, "Boot-Datenträger (IsBoot)")
        letters = set(d.letters)
        for L in sorted(letters & system_letters):
            _lock(d, f"Enthält das Windows-/Systemlaufwerk {L}:")
        for L in sorted(letters & page_letters):
            _lock(d, f"Enthält eine Auslagerungsdatei ({L}:)")
        for L in sorted(letters & own):
            _lock(d, f"IsoFlasher/Python läuft von {L}:")
        d.volumes.sort(key=lambda v: (v.partition_number or 0))
    return Snapshot(disks=sorted(disks.values(), key=lambda d: d.number))


def _lock(d: Disk, reason: str) -> None:
    if reason not in d.hard_locks:
        d.hard_locks.append(reason)


def system_letters() -> set[str]:
    return {ps.system_drive(), ps.windows_drive()}


def query() -> Snapshot:
    raw = ps.run_json(_QUERY, timeout=90) or {}
    return build_snapshot(raw, system_letters())


_DISK_RE = re.compile(r"^(?:disk\s*:?\s*|datenträger\s*)?(\d{1,3})$", re.I)
_LETTER_RE = re.compile(r"^([A-Za-z])(:\\?)?$")


def resolve_target(snap: Snapshot, text: str) -> Disk:
    """'3' / 'disk:3' -> Datenträger 3; 'E:' -> Datenträger mit Laufwerk E:."""
    t = (text or "").strip()
    m = _DISK_RE.match(t)
    if m:
        d = snap.disk(int(m.group(1)))
        if d is None:
            raise ValueError(f"Datenträger {m.group(1)} nicht gefunden")
        return d
    m = _LETTER_RE.match(t)
    if m:
        d = snap.disk_of_letter(m.group(1))
        if d is None:
            raise ValueError(f"Laufwerk {m.group(1).upper()}: gehört zu keinem Datenträger")
        return d
    raise ValueError(f"Ungültiges Ziel '{text}': Datenträgernummer (z. B. 3) oder Laufwerksbuchstabe (E:) angeben")


def confirm_ok(d: Disk, typed: str | None) -> bool:
    """Bestätigung: exakt die Datenträgernummer oder einer seiner Laufwerksbuchstaben."""
    t = (typed or "").strip()
    if t == str(d.number):
        return True
    m = re.fullmatch(r"([A-Za-z]):?", t)
    return bool(m) and m.group(1).upper() in d.letters


def confirm_hint(d: Disk) -> str:
    return str(d.number) + (f" oder {d.letters[0]}:" if d.letters else "")
