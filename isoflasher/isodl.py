"""Offizielle ISOs herunterladen – Link und SHA-256 kommen jeweils von der offiziellen Seite.

Linux/Tools: Version und Prüfsumme werden live aus den offiziellen Verzeichnissen bzw. SHA256SUMS-
Dateien gelesen und nach dem Download verglichen. Windows: Fido (GPLv3, von Rufus) holt den
offiziellen, zeitlich begrenzten Microsoft-Link. Microsoft liefert dabei keine maschinenlesbare
Prüfsumme – die SHA-256-Werte stehen auf microsoft.com/software-download und können eingegeben werden.
"""
from __future__ import annotations

import os
import re
import subprocess
import urllib.request

from . import checksum, helpers, ps
from .jobs import Job, OpError

UA = {"User-Agent": helpers.USER_AGENT}


def _get(url: str, limit: int = 4 * 1024 * 1024) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(limit).decode("utf-8", "replace")


def _ver_key(v: str):
    return [int(x) for x in re.findall(r"\d+", v)]


def _sum_for(sums: str, name: str) -> str | None:
    for ln in sums.splitlines():
        m = re.match(r"([0-9a-fA-F]{64})\s+\*?(\S+)", ln.strip())
        if m and os.path.basename(m.group(2)) == name:
            return m.group(1).lower()
    return None


def ubuntu(variant: str = "desktop") -> dict:
    idx = _get("https://releases.ubuntu.com/")
    lts = sorted({v for v in re.findall(r'href="(\d\d\.04)/"', idx) if int(v[:2]) % 2 == 0}, key=_ver_key)
    if not lts:
        raise OpError("Ubuntu-Versionen nicht gefunden")
    base = f"https://releases.ubuntu.com/{lts[-1]}/"
    sums = _get(base + "SHA256SUMS")
    names = [n for n in re.findall(r"\*?(ubuntu-[\d.]+-" + re.escape(variant) + r"-amd64\.iso)", sums)]
    if not names:
        raise OpError(f"Ubuntu {lts[-1]}: keine {variant}-ISO in SHA256SUMS")
    name = sorted(set(names), key=lambda n: _ver_key(n.split("-")[1]))[-1]
    return {"name": f"Ubuntu {lts[-1]} LTS ({variant})", "file": name, "url": base + name,
            "sha256": _sum_for(sums, name), "sums_url": base + "SHA256SUMS"}


def mint(edition: str = "cinnamon") -> dict:
    base = "https://mirrors.edge.kernel.org/linuxmint/stable/"
    vers = sorted(re.findall(r'href="(\d+(?:\.\d+)?)/"', _get(base)), key=_ver_key)
    if not vers:
        raise OpError("Linux-Mint-Versionen nicht gefunden")
    v = vers[-1]
    sums = _get(f"{base}{v}/sha256sum.txt")
    name = f"linuxmint-{v}-{edition}-64bit.iso"
    return {"name": f"Linux Mint {v} {edition.capitalize()}", "file": name, "url": f"{base}{v}/{name}",
            "sha256": _sum_for(sums, name), "sums_url": f"{base}{v}/sha256sum.txt"}


def debian(desktop: str = "gnome") -> dict:
    base = "https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/"
    sums = _get(base + "SHA256SUMS")
    m = re.search(r"(debian-live-[\d.]+-amd64-" + re.escape(desktop) + r"\.iso)\b", sums)
    if not m:
        raise OpError(f"Debian Live {desktop} nicht gefunden")
    return {"name": f"Debian Live ({desktop})", "file": m.group(1), "url": base + m.group(1),
            "sha256": _sum_for(sums, m.group(1)), "sums_url": base + "SHA256SUMS"}


def memtest() -> dict:
    page = _get("https://www.memtest.org/")
    m = re.search(r'/download/(v[\d.]+)/(mt86plus_[\d.]+_x86_64\.iso\.zip)', page)
    if not m:
        raise OpError("Memtest86+-Download nicht gefunden")
    v, name = m.groups()
    sums = _get(f"https://www.memtest.org/download/{v}/sha256sum.txt")
    return {"name": f"Memtest86+ {v[1:]} (x86_64, ZIP mit ISO)", "file": name,
            "url": f"https://www.memtest.org/download/{v}/{name}", "sha256": _sum_for(sums, name),
            "sums_url": f"https://www.memtest.org/download/{v}/sha256sum.txt",
            "note": "ZIP – IsoFlasher kann die enthaltene ISO direkt 1:1 schreiben"}


def gparted() -> dict:
    page = _get("https://gparted.org/download.php")
    m = re.search(r'href="(https://downloads\.sourceforge\.net/gparted/(gparted-live-[\w.\-]+-amd64\.iso))"', page)
    if not m:
        raise OpError("GParted-Download nicht gefunden")
    url, name = m.groups()
    sums = _get("https://gparted.org/gparted-live/stable/CHECKSUMS.TXT")
    return {"name": "GParted Live " + name[13:-10], "file": name, "url": url,
            "sha256": _sum_for(sums, name), "sums_url": "https://gparted.org/gparted-live/stable/CHECKSUMS.TXT"}


def windows(version: str = "11", edition: str = "Pro", lang: str = "German", arch: str = "x64",
            release: str = "Latest", allow_download: bool = False, job: Job | None = None) -> dict:
    fido = helpers.require("fido", allow_download, job)
    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(fido),
            "-Win", version, "-Rel", release, "-Ed", edition, "-Lang", lang, "-Arch", arch, "-GetUrl"]
    p = subprocess.run(args, capture_output=True, timeout=180, creationflags=ps.CREATE_NO_WINDOW)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    m = re.search(r"https://\S+\.iso\S*", out)
    if not m:
        raise OpError("Fido lieferte keinen Link: " + out.strip()[-400:])
    url = m.group(0)
    name = re.search(r"/([^/?]+\.iso)", url).group(1)
    return {"name": f"Windows {version} {edition} ({lang}, {arch})", "file": name, "url": url,
            "sha256": None, "sums_url": "https://www.microsoft.com/software-download/windows" + version,
            "note": "Microsoft liefert die Prüfsumme nur auf der Download-Seite – dort vergleichen bzw. eingeben"}


CATALOG = {"ubuntu": ubuntu, "mint": mint, "debian": debian, "memtest": memtest, "gparted": gparted,
           "windows": windows}
NAMES = {"ubuntu": "Ubuntu LTS", "mint": "Linux Mint", "debian": "Debian Live", "memtest": "Memtest86+",
         "gparted": "GParted Live", "windows": "Windows (über Fido)"}


def resolve(key: str, **kw) -> dict:
    if key not in CATALOG:
        raise OpError(f"Unbekannt: {key} (möglich: {', '.join(CATALOG)})")
    info = CATALOG[key](**kw)
    info["key"] = key
    return info


def download(info: dict, folder: str, job: Job, expected: str | None = None) -> dict:
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, info["file"])
    exp = checksum.parse_expected(expected, info["file"]) if expected else info.get("sha256")
    res = helpers.http_download(info["url"], dest, job, exp, f"{info['name']} laden")
    res.update({"name": info["name"], "url": info["url"], "expected_sha256": exp,
                "checksum_source": "eingegeben" if expected else (info.get("sums_url") if exp else None)})
    if not exp:
        res["note"] = "Keine offizielle Prüfsumme verfügbar – SHA-256 selbst vergleichen: " + res["sha256"]
    return res
