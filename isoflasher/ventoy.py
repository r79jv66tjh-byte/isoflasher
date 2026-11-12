"""Multiboot mit Ventoy (GPLv3): Stick einrichten, ISOs kopieren/auflisten/löschen, Persistenz.

Ventoy2Disk.exe wird im offiziellen Kommandozeilenmodus aufgerufen:
    Ventoy2Disk.exe VTOYCLI /I /PhyDrive:N [/GPT] [/NOSB] [/NOUSBCheck] [/R:MB] [/FS:exFAT]
Fortschritt steht in cli_percent.txt, Ergebnis in cli_done.txt (0 = OK), Log in cli_log.txt.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time

from . import detect, disks, extfs, guard, helpers, ps
from .jobs import Job, Meter, OpError, message

CHUNK = 4 * 1024 * 1024
PERSIST_LABEL = {"casper": "casper-rw", "live-boot": "persistence"}


def ventoy_volume(d: disks.Disk):
    """Datenpartition eines Ventoy-Sticks (Label „Ventoy“ + Partition „VTOYEFI“) oder None."""
    labels = {v.label.upper() for v in d.volumes}
    if "VTOYEFI" not in labels:
        return None
    for v in d.volumes:
        if v.partition_number == 1 and v.letter:
            return v
    return None


def is_ventoy(d: disks.Disk) -> bool:
    return ventoy_volume(d) is not None


def install(number: int, confirm: str, job: Job, gpt: bool = True, secure_boot: bool = True,
            reserve_mb: int = 0, fs: str = "exFAT", allow_download: bool = False,
            expect_uid: str | None = None) -> dict:
    exe = helpers.require("ventoy", allow_download, job)
    d = guard.check(number, confirm, expect_uid)
    tgt = f"Datenträger {d.number}"
    wd = exe.parent
    for f in ("cli_percent.txt", "cli_done.txt", "cli_log.txt"):
        try:
            os.remove(wd / f)
        except OSError:
            pass
    args = [str(exe), "VTOYCLI", "/I", f"/PhyDrive:{d.number}"]
    if gpt:
        args.append("/GPT")
    if not secure_boot:
        args.append("/NOSB")
    if d.is_test_disk or d.bus_type != "USB":
        args.append("/NOUSBCheck")    # SD-Karten-Leser und Test-VHDX sind kein „USB“ – Sperren prüft IsoFlasher selbst
    if reserve_mb:
        args.append(f"/R:{int(reserve_mb)}")
    if fs:
        args.append(f"/FS:{fs.upper()}")
    message(job, f"{tgt}: Ventoy wird installiert …", tgt, 0)
    p = subprocess.Popen(args, cwd=str(wd), creationflags=ps.CREATE_NO_WINDOW)

    class _K:
        def kill(self_):
            p.kill()
    k = _K()
    job.children.append(k)
    meter = Meter(job, "ventoy", 100, tgt, "Ventoy installieren")
    try:
        while p.poll() is None:
            if job.cancelled:
                p.kill()
                job.check()
            try:
                pct = int((wd / "cli_percent.txt").read_text(errors="ignore").strip() or 0)
                meter.update(pct)
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
    finally:
        job.children.remove(k)
    done = ""
    try:
        done = (wd / "cli_done.txt").read_text(errors="ignore").strip()
    except OSError:
        pass
    log = ""
    try:
        log = (wd / "cli_log.txt").read_text(errors="replace")[-1500:]
    except OSError:
        pass
    if done != "0":
        raise OpError(f"Ventoy-Installation fehlgeschlagen (cli_done={done or '-'}, Exit {p.returncode}). "
                      f"Log-Ende: {log[-400:]}")
    meter.update(100, force=True)
    # neu einlesen, Buchstaben abwarten
    vol = None
    for _ in range(30):
        snap = disks.query()
        nd = snap.disk(d.number)
        vol = ventoy_volume(nd) if nd else None
        if vol:
            break
        time.sleep(1)
    message(job, f"{tgt}: Ventoy installiert", tgt, 100)
    return {"target": d.number, "gpt": gpt, "secure_boot": secure_boot, "fs": fs,
            "letter": f"{vol.letter}:" if vol else None, "ventoy_version": helpers.HELPERS["ventoy"].version}


def _target(number: int, confirm: str | None, expect_uid=None, sources=None, check_confirm=True):
    if check_confirm:
        d = guard.check(number, confirm, expect_uid, sources=sources, require_admin=False)
    else:
        d = disks.query().disk(int(number))
        if d is None:
            raise OpError(f"Datenträger {number} nicht gefunden")
    v = ventoy_volume(d)
    if not v:
        raise OpError(f"Datenträger {d.number} ist kein Ventoy-Stick (oder Datenpartition ohne Laufwerksbuchstaben)")
    return d, v


def list_isos(number: int) -> dict:
    d, v = _target(number, None, check_confirm=False)
    root = f"{v.letter}:\\"
    items = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [x for x in dn if x.lower() not in ("ventoy", "persistence", "system volume information")]
        for f in fn:
            if f.lower().endswith((".iso", ".img", ".wim", ".vhd", ".vhdx", ".efi", ".vtoy")):
                full = os.path.join(dp, f)
                items.append({"path": "/" + os.path.relpath(full, root).replace("\\", "/"),
                              "size": os.path.getsize(full)})
    cfg = read_config(v.letter)
    pers = {e.get("image"): e.get("backend") for e in cfg.get("persistence", [])}
    for it in items:
        it["persistence"] = pers.get(it["path"])
    return {"target": d.number, "letter": f"{v.letter}:", "label": v.label, "free_bytes": v.free,
            "size_bytes": v.size, "isos": items}


def read_config(letter: str) -> dict:
    p = f"{letter}:\\ventoy\\ventoy.json"
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_config(letter: str, cfg: dict):
    os.makedirs(f"{letter}:\\ventoy", exist_ok=True)
    with open(f"{letter}:\\ventoy\\ventoy.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def add_iso(number: int, iso: str, confirm: str, job: Job, subdir: str = "", verify: bool = True,
            expect_uid: str | None = None) -> dict:
    d, v = _target(number, confirm, expect_uid, sources=[iso])
    size = os.path.getsize(iso)
    if size > v.free:
        raise OpError(f"Nicht genug Platz auf {v.letter}: ({disks.fmt_size(v.free)} frei, braucht {disks.fmt_size(size)})")
    if v.filesystem.upper() == "FAT32" and size > detect.FAT32_MAX:
        raise OpError("Die ISO ist größer als 4 GB, die Ventoy-Partition ist aber FAT32")
    rel = os.path.join(subdir.strip("/\\"), os.path.basename(iso)) if subdir else os.path.basename(iso)
    dst = os.path.join(f"{v.letter}:\\", rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tgt = f"Datenträger {d.number}"
    h = hashlib.sha256()
    meter = Meter(job, "copy", size, tgt, "ISO kopieren")
    done = 0
    tmp = dst + ".part"
    try:
        with open(iso, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                job.check()
                b = fi.read(CHUNK)
                if not b:
                    break
                fo.write(b)
                h.update(b)
                done += len(b)
                meter.update(done)
            fo.flush()
            os.fsync(fo.fileno())
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    res = {"target": d.number, "file": "/" + rel.replace("\\", "/"), "bytes": done, "sha256": h.hexdigest()}
    if verify:
        _flush_volume(v.letter)
        hh = hashlib.sha256()
        m2 = Meter(job, "verify", size, tgt, "Prüfen")
        n = 0
        with open(dst, "rb") as f:
            while True:
                job.check()
                b = f.read(CHUNK)
                if not b:
                    break
                hh.update(b)
                n += len(b)
                m2.update(n)
        res["verified"] = hh.hexdigest() == res["sha256"]
        if not res["verified"]:
            raise OpError("Kopie auf dem Stick ist fehlerhaft (Prüfsumme weicht ab)")
    return res


def _flush_volume(letter: str):
    """Volume kurz aushängen, damit beim Prüfen wirklich vom Stick gelesen wird (braucht Admin)."""
    if not ps.is_admin():
        return
    from . import rawio
    h = rawio.lock_dismount(f"\\\\.\\{letter}:")
    rawio.release([h] if h else [])


def remove_iso(number: int, path: str, confirm: str, expect_uid: str | None = None) -> dict:
    d, v = _target(number, confirm, expect_uid)
    rel = path.replace("/", "\\").lstrip("\\")
    full = os.path.normpath(os.path.join(f"{v.letter}:\\", rel))
    if not full.upper().startswith(f"{v.letter}:\\") or not os.path.isfile(full):
        raise OpError(f"Datei {path} nicht auf dem Ventoy-Stick gefunden")
    if os.path.normcase(os.path.dirname(full)).endswith("\\ventoy"):
        raise OpError("Dateien im Ordner /ventoy werden nicht gelöscht")
    os.remove(full)
    cfg = read_config(v.letter)
    removed_backend = None
    key = "/" + rel.replace("\\", "/")
    if cfg.get("persistence"):
        keep = []
        for e in cfg["persistence"]:
            if e.get("image") == key:
                removed_backend = e.get("backend")
            else:
                keep.append(e)
        cfg["persistence"] = keep
        write_config(v.letter, cfg)
    return {"target": d.number, "removed": key, "persistence_entry_removed": removed_backend}


def add_persistence(number: int, iso_path: str, size_mb: int, confirm: str, job: Job,
                    live_system: str | None = None, expect_uid: str | None = None) -> dict:
    """Persistenz-Datei (ext4) für eine ISO auf dem Ventoy-Stick anlegen und in ventoy.json eintragen."""
    d, v = _target(number, confirm, expect_uid)
    key = "/" + iso_path.replace("\\", "/").lstrip("/")
    iso_full = os.path.join(f"{v.letter}:\\", key.lstrip("/").replace("/", "\\"))
    if not os.path.isfile(iso_full):
        raise OpError(f"ISO {key} liegt nicht auf dem Stick – erst hinzufügen")
    if not live_system:
        live_system = detect.analyze(iso_full, mount_ok=False).live_system
    label = PERSIST_LABEL.get(live_system or "")
    if not label:
        raise OpError("Für diese ISO ist keine Persistenz bekannt (unterstützt: Ubuntu/Mint/… (casper) und "
                      "Debian/Kali/… (live-boot))")
    size = int(size_mb) * 1024 * 1024
    if v.filesystem.upper() == "FAT32" and size > detect.FAT32_MAX:
        raise OpError("Persistenz-Datei > 4 GB geht auf FAT32 nicht")
    if size + 16 * 1024 * 1024 > v.free:
        raise OpError("Nicht genug freier Platz für die Persistenz-Datei")
    base = os.path.splitext(os.path.basename(key))[0]
    backend = f"/persistence/{base}.dat"
    path = os.path.join(f"{v.letter}:\\persistence", base + ".dat")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    files = {"persistence.conf": b"/ union\n"} if label == "persistence" else None
    message(job, f"Persistenz-Datei {backend} ({size_mb} MB, {label}) wird angelegt …", f"Datenträger {d.number}")
    info = extfs.make_file(path, size, label, files)
    cfg = read_config(v.letter)
    entries = [e for e in cfg.get("persistence", []) if e.get("image") != key]
    entries.append({"image": key, "backend": backend})
    cfg["persistence"] = entries
    write_config(v.letter, cfg)
    return {"target": d.number, "image": key, "backend": backend, "label": label, "size_mb": size_mb,
            "filesystem": info}
