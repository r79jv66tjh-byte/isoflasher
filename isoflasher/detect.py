"""Abbild-Typ erkennen: Windows-Installations-ISO, hybride Linux-/Tool-ISO, Roh-Abbild (.img), gepackt.

Liest ISO 9660 (inkl. Joliet) selbst. Windows-ISOs stecken ihre Dateien oft nur ins UDF-Dateisystem
(im ISO-9660-Teil liegt dann nur README.TXT) – dann wird die ISO kurz schreibgeschützt eingehängt
(Mount-DiskImage, braucht keine Adminrechte) und wieder ausgehängt.
"""
from __future__ import annotations

import os
import struct
import zipfile
from dataclasses import dataclass, field

from . import ps

SECTOR = 2048
FAT32_MAX = 4 * 1024 ** 3 - 1
COMPRESSED = (".xz", ".gz", ".bz2", ".zip")


@dataclass
class ImageInfo:
    path: str
    size: int                       # Dateigröße
    kind: str                       # windows | hybrid | iso | raw | unknown
    mode: str                       # empfohlener Weg: windows | dd | ventoy
    label: str = ""
    compressed: str = ""            # "", xz, gz, bz2, zip
    data_size: int | None = None    # Größe der Daten nach dem Entpacken (falls bekannt)
    bootable_bios: bool = False
    bootable_uefi: bool = False
    hybrid: bool = False
    partition_table: str = ""       # MBR | GPT | ""
    distro: str = ""
    live_system: str = ""           # casper | live-boot | archiso | ""
    windows: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def kind_text(self) -> str:
        return {"windows": "Windows-Installation", "hybrid": "Linux/Tool (hybride ISO)",
                "iso": "ISO (nicht hybrid)", "raw": "Datenträger-Abbild",
                "unknown": "Unbekannt"}.get(self.kind, self.kind)

    @property
    def mode_text(self) -> str:
        return {"windows": "Windows-Stick (partitionieren, Dateien kopieren)",
                "dd": "1:1 schreiben (DD) mit Prüfsummen-Vergleich",
                "ventoy": "Multiboot (Ventoy) empfohlen"}.get(self.mode, self.mode)

    def to_json(self) -> dict:
        return {"path": self.path, "file_size": self.size, "kind": self.kind, "kind_text": self.kind_text,
                "recommended_mode": self.mode, "mode_text": self.mode_text, "label": self.label,
                "compressed": self.compressed or None, "data_size": self.data_size,
                "partition_table": self.partition_table or None, "hybrid": self.hybrid,
                "bootable_bios": self.bootable_bios, "bootable_uefi": self.bootable_uefi,
                "distro": self.distro or None, "live_system": self.live_system or None,
                "persistence_possible": bool(self.live_system),
                "windows": self.windows or None, "notes": self.notes}


# ---------------------------------------------------------------- ISO 9660

class Iso9660:
    def __init__(self, f):
        self.f = f
        self.label = ""
        self.root: tuple[int, int] | None = None
        self.joliet = False
        self.boot_catalog: int | None = None
        self.udf = False
        for i in range(16, 64):
            f.seek(i * SECTOR)
            d = f.read(SECTOR)
            if len(d) < SECTOR:
                break
            ident = d[1:6]
            if ident in (b"BEA01", b"NSR02", b"NSR03"):
                self.udf = self.udf or ident.startswith(b"NSR")
                continue
            if ident != b"CD001":
                continue
            t = d[0]
            if t == 0 and d[7:30].rstrip(b"\0") == b"EL TORITO SPECIFICATION":
                self.boot_catalog = struct.unpack_from("<I", d, 0x47)[0]
            elif t == 1:
                self.label = self.label or d[40:72].decode("ascii", "replace").strip()
                if self.root is None:
                    self.root = struct.unpack_from("<I", d, 156 + 2)[0], struct.unpack_from("<I", d, 156 + 10)[0]
            elif t == 2 and d[88:91] in (b"%/@", b"%/C", b"%/E"):
                self.joliet = True
                self.root = struct.unpack_from("<I", d, 156 + 2)[0], struct.unpack_from("<I", d, 156 + 10)[0]
                lab = d[40:72].decode("utf-16-be", "replace").strip("\0 ")
                if lab:
                    self.label = lab
            elif t == 255:
                pass
        if self.root is None and not self.udf:
            raise ValueError("keine ISO-9660-/UDF-Struktur")

    def _dir(self, lba: int, size: int):
        self.f.seek(lba * SECTOR)
        data = self.f.read(min(size, 4 * 1024 * 1024))
        pos = 0
        while pos < len(data):
            ln = data[pos]
            if ln == 0:
                pos = (pos // SECTOR + 1) * SECTOR
                continue
            rec = data[pos:pos + ln]
            pos += ln
            if len(rec) < 34:
                break
            ext, dsize, flags, nlen = (struct.unpack_from("<I", rec, 2)[0], struct.unpack_from("<I", rec, 10)[0],
                                       rec[25], rec[32])
            raw = rec[33:33 + nlen]
            if raw in (b"\0", b"\1"):
                continue
            name = raw.decode("utf-16-be", "replace") if self.joliet else raw.decode("ascii", "replace")
            name = name.split(";")[0].rstrip(".")
            yield name, bool(flags & 2), ext, dsize

    def files(self, max_depth: int = 3) -> dict[str, int]:
        """Pfad (klein, mit /) -> Größe; Ordner mit Größe -1."""
        out: dict[str, int] = {}
        if self.root is None:          # nur UDF
            return out
        stack = [("", self.root[0], self.root[1], 0)]
        seen = set()
        while stack:
            prefix, lba, size, depth = stack.pop()
            if lba in seen:
                continue
            seen.add(lba)
            for name, is_dir, ext, dsize in self._dir(lba, size):
                p = (prefix + "/" + name).lower()
                if is_dir:
                    out[p] = -1
                    if depth < max_depth:
                        stack.append((p, ext, dsize, depth + 1))
                else:
                    out[p] = out.get(p, 0) + dsize     # mehrteilige Dateien (>4 GB) addieren
        return out

    def has_efi_boot_entry(self) -> bool:
        if self.boot_catalog is None:
            return False
        self.f.seek(self.boot_catalog * SECTOR)
        cat = self.f.read(SECTOR)
        for off in range(0, SECTOR, 32):
            e = cat[off:off + 32]
            if e[:1] in (b"\x90", b"\x91") and e[1] == 0xEF:   # Section-Header mit Plattform EFI
                return True
            if off == 0 and e[1] == 0xEF:
                return True
        return False


# ---------------------------------------------------------------- Partitionstabelle

def partition_table(head: bytes) -> tuple[str, bool]:
    """('GPT'|'MBR'|'', hat_bootcode)."""
    if len(head) >= 520 and head[512:520] == b"EFI PART":
        return "GPT", head[:440].strip(b"\0") != b""
    if len(head) >= 512 and head[510:512] == b"\x55\xaa":
        parts = [head[446 + 16 * i: 462 + 16 * i] for i in range(4)]
        if any(p[4] != 0 for p in parts) and all(p[0] in (0, 0x80) for p in parts):
            return "MBR", head[:440].strip(b"\0") != b""
    return "", False


def _distro(label: str, files: dict[str, int]) -> tuple[str, str]:
    lab = label.lower()
    live = ""
    if "/casper" in files:
        live = "casper"
    elif "/live" in files and any(k.startswith("/live/filesystem.squashfs") for k in files):
        live = "live-boot"
    elif "/arch" in files:
        live = "archiso"
    names = [("mint", "Linux Mint"), ("ubuntu", "Ubuntu"), ("kubuntu", "Kubuntu"), ("xubuntu", "Xubuntu"),
             ("gparted", "GParted Live"), ("clonezilla", "Clonezilla"), ("debian", "Debian"),
             ("fedora", "Fedora"), ("arch", "Arch Linux"), ("manjaro", "Manjaro"), ("pop", "Pop!_OS"),
             ("memtest", "Memtest86+"), ("mt86", "Memtest86+"), ("systemrescue", "SystemRescue"),
             ("kali", "Kali Linux"), ("opensuse", "openSUSE"), ("tails", "Tails"), ("ventoy", "Ventoy")]
    for key, name in names:
        if key in lab:
            return name, live
    if any("memtest" in k for k in files):
        return "Memtest86+", live
    if "/casper" in files:
        return "Ubuntu-basiert", live
    return "", live


def _inspect_windows_mounted(path: str) -> dict:
    """ISO einhängen (schreibgeschützt), Windows-Dateien prüfen, wieder aushängen."""
    script = (f"$p={ps.quote(path)};"
              "$img = Mount-DiskImage -ImagePath $p -Access ReadOnly -NoDriveLetter -PassThru;"
              "try { Start-Sleep -Milliseconds 300; $v = ($img | Get-Volume).Path;"
              "$r=[ordered]@{label=($img | Get-Volume).FileSystemLabel}; foreach ($f in 'sources\\install.wim','sources\\install.esd','sources\\install.swm','sources\\boot.wim','bootmgr','efi\\boot\\bootx64.efi','efi\\boot\\bootaa64.efi','boot\\bootsect.exe','sources\\setup.exe') {"
              "$i = Get-Item -LiteralPath ($v + $f) -ErrorAction SilentlyContinue; $r[$f] = if ($i) { $i.Length } else { $null } };"
              "[pscustomobject]$r | ConvertTo-Json -Compress } finally { Dismount-DiskImage -ImagePath $p | Out-Null }")
    return ps.run_json(script, timeout=120) or {}


def _windows_from_files(files: dict[str, int]) -> dict:
    m = {"sources\\install.wim": "/sources/install.wim", "sources\\install.esd": "/sources/install.esd",
         "sources\\install.swm": "/sources/install.swm", "sources\\boot.wim": "/sources/boot.wim",
         "bootmgr": "/bootmgr", "efi\\boot\\bootx64.efi": "/efi/boot/bootx64.efi",
         "efi\\boot\\bootaa64.efi": "/efi/boot/bootaa64.efi", "boot\\bootsect.exe": "/boot/bootsect.exe",
         "sources\\setup.exe": "/sources/setup.exe"}
    return {k: (files[v] if v in files else None) for k, v in m.items()}


def _windows_summary(w: dict, label: str) -> dict | None:
    inst = next((k for k in ("sources\\install.wim", "sources\\install.esd", "sources\\install.swm") if w.get(k) is not None), None)
    if not inst or w.get("sources\\boot.wim") is None:
        return None
    size = w.get(inst) or 0
    lab = (label or "").upper()
    return {"install_file": inst.replace("\\", "/"), "install_size": size,
            "needs_split": size > FAT32_MAX and inst.endswith(".wim"),
            "too_big_for_fat32": size > FAT32_MAX,
            "uefi": w.get("efi\\boot\\bootx64.efi") is not None or w.get("efi\\boot\\bootaa64.efi") is not None,
            "bios": w.get("bootmgr") is not None,
            "arm64": w.get("efi\\boot\\bootaa64.efi") is not None,
            "windows11_guess": any(t in lab for t in ("CCCOMA", "CPRA", "CCSA", "CENA", "GE_", "GA_", "HC_", "WIN11"))}


def compressed_kind(path: str) -> str:
    low = path.lower()
    for ext in COMPRESSED:
        if low.endswith(ext):
            return ext[1:]
    return ""


def zip_member(path: str) -> zipfile.ZipInfo:
    z = zipfile.ZipFile(path)
    cands = [i for i in z.infolist() if not i.is_dir()]
    good = [i for i in cands if i.filename.lower().endswith((".img", ".iso", ".bin", ".raw"))] or cands
    if not good:
        raise ValueError("ZIP enthält keine Datei")
    return max(good, key=lambda i: i.file_size)


def analyze(path: str, mount_ok: bool = True) -> ImageInfo:
    path = os.path.abspath(path)
    size = os.path.getsize(path)
    comp = compressed_kind(path)
    info = ImageInfo(path=path, size=size, kind="unknown", mode="dd", compressed=comp)
    if comp:
        from .source import open_source
        with open_source(path) as src:
            head = src.read(64 * 1024)
            info.data_size = src.total
        info.partition_table, _ = partition_table(head)
        info.kind = "raw"
        info.mode = "dd"
        if head[0x8001:0x8006] == b"CD001":
            info.notes.append("Gepackte ISO – wird 1:1 geschrieben.")
        if not info.partition_table:
            info.notes.append("Keine Partitionstabelle erkannt – ist das wirklich ein Datenträger-Abbild?")
        return info

    info.data_size = size
    with open(path, "rb") as f:
        head = f.read(64 * 1024)
        info.partition_table, _ = partition_table(head)
        info.hybrid = bool(info.partition_table)
        try:
            iso = Iso9660(f)
        except (ValueError, struct.error):
            iso = None
        if iso is None:
            info.kind = "raw" if info.partition_table else "unknown"
            info.mode = "dd"
            if not info.partition_table:
                info.notes.append("Weder ISO noch Partitionstabelle erkannt – 1:1 schreiben nur, wenn du sicher bist.")
            return info
        info.label = iso.label
        files = iso.files()
        info.bootable_bios = iso.boot_catalog is not None
        info.bootable_uefi = iso.has_efi_boot_entry() or "/efi/boot" in files
        info.distro, info.live_system = _distro(iso.label, files)
        udf_only = iso.udf and set(files) <= {"/readme.txt"}
        if udf_only:
            info.notes.append("Nur UDF-Dateisystem (typisch für Windows-ISOs).")

    w = _windows_from_files(files)
    if udf_only and mount_ok:
        try:
            m = _inspect_windows_mounted(path)
            w = {k: m.get(k) for k in w}
            info.label = m.get("label") or info.label
        except ps.PSError as e:
            info.notes.append(f"ISO konnte zum Prüfen nicht eingehängt werden: {e}")
    win = _windows_summary(w, info.label)
    if win:
        info.kind, info.mode, info.windows = "windows", "windows", win
        info.bootable_uefi = info.bootable_uefi or win["uefi"]
        if win["needs_split"]:
            info.notes.append("install.wim ist größer als 4 GB – wird für FAT32 mit wimlib geteilt.")
        if win["too_big_for_fat32"] and not win["needs_split"]:
            info.notes.append("install.esd ist größer als 4 GB – passt nicht auf FAT32.")
        return info
    if info.hybrid:
        info.kind, info.mode = "hybrid", "dd"
    else:
        info.kind, info.mode = "iso", "ventoy"
        info.notes.append("Die ISO ist nicht hybrid; 1:1 geschrieben bootet sie vom Stick meist nicht. "
                          "Empfohlen: Multiboot (Ventoy).")
    if info.live_system:
        info.notes.append("Live-System mit Persistenz-Unterstützung erkannt (" + info.live_system + ").")
    return info
