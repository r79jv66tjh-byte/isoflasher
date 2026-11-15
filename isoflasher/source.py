"""Abbild-Quelle lesen: .iso/.img direkt oder gepackt (.xz, .gz, .bz2, .zip) als Datenstrom."""
from __future__ import annotations

import bz2
import contextlib
import gzip
import lzma
import os
import struct
import zipfile

from .detect import compressed_kind, zip_member


class Source:
    def __init__(self, path: str):
        self.path = path
        self.kind = compressed_kind(path)
        self.file_size = os.path.getsize(path)
        self.raw = open(path, "rb")
        self.total: int | None = None       # entpackte Größe, falls bekannt
        self._zip = None
        if self.kind == "":
            self.stream = self.raw
            self.total = self.file_size
        elif self.kind == "xz":
            self.total = _xz_size(self.raw)
            self.raw.seek(0)
            self.stream = lzma.LZMAFile(self.raw)
        elif self.kind == "gz":
            self.stream = gzip.GzipFile(fileobj=self.raw)
        elif self.kind == "bz2":
            self.stream = bz2.BZ2File(self.raw)
        elif self.kind == "zip":
            self._zip = zipfile.ZipFile(self.raw)
            m = zip_member(path)
            self.total = m.file_size
            self.stream = self._zip.open(m)
        else:
            raise ValueError(f"Format nicht unterstützt: {path}")

    def read(self, n: int) -> bytes:
        return self.stream.read(n)

    def read_full(self, n: int) -> bytes:
        parts, got = [], 0
        while got < n:
            b = self.stream.read(n - got)
            if not b:
                break
            parts.append(b)
            got += len(b)
        return b"".join(parts)

    def consumed(self) -> int:
        """Gelesene Bytes der (gepackten) Datei – für den Fortschritt bei unbekannter Größe."""
        try:
            return self.raw.tell()
        except (OSError, ValueError):
            return 0

    def close(self):
        # ExitStack closes all three even if one fails, then re-raises that failure.
        with contextlib.ExitStack() as stack:
            for x in (self.raw, self._zip, self.stream):
                if x is not None:
                    stack.callback(x.close)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_source(path: str) -> Source:
    return Source(path)


def _xz_size(f) -> int | None:
    """Entpackte Größe aus dem xz-Index lesen (nur einfache, einteilige Dateien)."""
    try:
        f.seek(-12, os.SEEK_END)
        footer = f.read(12)
        if footer[10:12] != b"YZ":
            return None
        backward = (struct.unpack("<I", footer[4:8])[0] + 1) * 4
        f.seek(-12 - backward, os.SEEK_END)
        idx = f.read(backward)
        if idx[0] != 0:
            return None
        pos = 1

        def vli():
            nonlocal pos
            val, shift = 0, 0
            while True:
                b = idx[pos]
                pos += 1
                val |= (b & 0x7F) << shift
                if not b & 0x80:
                    return val
                shift += 7
        n = vli()
        total = 0
        for _ in range(n):
            vli()
            total += vli()
        # Mehrere Streams hintereinander werden hier nicht unterstützt -> nur, wenn es passt
        return total
    except (OSError, IndexError, struct.error):
        return None
