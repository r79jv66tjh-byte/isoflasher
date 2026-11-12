"""SHA-256 von Dateien bilden und mit eingegebenen bzw. offiziellen Werten vergleichen."""
from __future__ import annotations

import hashlib
import os
import re

from .jobs import Job, Meter

_HEX = re.compile(r"\b([0-9a-fA-F]{64})\b")


def sha256_file(path: str, job: Job | None = None, label: str = "SHA-256") -> str:
    job = job or Job()
    h = hashlib.sha256()
    total = os.path.getsize(path)
    meter = Meter(job, "hash", total, os.path.basename(path), label)
    done = 0
    with open(path, "rb") as f:
        while True:
            job.check()
            b = f.read(4 * 1024 * 1024)
            if not b:
                break
            h.update(b)
            done += len(b)
            meter.update(done)
    meter.update(done, force=True)
    return h.hexdigest()


def parse_expected(text: str, filename: str | None = None) -> str | None:
    """Erwarteten Wert aus Eingabe oder einer SHA256SUMS-Datei holen.

    Akzeptiert einen reinen Hex-Wert oder Zeilen wie ``<hash> *datei.iso`` / ``SHA256 (datei) = <hash>``.
    Bei mehreren Zeilen wird die Zeile zum Dateinamen gewählt."""
    if not text:
        return None
    text = text.strip()
    if os.path.isfile(text):
        with open(text, encoding="utf-8", errors="replace") as f:
            text = f.read()
    lines = [ln for ln in text.splitlines() if _HEX.search(ln)]
    if not lines:
        return None
    if len(lines) > 1 and filename:
        base = os.path.basename(filename).lower()
        hit = [ln for ln in lines if base in ln.lower()]
        if hit:
            lines = hit
        else:
            return None
    return _HEX.search(lines[0]).group(1).lower()


def compare(path: str, expected: str | None, job: Job | None = None) -> dict:
    actual = sha256_file(path, job)
    exp = parse_expected(expected or "", path) if expected else None
    res = {"file": path, "size": os.path.getsize(path), "sha256": actual}
    if expected is not None:
        res["expected"] = exp
        res["match"] = (exp == actual) if exp else None
        if exp is None:
            res["note"] = "Kein gültiger SHA-256-Wert (64 Hex-Zeichen) für diese Datei gefunden"
    return res
