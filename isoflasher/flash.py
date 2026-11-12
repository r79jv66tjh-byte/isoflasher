"""1:1 schreiben (DD), zurücklesen und vergleichen; Datenträger als Abbild sichern.

Ablauf beim Schreiben:
  1. alle Volumes des Datenträgers sperren und aushängen (Handles bleiben offen),
  2. Daten ab 1 MiB schreiben, den ersten MiB (Partitionstabelle) ganz zum Schluss – so hängt Windows
     nicht mitten im Schreiben neue Partitionen ein,
  3. SHA-256 der geschriebenen Daten mitrechnen, danach alles zurücklesen und vergleichen.
"""
from __future__ import annotations

import gzip
import hashlib
import lzma
import os
import zipfile

from . import disks, guard, ps, rawio
from .jobs import Cancelled, Job, Meter, OpError, message
from .source import open_source

CHUNK = 4 * 1024 * 1024
HEAD = 1024 * 1024


class MiBHashes:
    """MD5 je MiB (in Datenträger-Reihenfolge), um bei einer Abweichung die Stelle zu finden."""

    def __init__(self):
        self.list: list[bytes] = []
        self._h = hashlib.md5()
        self._n = 0

    def feed(self, data: bytes):
        mv = memoryview(data)
        while mv:
            k = min(len(mv), 1024 * 1024 - self._n)
            self._h.update(mv[:k])
            self._n += k
            mv = mv[k:]
            if self._n == 1024 * 1024:
                self.list.append(self._h.digest())
                self._h, self._n = hashlib.md5(), 0

    def done(self) -> list[bytes]:
        if self._n:
            self.list.append(self._h.digest())
            self._h, self._n = hashlib.md5(), 0
        return self.list


def first_diff(a: list[bytes], b: list[bytes]) -> tuple[int | None, int]:
    bad = [i for i in range(max(len(a), len(b))) if (a[i] if i < len(a) else None) != (b[i] if i < len(b) else None)]
    return (bad[0] if bad else None), len(bad)


def _round_up(n: int, s: int) -> int:
    return (n + s - 1) // s * s


def lock_disk_volumes(d: disks.Disk) -> list:
    handles = []
    for v in d.volumes:
        path = (f"\\\\.\\{v.letter}:" if v.letter else v.path)
        if path:
            h = rawio.lock_dismount(path)
            if h:
                handles.append(h)
    return handles


def wipe_layout(d: disks.Disk):
    """Partitionen entfernen, damit Windows die alten Volumes nicht wieder einhängt."""
    try:
        ps.run(f"$d=Get-Disk -Number {d.number}; if ($d.PartitionStyle -ne 'RAW') "
               f"{{ Clear-Disk -Number {d.number} -RemoveData -RemoveOEM -Confirm:$false }}", timeout=120)
    except ps.PSError:
        pass   # z. B. RAW oder unbekannter Stil – die Rohdaten werden ohnehin überschrieben


def rescan(number: int):
    try:
        with rawio.Device(f"\\\\.\\PhysicalDrive{int(number)}", write=False) as dev:
            dev.update_properties()
        ps.run("Update-HostStorageCache", timeout=60)
    except (OSError, ps.PSError):
        pass


def write_image(number: int, image: str, confirm: str, job: Job, expect_uid: str | None = None,
                verify: bool = True) -> dict:
    """Abbild (auch .xz/.gz/.bz2/.zip) 1:1 auf Datenträger ``number`` schreiben."""
    tgt = f"Datenträger {number}"
    with open_source(image) as src:
        need = src.total
        d = guard.check(number, confirm, expect_uid, sources=[image], need=need)
        sector = max(512, d.sector_size)
        message(job, f"{tgt}: Volumes werden gesperrt und ausgehängt …", tgt)
        locks = lock_disk_volumes(d)
        written = 0
        h = hashlib.sha256()
        mib = MiBHashes()
        meter = Meter(job, "write", need, tgt, "Schreiben")
        try:
            buf = rawio.AlignedBuffer(CHUNK)
            try:
                with rawio.Device(f"\\\\.\\PhysicalDrive{d.number}", write=True) as dev:
                    head = src.read_full(HEAD)
                    if not head:
                        raise OpError("Abbild ist leer")
                    # Alte Partitionstabelle entfernen: Windows gibt damit alle alten Volumes frei
                    # (auch solche ohne Laufwerksbuchstaben, die sich nicht sperren ließen).
                    dev.delete_layout()
                    h.update(head)
                    mib.feed(head)
                    written = len(head)
                    dev.seek(HEAD)
                    while True:
                        job.check()
                        data = src.read_full(CHUNK) if len(head) == HEAD else b""
                        if not data:
                            break
                        h.update(data)
                        mib.feed(data)
                        if written + len(data) > d.size:
                            raise OpError("Abbild ist größer als der Datenträger")
                        n = buf.load(data, _round_up(len(data), sector))
                        dev.write(buf, n)
                        written += len(data)
                        if need:
                            meter.update(written)
                        else:   # entpackte Größe unbekannt (.gz/.bz2): Fortschritt der gepackten Datei
                            meter.update(src.consumed(), total=src.file_size)
                    job.check()
                    # zum Schluss die Partitionstabelle
                    n = buf.load(head, _round_up(len(head), sector))
                    dev.write(buf, n, pos=0)
                    dev.flush()
                    meter.update(written if need else src.file_size, force=True)
            finally:
                buf.free()
            digest = h.hexdigest()
            res = {"target": d.number, "model": d.model, "image": image, "bytes_written": written,
                   "sha256_written": digest, "verified": False, "warnings": []}
            if verify:
                # Zurücklesen, solange die Volumes noch gesperrt sind und Windows die neue Partitionstabelle
                # noch nicht eingelesen hat – so wird genau geprüft, was auf dem Medium steht.
                rmib = MiBHashes()
                rb = read_hash(d.number, written, job, tgt, sector, rmib)
                res["sha256_readback"] = rb
                res["verified"] = rb == digest
                if rb != digest:
                    first, count = first_diff(mib.done(), rmib.done())
                    raise OpError(f"{tgt}: Prüfsumme nach dem Schreiben stimmt NICHT (geschrieben {digest[:16]}…, "
                                  f"gelesen {rb[:16]}…; erste Abweichung im MiB {first}, {count} MiB betroffen). "
                                  "Stick defekt oder gefälscht?")
        finally:
            rawio.release(locks)
            rescan(d.number)
    res["warnings"] += windows_changes(head, _read_first_sector(d.number, sector, len(head)))
    message(job, f"{tgt}: fertig" + (" – Prüfsumme stimmt" if verify else ""), tgt, 100)
    return res


def windows_changes(head: bytes, after: bytes | None) -> list[str]:
    """Was Windows nach dem Einlesen am Anfang des Sticks geändert hat (nach erfolgreicher Prüfung)."""
    if after is None:
        return []
    k = min(len(head), len(after))
    diff = [i for i in range(k) if after[i] != head[i]]
    if not diff:
        return []
    out = []
    sig = [i for i in diff if 440 <= i <= 445]
    gpt = [i for i in diff if i >= 512]
    other = [i for i in diff if i < 440 or 446 <= i < 512]
    if sig:
        out.append("Windows hat nach dem Einlesen eine Datenträger-Signatur in den MBR eingetragen (Bytes 440–443), "
                   "weil die im Abbild fehlte oder doppelt war. Bei Raspberry-Pi-Abbildern, die die Signatur als "
                   "PARTUUID nutzen, kann das den Start verhindern.")
    if gpt:
        out.append("Windows hat nach dem Einlesen die GPT-Verwaltungsdaten angepasst (Sicherungskopie ans Ende "
                   "des Sticks verlegt). Die Partitionen selbst sind unverändert.")
    if other:
        out.append(f"Windows hat Sektor 0 nach dem Schreiben verändert (Bytes {other[:8]} …)")
    return out


def _read_first_sector(number: int, sector: int, length: int = 512) -> bytes | None:
    length = max(512, min(length, 64 * 1024))
    buf = rawio.AlignedBuffer(64 * 1024)
    try:
        with rawio.Device(f"\\\\.\\PhysicalDrive{int(number)}", write=False) as dev:
            got = dev.read(buf, _round_up(length, max(512, sector)), 0)
            return buf.bytes(min(got, length))
    except OSError:
        return None
    finally:
        buf.free()


def read_hash(number: int, length: int, job: Job, tgt: str = "", sector: int = 512,
              mib: MiBHashes | None = None) -> str:
    """Die ersten ``length`` Bytes des Datenträgers lesen und SHA-256 bilden."""
    h = hashlib.sha256()
    meter = Meter(job, "verify", length, tgt or f"Datenträger {number}", "Prüfen")
    buf = rawio.AlignedBuffer(CHUNK)
    try:
        with rawio.Device(f"\\\\.\\PhysicalDrive{int(number)}", write=False) as dev:
            pos = 0
            while pos < length:
                job.check()
                want = min(CHUNK, _round_up(length - pos, sector))
                got = dev.read(buf, want, pos)
                if got <= 0:
                    raise OpError(f"Lesefehler bei Byte {pos}")
                take = min(got, length - pos)
                b = buf.bytes(take)
                h.update(b)
                if mib is not None:
                    mib.feed(b)
                pos += take
                meter.update(pos)
            meter.update(pos, force=True)
    finally:
        buf.free()
    return h.hexdigest()


# ---------------------------------------------------------------- Sichern

def used_size(d: disks.Disk) -> int:
    """Ende der letzten Partition (MBR) – bei GPT/RAW der ganze Datenträger."""
    if d.partition_style != "MBR":
        return d.size
    out = ps.as_list(ps.run_json(f"Get-Partition -DiskNumber {d.number} -ErrorAction SilentlyContinue | "
                                 "Select-Object Offset,Size | ConvertTo-Json -Compress"))
    ends = [int(p["Offset"]) + int(p["Size"]) for p in out]
    return min(d.size, _round_up(max(ends), 1024 * 1024)) if ends else d.size


class _Writer:
    """Zieldatei, optional gepackt (Endung bestimmt das Format)."""

    def __init__(self, path: str, final_name: str | None = None):
        self.path = path
        low = (final_name or path).lower()
        self.zip = None
        if low.endswith(".xz"):
            self.f = lzma.open(path, "wb", preset=1)
        elif low.endswith(".gz"):
            self.f = gzip.open(path, "wb", compresslevel=3)
        elif low.endswith(".zip"):
            self.zip = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True)
            inner = os.path.basename(final_name or path)[:-4]
            if not inner.lower().endswith((".img", ".iso")):
                inner += ".img"
            zi = zipfile.ZipInfo(inner)
            zi.compress_type = zipfile.ZIP_DEFLATED
            self.f = self.zip.open(zi, "w", force_zip64=True)
        else:
            self.f = open(path, "wb")

    def write(self, b: bytes):
        self.f.write(b)

    def close(self):
        self.f.close()
        if self.zip:
            self.zip.close()


def backup(number: int, out_path: str, job: Job, used_only: bool = False, verify: bool = True,
           expect_uid: str | None = None) -> dict:
    """Datenträger lesen und als .img (.img.xz/.img.gz/.zip) speichern. Ändert den Datenträger nicht."""
    snap = disks.query()
    d = snap.disk(int(number))
    if d is None:
        raise OpError(f"Datenträger {number} nicht gefunden")
    if expect_uid is not None and d.uid() != expect_uid:
        raise OpError(f"Datenträger {number} ist nicht mehr derselbe (umgesteckt?)")
    reasons = [r for r in d.blocks() if "Kein Wechseldatenträger" in r or "System" in r or "Boot" in r
               or "Windows" in r or "Auslagerung" in r]
    if reasons:
        raise OpError(f"Datenträger {number} darf nicht gesichert werden: " + "; ".join(reasons))
    if not ps.is_admin():
        raise OpError("Administratorrechte nötig")
    if guard.source_letters([out_path]) & set(d.letters):
        raise OpError("Die Sicherungsdatei darf nicht auf dem gesicherten Datenträger liegen")
    length = used_size(d) if used_only else d.size
    tgt = f"Datenträger {number}"
    h = hashlib.sha256()
    meter = Meter(job, "backup", length, tgt, "Sichern")
    tmp = out_path + ".part"
    w = _Writer(tmp, out_path)
    buf = rawio.AlignedBuffer(CHUNK)
    try:
        with rawio.Device(f"\\\\.\\PhysicalDrive{d.number}", write=False) as dev:
            pos = 0
            while pos < length:
                job.check()
                want = min(CHUNK, length - pos)
                got = dev.read(buf, want, pos)
                if got <= 0:
                    raise OpError(f"Lesefehler bei Byte {pos}")
                data = buf.bytes(min(got, want))
                h.update(data)
                w.write(data)
                pos += len(data)
                meter.update(pos)
        w.close()
        os.replace(tmp, out_path)
    except BaseException:
        try:
            w.close()
        except Exception:
            pass
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    finally:
        buf.free()
    digest = h.hexdigest()
    with open(out_path + ".sha256", "w", encoding="ascii") as f:
        f.write(f"{digest} *{os.path.basename(out_path)}  (SHA-256 der entpackten Rohdaten)\n")
    res = {"source": d.number, "model": d.model, "file": out_path, "bytes": length,
           "sha256_data": digest, "verified": False}
    if verify:
        hh = hashlib.sha256()
        m2 = Meter(job, "verify", length, tgt, "Sicherung prüfen")
        with open_source(out_path) as src:
            done = 0
            while True:
                job.check()
                b = src.read(CHUNK)
                if not b:
                    break
                hh.update(b)
                done += len(b)
                m2.update(done)
        res["sha256_file_data"] = hh.hexdigest()
        res["verified"] = hh.hexdigest() == digest
        if not res["verified"]:
            raise OpError("Sicherung fehlerhaft: Prüfsumme der Datei passt nicht zu den gelesenen Daten")
    message(job, f"{tgt}: Sicherung fertig", tgt, 100)
    return res


__all__ = ["write_image", "read_hash", "backup", "used_size", "Cancelled"]
