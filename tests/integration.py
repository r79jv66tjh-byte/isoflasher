"""Integrationstest an zwei TEST-VHDX (als Administrator ausführen).

Testet alle schreibenden Funktionen über CLI und Module: 1:1 schreiben (MBR/GPT-Hybrid-ISO, .img,
.img.xz/.gz/.zip, zwei Sticks gleichzeitig), Prüfsummen-Vergleich, Persistenz-Partition (MBR + GPT),
Sichern/Zurückschreiben, Abbrechen, Windows-Stick (GPT/UEFI und MBR/BIOS, install.wim teilen,
autounattend.xml), Ventoy (installieren, ISOs kopieren/auflisten/löschen, Persistenz-Datei) und die Sperren.

Vor jeder schreibenden Aktion wird geprüft, dass das Ziel eine der Test-VHDX ist (Bustyp + Dateipfad).
Am Ende werden die VHDX getrennt und gelöscht.

    python tests\\integration.py [--only name,name] [--keep]
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
import traceback
import xml.dom.minidom
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from isoflasher import disks, flash, guard, ps, rawio  # noqa: E402
from isoflasher.jobs import Cancelled, Job  # noqa: E402
from testdisk import TestDisk  # noqa: E402

WORK = ROOT / "tests" / "_work"
IMG = WORK / "images"
RESULTS: list[dict] = []
LOG = WORK / "integration.log"
EXT4_CHECK = os.environ.get("ISOFLASHER_EXT4_CHECK")   # optional: Python mit Paket „ext4“ (unabhängiger Leser)


def log(name, ok, **info):
    RESULTS.append({"test": name, "ok": bool(ok), **info})
    line = ("OK   " if ok else "FAIL ") + name + " " + json.dumps(info, ensure_ascii=False, default=str)[:600]
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def sha(path: Path, n: int | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        left = n
        while True:
            b = f.read(1 << 20 if left is None else min(1 << 20, left))
            if not b:
                break
            h.update(b)
            if left is not None:
                left -= len(b)
                if left <= 0:
                    break
    return h.hexdigest()


def cli(*args) -> tuple[int, dict | str]:
    p = subprocess.run([sys.executable, str(ROOT / "cli.py"), *map(str, args)], capture_output=True, timeout=3600)
    out = p.stdout.decode("utf-8", "replace")
    try:
        return p.returncode, json.loads(out)
    except ValueError:
        return p.returncode, (out + p.stderr.decode("utf-8", "replace")).strip()


def read_disk(n: int, pos: int, length: int) -> bytes:
    with rawio.Device(f"\\\\.\\PhysicalDrive{n}", write=False) as dev:
        buf = rawio.AlignedBuffer((length + 511) // 512 * 512)
        try:
            got = dev.read(buf, (length + 511) // 512 * 512, pos)
            return buf.bytes(min(got, length))
        finally:
            buf.free()


def disk_hash(n: int, length: int) -> str:
    return flash.read_hash(n, length, Job())


def disk_matches(n: int, img: Path) -> tuple[bool, bool]:
    """(gleich ohne MBR-Signatur, Signatur unverändert) – Windows darf Bytes 440–445 nachträglich setzen."""
    data = img.read_bytes()
    got = read_disk(n, 0, len(data))
    mask = lambda b: b[:440] + b"\0" * 6 + b[446:]
    return mask(got) == mask(data), got[440:446] == data[440:446]


def partitions(n: int) -> list[dict]:
    return ps.as_list(ps.run_json(f"Get-Partition -DiskNumber {n} -ErrorAction SilentlyContinue | Select-Object "
                                  "PartitionNumber,Offset,Size,@{n='T';e={[string]$_.Type}},GptType,MbrType,IsActive,"
                                  "@{n='L';e={[string]$_.DriveLetter}} | ConvertTo-Json -Compress"))


def ext_check(n: int, offset: int, length: int, label: str, want_file: str | None) -> dict:
    sb = read_disk(n, offset + 1024, 1024)
    magic = struct.unpack_from("<H", sb, 56)[0]
    lab = sb[120:136].rstrip(b"\0").decode()
    res = {"magic_ok": magic == 0xEF53, "label": lab}
    if EXT4_CHECK and length <= 512 << 20:
        dump = WORK / "_extdump.img"
        with open(dump, "wb") as f:
            pos = 0
            while pos < length:
                k = min(8 << 20, length - pos)
                f.write(read_disk(n, offset + pos, k))
                pos += k
        res["external"] = ext_external(dump, want_file)
        dump.unlink()
    res["ok"] = res["magic_ok"] and lab == label and res.get("external", {}).get("ok", True)
    return res


def ext_external(path: Path, want_file: str | None) -> dict:
    code = ("import sys,ext4,json\nv=ext4.Volume(open(sys.argv[1],'rb'))\n"
            "names=[d.name_str for d,t in v.root.opendir()]\nout={'names':names}\n"
            "f=sys.argv[2] if len(sys.argv)>2 else ''\n"
            "if f: out['content']=v.inode_at('/'+f).open().read().decode()\nprint(json.dumps(out))")
    p = subprocess.run([EXT4_CHECK, "-I", "-c", code, str(path)] + ([want_file] if want_file else []),
                       capture_output=True, timeout=120)
    try:
        r = json.loads(p.stdout.decode())
        r["ok"] = "lost+found" in r["names"] and (not want_file or r.get("content") == "/ union\n")
        return r
    except ValueError:
        return {"ok": False, "err": p.stderr.decode(errors="replace")[-300:]}


class Ctx:
    def __init__(self):
        self.a = TestDisk(WORK / "isoflasher_test_a.vhdx", 1024)
        self.b = TestDisk(WORK / "isoflasher_test_b.vhdx", 1024)


# ---------------------------------------------------------------- Tests

def t_locks(c: Ctx):
    snap = disks.query()
    sysd = next(d for d in snap.disks if d.is_system or d.is_boot)
    try:
        guard.check(sysd.number, str(sysd.number))
        log("lock_system_disk", False, msg="nicht gesperrt!")
    except guard.Blocked as e:
        log("lock_system_disk", True, msg=str(e)[:200])
    internal = [d for d in snap.disks if not d.removable and not d.hard_locks]
    for d in internal:
        try:
            guard.check(d.number, str(d.number))
            log(f"lock_internal_disk_{d.number}", False)
        except guard.Blocked as e:
            log(f"lock_internal_disk_{d.number}", "Kein Wechseldatenträger" in str(e), msg=str(e)[:200])
    # Test-VHDX ohne Freigabe -> gesperrt
    saved = os.environ.pop("ISOFLASHER_TEST_DISKS")
    try:
        guard.check(c.a.number, str(c.a.number))
        log("lock_unregistered_vhdx", False)
    except guard.Blocked as e:
        log("lock_unregistered_vhdx", "Kein Wechseldatenträger" in str(e), msg=str(e)[:200])
    finally:
        os.environ["ISOFLASHER_TEST_DISKS"] = saved
    try:
        guard.check(c.a.number, str(c.b.number))
        log("lock_wrong_confirm", False)
    except guard.Blocked as e:
        log("lock_wrong_confirm", "passt nicht" in str(e), msg=str(e)[:200])
    try:
        guard.check(c.a.number, str(c.a.number), expect_uid="anders|x|1")
        log("lock_uid_changed", False)
    except guard.Blocked as e:
        log("lock_uid_changed", "nicht mehr derselbe" in str(e), msg=str(e)[:200])
    # Quelle auf dem Ziel
    L = c.a.prepare("SRCSTICK")
    src = Path(f"{L}:\\source.iso")
    src.write_bytes(os.urandom(1 << 20))
    try:
        guard.check(c.a.number, L, sources=[str(src)])
        log("lock_source_on_target", False)
    except guard.Blocked as e:
        log("lock_source_on_target", "Quelldatei" in str(e), msg=str(e)[:200])
    # CLI-Sperren
    code, out = cli("flash", "--image", IMG / "raw.img", "--target", sysd.number, "--confirm-target", sysd.number)
    log("cli_lock_system", code == 2, code=code, out=str(out)[:200])
    code, out = cli("flash", "--image", IMG / "raw.img", "--target", c.a.number)
    log("cli_missing_confirm", code == 2 and src.exists(), code=code, out=str(out)[:200])
    code, out = cli("flash", "--image", IMG / "raw.img", "--target", c.a.number, "--confirm-target", "Z:")
    log("cli_wrong_confirm", code == 2 and src.exists(), code=code, out=str(out)[:200])
    code, out = cli("flash", "--image", IMG / "raw.img", "--target", c.a.number, "--dry-run")
    log("cli_dry_run_no_change", code == 0 and isinstance(out, dict) and out.get("dry_run") and src.exists(),
        code=code)
    code, out = cli("flash", "--image", src, "--target", c.a.number, "--confirm-target", L)
    log("cli_source_on_target", code == 2, code=code, out=str(out)[:200])
    code, out = cli("drives", "--selectable")
    nums = [d["number"] for d in out["disks"]] if isinstance(out, dict) else []
    locked = {d.number for d in snap.disks if d.blocks()}
    log("cli_drives_selectable_excludes_locked", {c.a.number, c.b.number} <= set(nums) and not (set(nums) & locked),
        nums=nums, locked=sorted(locked))


def t_flash_parallel(c: Ctx):
    c.a.prepare("STICKA")
    c.b.prepare("STICKB")
    c.a.assert_test(), c.b.assert_test()
    img = IMG / "hybrid_mbr.iso"
    t0 = time.time()
    code, out = cli("flash", "--image", img, "--target", f"{c.a.number},{c.b.number}",
                    "--confirm-target", f"{c.a.number},{c.b.number}")
    ok = code == 0 and isinstance(out, dict) and len(out["results"]) == 2 and all(r["verified"] for r in out["results"])
    ind = [disk_matches(c.a.number, img), disk_matches(c.b.number, img)]
    log("flash_two_sticks_parallel_verified", ok and all(m for m, _ in ind), code=code, secs=round(time.time() - t0, 1),
        independent_check=ind, warnings=[r.get("warnings") for r in out.get("results", [])] if isinstance(out, dict) else None,
        out=str(out)[:300] if not ok else "")
    info = ps.run_json(f"Get-Disk -Number {c.a.number} | Select-Object @{{n='S';e={{[string]$_.PartitionStyle}}}} | ConvertTo-Json -Compress")
    log("flash_partition_table_visible", info.get("S") == "MBR", style=info)


def t_persist_mbr(c: Ctx):
    c.a.assert_test()
    code, out = cli("persistence", "--target", c.a.number, "--live-system", "casper", "--size-mb", 200,
                    "--confirm-target", c.a.number)
    ok = code == 0 and isinstance(out, dict)
    parts = partitions(c.a.number)
    ext = None
    if ok:
        ext = ext_check(c.a.number, out["start_lba"] * 512, out["sectors"] * 512, "casper-rw", None)
    # ISO-Inhalt ab Sektor 1 unverändert (Sektor 0 = MBR bekommt den neuen Eintrag)
    n = (IMG / "hybrid_mbr.iso").stat().st_size
    iso_ok = hashlib.sha256(read_disk(c.a.number, 512, n - 512)).hexdigest() ==         hashlib.sha256((IMG / "hybrid_mbr.iso").read_bytes()[512:]).hexdigest()
    log("persistence_partition_mbr", ok and ext and ext["ok"] and len(parts) >= 2 and iso_ok,
        code=code, out=str(out)[:300], parts=[(p["PartitionNumber"], p["Size"], p.get("MbrType")) for p in parts],
        ext=ext, iso_unchanged=iso_ok)


def t_persist_gpt(c: Ctx):
    c.b.assert_test()
    img = IMG / "hybrid_gpt.iso"
    code, out = cli("flash", "--image", img, "--target", c.b.number, "--confirm-target", c.b.number,
                    "--persistence-mb", 0, "--live-system", "live-boot")
    ok = code == 0 and isinstance(out, dict) and out["results"] and out["results"][0]["verified"]
    p = out["results"][0].get("persistence") if ok else None
    parts = partitions(c.b.number)
    ext = ext_check(c.b.number, p["start_lba"] * 512, p["sectors"] * 512, "persistence", "persistence.conf") if p else None
    st = ps.run_json(f"Get-Disk -Number {c.b.number} | Select-Object @{{n='S';e={{[string]$_.PartitionStyle}}}},Size,LargestFreeExtent | ConvertTo-Json -Compress")
    # GPT-Sicherung am Ende des Datenträgers prüfen
    size = st["Size"]
    tail = read_disk(c.b.number, size - 512, 512)
    log("flash_gpt_with_persistence_rest", bool(ok and p and ext["ok"] and len(parts) >= 2 and st["S"] == "GPT"
                                                and tail[:8] == b"EFI PART"),
        code=code, persistence=p, parts=[(x["PartitionNumber"], x["Size"], x.get("GptType")) for x in parts],
        ext=ext, disk=st, out=str(out)[:300] if not ok else "")


def t_compressed(c: Ctx):
    raw_hash = sha(IMG / "raw.img")
    for name, disk in (("raw.img.xz", c.a), ("raw.zip", c.b), ("raw.img.gz", c.a), ("raw.img", c.b)):
        disk.assert_test()
        code, out = cli("flash", "--image", IMG / name, "--target", disk.number, "--confirm-target", disk.number)
        ok = code == 0 and isinstance(out, dict) and out["results"][0]["verified"]
        same, sig = disk_matches(disk.number, IMG / "raw.img") if ok else (False, False)
        log(f"flash_{name.replace('.', '_')}", ok and out["results"][0]["sha256_written"] == raw_hash and same,
            code=code, signature_unchanged=sig, warnings=out["results"][0].get("warnings") if ok else None,
            out=str(out)[:300] if not ok else "")


def t_backup_restore(c: Ctx):
    c.b.assert_test()
    full, small = WORK / "backup_full.img", WORK / "backup_used.img.xz"
    for f in (full, small, Path(str(full) + ".sha256"), Path(str(small) + ".sha256")):
        if f.exists():
            f.unlink()
    code, out = cli("backup", "--target", c.b.number, "--out", full, "--confirm-target", c.b.number)
    n = (IMG / "raw.img").stat().st_size
    ok1 = code == 0 and out.get("verified") and full.stat().st_size == ps.run_json(
        f"(Get-Disk -Number {c.b.number}).Size") and hashlib.sha256(read_disk(c.b.number, 0, n)).hexdigest() == sha(full, n)
    log("backup_full_img", ok1, code=code, out=str(out)[:300])
    code, out = cli("backup", "--target", c.b.number, "--out", small, "--used-only", "--confirm-target", c.b.number)
    ok2 = code == 0 and out.get("verified") and out.get("sha256_data") == hashlib.sha256(read_disk(c.b.number, 0, n)).hexdigest()         and out.get("bytes") == n
    log("backup_used_only_xz", ok2, code=code, bytes=out.get("bytes") if isinstance(out, dict) else None,
        file_size=small.stat().st_size if small.exists() else None, out=str(out)[:300])
    c.a.assert_test()
    code, out = cli("flash", "--image", small, "--target", c.a.number, "--confirm-target", c.a.number)
    ok3 = code == 0 and out["results"][0]["verified"] and disk_matches(c.a.number, IMG / "raw.img")[0]
    log("restore_from_backup_xz", ok3, code=code, out=str(out)[:300] if not ok3 else "")
    code, out = cli("backup", "--target", c.b.number, "--out", full, "--confirm-target", c.b.number)
    log("backup_refuses_existing_file", code == 2, code=code)


def t_cancel(c: Ctx):
    big = WORK / "big.img"
    if not big.exists():
        with open(big, "wb") as f:
            for _ in range(400):
                f.write(os.urandom(1 << 20))
    c.a.assert_test()
    d = disks.query().disk(c.a.number)
    job = Job()
    seen = []

    def prog(p):
        seen.append(p)
        if p.get("phase") == "write" and (p.get("done") or 0) > 40 << 20:
            job.cancel()
    job.progress = prog
    t0 = time.time()
    try:
        flash.write_image(c.a.number, str(big), str(c.a.number), job, d.uid())
        log("cancel_during_write", False, msg="lief durch")
    except Cancelled:
        last = max((p.get("done") or 0) for p in seen if p.get("phase") == "write")
        has_speed = any(p.get("speed") for p in seen) and any(p.get("eta") is not None for p in seen)
        log("cancel_during_write", last < 400 << 20, stopped_at_mb=last >> 20, secs=round(time.time() - t0, 1),
            progress_has_speed_and_eta=has_speed)


def t_windows_gpt(c: Ctx):
    c.a.assert_test()
    iso = IMG / "windows.iso"
    code, out = cli("windows", "--image", iso, "--target", c.a.number, "--confirm-target", c.a.number,
                    "--style", "GPT", "--force-split", "--split-mb", 20, "--bypass-hw", "--bypass-online",
                    "--local-user", "TestUser", "--no-privacy", "--no-bitlocker")
    ok = code == 0 and isinstance(out, dict)
    details = {}
    if ok:
        L = out["letter"][0]
        src = Path(f"{L}:\\sources")
        parts = sorted(p.name for p in src.iterdir() if p.name.lower().startswith("install"))
        x = Path(f"{L}:\\autounattend.xml").read_text(encoding="utf-8")
        xml.dom.minidom.parseString(x)
        vol = ps.run_json(f"Get-Volume -DriveLetter {L} | Select-Object @{{n='FS';e={{[string]$_.FileSystemType}}}} | ConvertTo-Json -Compress")
        st = ps.run_json(f"(Get-Disk -Number {c.a.number}).PartitionStyle | ConvertTo-Json")
        details = {"install_parts": parts, "fs": vol, "style": st, "verify": out.get("verify"),
                   "unattend_has": [k for k in ("BypassTPMCheck", "BypassNRO", "ProtectYourPC", "PreventDeviceEncryption", "TestUser") if k in x],
                   "bootx64": Path(f"{L}:\\efi\\boot\\bootx64.efi").exists()}
        ok = (len(parts) >= 2 and "install.wim" not in parts and vol.get("FS") == "FAT32" and str(st) in ("GPT", "2")
              and out["verify"]["mismatches"] == [] and out["verify"].get("wim_verify") and len(details["unattend_has"]) == 5
              and details["bootx64"])
    log("windows_gpt_split_unattend", ok, code=code, details=details, out=str(out)[:400] if not ok else "")


def t_windows_mbr(c: Ctx):
    c.b.assert_test()
    iso = IMG / "windows.iso"
    code, out = cli("windows", "--image", iso, "--target", c.b.number, "--confirm-target", c.b.number, "--style", "MBR")
    ok = code == 0 and isinstance(out, dict)
    details = {}
    if ok:
        parts = partitions(c.b.number)
        mbr = read_disk(c.b.number, 0, 512)
        pbr = read_disk(c.b.number, parts[0]["Offset"], 4096)
        details = {"active": parts[0].get("IsActive"), "mbr_code": mbr[:440].strip(b"\0") != b"",
                   "pbr_bootmgr": b"BOOTMGR" in pbr, "bootsect": out.get("bootsect"), "install_wim":
                   Path(f"{out['letter'][0]}:\\sources\\install.wim").exists(), "verify": out.get("verify")}
        ok = details["active"] and details["mbr_code"] and details["pbr_bootmgr"] and details["install_wim"] \
            and out["verify"]["mismatches"] == []
    log("windows_mbr_bios_bootsect", ok, code=code, details=details, out=str(out)[:400] if not ok else "")


def t_ventoy(c: Ctx):
    c.a.assert_test()
    code, out = cli("ventoy-install", "--target", c.a.number, "--confirm-target", c.a.number)
    ok = code == 0 and isinstance(out, dict) and out.get("letter")
    log("ventoy_install", ok, code=code, out=str(out)[:400])
    if not ok:
        return
    code, out = cli("ventoy-add", "--target", c.a.number, "--confirm-target", c.a.number,
                    "--image", IMG / "hybrid_mbr.iso", "--persistence-mb", 128)
    ok = code == 0 and out.get("verified") and out.get("sha256") == sha(IMG / "hybrid_mbr.iso")
    log("ventoy_add_iso_verified_with_persistence", ok and out.get("persistence", {}).get("label") == "casper-rw",
        code=code, out=str(out)[:400])
    code, out2 = cli("ventoy-add", "--target", c.a.number, "--confirm-target", c.a.number,
                     "--image", IMG / "plain.iso", "--subdir", "Tools")
    log("ventoy_add_second_iso_subdir", code == 0 and out2.get("verified"), code=code)
    code, lst = cli("ventoy-list", "--target", c.a.number)
    paths = {i["path"]: i.get("persistence") for i in lst.get("isos", [])} if isinstance(lst, dict) else {}
    log("ventoy_list", paths.get("/hybrid_mbr.iso") == "/persistence/hybrid_mbr.dat" and "/Tools/plain.iso" in paths,
        isos=paths)
    L = lst["letter"][0]
    dat = Path(f"{L}:\\persistence\\hybrid_mbr.dat")
    ext = ext_external(dat, None) if EXT4_CHECK else {"ok": True, "skipped": True}
    sb = open(dat, "rb").read(2048)[1024:]
    cfg = json.loads(Path(f"{L}:\\ventoy\\ventoy.json").read_text(encoding="utf-8"))
    log("ventoy_persistence_file", ext["ok"] and struct.unpack_from("<H", sb, 56)[0] == 0xEF53
        and sb[120:129] == b"casper-rw" and cfg["persistence"][0]["backend"] == "/persistence/hybrid_mbr.dat",
        ext=ext, cfg=cfg)
    code, out = cli("ventoy-remove", "--target", c.a.number, "--confirm-target", c.a.number, "--path", "/Tools/plain.iso")
    code2, lst = cli("ventoy-list", "--target", c.a.number)
    log("ventoy_remove", code == 0 and all(i["path"] != "/Tools/plain.iso" for i in lst["isos"]), code=code)
    code, out = cli("ventoy-remove", "--target", c.a.number, "--confirm-target", c.b.number, "--path", "/hybrid_mbr.iso")
    log("ventoy_remove_wrong_confirm_blocked", code == 2 and Path(f"{L}:\\hybrid_mbr.iso").exists(), code=code)


def t_gui(c: Ctx):
    """Dieselben Wege über die Oberfläche (ohne Dialoge: Bestätigung wird wie getippt übergeben)."""
    from isoflasher import gui
    app = gui.App()
    app.quiet = True

    def pump(cond, timeout=900):
        t0 = time.time()
        while time.time() - t0 < timeout:
            app.update()
            if cond():
                return True
            time.sleep(0.05)
        return False

    def wait_job():
        pump(lambda: app.busy, 5)
        return pump(lambda: not app.busy)

    def fresh():
        app.snap = None
        app.refresh()
        pump(lambda: app.snap is not None and app.snap.disk(c.a.number) is not None, 120)
    try:
        fresh()
        # gesperrte Auswahl wird abgelehnt
        app.select([0])
        app.var_image.set(str(IMG / "hybrid_mbr.iso"))
        app.start_create(typed=["0"])
        log("gui_system_disk_refused", not app.busy and "Gesperrt" in app.log.get("1.0", "end"))
        # zwei Sticks gleichzeitig
        c.a.assert_test(), c.b.assert_test()
        app.select([c.a.number, c.b.number])
        app.var_mode.set("auto")
        app.start_create(typed=[str(c.a.number), str(c.b.number)])
        wait_job()
        r = app.last_result or {}
        ok = not app.last_error and len(r.get("results", [])) == 2 and all(x["verified"] for x in r["results"])
        log("gui_flash_two_sticks", ok, err=app.last_error, progress_rows=len(app.ptree.get_children()))
        # Sichern über die GUI
        fresh()
        out = WORK / "gui_backup.img.gz"
        for f in (out, Path(str(out) + ".sha256")):
            if f.exists():
                f.unlink()
        app.select([c.b.number])
        app.var_bk.set(str(out))
        app.var_bk_used.set(True)
        app.start_backup(typed=[str(c.b.number)])
        wait_job()
        log("gui_backup", not app.last_error and (app.last_result or {}).get("verified"), err=app.last_error)
        # Prüfsumme-Reiter
        app.var_hf.set(str(IMG / "raw.img"))
        app.var_hexp.set(sha(IMG / "raw.img"))
        app.start_hash()
        wait_job()
        log("gui_hash_match", (app.last_result or {}).get("match") is True and "STIMMT" in app.lbl_hash.cget("text"))
        app.var_hexp.set("0" * 64)
        app.start_hash()
        wait_job()
        log("gui_hash_mismatch", (app.last_result or {}).get("match") is False)
        # Windows-Stick über die GUI (GPT, Konto-Optionen)
        fresh()
        c.a.assert_test()
        app.select([c.a.number])
        app.var_image.set(str(IMG / "windows.iso"))
        app.var_mode.set("auto")
        app.var_style.set("GPT")
        app.var_hw.set(True)
        app.var_online.set(True)
        app.start_create(typed=[str(c.a.number)])
        wait_job()
        r = app.last_result or {}
        log("gui_windows_stick", not app.last_error and r.get("verify", {}).get("mismatches") == [] and r.get("unattend"),
            err=app.last_error)
        # Abbrechen über die GUI
        fresh()
        c.b.assert_test()
        app.select([c.b.number])
        app.var_image.set(str(WORK / "big.img"))
        app.var_mode.set("dd")
        app.last_error = None
        app.start_create(typed=[str(c.b.number)])
        started = pump(lambda: app.busy, 10)

        def writing():
            for i in app.ptree.get_children():
                v = app.ptree.item(i, "values")
                if "Schreiben" in v[0] and v[1] not in ("…", "") and float(v[1]) > 2:
                    return True
            return False
        in_write = pump(writing, 120)
        pct_at_cancel = [app.ptree.item(i, "values") for i in app.ptree.get_children()]
        app.cancel()
        wait_job()
        log("gui_cancel", started and in_write and bool(app.last_error) and "abgebrochen" in app.last_error.lower(),
            err=app.last_error, started=started, in_write=in_write, at=pct_at_cancel,
            gui_log=app.log.get("1.0", "end")[-600:])
        # Ventoy über die GUI
        fresh()
        c.b.assert_test()
        app.select([c.b.number])
        app.start_ventoy_install(typed=[str(c.b.number)])
        wait_job()
        fresh()
        app.select([c.b.number])
        app.start_ventoy_add(files=[str(IMG / "hybrid_gpt.iso")], typed=[str(c.b.number)])
        wait_job()
        app.nb.select(1)
        app.refresh_ventoy()
        items = list(app.vtree.get_children())
        log("gui_ventoy_install_add", not app.last_error and "/hybrid_gpt.iso" in items, err=app.last_error, items=items)
    finally:
        app.destroy()


TESTS = [("locks", t_locks), ("flash", t_flash_parallel), ("persist_mbr", t_persist_mbr),
         ("persist_gpt", t_persist_gpt), ("compressed", t_compressed), ("backup", t_backup_restore),
         ("cancel", t_cancel), ("windows_gpt", t_windows_gpt), ("windows_mbr", t_windows_mbr),
         ("ventoy", t_ventoy), ("gui", t_gui)]


def detach_stale() -> list[str]:
    """Noch angehängte Test-VHDX aus tests\\_work (z. B. nach abgebrochenem Lauf) abmelden."""
    out = []
    for r in ps.as_list(ps.run_json("Get-Disk | Where-Object { $_.BusType -eq 'File Backed Virtual' } | "
                                    "Select-Object Location | ConvertTo-Json -Compress")):
        loc = str(r.get("Location") or "")
        if loc.lower().startswith(str(WORK).lower() + "\\") and loc.lower().endswith(".vhdx"):
            ps.run(f"Dismount-DiskImage -ImagePath {ps.quote(loc)} | Out-Null")
            out.append(loc)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--ext4-python", help="Python mit Paket 'ext4' für die unabhängige ext4-Prüfung")
    a = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    global EXT4_CHECK
    EXT4_CHECK = a.ext4_python or EXT4_CHECK
    WORK.mkdir(parents=True, exist_ok=True)
    LOG.write_text("", encoding="utf-8")
    if not ps.is_admin():
        print("Bitte als Administrator starten")
        return 1
    stale = detach_stale()
    if stale:
        print("Alte Test-VHDX abgemeldet:", stale)
    c = Ctx()
    # Abmelden auch bei Strg+C/Fehlern außerhalb des try (atexit) – doppelt schadet nicht
    atexit.register(lambda: None if a.keep else (c.a.destroy(quiet=True), c.b.destroy(quiet=True)))
    try:
        c.a.create()
        c.b.create()
        log("setup", True, a=c.a.number, b=c.b.number)
        only = set(a.only.split(",")) if a.only else None
        for name, fn in TESTS:
            if only and name not in only:
                continue
            try:
                fn(c)
            except SystemExit:
                raise
            except Exception as e:
                log(name + "_exception", False, err=f"{type(e).__name__}: {e}", tb=traceback.format_exc()[-1500:])
    finally:
        if not a.keep:
            c.a.destroy()
            c.b.destroy()
        (WORK / "integration_result.json").write_text(json.dumps(RESULTS, indent=2, ensure_ascii=False, default=str),
                                                      encoding="utf-8")
    bad = [r["test"] for r in RESULTS if not r["ok"]]
    print(f"{len(RESULTS) - len(bad)}/{len(RESULTS)} OK", "FEHLER: " + ", ".join(bad) if bad else "")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
