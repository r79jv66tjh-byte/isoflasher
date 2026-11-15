"""Gemeinsame Bausteine für lange Vorgänge: Abbrechen, Fortschritt mit Geschwindigkeit und Restzeit."""
from __future__ import annotations

import threading
import time
from typing import Callable


class OpError(RuntimeError):
    pass


class Cancelled(OpError):
    def __init__(self, msg: str = "Abgebrochen"):
        super().__init__(msg)


class Job:
    """Wird an jeden Vorgang übergeben. ``progress(dict)`` bekommt Fortschrittsmeldungen."""

    def __init__(self, progress: Callable[[dict], None] | None = None):
        self._cancel = threading.Event()
        self.progress = progress or (lambda d: None)
        self.children: list = []       # z. B. laufende Prozesse, die bei Abbruch beendet werden

    def cancel(self):
        self._cancel.set()
        for c in list(self.children):
            c.kill()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self):
        if self._cancel.is_set():
            raise Cancelled()


def fmt_duration(s: float | None) -> str:
    if s is None or s < 0:
        return "–"
    s = int(s + 0.5)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def fmt_speed(bps: float | None) -> str:
    if not bps:
        return "–"
    return f"{bps / 1e6:.1f} MB/s".replace(".", ",")


class Meter:
    """Berechnet Prozent, Geschwindigkeit (gleitend) und Restzeit und meldet höchstens ~5x/s."""

    def __init__(self, job: Job, phase: str, total: int | None, target: str = "", step: str = ""):
        self.job, self.phase, self.total, self.target, self.step = job, phase, total, target, step
        self.t0 = time.monotonic()
        self.last_emit = 0.0
        self.samples: list[tuple[float, int]] = [(self.t0, 0)]
        self.done = 0

    def update(self, done: int, force: bool = False, total: int | None = None):
        if total is not None:
            self.total = total
        self.done = done
        now = time.monotonic()
        if not force and now - self.last_emit < 0.2:
            return
        self.last_emit = now
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[0][0] > 5:
            self.samples.pop(0)
        t_a, d_a = self.samples[0]
        speed = (done - d_a) / (now - t_a) if now > t_a else None
        pct = (100.0 * done / self.total) if self.total else None
        eta = ((self.total - done) / speed) if (self.total and speed) else None
        self.job.progress({"phase": self.phase, "step": self.step, "target": self.target,
                           "done": done, "total": self.total, "percent": pct, "speed": speed,
                           "eta": eta, "elapsed": now - self.t0,
                           "text": self.text(pct, speed, eta)})

    def text(self, pct, speed, eta) -> str:
        p = f"{pct:5.1f} %" if pct is not None else ""
        return f"{self.step} {p}  {fmt_speed(speed)}  Rest {fmt_duration(eta)}".strip()

    def finish(self):
        self.update(self.done, force=True)


def message(job: Job, text: str, target: str = "", percent: float | None = None):
    job.progress({"phase": "info", "target": target, "text": text, "percent": percent})
