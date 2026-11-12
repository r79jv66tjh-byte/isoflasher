"""Leeres ext4-Dateisystem ohne Journal erzeugen (für Linux-Persistenz) – ohne Linux-Werkzeuge.

Windows kann kein ext2/3/4 formatieren. Linux (casper, live-boot, Ventoy) hängt es über den ext4-Treiber
ein (Features: filetype, extents, sparse_super, large_file). Erzeugt wird: Superblock (+ Sicherungen nach „sparse_super“),
Gruppendeskriptoren, Bitmaps, Inode-Tabellen, Wurzelordner, lost+found und optional kleine Dateien
im Wurzelordner (z. B. persistence.conf für Debian).

Es werden nur die Metadaten-Blöcke geschrieben; der Rest muss Nullen enthalten (frisch angelegte
Datei bzw. vorher genullter Bereich) – Inode-Tabellen werden aber vollständig genullt geschrieben.
"""
from __future__ import annotations

import os
import struct
import time
import uuid

BS = 4096                  # Blockgröße
BPG = BS * 8               # Blöcke pro Gruppe (eine Bitmap-Seite)
INODE_SIZE = 128
GDESC = 32


def _has_super(g: int) -> bool:
    if g in (0, 1):
        return True
    for p in (3, 5, 7):
        x = g
        while x % p == 0:
            x //= p
        if x == 1:
            return True
    return False


class Layout:
    def __init__(self, size: int, inodes_per_group: int | None = None):
        blocks = size // BS
        if blocks < 64:
            raise ValueError("ext2: zu klein (mindestens 256 KB)")
        self.groups = (blocks + BPG - 1) // BPG
        self.ipg = inodes_per_group or min(8192, max(128, (BPG // 4)))
        # Inodes pro Gruppe auf ganze Inode-Tabellen-Blöcke und Bitmap begrenzen
        self.ipg = min(self.ipg, BS * 8) // (BS // INODE_SIZE) * (BS // INODE_SIZE)
        self.itb = self.ipg * INODE_SIZE // BS
        self.gdt_blocks = (self.groups * GDESC + BS - 1) // BS
        last = blocks - (self.groups - 1) * BPG
        if self.groups > 1 and last < self.meta(self.groups - 1) + 16:
            self.groups -= 1          # letzte, zu kleine Gruppe weglassen
            blocks = self.groups * BPG
            self.gdt_blocks = (self.groups * GDESC + BS - 1) // BS
        self.blocks = blocks

    def meta(self, g: int) -> int:
        return (1 + self.gdt_blocks if _has_super(g) else 0) + 2 + self.itb

    def start(self, g: int) -> int:
        return g * BPG

    def gsize(self, g: int) -> int:
        return min(BPG, self.blocks - g * BPG)

    def bitmaps(self, g: int) -> tuple[int, int, int]:
        b = self.start(g) + (1 + self.gdt_blocks if _has_super(g) else 0)
        return b, b + 1, b + 2           # Block-Bitmap, Inode-Bitmap, Inode-Tabelle


EXTENTS_FL = 0x80000


def _inode(mode: int, size: int, links: int, blocks: list[int], now: int) -> bytes:
    """Inode mit Extent-Baum (Tiefe 0, ein Extent – die Blöcke liegen zusammenhängend)."""
    assert blocks == list(range(blocks[0], blocks[0] + len(blocks)))
    ext = struct.pack("<HHHHI", 0xF30A, 1, 4, 0, 0)
    ext += struct.pack("<IHHI", 0, len(blocks), blocks[0] >> 32, blocks[0] & 0xFFFFFFFF)
    iblock = ext.ljust(60, b"\0")
    return struct.pack("<HHIIIIIHHII4s60sIIII12s", mode, 0, size, now, now, now, 0, 0, links,
                       len(blocks) * (BS // 512), EXTENTS_FL, b"\0" * 4, iblock, 0, 0, 0, 0, b"\0" * 12)


def _dirblock(entries: list[tuple[int, str, int]]) -> bytes:
    out = b""
    for i, (ino, name, ftype) in enumerate(entries):
        nb = name.encode("utf-8")
        rec = (8 + len(nb) + 3) // 4 * 4
        if i == len(entries) - 1:
            rec = BS - len(out)
        out += struct.pack("<IHBB", ino, rec, len(nb), ftype) + nb + b"\0" * (rec - 8 - len(nb))
    return out


def make(f, size: int, label: str = "", files: dict[str, bytes] | None = None, offset: int = 0,
         progress=None) -> dict:
    """ext2 in die offene Datei/den Datenträger ``f`` ab ``offset`` schreiben (``f`` hat seek/write)."""
    files = files or {}
    L = Layout(size)
    now = int(time.time())
    uid = uuid.uuid4().bytes
    first_ino = 11
    # Inodes: 2 = Wurzel, 11 = lost+found, 12.. = Dateien
    file_inos = {name: 12 + i for i, name in enumerate(files)}
    used_inodes = 11 + len(files)
    if used_inodes > L.ipg:
        raise ValueError("ext2: zu viele Dateien")

    def wr(block: int, data: bytes):
        f.seek(offset + block * BS)
        f.write(data)

    # Datenblöcke in Gruppe 0 hinter den Metadaten
    _, _, it0 = L.bitmaps(0)
    data0 = it0 + L.itb
    root_blk, lf_blk = data0, data0 + 1
    file_blks: dict[str, list[int]] = {}
    nxt = data0 + 2
    for name, content in files.items():
        n = max(1, (len(content) + BS - 1) // BS)
        if n > 12:
            raise ValueError("ext2: Datei zu groß (max. 48 KB)")
        file_blks[name] = list(range(nxt, nxt + n))
        nxt += n
    used_data0 = nxt - data0

    # Gruppendeskriptoren + Bitmaps
    gdt = b""
    total_free_blocks = 0
    for g in range(L.groups):
        bb, ib, it = L.bitmaps(g)
        used = L.meta(g) + (used_data0 if g == 0 else 0)
        gs = L.gsize(g)
        free_b = gs - used
        free_i = L.ipg - (used_inodes if g == 0 else 0)
        total_free_blocks += free_b
        gdt += struct.pack("<IIIHHH14s", bb, ib, it, free_b, free_i, 2 if g == 0 else 0, b"\0" * 14)
        bm = bytearray(BS)
        for i in range(used):
            bm[i >> 3] |= 1 << (i & 7)
        for i in range(gs, BPG):
            bm[i >> 3] |= 1 << (i & 7)
        wr(bb, bytes(bm))
        im = bytearray(BS)
        for i in range(used_inodes if g == 0 else 0):
            im[i >> 3] |= 1 << (i & 7)
        for i in range(L.ipg, BS * 8):
            im[i >> 3] |= 1 << (i & 7)
        wr(ib, bytes(im))
        zero = b"\0" * (BS * min(L.itb, 64))
        for k in range(0, L.itb, 64):
            wr(it + k, zero[:BS * min(64, L.itb - k)])
        if hasattr(f, "flush_hint"):
            f.flush_hint()
        if progress:
            progress(g + 1, L.groups)
    gdt = gdt.ljust(L.gdt_blocks * BS, b"\0")

    free_inodes = L.ipg * L.groups - used_inodes
    for g in range(L.groups):
        if not _has_super(g):
            continue
        sb = struct.pack("<IIIIIIIIIIIIIHhHHHHIIIIHHIHHIII16s16s64sI",
                         L.ipg * L.groups, L.blocks, 0, total_free_blocks, free_inodes, 0, 2, 2,
                         BPG, BPG, L.ipg, 0, now, 0, -1, 0xEF53, 1, 1, 0, now, 0, 0, 1, 0, 0,
                         first_ino, INODE_SIZE, g, 0, 0x0002 | 0x0040, 0x0001 | 0x0002, uid,
                         label.encode("utf-8")[:16].ljust(16, b"\0"), b"\0" * 64, 0)
        sb = sb.ljust(1024, b"\0")
        blk = L.start(g)
        if g == 0:
            wr(0, b"\0" * 1024 + sb + b"\0" * (BS - 2048))
        else:
            wr(blk, sb.ljust(BS, b"\0"))
        wr(blk + 1, gdt)

    # Inodes schreiben (Gruppe 0)
    def put_inode(n: int, data: bytes):
        f.seek(offset + it0 * BS + (n - 1) * INODE_SIZE)
        f.write(data.ljust(INODE_SIZE, b"\0"))

    put_inode(2, _inode(0o40755, BS, 3, [root_blk], now))
    put_inode(11, _inode(0o40700, BS, 2, [lf_blk], now))
    entries = [(2, ".", 2), (2, "..", 2), (11, "lost+found", 2)]
    for name, content in files.items():
        put_inode(file_inos[name], _inode(0o100644, len(content), 1, file_blks[name], now))
        entries.append((file_inos[name], name, 1))
        for i, b in enumerate(file_blks[name]):
            wr(b, content[i * BS:(i + 1) * BS].ljust(BS, b"\0"))
    wr(root_blk, _dirblock(entries))
    wr(lf_blk, _dirblock([(11, ".", 2), (2, "..", 2)]))
    return {"blocks": L.blocks, "groups": L.groups, "inodes": L.ipg * L.groups, "label": label,
            "size": L.blocks * BS}


def make_file(path: str, size: int, label: str, files: dict[str, bytes] | None = None) -> dict:
    """Persistenz-Datei anlegen (Größe sofort reservieren, Inhalt = ext2)."""
    with open(path, "wb") as f:
        f.truncate(size)
        info = make(f, size, label, files)
        f.flush()
        os.fsync(f.fileno())
    return info
