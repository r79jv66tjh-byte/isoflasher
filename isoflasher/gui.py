"""tkinter-Oberfläche von IsoFlasher (deutsch)."""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import checksum, detect, disks, flash, guard, helpers, isodl, persist, ps, ventoy, windows
from .disks import fmt_size
from .jobs import Cancelled, Job, fmt_duration, fmt_speed

IMAGE_TYPES = [("Abbilder", "*.iso *.img *.img.xz *.xz *.img.gz *.gz *.zip *.bz2 *.raw *.bin"), ("Alle Dateien", "*.*")]


class ConfirmDialog(tk.Toplevel):
    """Zusammenfassung + pro Ziel die Datenträgernummer eintippen. ``result`` = Liste der Eingaben oder None."""

    def __init__(self, master, title: str, targets: list[disks.Disk], lines: list[str], warnings=()):
        super().__init__(master)
        self.title("Bitte bestätigen")
        self.transient(master)
        self.resizable(False, False)
        self.result: list[str] | None = None
        self.targets = targets
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=title, font=("Segoe UI", 11, "bold")).pack(anchor="w")
        txt = tk.Text(frm, width=90, height=min(18, len(lines) + 1), font=("Consolas", 10), relief="solid",
                      borderwidth=1, wrap="word")
        txt.insert("end", "\n".join(lines))
        txt.configure(state="disabled")
        txt.pack(fill="x", pady=(6, 8))
        for w in warnings:
            ttk.Label(frm, text="⚠ " + w, foreground="#b00020", wraplength=640).pack(anchor="w")
        ttk.Label(frm, text="ALLE DATEN auf diesen Datenträgern gehen verloren. Zum Bestätigen jeweils die "
                            "Datenträgernummer (oder den Laufwerksbuchstaben) eintippen:",
                  wraplength=640).pack(anchor="w", pady=(10, 4))
        self.vars = []
        for d in targets:
            row = ttk.Frame(frm)
            row.pack(fill="x", pady=2)
            v = tk.StringVar()
            e = ttk.Entry(row, textvariable=v, width=8, font=("Consolas", 12))
            e.pack(side="left")
            ttk.Label(row, text=f"  ← {disks.confirm_hint(d)}   ({d.model}, {fmt_size(d.size)}, "
                                f"Seriennr. {d.serial or '?'})").pack(side="left")
            v.trace_add("write", self._check)
            self.vars.append(v)
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(12, 0))
        self.ok = ttk.Button(btns, text="Jetzt ausführen", state="disabled", command=self._ok)
        self.ok.pack(side="right")
        ttk.Button(btns, text="Abbrechen", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _valid(self) -> bool:
        return all(disks.confirm_ok(d, v.get()) for d, v in zip(self.targets, self.vars))

    def _check(self, *_):
        self.ok.configure(state="normal" if self._valid() else "disabled")

    def _ok(self):
        if self._valid():
            self.result = [v.get() for v in self.vars]
            self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("IsoFlasher – bootfähige USB-Sticks erstellen")
        self.geometry("1150x820")
        self.minsize(950, 680)
        self.admin = ps.is_admin()
        self.snap: disks.Snapshot | None = None
        self.q: queue.Queue = queue.Queue()
        self.job: Job | None = None
        self.busy = False
        self.quiet = False                 # Tests: keine Meldungsfenster
        self.last_result = None
        self.last_error: str | None = None
        self.info: detect.ImageInfo | None = None
        self.dl_info: dict | None = None
        self._build()
        self.after(100, self._poll)
        self.refresh()

    # ============================================================ Aufbau
    def _build(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        top = ttk.Frame(self, padding=(10, 8, 10, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Ziel-Datenträger (nur USB-Sticks/SD-Karten; mit Strg mehrere wählen):",
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(top, text="Aktualisieren", command=self.refresh).pack(side="right")
        self.lbl_admin = ttk.Label(top, text="" if self.admin else "⚠ ohne Adminrechte – nur Ansicht",
                                   foreground="#b00020")
        self.lbl_admin.pack(side="right", padx=10)

        cols = ("model", "size", "bus", "letters", "state")
        self.tree = ttk.Treeview(self, columns=cols, height=6, selectmode="extended")
        for c, t, w in (("#0", "Datenträger", 110), ("model", "Modell", 260), ("size", "Größe", 90),
                        ("bus", "Bus", 90), ("letters", "Laufwerke", 160), ("state", "Status", 380)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("locked", foreground="#888888")
        self.tree.tag_configure("ventoy", foreground="#00639c")
        self.tree.pack(fill="x", padx=10, pady=6)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=10)
        self._tab_create()
        self._tab_ventoy()
        self._tab_backup()
        self._tab_hash()
        self._tab_download()

        bottom = ttk.Frame(self, padding=(10, 4, 10, 8))
        bottom.pack(fill="x")
        self.ptree = ttk.Treeview(bottom, columns=("step", "pct", "speed", "eta"), height=3, show="headings")
        for c, t, w in (("step", "Schritt", 420), ("pct", "%", 70), ("speed", "Geschwindigkeit", 130),
                        ("eta", "Restzeit", 120)):
            self.ptree.heading(c, text=t)
            self.ptree.column(c, width=w, anchor="w")
        self.ptree.pack(fill="x")
        row = ttk.Frame(bottom)
        row.pack(fill="x", pady=(4, 0))
        self.pbar = ttk.Progressbar(row, maximum=100)
        self.pbar.pack(side="left", fill="x", expand=True)
        self.btn_cancel = ttk.Button(row, text="Abbrechen", state="disabled", command=self.cancel)
        self.btn_cancel.pack(side="left", padx=(8, 0))
        self.log = tk.Text(bottom, height=6, font=("Consolas", 9), state="disabled", wrap="word")
        self.log.pack(fill="x", pady=(4, 0))

    def _tab_create(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Stick erstellen")
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Abbild (ISO/IMG):").pack(side="left")
        self.var_image = tk.StringVar()
        ttk.Entry(row, textvariable=self.var_image).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Durchsuchen …", command=self._pick_image).pack(side="left")
        ttk.Button(row, text="Erkennen", command=self.analyze_image).pack(side="left", padx=(6, 0))
        self.lbl_info = ttk.Label(f, text="Noch kein Abbild gewählt.", wraplength=1050, justify="left")
        self.lbl_info.pack(anchor="w", pady=8)

        mrow = ttk.Frame(f)
        mrow.pack(fill="x")
        ttk.Label(mrow, text="Weg:").pack(side="left")
        self.var_mode = tk.StringVar(value="auto")
        for v, t in (("auto", "Automatisch (empfohlen)"), ("dd", "1:1 schreiben (DD)"), ("windows", "Windows-Stick")):
            ttk.Radiobutton(mrow, text=t, value=v, variable=self.var_mode).pack(side="left", padx=6)
        self.var_verify = tk.BooleanVar(value=True)
        ttk.Checkbutton(mrow, text="Nach dem Schreiben zurücklesen und prüfen", variable=self.var_verify).pack(side="left", padx=16)

        wf = ttk.LabelFrame(f, text="Windows-Optionen", padding=8)
        wf.pack(fill="x", pady=8)
        r1 = ttk.Frame(wf)
        r1.pack(fill="x")
        self.var_style = tk.StringVar(value="GPT")
        ttk.Radiobutton(r1, text="GPT – UEFI (neue PCs)", value="GPT", variable=self.var_style).pack(side="left")
        ttk.Radiobutton(r1, text="MBR – BIOS + UEFI (alte PCs)", value="MBR", variable=self.var_style).pack(side="left", padx=12)
        ttk.Label(r1, text="Name:").pack(side="left", padx=(20, 4))
        self.var_label = tk.StringVar()
        ttk.Entry(r1, textvariable=self.var_label, width=14).pack(side="left")
        r2 = ttk.Frame(wf)
        r2.pack(fill="x", pady=(6, 0))
        self.var_hw = tk.BooleanVar()
        self.var_online = tk.BooleanVar()
        self.var_privacy = tk.BooleanVar()
        self.var_bitlocker = tk.BooleanVar()
        for var, t in ((self.var_hw, "TPM/Secure Boot/RAM-Prüfung aus"), (self.var_online, "Online-Konto-Zwang aus"),
                       (self.var_privacy, "Datenschutz-Fragen aus"), (self.var_bitlocker, "BitLocker-Autoverschlüsselung aus")):
            ttk.Checkbutton(r2, text=t, variable=var).pack(side="left", padx=(0, 12))
        r3 = ttk.Frame(wf)
        r3.pack(fill="x", pady=(6, 0))
        ttk.Label(r3, text="Lokales Konto anlegen (optional, Benutzername):").pack(side="left")
        self.var_user = tk.StringVar()
        ttk.Entry(r3, textvariable=self.var_user, width=20).pack(side="left", padx=6)
        ttk.Label(r3, text="(Kennwort leer – beim ersten Anmelden setzen)", foreground="#666").pack(side="left")

        pf = ttk.LabelFrame(f, text="Persistenz für Linux-Live-Sticks (nur 1:1-Weg)", padding=8)
        pf.pack(fill="x")
        self.var_persist = tk.BooleanVar()
        ttk.Checkbutton(pf, text="Persistenz-Partition im freien Platz anlegen, Größe (MB, 0 = Rest):",
                        variable=self.var_persist).pack(side="left")
        self.var_persist_mb = tk.StringVar(value="0")
        ttk.Entry(pf, textvariable=self.var_persist_mb, width=8).pack(side="left", padx=6)
        ttk.Label(pf, text="Beim Start Boot-Parameter „persistent“ (Ubuntu/Mint) bzw. „persistence“ (Debian) ergänzen.",
                  foreground="#666").pack(side="left", padx=8)

        self.btn_start = ttk.Button(f, text="Start", command=self.start_create)
        self.btn_start.pack(anchor="e", pady=10)

    def _tab_ventoy(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Multiboot (Ventoy)")
        ttk.Label(f, text="Ventoy (GPLv3, ventoy.net) bootet beliebig viele ISOs von einem Stick. Einmal einrichten, "
                          "danach ISOs einfach hinzufügen oder löschen.", wraplength=1050).pack(anchor="w")
        r = ttk.Frame(f)
        r.pack(fill="x", pady=8)
        self.var_v_gpt = tk.BooleanVar(value=True)
        self.var_v_sb = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="GPT", variable=self.var_v_gpt).pack(side="left")
        ttk.Checkbutton(r, text="Secure-Boot-Unterstützung", variable=self.var_v_sb).pack(side="left", padx=10)
        ttk.Label(r, text="Dateisystem:").pack(side="left")
        self.var_v_fs = tk.StringVar(value="exFAT")
        ttk.Combobox(r, textvariable=self.var_v_fs, values=["exFAT", "NTFS", "FAT32"], width=8, state="readonly").pack(side="left", padx=4)
        ttk.Button(r, text="Ventoy auf Stick installieren (löscht alles)", command=self.start_ventoy_install).pack(side="left", padx=16)
        self.vtree = ttk.Treeview(f, columns=("size", "pers"), height=8)
        self.vtree.heading("#0", text="ISO auf dem Stick")
        self.vtree.heading("size", text="Größe")
        self.vtree.heading("pers", text="Persistenz")
        self.vtree.column("#0", width=520)
        self.vtree.pack(fill="both", expand=True)
        r2 = ttk.Frame(f)
        r2.pack(fill="x", pady=8)
        ttk.Button(r2, text="ISOs hinzufügen …", command=self.start_ventoy_add).pack(side="left")
        ttk.Button(r2, text="Ausgewählte löschen", command=self.start_ventoy_remove).pack(side="left", padx=6)
        ttk.Label(r2, text="Persistenz (MB):").pack(side="left", padx=(20, 4))
        self.var_v_pmb = tk.StringVar(value="4096")
        ttk.Entry(r2, textvariable=self.var_v_pmb, width=8).pack(side="left")
        ttk.Button(r2, text="Persistenz-Datei für Auswahl anlegen", command=self.start_ventoy_persist).pack(side="left", padx=6)
        ttk.Button(r2, text="Liste aktualisieren", command=self.refresh_ventoy).pack(side="right")

    def _tab_backup(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Abbild sichern / zurückschreiben")
        ttk.Label(f, text="Den oben gewählten Stick bzw. die SD-Karte als Abbild sichern (ändert den Datenträger nicht). "
                          "Endung bestimmt das Format: .img (roh), .img.xz, .img.gz oder .zip (gepackt).",
                  wraplength=1050).pack(anchor="w")
        r = ttk.Frame(f)
        r.pack(fill="x", pady=8)
        ttk.Label(r, text="Datei:").pack(side="left")
        self.var_bk = tk.StringVar()
        ttk.Entry(r, textvariable=self.var_bk).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r, text="Speichern unter …", command=self._pick_backup).pack(side="left")
        self.var_bk_used = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Nur belegten Bereich sichern (bis Ende der letzten Partition, nur MBR)",
                        variable=self.var_bk_used).pack(anchor="w")
        ttk.Button(f, text="Sichern", command=self.start_backup).pack(anchor="w", pady=8)
        ttk.Separator(f).pack(fill="x", pady=8)
        ttk.Label(f, text="Zurückschreiben: Abbild (.img, .img.xz, .zip …) im Reiter „Stick erstellen“ wählen – "
                          "es wird 1:1 geschrieben und geprüft.").pack(anchor="w")
        ttk.Button(f, text="Abbild zum Zurückschreiben wählen …", command=self._restore_pick).pack(anchor="w", pady=6)

    def _tab_hash(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Prüfsumme")
        r = ttk.Frame(f)
        r.pack(fill="x")
        ttk.Label(r, text="Datei:").pack(side="left")
        self.var_hf = tk.StringVar()
        ttk.Entry(r, textvariable=self.var_hf).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r, text="Durchsuchen …", command=lambda: self._pick_into(self.var_hf)).pack(side="left")
        r2 = ttk.Frame(f)
        r2.pack(fill="x", pady=8)
        ttk.Label(r2, text="Erwarteter SHA-256 (oder Inhalt/Datei SHA256SUMS):").pack(side="left")
        self.var_hexp = tk.StringVar()
        ttk.Entry(r2, textvariable=self.var_hexp).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r2, text="SUMS-Datei …", command=lambda: self._pick_into(self.var_hexp, [("Prüfsummen", "*.txt *SUMS* *.sha256"), ("Alle", "*.*")])).pack(side="left")
        ttk.Button(f, text="Prüfsumme berechnen und vergleichen", command=self.start_hash).pack(anchor="w")
        self.lbl_hash = ttk.Label(f, text="", font=("Consolas", 10), wraplength=1050)
        self.lbl_hash.pack(anchor="w", pady=10)

    def _tab_download(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Herunterladen")
        ttk.Label(f, text="Offizielle ISOs: Link und SHA-256 kommen von der Seite des Herstellers; nach dem Laden wird "
                          "verglichen. Windows über Fido (GPLv3) – die Prüfsumme steht auf microsoft.com.",
                  wraplength=1050).pack(anchor="w")
        r = ttk.Frame(f)
        r.pack(fill="x", pady=8)
        self.var_dl = tk.StringVar(value=isodl.NAMES["ubuntu"])
        ttk.Combobox(r, textvariable=self.var_dl, values=list(isodl.NAMES.values()), state="readonly", width=24).pack(side="left")
        ttk.Label(r, text="Windows:").pack(side="left", padx=(16, 4))
        self.var_wv = tk.StringVar(value="11")
        ttk.Combobox(r, textvariable=self.var_wv, values=["11", "10"], width=4, state="readonly").pack(side="left")
        self.var_wl = tk.StringVar(value="German")
        ttk.Combobox(r, textvariable=self.var_wl, values=["German", "English International", "English", "French", "Italian", "Spanish"], width=18).pack(side="left", padx=4)
        ttk.Button(r, text="Infos holen", command=self.start_dl_resolve).pack(side="left", padx=10)
        r2 = ttk.Frame(f)
        r2.pack(fill="x")
        ttk.Label(r2, text="Ordner:").pack(side="left")
        self.var_dlf = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))
        ttk.Entry(r2, textvariable=self.var_dlf).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r2, text="…", command=lambda: self.var_dlf.set(filedialog.askdirectory() or self.var_dlf.get())).pack(side="left")
        r3 = ttk.Frame(f)
        r3.pack(fill="x", pady=6)
        ttk.Label(r3, text="Eigener SHA-256 (optional, z. B. von microsoft.com):").pack(side="left")
        self.var_dlexp = tk.StringVar()
        ttk.Entry(r3, textvariable=self.var_dlexp).pack(side="left", fill="x", expand=True, padx=6)
        self.lbl_dl = ttk.Label(f, text="", wraplength=1050, justify="left")
        self.lbl_dl.pack(anchor="w", pady=6)
        ttk.Button(f, text="Herunterladen", command=self.start_download).pack(anchor="w")

    # ============================================================ Laufwerke
    def refresh(self):
        def work():
            try:
                self.q.put(("snap", disks.query()))
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", f"Laufwerke lesen fehlgeschlagen: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _show_snap(self, snap):
        sel = set(self.tree.selection())
        self.snap = snap
        self.tree.delete(*self.tree.get_children())
        for d in snap.disks:
            b = d.blocks()
            v = ventoy.is_ventoy(d)
            state = ("gesperrt: " + b[0]) if b else ("Ventoy-Stick" if v else "auswählbar")
            iid = str(d.number)
            self.tree.insert("", "end", iid=iid, text=f"Datenträger {d.number}",
                             values=(d.model, fmt_size(d.size), d.bus_type,
                                     ", ".join(f"{x}: {vv.label}".strip() for x, vv in ((v2.letter, v2) for v2 in d.volumes) if x),
                                     state), tags=(("locked",) if b else ("ventoy",) if v else ()))
        keep = [i for i in sel if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)
        self._on_select()

    def selected_disks(self) -> list[disks.Disk]:
        if not self.snap:
            return []
        return [d for d in (self.snap.disk(int(i)) for i in self.tree.selection()) if d]

    def select(self, numbers: list[int]):
        self.tree.selection_set([str(n) for n in numbers if self.tree.exists(str(n))])
        self._on_select()

    def _on_select(self):
        if self.nb.index("current") == 1:
            self.refresh_ventoy()

    # ============================================================ Hilfen
    def _pick_image(self):
        p = filedialog.askopenfilename(filetypes=IMAGE_TYPES)
        if p:
            self.var_image.set(p)
            self.analyze_image()

    def _pick_into(self, var, types=None):
        p = filedialog.askopenfilename(filetypes=types or [("Alle Dateien", "*.*")])
        if p:
            var.set(p)

    def _pick_backup(self):
        p = filedialog.asksaveasfilename(defaultextension=".img.xz",
                                         filetypes=[("Gepackt (xz)", "*.img.xz"), ("Roh", "*.img"),
                                                    ("gzip", "*.img.gz"), ("ZIP", "*.zip")])
        if p:
            self.var_bk.set(p)

    def _restore_pick(self):
        self.nb.select(0)
        self._pick_image()

    def _log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _msg(self, kind: str, text: str):
        self._log(("FEHLER: " if kind == "error" else "") + text)
        if not self.quiet:
            (messagebox.showerror if kind == "error" else messagebox.showinfo)("IsoFlasher", text, parent=self)

    def analyze_image(self, path: str | None = None):
        path = path or self.var_image.get().strip()
        if not path or not os.path.isfile(path):
            self.lbl_info.configure(text="Datei nicht gefunden.")
            return None
        try:
            info = detect.analyze(path)
        except Exception as e:  # noqa: BLE001
            self.lbl_info.configure(text=f"Fehler beim Erkennen: {e}")
            return None
        self.info = info
        parts = [f"Typ: {info.kind_text}", f"Empfohlen: {info.mode_text}"]
        if info.label:
            parts.append(f"Name: {info.label}")
        if info.distro:
            parts.append(f"System: {info.distro}")
        parts.append(f"Größe: {fmt_size(info.data_size or info.size)}" + (f" (gepackt {info.compressed})" if info.compressed else ""))
        if info.windows:
            w = info.windows
            parts.append(f"{w['install_file']}: {fmt_size(w['install_size'])}" + (" → wird geteilt" if w["needs_split"] else ""))
        self.lbl_info.configure(text="   ·   ".join(parts) + ("\n" + "\n".join(info.notes) if info.notes else ""))
        return info

    def _ensure_helper(self, key: str) -> bool:
        """Fehlenden Helfer nach Rückfrage laden (im Vordergrund mit Fortschritt). True = vorhanden."""
        if helpers.find(key):
            return True
        h = helpers.HELPERS[key]
        text = (f"{h.name} {h.version} wird benötigt ({h.purpose}).\n\nQuelle: {h.url}\nLizenz: {h.license}\n"
                f"Größe: {h.size // 1024} KB\nSHA-256: {h.sha256}\n({h.hash_source})\n\n"
                f"Jetzt herunterladen und prüfen? Ablage: {helpers.tools_dir()}")
        if self.quiet or not messagebox.askyesno("Open-Source-Helfer laden?", text, parent=self):
            self._log(f"{h.name} fehlt – Download nicht bestätigt.")
            return False
        self._run(f"{h.name} laden", lambda job: {"helper": str(helpers.install(key, job))}, refresh=False)
        return False   # nach dem Laden erneut starten

    def ask_confirmation(self, title, targets, lines, warnings=()) -> list[str] | None:
        dlg = ConfirmDialog(self, title, targets, lines, warnings)
        self.wait_window(dlg)
        return dlg.result

    def _targets(self, multi: bool, sources=None, need=None) -> list[disks.Disk] | None:
        if self.busy:
            return None
        if not self.admin:
            self._msg("error", "Dafür sind Administratorrechte nötig. IsoFlasher bitte neu starten und die UAC-Abfrage bestätigen.")
            return None
        ds = self.selected_disks()
        if not ds:
            self._msg("error", "Bitte oben einen Ziel-Datenträger auswählen.")
            return None
        if len(ds) > 1 and not multi:
            self._msg("error", "Hierfür bitte genau einen Datenträger auswählen.")
            return None
        bad = [f"Datenträger {d.number}: " + "; ".join(p) for d in ds if (p := guard.problems(d, sources, need))]
        if bad:
            self._msg("error", "Gesperrt:\n" + "\n".join(bad))
            return None
        return ds

    @staticmethod
    def _disk_lines(ds) -> list[str]:
        out = []
        for d in ds:
            out.append(f"Datenträger {d.number}: {d.model} – {fmt_size(d.size)} – {d.bus_type} – Seriennr. {d.serial or '?'}")
            for v in d.volumes:
                out.append(f"     {v.letter + ':' if v.letter else '  '} „{v.label}“ {v.filesystem} {fmt_size(v.size)}")
        return out

    # ============================================================ Aktionen
    def start_create(self, typed: list[str] | None = None):
        info = self.analyze_image()
        if not info:
            self._msg("error", "Bitte zuerst ein Abbild wählen.")
            return
        mode = self.var_mode.get()
        mode = info.mode if mode == "auto" else mode
        if mode == "ventoy":
            self._msg("error", "Diese ISO ist nicht hybrid und bootet 1:1 geschrieben meist nicht.\n"
                               "Bitte den Reiter „Multiboot (Ventoy)“ verwenden oder oben „1:1 schreiben“ erzwingen.")
            self.nb.select(1)
            return
        if mode == "windows" and info.kind != "windows":
            self._msg("error", "Das ist keine Windows-Installations-ISO.")
            return
        ds = self._targets(multi=(mode == "dd"), sources=[info.path],
                           need=info.data_size if mode == "dd" else None)
        if not ds:
            return
        img = info.path
        verify = self.var_verify.get()
        if mode == "windows":
            opts = windows.WinOptions(style=self.var_style.get(), label=self.var_label.get().strip(),
                                      bypass_hw=self.var_hw.get(), bypass_online=self.var_online.get(),
                                      local_user=self.var_user.get().strip(), no_privacy_questions=self.var_privacy.get(),
                                      no_bitlocker=self.var_bitlocker.get(), verify=verify, allow_download=False)
            if (info.windows.get("needs_split") or opts.force_split) and not self._ensure_helper("wimlib"):
                return
            lines = self._disk_lines(ds) + ["", f"Windows-Stick aus {os.path.basename(img)}",
                                            f"  Partitionstabelle {opts.style}, FAT32 (max. 32 GB)",
                                            "  Dateien kopieren" + (", install.wim teilen" if info.windows.get("needs_split") else ""),
                                            "  autounattend.xml: " + ("ja" if opts.wants_unattend else "nein"),
                                            "  danach jede Datei prüfen" if verify else ""]
            typed = typed or self.ask_confirmation("Windows-Installationsstick erstellen", ds, lines)
            if not typed:
                return
            d = ds[0]
            self._run("Windows-Stick", lambda job: windows.create(d.number, img, typed[0], opts, job, d.uid()))
            return
        persist_mb = None
        if self.var_persist.get():
            if not info.live_system:
                self._msg("error", "Für dieses Abbild ist keine Persistenz bekannt (nur Ubuntu/Mint-artige (casper) und Debian-artige (live-boot) Live-Systeme).")
                return
            try:
                persist_mb = max(0, int(self.var_persist_mb.get() or 0))
            except ValueError:
                self._msg("error", "Persistenz-Größe bitte als Zahl (MB) angeben.")
                return
        warn = ["Windows-ISOs 1:1 geschrieben booten meist nicht."] if info.kind == "windows" else []
        lines = self._disk_lines(ds) + ["", f"1:1 schreiben: {os.path.basename(img)} ({fmt_size(info.data_size or info.size)})",
                                        "  danach zurücklesen und SHA-256 vergleichen" if verify else "  ohne Prüfung"]
        if persist_mb is not None:
            lines.append(f"  Persistenz-Partition {persist_mb or 'Rest'} MB ({info.live_system})")
        typed = typed or self.ask_confirmation(f"Abbild auf {len(ds)} Datenträger schreiben", ds, lines, warn)
        if not typed:
            return
        pairs = list(zip(ds, typed))

        def work(job):
            results, errors = [], []

            def one(d, t):
                try:
                    r = flash.write_image(d.number, img, t, job, d.uid(), verify=verify)
                    if persist_mb is not None:
                        r["persistence"] = persist.add_partition(d.number, info.live_system, t, job, persist_mb,
                                                                 image_end=r["bytes_written"])
                    results.append(r)
                except Cancelled:
                    errors.append(f"Datenträger {d.number}: abgebrochen")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"Datenträger {d.number}: {e}")
            ths = [threading.Thread(target=one, args=p, daemon=True) for p in pairs]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            if errors:
                raise RuntimeError("\n".join(errors) + (f"\n\nErfolgreich: {len(results)}" if results else ""))
            return {"results": results}
        self._run("1:1 schreiben", work)

    def start_ventoy_install(self, typed=None):
        ds = self._targets(multi=False)
        if not ds or not self._ensure_helper("ventoy"):
            return
        d = ds[0]
        gpt, sb, fs = self.var_v_gpt.get(), self.var_v_sb.get(), self.var_v_fs.get()
        lines = self._disk_lines(ds) + ["", f"Ventoy {helpers.HELPERS['ventoy'].version} installieren (GPLv3)",
                                        f"  {'GPT' if gpt else 'MBR'}, Secure Boot {'an' if sb else 'aus'}, {fs}"]
        typed = typed or self.ask_confirmation("Ventoy installieren", ds, lines)
        if not typed:
            return
        self._run("Ventoy installieren", lambda job: ventoy.install(d.number, typed[0], job, gpt, sb, 0, fs,
                                                                    expect_uid=d.uid()))

    def _ventoy_disk(self) -> disks.Disk | None:
        ds = self.selected_disks()
        if len(ds) != 1 or not ventoy.is_ventoy(ds[0]):
            self._msg("error", "Bitte oben genau einen Ventoy-Stick auswählen (oder erst Ventoy installieren).")
            return None
        return ds[0]

    def refresh_ventoy(self):
        self.vtree.delete(*self.vtree.get_children())
        ds = self.selected_disks()
        if len(ds) != 1 or not ventoy.is_ventoy(ds[0]):
            return
        try:
            lst = ventoy.list_isos(ds[0].number)
        except Exception as e:  # noqa: BLE001
            self._log(f"Ventoy-Liste: {e}")
            return
        for it in lst["isos"]:
            self.vtree.insert("", "end", iid=it["path"], text=it["path"],
                              values=(fmt_size(it["size"]), it.get("persistence") or ""))
        self._log(f"Ventoy-Stick {lst['letter']}: {len(lst['isos'])} Abbilder, frei {fmt_size(lst['free_bytes'])}")

    def start_ventoy_add(self, files=None, typed=None):
        d = self._ventoy_disk()
        if not d:
            return
        files = files or list(filedialog.askopenfilenames(filetypes=[("ISO", "*.iso *.img *.wim *.vhd *.vhdx"), ("Alle", "*.*")]))
        if not files:
            return
        if not self._targets(multi=False, sources=files):
            return
        lines = self._disk_lines([d]) + ["", "Hinzufügen (nichts wird gelöscht):"] + [f"  {os.path.basename(f)}" for f in files]
        typed = typed or self.ask_confirmation("ISOs auf Ventoy-Stick kopieren", [d], lines)
        if not typed:
            return
        self._run("ISOs kopieren", lambda job: {"added": [ventoy.add_iso(d.number, f, typed[0], job, expect_uid=d.uid())
                                                          for f in files]}, after=self.refresh_ventoy)

    def start_ventoy_remove(self, paths=None, typed=None):
        d = self._ventoy_disk()
        if not d:
            return
        paths = paths or list(self.vtree.selection())
        if not paths:
            self._msg("error", "Bitte in der Liste ISOs auswählen.")
            return
        typed = typed or self.ask_confirmation("Vom Ventoy-Stick löschen", [d], ["Löschen:"] + [f"  {p}" for p in paths])
        if not typed:
            return
        self._run("ISOs löschen", lambda job: {"removed": [ventoy.remove_iso(d.number, p, typed[0], d.uid()) for p in paths]},
                  after=self.refresh_ventoy)

    def start_ventoy_persist(self, path=None, typed=None):
        d = self._ventoy_disk()
        if not d:
            return
        sel = [path] if path else list(self.vtree.selection())
        if len(sel) != 1:
            self._msg("error", "Bitte genau eine ISO in der Liste auswählen.")
            return
        try:
            mb = int(self.var_v_pmb.get())
        except ValueError:
            self._msg("error", "Größe bitte als Zahl (MB).")
            return
        typed = typed or self.ask_confirmation("Persistenz-Datei anlegen", [d], [f"{sel[0]}: Persistenz-Datei {mb} MB (ext4)"])
        if not typed:
            return
        self._run("Persistenz-Datei", lambda job: ventoy.add_persistence(d.number, sel[0], mb, typed[0], job,
                                                                         expect_uid=d.uid()), after=self.refresh_ventoy)

    def start_backup(self, typed=None):
        out = self.var_bk.get().strip()
        if not out:
            self._msg("error", "Bitte eine Zieldatei angeben.")
            return
        if os.path.exists(out):
            self._msg("error", "Die Datei gibt es schon – bitte anderen Namen wählen.")
            return
        ds = self.selected_disks()
        if len(ds) != 1 or not self.admin:
            self._msg("error", "Bitte genau einen Datenträger wählen (Adminrechte nötig).")
            return
        d = ds[0]
        if [p for p in guard.problems(d, [out]) if "schreibgeschützt" not in p]:
            self._msg("error", "Gesperrt: " + "; ".join(guard.problems(d, [out])))
            return
        used = self.var_bk_used.get()
        typed = typed or self.ask_confirmation("Abbild sichern (Datenträger wird nur gelesen)", [d],
                                               self._disk_lines([d]) + ["", f"→ {out}"])
        if not typed or not disks.confirm_ok(d, typed[0]):
            return
        self._run("Sichern", lambda job: flash.backup(d.number, out, job, used, True, d.uid()), refresh=False)

    def start_hash(self):
        f = self.var_hf.get().strip()
        if not os.path.isfile(f):
            self._msg("error", "Datei nicht gefunden.")
            return
        exp = self.var_hexp.get().strip() or None

        def done(res):
            if res.get("match") is True:
                self.lbl_hash.configure(text=f"✔ STIMMT ÜBEREIN\n{res['sha256']}", foreground="#1b7f2a")
            elif res.get("match") is False:
                self.lbl_hash.configure(text=f"✘ WEICHT AB!\nIst:  {res['sha256']}\nSoll: {res['expected']}", foreground="#b00020")
            else:
                self.lbl_hash.configure(text=f"SHA-256: {res['sha256']}" + (f"\n{res.get('note')}" if res.get("note") else ""),
                                        foreground="#000000")
        self._run("Prüfsumme", lambda job: checksum.compare(f, exp, job), refresh=False, after_res=done)

    def _dl_key(self) -> str:
        return next(k for k, v in isodl.NAMES.items() if v == self.var_dl.get())

    def start_dl_resolve(self):
        key = self._dl_key()
        if key == "windows" and not self._ensure_helper("fido"):
            return
        kw = {"version": self.var_wv.get(), "lang": self.var_wl.get()} if key == "windows" else {}

        def done(res):
            self.dl_info = res
            self.lbl_dl.configure(text=f"{res['name']}\nDatei: {res['file']}\nQuelle: {res['url'][:120]}\n"
                                       f"SHA-256 (offiziell): {res.get('sha256') or '– (selbst eingeben)'}"
                                       + (f"\n{res['note']}" if res.get("note") else ""))
        self._run("Download-Infos", lambda job: isodl.resolve(key, **kw), refresh=False, after_res=done)

    def start_download(self):
        if not self.dl_info or self.dl_info.get("key") != self._dl_key():
            self.start_dl_resolve()
            return
        info, folder, exp = self.dl_info, self.var_dlf.get(), self.var_dlexp.get().strip() or None

        def done(res):
            self.var_image.set(res["file"])
            self.var_hf.set(res["file"])

        self._run("Herunterladen", lambda job: isodl.download(info, folder, job, exp), refresh=False, after_res=done)

    # ============================================================ Ablauf
    def _run(self, name: str, fn, refresh: bool = True, after=None, after_res=None):
        if self.busy:
            return
        self.busy = True
        self.job = Job(lambda p: self.q.put(("prog", p)))
        job = self.job
        self.ptree.delete(*self.ptree.get_children())
        self.btn_cancel.configure(state="normal")
        self._log(f"== {name}")

        def work():
            try:
                self.q.put(("done", fn(job), None, refresh, after, after_res))
            except Cancelled:
                self.q.put(("done", None, "Abgebrochen.", refresh, after, after_res))
            except Exception as e:  # noqa: BLE001
                self.q.put(("done", None, str(e), refresh, after, after_res))
        threading.Thread(target=work, daemon=True).start()

    def cancel(self):
        if self.job and (self.quiet or messagebox.askyesno(
                "Abbrechen?", "Vorgang abbrechen?\n\nEin teilweise beschriebener Stick ist danach unbrauchbar und "
                              "muss neu beschrieben werden.", parent=self)):
            self.job.cancel()
            self._log("Abbruch angefordert …")

    def _progress(self, p: dict):
        if p.get("phase") == "info":
            self._log(p.get("text", ""))
            return
        key = p.get("target") or "-"
        vals = (f"{key}: {p.get('step', '')}", f"{p['percent']:.1f}" if p.get("percent") is not None else "…",
                fmt_speed(p.get("speed")), fmt_duration(p.get("eta")))
        if self.ptree.exists(key):
            self.ptree.item(key, values=vals)
        else:
            self.ptree.insert("", "end", iid=key, values=vals)
        pcts = [float(self.ptree.item(i, "values")[1]) for i in self.ptree.get_children()
                if self.ptree.item(i, "values")[1] not in ("…", "")]
        if pcts:
            self.pbar.configure(value=sum(pcts) / len(pcts))

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "prog":
                    self._progress(item[1])
                elif item[0] == "snap":
                    self._show_snap(item[1])
                elif item[0] == "log":
                    self._log(item[1])
                elif item[0] == "done":
                    self._finish(*item[1:])
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _finish(self, res, err, refresh, after, after_res):
        self.busy = False
        self.job = None
        self.btn_cancel.configure(state="disabled")
        self.last_result, self.last_error = res, err
        if err:
            self.pbar.configure(value=0)
            self._msg("error", err)
        else:
            self.pbar.configure(value=100)
            if after_res:
                after_res(res)
            self._msg("info", self._summary(res))
        if after:
            after()
        if refresh:
            self.refresh()

    @staticmethod
    def _summary(res) -> str:
        if not isinstance(res, dict):
            return "Fertig."
        if "results" in res:
            lines = []
            for r in res["results"]:
                lines.append(f"Datenträger {r['target']}: {fmt_size(r['bytes_written'])} geschrieben"
                             + (" – Prüfsumme stimmt ✔" if r.get("verified") else ""))
                if r.get("persistence"):
                    lines.append(f"   Persistenz-Partition „{r['persistence']['label']}“ angelegt")
                for w in r.get("warnings") or []:
                    lines.append("   Hinweis: " + w)
            return "Fertig.\n" + "\n".join(lines)
        if "verify" in res and "letter" in res:
            return (f"Windows-Stick fertig ({res['letter']}, {res['style']}). {res['verify']['files_checked']} Dateien "
                    "geprüft ✔" + (", install.wim geteilt" if res.get("split") else ""))
        if "sha256_data" in res:
            return f"Sicherung fertig: {res['file']}\nSHA-256: {res['sha256_data']}" + (" – geprüft ✔" if res.get("verified") else "")
        if "helper" in res:
            return f"Geladen und geprüft: {res['helper']}\nBitte den Vorgang erneut starten."
        if "sha256" in res and "file" in res and "match" not in res:
            return f"Gespeichert: {res['file']}\nSHA-256: {res['sha256']}" + (" – stimmt mit offiziellem Wert ✔" if res.get("verified") else "")
        if "match" in res:
            return "Prüfsumme stimmt ✔" if res["match"] else ("Prüfsumme WEICHT AB ✘" if res["match"] is False else res["sha256"])
        return "Fertig."


def main():
    App().mainloop()
