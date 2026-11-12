"""Externe Open-Source-Helfer (wimlib, Ventoy, Fido): finden oder nach Bestätigung laden.

Geladen wird nur von der offiziellen Quelle, in fester Version und mit fest hinterlegter SHA-256.
Stimmt die Prüfsumme nicht, wird die Datei verworfen. Ablage: %LOCALAPPDATA%\\IsoFlasher\\tools.
Wer die Programme selbst hinlegt, kann sie auch dort (oder in tools\\ neben IsoFlasher) ablegen.
"""
from __future__ import annotations

import hashlib
import lzma
import os
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .jobs import Job, Meter, OpError

USER_AGENT = "IsoFlasher/1.0 (+Python urllib)"


@dataclass(frozen=True)
class Helper:
    key: str
    name: str
    version: str
    url: str
    sha256: str
    size: int
    license: str
    homepage: str
    exe: str            # relativer Pfad nach dem Entpacken
    purpose: str
    hash_source: str

    def to_json(self) -> dict:
        p = find(self.key)
        return {"key": self.key, "name": self.name, "version": self.version, "url": self.url,
                "sha256": self.sha256, "size": self.size, "license": self.license,
                "homepage": self.homepage, "purpose": self.purpose, "hash_source": self.hash_source,
                "installed": bool(p), "path": str(p) if p else None}


HELPERS = {
    "wimlib": Helper(
        "wimlib", "wimlib", "1.14.5",
        "https://wimlib.net/downloads/wimlib-1.14.5-windows-x86_64-bin.zip",
        "2f446d6fa3866582175f1a22a7be198eeee0aec7aba5b4e04ad25c99eae2d265", 778747,
        "LGPLv3+ / GPLv3+ (siehe COPYING.txt im Paket)", "https://wimlib.net/",
        "wimlib-imagex.exe", "install.wim > 4 GB für FAT32 teilen und prüfen",
        "wimlib.net veröffentlicht keine Prüfsummen; Wert beim Einbau über HTTPS ermittelt und hier festgeschrieben"),
    "ventoy": Helper(
        "ventoy", "Ventoy", "1.1.18",
        "https://github.com/ventoy/Ventoy/releases/download/v1.1.18/ventoy-1.1.18-windows.zip",
        "082c478e8c432e1ac653899fcf50e3a4077a2851bc6112b5ddd30545962164a9", 16680699,
        "GPLv3", "https://www.ventoy.net/", "ventoy-1.1.18/Ventoy2Disk.exe",
        "Multiboot-Stick einrichten",
        "GitHub-Release v1.1.18 (Asset-Digest und sha256.txt)"),
    "fido": Helper(
        "fido", "Fido", "1.72",
        "https://github.com/pbatard/Fido/releases/download/v1.72/Fido.ps1.lzma",
        "d39b0784b9eea94afedd8703f3bee99a475657879707c0913d52e10dd9985f05", 19435,
        "GPLv3", "https://github.com/pbatard/Fido", "Fido.ps1",
        "Download-Links für offizielle Windows-ISOs von Microsoft holen",
        "GitHub-Release v1.72 (Asset-Digest)"),
}


def tools_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "IsoFlasher" / "tools"


def _candidates(h: Helper) -> list[Path]:
    local = Path(__file__).resolve().parents[1] / "tools"
    out = [tools_dir() / f"{h.key}-{h.version}" / h.exe]
    out += [local / f"{h.key}-{h.version}" / h.exe, local / h.key / Path(h.exe).name, local / Path(h.exe).name]
    return out


def find(key: str) -> Path | None:
    h = HELPERS[key]
    for c in _candidates(h):
        if c.is_file():
            return c
    exe = shutil.which(Path(h.exe).name)
    return Path(exe) if exe else None


def http_download(url: str, dest: str, job: Job, expected_sha256: str | None = None,
                  label: str = "Download") -> dict:
    """Datei laden (mit Fortschritt, Abbrechen) und optional SHA-256 prüfen."""
    tmp = dest + ".part"
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0) or None
            meter = Meter(job, "download", total, os.path.basename(dest), label)
            done = 0
            while True:
                job.check()
                b = r.read(1024 * 1024)
                if not b:
                    break
                f.write(b)
                h.update(b)
                done += len(b)
                meter.update(done)
            meter.update(done, force=True)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    digest = h.hexdigest()
    if expected_sha256 and digest != expected_sha256.lower():
        os.remove(tmp)
        raise OpError(f"Prüfsumme falsch für {url}: erwartet {expected_sha256}, erhalten {digest}. Datei verworfen.")
    os.replace(tmp, dest)
    return {"file": dest, "sha256": digest, "bytes": done, "verified": bool(expected_sha256)}


def _safe_extract(zf: zipfile.ZipFile, target: Path):
    root = target.resolve()
    for m in zf.infolist():
        p = (target / m.filename).resolve()
        if root not in p.parents and p != root:
            raise OpError(f"Unsicherer Pfad im Archiv: {m.filename}")
    zf.extractall(target)


def install(key: str, job: Job | None = None) -> Path:
    """Helfer laden, prüfen und entpacken. Nur nach Bestätigung durch den Nutzer aufrufen!"""
    job = job or Job()
    h = HELPERS[key]
    dest_dir = tools_dir() / f"{h.key}-{h.version}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = h.url.rsplit("/", 1)[1]
    archive = str(dest_dir / fname)
    http_download(h.url, archive, job, h.sha256, f"{h.name} laden")
    if fname.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            _safe_extract(z, dest_dir)
        os.remove(archive)
    elif fname.endswith(".lzma"):
        with open(archive, "rb") as f:
            data = f.read()
        # .lzma („LZMA alone“): 1 Byte Eigenschaften, 4 Byte Wörterbuch, 8 Byte Größe, dann Rohdaten.
        props, dict_size = data[0], int.from_bytes(data[1:5], "little")
        size = int.from_bytes(data[5:13], "little")
        flt = {"id": lzma.FILTER_LZMA1, "lc": props % 9, "lp": (props // 9) % 5, "pb": props // 45,
               "dict_size": dict_size}
        out = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[flt]).decompress(data[13:])
        if size != 0xFFFFFFFFFFFFFFFF:
            out = out[:size]
        (dest_dir / h.exe).write_bytes(out)
        os.remove(archive)
    exe = dest_dir / h.exe
    if not exe.is_file():
        raise OpError(f"{h.name}: {h.exe} nach dem Entpacken nicht gefunden")
    return exe


def require(key: str, allow_download: bool, job: Job | None = None) -> Path:
    p = find(key)
    if p:
        return p
    h = HELPERS[key]
    if not allow_download:
        raise OpError(f"{h.name} {h.version} fehlt ({h.purpose}). Laden von {h.url} "
                      f"({h.size // 1024} KB, {h.license}) muss erst bestätigt werden "
                      f"(GUI-Dialog bzw. CLI --allow-download).")
    return install(key, job)
