"""IsoFlasher-Kommandozeile für Claude/ToolHub – jeder Befehl gibt genau ein JSON-Objekt aus.

Nur lesend:  drives, iso-info, verify, helpers, ventoy-list, catalog
Ändernd:     flash, windows, ventoy-install, ventoy-add, ventoy-remove, ventoy-persistence,
             persistence, backup, download
Ändernde Befehle verstehen --dry-run (nur Vorschau). Ohne --dry-run muss --confirm-target exakt die
Datenträgernummer (oder einen Laufwerksbuchstaben des Ziels) enthalten, bei mehreren Zielen alle,
durch Komma getrennt. Es gelten dieselben Sperren wie in der GUI: nur USB-Sticks/SD-Karten.
Fehlen Adminrechte, fragt Windows per UAC; der Vorgang läuft dann in einem versteckten Admin-Prozess.
Fehler: Meldung auf stderr, Exit-Code 1 (2 = gesperrt / Bestätigung falsch).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from isoflasher import (checksum, detect, disks, elevate, flash, guard, helpers, isodl,  # noqa: E402
                        persist, ps, ventoy, windows)
from isoflasher.jobs import Job, OpError  # noqa: E402


class CliError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def _job() -> Job:
    if os.environ.get("ISOFLASHER_PROGRESS") == "1":
        def p(d):
            print(json.dumps({"progress": d.get("text"), "percent": d.get("percent")}, ensure_ascii=False),
                  file=sys.stderr, flush=True)
        return Job(p)
    return Job()


# ---------------------------------------------------------------- lesend

def cmd_drives(a) -> dict:
    snap = disks.query()
    ds = [d.to_json() for d in snap.disks]
    for d, j in zip(snap.disks, ds):
        j["ventoy"] = ventoy.is_ventoy(d)
        j["confirm_with"] = disks.confirm_hint(d)
    if a.selectable:
        ds = [d for d in ds if d["selectable"]]
    return {"admin": ps.is_admin(), "disks": ds}


def cmd_iso_info(a) -> dict:
    if not os.path.isfile(a.image):
        raise CliError(f"Datei nicht gefunden: {a.image}")
    return detect.analyze(a.image).to_json()


def cmd_verify(a) -> dict:
    if not os.path.isfile(a.file):
        raise CliError(f"Datei nicht gefunden: {a.file}")
    return checksum.compare(a.file, a.expected, _job())


def cmd_helpers(a) -> dict:
    return {"tools_dir": str(helpers.tools_dir()), "helpers": [h.to_json() for h in helpers.HELPERS.values()]}


def cmd_ventoy_list(a) -> dict:
    snap = disks.query()
    d = _resolve(snap, a.target)
    return ventoy.list_isos(d.number)


def cmd_catalog(a) -> dict:
    out = []
    keys = [a.key] if a.key else [k for k in isodl.CATALOG if k != "windows"]
    for k in keys:
        try:
            out.append(isodl.resolve(k))
        except Exception as e:      # Netzwerk o. ä.
            out.append({"key": k, "error": str(e)})
    return {"items": out, "windows": "über download --key windows (Fido, braucht --allow-download beim ersten Mal)"}


# ---------------------------------------------------------------- ändernd

def _resolve(snap, text) -> disks.Disk:
    try:
        return disks.resolve_target(snap, text)
    except ValueError as e:
        raise CliError(str(e)) from None


def _targets(a, sources=None, need=None) -> tuple[list[disks.Disk], list[str], dict]:
    snap = disks.query()
    tlist = [t.strip() for t in str(a.target).split(",") if t.strip()]
    clist = [t.strip() for t in str(a.confirm_target or "").split(",") if t.strip()]
    ds = [_resolve(snap, t) for t in tlist]
    if len({d.number for d in ds}) != len(ds):
        raise CliError("Ein Datenträger ist doppelt angegeben")
    plan = []
    for i, d in enumerate(ds):
        typed = clist[i] if i < len(clist) else None
        plan.append({**d.to_json(), "problems": guard.problems(d, sources, need),
                     "confirm_target_needed": disks.confirm_hint(d),
                     "confirm_target_ok": disks.confirm_ok(d, typed)})
    return ds, clist, {"targets": plan}


def _gate(a, ds, clist, plan) -> None:
    bad = [f"{p['number']}: " + "; ".join(p["problems"]) for p in plan["targets"] if p["problems"]]
    if bad:
        raise CliError("Gesperrt – " + " | ".join(bad), 2)
    if not a.confirm_target:
        raise CliError("--confirm-target fehlt (nötig: " + ",".join(p["confirm_target_needed"] for p in plan["targets"]) + ")", 2)
    wrong = [str(p["number"]) for p in plan["targets"] if not p["confirm_target_ok"]]
    if wrong:
        raise CliError("--confirm-target passt nicht zu Datenträger " + ", ".join(wrong), 2)


def _elevate_or(a):
    if not ps.is_admin():
        return _run_elevated()
    return None


def cmd_flash(a) -> dict:
    if not os.path.isfile(a.image):
        raise CliError(f"Abbild nicht gefunden: {a.image}")
    info = detect.analyze(a.image)
    ds, clist, plan = _targets(a, [a.image], info.data_size)
    plan.update({"image": info.to_json(), "steps": ["Volumes sperren", "1:1 schreiben", "zurücklesen + SHA-256 vergleichen"]
                 + (["Persistenz-Partition anlegen"] if a.persistence_mb is not None else [])})
    if info.kind == "windows":
        plan["warning"] = "Windows-ISO: 1:1 schreiben bootet meist nicht – Befehl 'windows' verwenden"
    if a.dry_run:
        return {"dry_run": True, **plan}
    _gate(a, ds, clist, plan)
    el = _elevate_or(a)
    if el:
        return el
    results, errors = {}, {}
    job = _job()

    def one(d, typed):
        try:
            r = flash.write_image(d.number, a.image, typed, job, d.uid(), verify=not a.no_verify)
            if a.persistence_mb is not None:
                r["persistence"] = persist.add_partition(d.number, a.live_system or info.live_system or "",
                                                         typed, job, a.persistence_mb, image_end=r["bytes_written"])
            results[d.number] = r
        except Exception as e:     # pro Stick getrennt melden
            errors[d.number] = str(e)
    ths = [threading.Thread(target=one, args=(d, clist[i])) for i, d in enumerate(ds)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    out = {"dry_run": False, "results": [results[k] for k in sorted(results)],
           "errors": {str(k): v for k, v in errors.items()}}
    if errors:
        out["error"] = "; ".join(f"Datenträger {k}: {v}" for k, v in errors.items())
        out["code"] = 1
    return out


def cmd_windows(a) -> dict:
    if not os.path.isfile(a.image):
        raise CliError(f"ISO nicht gefunden: {a.image}")
    info = detect.analyze(a.image)
    ds, clist, plan = _targets(a, [a.image])
    if len(ds) != 1:
        raise CliError("windows: genau ein Ziel angeben")
    opts = _win_opts(a)
    plan.update({"image": info.to_json(), "options": opts.__dict__,
                 "steps": [f"Partitionstabelle {opts.style}, FAT32 (max. 32 GB)", "Dateien kopieren"]
                 + (["install.wim mit wimlib teilen"] if info.windows.get("needs_split") or opts.force_split else [])
                 + (["autounattend.xml schreiben"] if opts.wants_unattend else [])
                 + (["BIOS-Bootsektor (bootsect)"] if opts.style == "MBR" else []) + ["alle Dateien prüfen"],
                 "autounattend_preview": windows.unattend_xml(opts) if opts.wants_unattend else None,
                 "wimlib": helpers.HELPERS["wimlib"].to_json() if info.windows.get("needs_split") else None})
    if info.kind != "windows":
        plan["error_preview"] = "Keine Windows-Installations-ISO"
    if a.dry_run:
        return {"dry_run": True, **plan}
    if info.kind != "windows":
        raise CliError("Keine Windows-Installations-ISO erkannt")
    _gate(a, ds, clist, plan)
    el = _elevate_or(a)
    if el:
        return el
    return {"dry_run": False, **windows.create(ds[0].number, a.image, clist[0], opts, _job(), ds[0].uid())}


def _win_opts(a) -> windows.WinOptions:
    return windows.WinOptions(style=a.style, label=a.label or "", bypass_hw=a.bypass_hw,
                              bypass_online=a.bypass_online, local_user=a.local_user or "",
                              no_privacy_questions=a.no_privacy, no_bitlocker=a.no_bitlocker,
                              allow_download=a.allow_download, split_mb=a.split_mb,
                              force_split=a.force_split, verify=not a.no_verify)


def cmd_ventoy_install(a) -> dict:
    ds, clist, plan = _targets(a)
    if len(ds) != 1:
        raise CliError("ventoy-install: genau ein Ziel angeben")
    plan.update({"ventoy": helpers.HELPERS["ventoy"].to_json(), "gpt": not a.mbr,
                 "secure_boot": not a.no_secure_boot, "fs": a.fs, "reserve_mb": a.reserve_mb})
    if a.dry_run:
        return {"dry_run": True, **plan}
    _gate(a, ds, clist, plan)
    el = _elevate_or(a)
    if el:
        return el
    return {"dry_run": False, **ventoy.install(ds[0].number, clist[0], _job(), gpt=not a.mbr,
                                               secure_boot=not a.no_secure_boot, reserve_mb=a.reserve_mb,
                                               fs=a.fs, allow_download=a.allow_download,
                                               expect_uid=ds[0].uid())}


def _ventoy_target(a, sources=None):
    ds, clist, plan = _targets(a, sources)
    if len(ds) != 1:
        raise CliError("genau ein Ziel angeben")
    if not ventoy.is_ventoy(ds[0]):
        raise CliError(f"Datenträger {ds[0].number} ist kein Ventoy-Stick")
    return ds, clist, plan


def cmd_ventoy_add(a) -> dict:
    if not os.path.isfile(a.image):
        raise CliError(f"Datei nicht gefunden: {a.image}")
    ds, clist, plan = _ventoy_target(a, [a.image])
    plan.update({"image": a.image, "size": os.path.getsize(a.image), "subdir": a.subdir})
    if a.dry_run:
        return {"dry_run": True, **plan}
    _gate(a, ds, clist, plan)
    el = _elevate_or(a)
    if el:
        return el
    r = ventoy.add_iso(ds[0].number, a.image, clist[0], _job(), a.subdir or "", expect_uid=ds[0].uid())
    if a.persistence_mb:
        r["persistence"] = ventoy.add_persistence(ds[0].number, r["file"], a.persistence_mb, clist[0], _job())
    return {"dry_run": False, **r}


def cmd_ventoy_remove(a) -> dict:
    ds, clist, plan = _ventoy_target(a)
    plan["remove"] = a.path
    if a.dry_run:
        return {"dry_run": True, **plan}
    _gate(a, ds, clist, plan)
    return {"dry_run": False, **ventoy.remove_iso(ds[0].number, a.path, clist[0], ds[0].uid())}


def cmd_ventoy_persistence(a) -> dict:
    ds, clist, plan = _ventoy_target(a)
    plan.update({"iso": a.iso, "size_mb": a.size_mb})
    if a.dry_run:
        return {"dry_run": True, **plan}
    _gate(a, ds, clist, plan)
    return {"dry_run": False, **ventoy.add_persistence(ds[0].number, a.iso, a.size_mb, clist[0], _job(),
                                                         a.live_system, ds[0].uid())}


def cmd_persistence(a) -> dict:
    ds, clist, plan = _targets(a)
    if len(ds) != 1:
        raise CliError("persistence: genau ein Ziel angeben")
    plan.update({"live_system": a.live_system, "size_mb": a.size_mb or "Rest des Sticks"})
    if a.dry_run:
        return {"dry_run": True, **plan}
    _gate(a, ds, clist, plan)
    el = _elevate_or(a)
    if el:
        return el
    return {"dry_run": False, **persist.add_partition(ds[0].number, a.live_system, clist[0], _job(),
                                                      a.size_mb or 0, ds[0].uid())}


def cmd_backup(a) -> dict:
    ds, clist, plan = _targets(a, [a.out])
    if len(ds) != 1:
        raise CliError("backup: genau ein Quell-Datenträger")
    d = ds[0]
    # Sichern ändert den Stick nicht; gesperrt sind hier nur System-/interne Platten
    plan["targets"][0]["problems"] = [p for p in plan["targets"][0]["problems"] if "schreibgeschützt" not in p]
    plan.update({"out": a.out, "used_only": a.used_only,
                 "bytes": flash.used_size(d) if a.used_only else d.size})
    if os.path.exists(a.out):
        plan["targets"][0]["problems"].append(f"Datei existiert bereits: {a.out}")
    if a.dry_run:
        return {"dry_run": True, **plan}
    _gate(a, ds, clist, plan)
    el = _elevate_or(a)
    if el:
        return el
    return {"dry_run": False, **flash.backup(d.number, a.out, _job(), a.used_only, not a.no_verify, d.uid())}


def cmd_download(a) -> dict:
    kw = {}
    if a.key == "windows":
        kw = {"version": a.win_version, "edition": a.edition, "lang": a.lang, "arch": a.arch,
              "allow_download": a.allow_download}
    try:
        info = isodl.resolve(a.key, **kw)
    except OpError as e:
        raise CliError(str(e)) from None
    if a.dry_run:
        return {"dry_run": True, **info, "folder": a.folder}
    return {"dry_run": False, **isodl.download(info, a.folder, _job(), a.expected)}


# ---------------------------------------------------------------- Admin

def _run_elevated() -> dict:
    fd, out = tempfile.mkstemp(prefix="isoflasher_", suffix=".json")
    os.close(fd)
    args = [str(HERE / "cli.py")] + sys.argv[1:] + ["--result-file", out]
    try:
        code = elevate.run_elevated(sys.executable, args, cwd=str(HERE), wait=True, show=0)
    except OSError as e:
        raise CliError(str(e)) from None
    try:
        data = json.loads(Path(out).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        data = {}
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    if "error" in data and set(data) <= {"error", "code"}:
        raise CliError("(Admin-Prozess) " + data["error"], data.get("code", 1))
    if not data:
        raise CliError(f"Admin-Prozess endete mit Exit {code} ohne Ergebnis")
    data["elevated"] = True
    return data


# ---------------------------------------------------------------- Parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli.py", description="IsoFlasher (JSON-Ausgabe)")
    p.add_argument("--result-file", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--result-file", help=argparse.SUPPRESS)
        s.set_defaults(fn=fn)
        return s

    def change(s, target=True):
        if target:
            s.add_argument("--target", required=True, help="Datenträgernummer (3) oder Buchstabe (E:); mehrere mit Komma")
            s.add_argument("--confirm-target", dest="confirm_target", help="Bestätigung: dieselbe Nummer/derselbe Buchstabe")
        s.add_argument("--dry-run", dest="dry_run", action="store_true")

    s = add("drives", cmd_drives, "Datenträger mit Sperrstatus")
    s.add_argument("--selectable", action="store_true")
    s = add("iso-info", cmd_iso_info, "Abbild untersuchen (Typ, empfohlener Weg)")
    s.add_argument("--image", required=True)
    s = add("verify", cmd_verify, "SHA-256 einer Datei bilden und vergleichen")
    s.add_argument("--file", required=True)
    s.add_argument("--expected", help="SHA-256, SHA256SUMS-Inhalt oder Pfad zu einer SUMS-Datei")
    add("helpers", cmd_helpers, "Status der Open-Source-Helfer")
    s = add("ventoy-list", cmd_ventoy_list, "ISOs auf einem Ventoy-Stick")
    s.add_argument("--target", required=True)
    s = add("catalog", cmd_catalog, "Offizielle Download-Links mit Prüfsumme")
    s.add_argument("--key", choices=list(isodl.CATALOG))

    s = add("flash", cmd_flash, "Abbild 1:1 schreiben (auch .img.xz/.zip) mit Prüfsummen-Vergleich")
    s.add_argument("--image", required=True)
    s.add_argument("--no-verify", dest="no_verify", action="store_true")
    s.add_argument("--persistence-mb", dest="persistence_mb", type=int,
                   help="Persistenz-Partition anlegen (0 = Rest des Sticks)")
    s.add_argument("--live-system", dest="live_system", choices=["casper", "live-boot"])
    change(s)

    s = add("windows", cmd_windows, "Windows-Installationsstick")
    s.add_argument("--image", required=True)
    s.add_argument("--style", choices=["GPT", "MBR"], default="GPT")
    s.add_argument("--label")
    s.add_argument("--bypass-hw", dest="bypass_hw", action="store_true")
    s.add_argument("--bypass-online", dest="bypass_online", action="store_true")
    s.add_argument("--local-user", dest="local_user")
    s.add_argument("--no-privacy", dest="no_privacy", action="store_true")
    s.add_argument("--no-bitlocker", dest="no_bitlocker", action="store_true")
    s.add_argument("--split-mb", dest="split_mb", type=int, default=windows.SPLIT_MB)
    s.add_argument("--force-split", dest="force_split", action="store_true", help="install.wim immer teilen")
    s.add_argument("--allow-download", dest="allow_download", action="store_true")
    s.add_argument("--no-verify", dest="no_verify", action="store_true")
    change(s)

    s = add("ventoy-install", cmd_ventoy_install, "Ventoy auf Stick installieren (löscht alles)")
    s.add_argument("--mbr", action="store_true", help="MBR statt GPT")
    s.add_argument("--no-secure-boot", dest="no_secure_boot", action="store_true")
    s.add_argument("--reserve-mb", dest="reserve_mb", type=int, default=0)
    s.add_argument("--fs", default="exFAT", choices=["exFAT", "NTFS", "FAT32"])
    s.add_argument("--allow-download", dest="allow_download", action="store_true")
    change(s)

    s = add("ventoy-add", cmd_ventoy_add, "ISO auf Ventoy-Stick kopieren (+ prüfen)")
    s.add_argument("--image", required=True)
    s.add_argument("--subdir", default="")
    s.add_argument("--persistence-mb", dest="persistence_mb", type=int)
    change(s)
    s = add("ventoy-remove", cmd_ventoy_remove, "ISO vom Ventoy-Stick löschen")
    s.add_argument("--path", required=True, help="Pfad auf dem Stick, z. B. /ubuntu.iso")
    change(s)
    s = add("ventoy-persistence", cmd_ventoy_persistence, "Persistenz-Datei für eine ISO auf dem Ventoy-Stick")
    s.add_argument("--iso", required=True, help="Pfad auf dem Stick, z. B. /ubuntu.iso")
    s.add_argument("--size-mb", dest="size_mb", type=int, default=4096)
    s.add_argument("--live-system", dest="live_system", choices=["casper", "live-boot"])
    change(s)
    s = add("persistence", cmd_persistence, "Persistenz-Partition hinter einem 1:1 geschriebenen Abbild")
    s.add_argument("--live-system", dest="live_system", required=True, choices=["casper", "live-boot"])
    s.add_argument("--size-mb", dest="size_mb", type=int, default=0)
    change(s)

    s = add("backup", cmd_backup, "Stick/SD-Karte als Abbild sichern (.img, .img.xz, .img.gz, .zip)")
    s.add_argument("--out", required=True)
    s.add_argument("--used-only", dest="used_only", action="store_true", help="Nur bis Ende der letzten Partition (MBR)")
    s.add_argument("--no-verify", dest="no_verify", action="store_true")
    change(s)

    s = add("download", cmd_download, "Offizielle ISO laden und Prüfsumme vergleichen")
    s.add_argument("--key", required=True, choices=list(isodl.CATALOG))
    s.add_argument("--folder", default=str(Path.home() / "Downloads"))
    s.add_argument("--expected")
    s.add_argument("--win-version", dest="win_version", default="11")
    s.add_argument("--edition", default="Pro")
    s.add_argument("--lang", default="German")
    s.add_argument("--arch", default="x64")
    s.add_argument("--allow-download", dest="allow_download", action="store_true", help="Fido laden erlauben")
    change(s, target=False)
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Without a console (pythonw, .exe) the stream is None; in tests it is often a StringIO without reconfigure.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    a = build_parser().parse_args(argv)
    try:
        res = a.fn(a)
        code = int(res.get("code", 0)) if isinstance(res, dict) and "error" in res else 0
    except CliError as e:
        res, code = {"error": str(e), "code": e.code}, e.code
    except guard.Blocked as e:
        res, code = {"error": str(e), "code": 2}, 2
    except (OpError, ps.PSError, OSError, ValueError) as e:
        res, code = {"error": str(e), "code": 1}, 1
    except Exception as e:  # noqa: BLE001 – unerwartet, trotzdem als JSON melden
        res, code = {"error": f"{type(e).__name__}: {e}", "code": 1}, 1
    if a.result_file:
        Path(a.result_file).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        return code
    if "error" in res and set(res) <= {"error", "code"}:
        print(res["error"], file=sys.stderr)
        return code
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
