"""Persistenz-Partition für 1:1 geschriebene Linux-Live-Sticks.

Nach dem 1:1-Schreiben endet die ISO meist nach wenigen GB, der Rest des Sticks ist ungenutzt.
Dort wird eine Partition mit ext4 (ohne Journal) angelegt:
  * casper (Ubuntu, Mint, …):    Label ``casper-rw``
  * live-boot (Debian, Kali, …): Label ``persistence`` + Datei ``persistence.conf`` mit ``/ union``
Die Partitionstabelle der ISO wird dafür ergänzt (MBR: freier Eintrag; GPT: neuer Eintrag, Kopf und
Sicherungskopie am echten Ende des Sticks neu geschrieben, CRCs neu berechnet).

Hinweis: Beim Start muss meist der Boot-Parameter ``persistent`` (casper) bzw. ``persistence``
(live-boot) ergänzt werden – die ISO selbst ist schreibgeschützt und wird nicht verändert.
"""
from __future__ import annotations

import struct
import uuid
import zlib

from . import disks, extfs, flash, guard, rawio
from .jobs import Job, OpError, message

LABEL = {"casper": "casper-rw", "live-boot": "persistence"}
LINUX_FS_GUID = uuid.UUID("0FC63DAF-8483-4772-8E79-3D69D8477DE4")
ALIGN = 1024 * 1024


class SparseImage:
    """Sammelt Schreibzugriffe blockweise (4 KiB) im Speicher – für sektorgenaues Schreiben."""

    def __init__(self, sink=None):
        self.blocks: dict[int, bytearray] = {}
        self.pos = 0
        self.sink = sink            # sink(block_nr, data) – schreibt gesammelte Blöcke weg

    def flush_hint(self):
        """Von extfs nach jeder Gruppe aufgerufen: ab 32 MiB gesammelt wegschreiben.
        (Später nur teilweise beschriebene Blöcke sind vorher Nullen – das bleibt korrekt.)"""
        if self.sink and len(self.blocks) >= 8192:
            self.drain()

    def drain(self):
        for b in sorted(self.blocks):
            self.sink(b, bytes(self.blocks[b]))
        self.blocks.clear()

    def seek(self, p: int):
        self.pos = p

    def write(self, data: bytes):
        p, i = self.pos, 0
        while i < len(data):
            b, off = divmod(p, 4096)
            n = min(4096 - off, len(data) - i)
            blk = self.blocks.setdefault(b, bytearray(4096))
            blk[off:off + n] = data[i:i + n]
            p += n
            i += n
        self.pos = p


def _read(dev: rawio.Device, pos: int, n: int, sector: int) -> bytes:
    buf = rawio.AlignedBuffer(max(n, sector))
    try:
        got = dev.read(buf, (n + sector - 1) // sector * sector, pos)
        return buf.bytes(min(got, n))
    finally:
        buf.free()


def _write(dev: rawio.Device, pos: int, data: bytes, sector: int):
    n = (len(data) + sector - 1) // sector * sector
    buf = rawio.AlignedBuffer(n)
    try:
        buf.load(data, n)
        dev.write(buf, n, pos)
    finally:
        buf.free()


def _gpt_header_parse(h: bytes) -> dict:
    (sig, rev, hsize, crc, _r, my, alt, first, last, guid, ent_lba, n_ent, ent_size, ent_crc) = \
        struct.unpack_from("<8sIIIIQQQQ16sQIII", h, 0)
    return dict(sig=sig, rev=rev, hsize=hsize, my=my, alt=alt, first=first, last=last, guid=guid,
                ent_lba=ent_lba, n_ent=n_ent, ent_size=ent_size, ent_crc=ent_crc)


def _gpt_header_build(g: dict, my: int, alt: int, ent_lba: int, last_usable: int, ent_crc: int) -> bytes:
    body = struct.pack("<8sIIIIQQQQ16sQIII", b"EFI PART", g["rev"], 92, 0, 0, my, alt, g["first"],
                       last_usable, g["guid"], ent_lba, g["n_ent"], g["ent_size"], ent_crc)
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body[:16] + struct.pack("<I", crc) + body[20:]


def add_partition(number: int, live_system: str, confirm: str, job: Job, size_mb: int = 0,
                  expect_uid: str | None = None, image_end: int = 0) -> dict:
    label = LABEL.get(live_system)
    if not label:
        raise OpError("Persistenz nur für casper (Ubuntu, Mint …) und live-boot (Debian, Kali …)")
    d = guard.check(number, confirm, expect_uid)
    sector = max(512, d.sector_size)
    tgt = f"Datenträger {d.number}"
    locks = flash.lock_disk_volumes(d)
    try:
        with rawio.Device(f"\\\\.\\PhysicalDrive{d.number}", write=True) as dev:
            head = _read(dev, 0, 64 * 1024, sector)
            total_lba = d.size // sector
            if head[sector:sector + 8] == b"EFI PART":
                res = _add_gpt(dev, head, sector, total_lba, size_mb, label, image_end)
            else:
                res = _add_mbr(dev, head, sector, total_lba, size_mb, image_end)
            start, count = res["start_lba"], res["sectors"]
            message(job, f"{tgt}: ext4 „{label}“ ({disks.fmt_size(count * sector)}) wird angelegt …", tgt)
            def sink(b, data):
                job.check()
                _write(dev, start * sector + b * 4096, data, sector)
            img = SparseImage(sink)
            img.write(b"\0" * 8192)          # alte Signaturen am Partitionsanfang löschen
            files = {"persistence.conf": b"/ union\n"} if label == "persistence" else None
            info = extfs.make(img, count * sector, label, files)
            img.drain()
            dev.flush()
    finally:
        rawio.release(locks)
        flash.rescan(d.number)
    res.update({"target": d.number, "label": label, "filesystem": info,
                "boot_parameter": "persistent" if live_system == "casper" else "persistence"})
    return res


def _used_end_mbr(head: bytes) -> int:
    end = 0
    for i in range(4):
        e = head[446 + 16 * i: 462 + 16 * i]
        if e[4]:
            s, n = struct.unpack_from("<II", e, 8)
            end = max(end, s + n)
    return end


def _region(used_end_lba: int, image_end: int, sector: int, last_lba_excl: int, size_mb: int) -> tuple[int, int]:
    al = ALIGN // sector
    start = max(used_end_lba, (image_end + sector - 1) // sector)
    start = (start + al - 1) // al * al
    avail = last_lba_excl - start
    count = avail if not size_mb else min(avail, size_mb * 1024 * 1024 // sector)
    count = count // 8 * 8      # ganze 4-KiB-Blöcke
    if count * sector < 64 * 1024 * 1024:
        raise OpError("Zu wenig freier Platz hinter dem Abbild (mindestens 64 MB nötig)")
    return start, count


def _add_mbr(dev, head: bytes, sector: int, total_lba: int, size_mb: int, image_end: int) -> dict:
    slot = next((i for i in range(4) if head[446 + 16 * i + 4] == 0), None)
    if slot is None:
        raise OpError("Alle vier MBR-Einträge sind belegt")
    start, count = _region(_used_end_mbr(head), image_end, sector, min(total_lba, 0xFFFFFFFF), size_mb)
    entry = bytes([0x00, 0xFE, 0xFF, 0xFF, 0x83, 0xFE, 0xFF, 0xFF]) + struct.pack("<II", start, count)
    mbr = bytearray(head[:sector])
    mbr[446 + 16 * slot: 462 + 16 * slot] = entry
    _write(dev, 0, bytes(mbr), sector)
    return {"style": "MBR", "partition_index": slot + 1, "start_lba": start, "sectors": count}


def _add_gpt(dev, head: bytes, sector: int, total_lba: int, size_mb: int, label_name: str, image_end: int) -> dict:
    g = _gpt_header_parse(head[sector:sector + 92])
    ent_bytes = g["n_ent"] * g["ent_size"]
    ents = bytearray(_read(dev, g["ent_lba"] * sector, ent_bytes, sector))
    if (zlib.crc32(bytes(ents)) & 0xFFFFFFFF) != g["ent_crc"]:
        raise OpError("GPT-Einträge beschädigt (CRC stimmt nicht) – Abbruch")
    used_end = 0
    free = None
    for i in range(g["n_ent"]):
        e = ents[i * g["ent_size"]:(i + 1) * g["ent_size"]]
        if e[:16] == b"\0" * 16:
            if free is None:
                free = i
            continue
        first, last = struct.unpack_from("<QQ", e, 32)
        used_end = max(used_end, last + 1)
    if free is None:
        raise OpError("Keine freie GPT-Partitionsnummer")
    ent_sectors = (ent_bytes + sector - 1) // sector
    last_lba = total_lba - 1
    last_usable = last_lba - ent_sectors - 1
    start, count = _region(used_end, image_end, sector, last_usable + 1, size_mb)
    name = label_name.encode("utf-16-le")[:72].ljust(72, b"\0")
    e = LINUX_FS_GUID.bytes_le + uuid.uuid4().bytes_le + struct.pack("<QQQ", start, start + count - 1, 0) + name
    ents[free * g["ent_size"]:free * g["ent_size"] + len(e)] = e
    ent_crc = zlib.crc32(bytes(ents)) & 0xFFFFFFFF
    backup_ent_lba = last_lba - ent_sectors
    primary = _gpt_header_build(g, 1, last_lba, g["ent_lba"], last_usable, ent_crc)
    backup = _gpt_header_build(g, last_lba, 1, backup_ent_lba, last_usable, ent_crc)
    _write(dev, g["ent_lba"] * sector, bytes(ents), sector)
    _write(dev, backup_ent_lba * sector, bytes(ents), sector)
    _write(dev, last_lba * sector, backup, sector)
    _write(dev, sector, primary, sector)
    # Schutz-MBR (0xEE) auf die ganze Größe ausdehnen
    mbr = bytearray(head[:sector])
    for i in range(4):
        o = 446 + 16 * i
        if mbr[o + 4] == 0xEE:
            s = struct.unpack_from("<I", mbr, o + 8)[0]
            struct.pack_into("<I", mbr, o + 12, min(total_lba - s, 0xFFFFFFFF))
            _write(dev, 0, bytes(mbr), sector)
            break
    return {"style": "GPT", "partition_index": free + 1, "start_lba": start, "sectors": count}
