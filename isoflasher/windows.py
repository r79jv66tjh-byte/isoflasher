"""Windows-Installationsstick: partitionieren, FAT32 formatieren, Dateien kopieren, install.wim teilen,
optional autounattend.xml (Windows-11-Anpassungen), Bootsektor für BIOS, danach alles prüfen.

GPT  -> UEFI-Start (Secure Boot bleibt möglich, da FAT32 + Microsofts eigene EFI-Dateien).
MBR  -> BIOS- und UEFI-Start (aktive Partition, Bootsektor per bootsect.exe aus der ISO bzw. von Windows).
FAT32 kann Windows nur bis 32 GB formatieren – auf größeren Sticks wird die Partition 32 GB groß.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from . import detect, disks, flash, guard, helpers, ps, rawio
from .jobs import Job, Meter, OpError, message

FAT32_LIMIT = 32 * 1024 ** 3
SPLIT_MB = 3800
CHUNK = 4 * 1024 * 1024


@dataclass
class WinOptions:
    style: str = "GPT"                  # GPT | MBR
    label: str = ""
    bypass_hw: bool = False             # TPM / Secure Boot / RAM
    bypass_online: bool = False         # Online-Konto-Zwang aus (BypassNRO)
    local_user: str = ""                # optional: lokales Konto anlegen
    no_privacy_questions: bool = False
    no_bitlocker: bool = False
    split_mb: int = SPLIT_MB
    force_split: bool = False           # install.wim auch unter 4 GB teilen
    allow_download: bool = False
    verify: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def wants_unattend(self) -> bool:
        return any((self.bypass_hw, self.bypass_online, self.local_user, self.no_privacy_questions,
                    self.no_bitlocker))


# ---------------------------------------------------------------- autounattend.xml

def unattend_xml(o: WinOptions, arch: str = "amd64") -> str:
    comp = (f'<component name="{{name}}" processorArchitecture="{arch}" publicKeyToken="31bf3856ad364e35" '
            'language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">')
    parts = ['<?xml version="1.0" encoding="utf-8"?>',
             '<unattend xmlns="urn:schemas-microsoft-com:unattend">']
    if o.bypass_hw:
        cmds = [f"reg add HKLM\\SYSTEM\\Setup\\LabConfig /v {v} /t REG_DWORD /d 1 /f"
                for v in ("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck")]
        parts += ['  <settings pass="windowsPE">', "    " + comp.format(name="Microsoft-Windows-Setup"),
                  "      <UserData><ProductKey><Key></Key><WillShowUI>OnError</WillShowUI></ProductKey>"
                  "<AcceptEula>false</AcceptEula></UserData>",
                  "      <RunSynchronous>"]
        for i, c in enumerate(cmds, 1):
            parts.append(f'        <RunSynchronousCommand wcm:action="add"><Order>{i}</Order>'
                         f"<Path>{escape(c)}</Path></RunSynchronousCommand>")
        parts += ["      </RunSynchronous>", "    </component>", "  </settings>"]
    spec = []
    if o.bypass_online:
        spec.append("reg add HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\OOBE /v BypassNRO /t REG_DWORD /d 1 /f")
    if o.no_bitlocker:
        spec.append("reg add HKLM\\SYSTEM\\CurrentControlSet\\Control\\BitLocker /v PreventDeviceEncryption /t REG_DWORD /d 1 /f")
    if spec:
        parts += ['  <settings pass="specialize">', "    " + comp.format(name="Microsoft-Windows-Deployment"),
                  "      <RunSynchronous>"]
        for i, c in enumerate(spec, 1):
            parts.append(f'        <RunSynchronousCommand wcm:action="add"><Order>{i}</Order>'
                         f"<Path>{escape(c)}</Path></RunSynchronousCommand>")
        parts += ["      </RunSynchronous>", "    </component>", "  </settings>"]
    if o.no_privacy_questions or o.local_user:
        parts += ['  <settings pass="oobeSystem">', "    " + comp.format(name="Microsoft-Windows-Shell-Setup")]
        if o.no_privacy_questions:
            parts.append("      <OOBE><ProtectYourPC>3</ProtectYourPC></OOBE>")
        if o.local_user:
            u = escape(o.local_user)
            parts += ["      <UserAccounts><LocalAccounts>",
                      '        <LocalAccount wcm:action="add"><Name>' + u + "</Name><DisplayName>" + u +
                      "</DisplayName><Group>Administrators</Group>"
                      "<Password><Value></Value><PlainText>true</PlainText></Password></LocalAccount>",
                      "      </LocalAccounts></UserAccounts>"]
        parts += ["    </component>", "  </settings>"]
    parts.append("</unattend>")
    return "\n".join(p for p in parts if p != "") + "\n"


# ---------------------------------------------------------------- ISO einhängen

class MountedIso:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.root = ""

    def __enter__(self):
        # Mit Laufwerksbuchstaben einhängen: wimlib und gestartete Programme kommen mit
        # \\?\Volume{…}-Pfaden nicht zurecht. Ohne freien Buchstaben ersatzweise der Volume-Pfad.
        out = ps.run_json(f"$i = Mount-DiskImage -ImagePath {ps.quote(self.path)} -Access ReadOnly -PassThru;"
                          "$v=$null; for ($k=0; $k -lt 20 -and -not $v.DriveLetter; $k++) { Start-Sleep -Milliseconds 250; $v = $i | Get-Volume };"
                          "[pscustomobject]@{L=[string]$v.DriveLetter; P=$v.Path} | ConvertTo-Json -Compress",
                          timeout=120)
        letter = disks._letter(out.get("L"))
        self.root = f"{letter}:\\" if letter else str(out["P"])
        if not self.root.endswith("\\"):
            self.root += "\\"
        return self

    def __exit__(self, *exc):
        try:
            ps.run(f"Dismount-DiskImage -ImagePath {ps.quote(self.path)} | Out-Null", timeout=60)
        except ps.PSError:
            pass


def _walk(root: str) -> list[tuple[str, int]]:
    out = []
    for dp, dn, fn in os.walk(root):
        for f in fn:
            full = os.path.join(dp, f)
            out.append((os.path.relpath(full, root), os.path.getsize(full)))
    return out


def _run_streamed(args: list[str], job: Job, meter: Meter | None, cwd: str | None = None) -> tuple[int, list[str]]:
    s = ps.Streamed(args, encoding="utf-8")
    job.children.append(s)
    lines = []
    try:
        for ln in s.lines():
            lines.append(ln)
            m = re.search(r"\((\d+)%\)", ln) or re.search(r"(\d+)%", ln)
            if meter and m and meter.total:
                meter.update(int(meter.total * int(m.group(1)) / 100))
        return s.wait(), lines
    finally:
        job.children.remove(s)


def _partition(d: disks.Disk, o: WinOptions, label: str) -> str:
    active = " -IsActive" if o.style == "MBR" else ""
    out = ps.run_json(
        f"$n={d.number}; $d=Get-Disk -Number $n; if ($d.PartitionStyle -ne 'RAW') "
        f"{{ Clear-Disk -Number $n -RemoveData -RemoveOEM -Confirm:$false }};"
        f"Initialize-Disk -Number $n -PartitionStyle {o.style};"
        # GPT legt evtl. eine MSR-Partition an -> größten freien Bereich nehmen, höchstens 32 GB
        f"$free=(Get-Disk -Number $n).LargestFreeExtent; $size=[math]::Min($free, {FAT32_LIMIT});"
        "$size=[int64]([math]::Floor($size / 1MB) * 1MB);"
        f"$p = New-Partition -DiskNumber $n -Size $size{active} -AssignDriveLetter;"
        "Start-Sleep -Milliseconds 500;"
        f"$p | Format-Volume -FileSystem FAT32 -NewFileSystemLabel {ps.quote(label)} -Confirm:$false -Force | Out-Null;"
        f"$p = Get-Partition -DiskNumber $n -PartitionNumber $p.PartitionNumber;"
        "[pscustomobject]@{L=[string]$p.DriveLetter} | ConvertTo-Json -Compress", timeout=600)
    letter = disks._letter(out.get("L"))
    if not letter:
        raise OpError("Neue Partition hat keinen Laufwerksbuchstaben bekommen")
    return letter


def _label_from(info: detect.ImageInfo) -> str:
    lab = re.sub(r"[^A-Z0-9_\-]", "", (info.label or "WININSTALL").upper())[:11]
    return lab or "WININSTALL"


def create(number: int, iso: str, confirm: str, opts: WinOptions, job: Job,
           expect_uid: str | None = None) -> dict:
    info = detect.analyze(iso)
    if info.kind != "windows":
        raise OpError(f"Keine Windows-Installations-ISO erkannt ({info.kind_text})")
    w = info.windows
    if w["too_big_for_fat32"] and not w["needs_split"]:
        raise OpError("install.esd ist größer als 4 GB und lässt sich für FAT32 nicht teilen")
    split = bool(w["needs_split"] or (opts.force_split and w["install_file"].endswith(".wim")))
    wimlib = helpers.require("wimlib", opts.allow_download, job) if split else None
    need = info.size + 64 * 1024 * 1024
    d = guard.check(number, confirm, expect_uid, sources=[iso], need=min(need, FAT32_LIMIT))
    tgt = f"Datenträger {d.number}"
    label = (opts.label or _label_from(info))[:11].upper()
    message(job, f"{tgt}: partitionieren ({opts.style}) und FAT32 formatieren …", tgt, 0)
    locks = flash.lock_disk_volumes(d)
    rawio.release(locks)
    letter = _partition(d, opts, label)
    dest = f"{letter}:\\"
    res = {"target": d.number, "letter": f"{letter}:", "style": opts.style, "label": label,
           "split": split, "unattend": opts.wants_unattend, "files": 0, "warnings": []}
    with MountedIso(iso) as m:
        files = _walk(m.root)
        inst_rel = w["install_file"].replace("/", "\\").lower()
        copy_list = [(r, s) for r, s in files if not (split and r.lower() == inst_rel)]
        total = sum(s for _, s in copy_list)
        meter = Meter(job, "copy", total, tgt, "Dateien kopieren")
        done = 0
        hashes: dict[str, str] = {}
        for rel, size in copy_list:
            job.check()
            src, dst = os.path.join(m.root, rel), os.path.join(dest, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            h = hashlib.sha256()
            with open(src, "rb") as fi, open(dst, "wb") as fo:
                while True:
                    job.check()
                    b = fi.read(CHUNK)
                    if not b:
                        break
                    fo.write(b)
                    h.update(b)
                    done += len(b)
                    meter.update(done)
            hashes[rel] = h.hexdigest()
        meter.update(done, force=True)
        res["files"] = len(copy_list)
        if split:
            message(job, f"{tgt}: install.wim wird geteilt (wimlib) …", tgt)
            sm = Meter(job, "split", w["install_size"], tgt, "install.wim teilen")
            code, lines = _run_streamed([str(wimlib), "split", os.path.join(m.root, inst_rel),
                                         os.path.join(dest, "sources", "install.swm"), str(opts.split_mb)], job, sm)
            job.check()
            if code != 0:
                raise OpError("wimlib split fehlgeschlagen: " + " | ".join(lines[-3:]))
            res["swm_parts"] = sorted(f for f in os.listdir(os.path.join(dest, "sources"))
                                      if f.lower().startswith("install") and f.lower().endswith(".swm"))
        if opts.style == "MBR":
            message(job, f"{tgt}: BIOS-Bootsektor schreiben (bootsect) …", tgt)
            res["bootsect"] = _bootsect(m.root, letter)
    if opts.wants_unattend:
        arch = "arm64" if w.get("arm64") else "amd64"
        with open(os.path.join(dest, "autounattend.xml"), "w", encoding="utf-8") as f:
            f.write(unattend_xml(opts, arch))
    if opts.verify:
        res["verify"] = _verify(d, letter, hashes, job, tgt, wimlib if split else None)
    message(job, f"{tgt}: Windows-Stick fertig", tgt, 100)
    return res


def _bootsect(iso_root: str, letter: str) -> dict:
    cands = [os.path.join(iso_root, "boot", "bootsect.exe"),
             os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "bootsect.exe")]
    errors = []
    tmpdir = tempfile.mkdtemp(prefix="isoflasher_bootsect_")
    try:
        for i, exe in enumerate(cands):
            if not os.path.isfile(exe):
                continue
            # Programme lassen sich nicht direkt aus \\?\Volume{…}-Pfaden starten -> erst kopieren
            run = os.path.join(tmpdir, f"bootsect{i}.exe")
            shutil.copyfile(exe, run)
            try:
                p = subprocess.run([run, "/nt60", f"{letter}:", "/force", "/mbr"], capture_output=True,
                                   creationflags=ps.CREATE_NO_WINDOW, timeout=120)
            except OSError as e:
                errors.append(f"{exe}: {e}")
                continue
            out = p.stdout.decode(ps.oem_encoding(), "replace").strip()
            if p.returncode == 0:
                return {"tool": "ISO" if i == 0 else exe, "ok": True}
            errors.append(f"{exe}: {out[-200:]}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    raise OpError("BIOS-Bootsektor konnte nicht geschrieben werden: " + (" | ".join(errors) or "bootsect.exe nicht gefunden"))


def _verify(d: disks.Disk, letter: str, hashes: dict[str, str], job: Job, tgt: str, wimlib) -> dict:
    """Volume aushängen (Cache leeren), dann jede Datei neu lesen und mit der Quelle vergleichen."""
    h = rawio.lock_dismount(f"\\\\.\\{letter}:")
    rawio.release([h] if h else [])
    total = 0
    root = f"{letter}:\\"
    for rel in hashes:
        total += os.path.getsize(os.path.join(root, rel))
    meter = Meter(job, "verify", total, tgt, "Prüfen")
    done, bad = 0, []
    for rel, exp in hashes.items():
        hh = hashlib.sha256()
        with open(os.path.join(root, rel), "rb") as f:
            while True:
                job.check()
                b = f.read(CHUNK)
                if not b:
                    break
                hh.update(b)
                done += len(b)
                meter.update(done)
        if hh.hexdigest() != exp:
            bad.append(rel)
    meter.update(done, force=True)
    res = {"files_checked": len(hashes), "mismatches": bad}
    if wimlib:
        swm = os.path.join(root, "sources", "install.swm")
        code, lines = _run_streamed([str(wimlib), "verify", swm, "--ref=" + os.path.join(root, "sources", "install*.swm")],
                                    job, None)
        res["wim_verify"] = code == 0
        if code != 0:
            bad.append("install.swm (wimlib verify: " + " | ".join(lines[-2:]) + ")")
    if bad:
        raise OpError(f"{tgt}: Prüfung fehlgeschlagen für: " + ", ".join(bad[:10]))
    return res
