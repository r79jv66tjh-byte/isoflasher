"""Letzte Prüfung direkt vor jedem Schreibvorgang (GUI und CLI nutzen dieselbe Funktion).

Fragt die Laufwerke neu ab, prüft alle Sperren, die Bestätigung und ob es noch derselbe
Datenträger ist (Kennung/Seriennummer/Größe) – ein umgesteckter Stick wird so nicht überschrieben.
"""
from __future__ import annotations

import os

from . import disks, ps
from .jobs import OpError


class Blocked(OpError):
    pass


def source_letters(paths: list[str]) -> set[str]:
    out = set()
    for p in paths or []:
        d = os.path.splitdrive(os.path.abspath(p))[0]
        if len(d) == 2 and d[1] == ":":
            out.add(d[0].upper())
    return out


def problems(d: disks.Disk, sources: list[str] | None = None, need: int | None = None) -> list[str]:
    r = d.blocks()
    for L in sorted(source_letters(sources or []) & set(d.letters)):
        r.append(f"Quelldatei liegt auf diesem Datenträger ({L}:)")
    if need is not None and d.size and need > d.size:
        r.append(f"Zu klein: braucht {disks.fmt_size(need)}, hat {disks.fmt_size(d.size)}")
    return r


def check(number: int, confirm: str | None, expect_uid: str | None = None,
          sources: list[str] | None = None, need: int | None = None,
          require_admin: bool = True) -> disks.Disk:
    if require_admin and not ps.is_admin():
        raise Blocked("Administratorrechte nötig")
    snap = disks.query()
    d = snap.disk(int(number))
    if d is None:
        raise Blocked(f"Datenträger {number} nicht (mehr) vorhanden")
    if expect_uid is not None and d.uid() != expect_uid:
        raise Blocked(f"Datenträger {number} ist nicht mehr derselbe (umgesteckt?) – bitte neu auswählen")
    p = problems(d, sources, need)
    if p:
        raise Blocked(f"Datenträger {number} gesperrt: " + "; ".join(p))
    if not disks.confirm_ok(d, confirm):
        raise Blocked(f"Bestätigung '{confirm or ''}' passt nicht zu Datenträger {number} "
                      f"(erwartet: {disks.confirm_hint(d)})")
    return d
