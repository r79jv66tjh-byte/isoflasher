"""Test-VHDX anlegen/anhängen/entfernen (diskpart). Nur für die Integrationstests, braucht Admin.

Sicherheitsnetz: ``assert_test(n)`` bricht hart ab, wenn Datenträger n nicht genau diese VHDX ist.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from isoflasher import ps  # noqa: E402


def diskpart(script: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="ascii") as f:
        f.write(script)
        name = f.name
    try:
        p = subprocess.run(["diskpart", "/s", name], capture_output=True, timeout=600)
        return p.stdout.decode(ps.oem_encoding(), "replace")
    finally:
        os.remove(name)


class TestDisk:
    def __init__(self, path: Path, size_mb: int):
        self.path, self.size_mb = Path(path), size_mb
        self.number: int | None = None

    def create(self):
        self.destroy(quiet=True)
        out = diskpart(f'create vdisk file="{self.path}" maximum={self.size_mb} type=expandable\n'
                       f'select vdisk file="{self.path}"\nattach vdisk\n')
        for _ in range(30):
            n = self.find()
            if n is not None:
                self.number = n
                self.register()
                return self
            time.sleep(1)
        raise RuntimeError("Test-VHDX nicht gefunden:\n" + out)

    def register(self):
        cur = [p for p in os.environ.get("ISOFLASHER_TEST_DISKS", "").split(";") if p]
        if str(self.path) not in cur:
            cur.append(str(self.path))
        os.environ["ISOFLASHER_TEST_DISKS"] = ";".join(cur)

    def find(self) -> int | None:
        data = ps.as_list(ps.run_json(
            "Get-Disk | Where-Object { $_.Location -eq " + ps.quote(str(self.path)) + " } | "
            "Select-Object Number | ConvertTo-Json -Compress"))
        return int(data[0]["Number"]) if data else None

    def assert_test(self, number: int | None = None):
        n = self.number if number is None else number
        d = ps.as_list(ps.run_json(f"Get-Disk -Number {int(n)} | Select-Object Number,"
                                   "@{n='Bus';e={[string]$_.BusType}},Location,IsSystem,IsBoot | ConvertTo-Json -Compress"))
        if (not d or d[0]["Bus"] != "File Backed Virtual" or str(d[0]["Location"]).lower() != str(self.path).lower()
                or d[0]["IsSystem"] or d[0]["IsBoot"]):
            raise SystemExit(f"SICHERHEITSABBRUCH: Datenträger {n} ist nicht die Test-VHDX: {d}")

    def letters(self) -> list[str]:
        out = ps.as_list(ps.run_json(f"Get-Partition -DiskNumber {self.number} -ErrorAction SilentlyContinue | "
                                     "Where-Object DriveLetter | Select-Object @{n='L';e={[string]$_.DriveLetter}} | ConvertTo-Json -Compress"))
        return [str(o["L"])[0] for o in out if o.get("L")]

    def prepare(self, label="TESTVOL") -> str:
        """Eine Partition mit Laufwerksbuchstaben anlegen (wie ein frischer Stick)."""
        self.assert_test()
        out = ps.run_json(
            f"$n={self.number}; $d = Get-Disk -Number $n; if ($d.PartitionStyle -ne 'RAW') {{ Clear-Disk -Number $n -RemoveData -RemoveOEM -Confirm:$false }};"
            f"Initialize-Disk -Number $n -PartitionStyle MBR;"
            f"$p = New-Partition -DiskNumber $n -UseMaximumSize -AssignDriveLetter;"
            f"$p | Format-Volume -FileSystem exFAT -NewFileSystemLabel {ps.quote(label)} -Confirm:$false -Force | Out-Null;"
            "[pscustomobject]@{L=[string]$p.DriveLetter} | ConvertTo-Json -Compress")
        return str(out["L"])[0]

    def destroy(self, quiet=False):
        if self.path.exists():
            diskpart(f'select vdisk file="{self.path}"\ndetach vdisk\n')
            time.sleep(1)
            try:
                self.path.unlink()
            except OSError as e:
                if not quiet:
                    print("VHDX konnte nicht gelöscht werden:", e)
