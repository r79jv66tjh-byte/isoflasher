# IsoFlasher

Windows-Tool (Python + tkinter) zum Erstellen bootfähiger USB-Sticks: Linux- und Tool-ISOs 1:1
schreiben und per Prüfsumme kontrollieren, Windows-Installationssticks (GPT/UEFI oder MBR/BIOS,
`install.wim` > 4 GB teilen, Windows-11-Anpassungen), Multiboot-Sticks mit Ventoy, Persistenz für
Linux-Live-Sticks, USB-Sticks und SD-Karten als Abbild sichern und zurückschreiben, offizielle ISOs
herunterladen.

**Sicherheitsprinzip:** Ziel dürfen nur USB-Sticks und SD-Karten sein. Systemdatenträger, Boot-
Datenträger, Datenträger mit Windows/Auslagerungsdatei und **alle internen Platten sind immer
gesperrt**, einen Schalter dafür gibt es nicht. Vor jeder Aktion kommt eine Zusammenfassung, und die
Datenträgernummer (oder der Laufwerksbuchstabe) muss für jedes Ziel eingetippt werden.

## Starten

```bash
python IsoFlasher.pyw
```

oder Doppelklick auf `IsoFlasher.pyw`. Das Programm braucht Administratorrechte und startet sich über
die UAC-Abfrage neu. Lehnt man ab, läuft es nur als Ansicht. Python 3.10+ (getestet mit 3.13), keine
zusätzlichen Pakete.

## Funktionen

| Reiter | Was passiert |
|---|---|
| **Stick erstellen** | Abbild wählen → Typ wird erkannt → empfohlener Weg. Mehrere Sticks gleichzeitig möglich (Strg-Klick). |
| **Multiboot (Ventoy)** | Ventoy installieren, ISOs hinzufügen (mit Prüfung), auflisten, löschen, Persistenz-Datei anlegen. |
| **Abbild sichern / zurückschreiben** | Stick/SD-Karte als `.img`, `.img.xz`, `.img.gz` oder `.zip` sichern (optional nur belegter Bereich), mit `.sha256`-Datei und Prüfung. Zurückschreiben = 1:1 schreiben. |
| **Prüfsumme** | SHA-256 einer Datei berechnen und mit eingegebenem Wert oder einer `SHA256SUMS`-Datei vergleichen. |
| **Herunterladen** | Ubuntu LTS, Linux Mint, Debian Live, Memtest86+, GParted (Link + SHA-256 live von der Herstellerseite, nach dem Laden verglichen); Windows 10/11 über Fido. |

Unten: Fortschritt je Stick mit Prozent, Geschwindigkeit, Restzeit, Abbrechen-Knopf und Protokoll.

### Typ-Erkennung

IsoFlasher liest ISO 9660/Joliet selbst und erkennt MBR-/GPT-Partitionstabellen, El-Torito-Bootkatalog
und typische Ordner (`/casper`, `/live`, `/arch`). Windows-ISOs enthalten ihre Dateien oft nur im
UDF-Teil – dann wird die ISO kurz schreibgeschützt eingehängt (`Mount-DiskImage`).

| Erkannt | Weg |
|---|---|
| Windows-Installations-ISO (`sources\install.wim/.esd`, `boot.wim`) | Windows-Stick |
| Hybride ISO (Ubuntu, Mint, GParted, Memtest86+, …) | 1:1 schreiben |
| `.img`, `.img.xz`, `.img.gz`, `.img.bz2`, `.zip` (z. B. Raspberry Pi) | 1:1 schreiben (direkt entpackt) |
| Nicht-hybride ISO | Hinweis: Ventoy verwenden (1:1 kann man erzwingen) |

### 1:1 schreiben (DD)

Alle Volumes des Sticks werden gesperrt und ausgehängt, die alte Partitionstabelle entfernt
(`IOCTL_DISK_DELETE_DRIVE_LAYOUT`), dann wird ungepuffert auf `\\.\PhysicalDriveN` geschrieben – der
erste MiB (Partitionstabelle) ganz zum Schluss. Während des Schreibens wird SHA-256 mitgerechnet,
danach alles zurückgelesen und verglichen. Bei einer Abweichung nennt die Meldung die erste falsche
Stelle (MiB) – typisch für defekte oder gefälschte Sticks.

### Windows-Stick

- **GPT** (UEFI) oder **MBR** (BIOS + UEFI, aktive Partition, Bootsektor per `bootsect.exe` aus der ISO,
  ersatzweise aus Windows). FAT32, damit UEFI und Secure Boot ohne Zusatz-Bootloader funktionieren.
  Windows formatiert FAT32 nur bis 32 GB – auf größeren Sticks wird die Partition 32 GB groß.
- `install.wim` > 4 GB wird mit **wimlib** in `install.swm`-Teile (3800 MB) geteilt; Windows Setup
  kann diese direkt verwenden. `install.esd` > 4 GB lässt sich nicht teilen.
- Optional `autounattend.xml` (wie Rufus): TPM/Secure-Boot/RAM-Prüfung aus (`LabConfig`),
  Online-Konto-Zwang aus (`BypassNRO`), lokales Konto (leeres Kennwort, beim ersten Anmelden setzen),
  Datenschutz-Fragen aus (`ProtectYourPC=3`), BitLocker-Autoverschlüsselung aus.
- Danach wird das Volume ausgehängt (Cache leeren) und jede Datei neu gelesen und mit der Quelle
  verglichen; geteilte WIMs prüft `wimlib-imagex verify`.

### Ventoy (Multiboot)

`Ventoy2Disk.exe VTOYCLI /I /PhyDrive:N [/GPT] [/NOSB] [/FS:…]` – Ventoys offizieller
Kommandozeilenmodus. `/NOUSBCheck` setzt IsoFlasher nur bei SD-Karten-Lesern und Test-VHDX;
die Sperren prüft IsoFlasher selbst. ISOs werden kopiert, auf dem Stick neu gelesen und per SHA-256
verglichen.

### Persistenz für Linux-Live-Sticks

Windows kann kein ext2/3/4 formatieren – IsoFlasher erzeugt das Dateisystem selbst
(`isoflasher/extfs.py`: ext4 ohne Journal mit Extents, wird unter Linux vom ext4-Treiber eingehängt).

- **Ventoy-Stick:** Persistenz-Datei `/persistence/<iso>.dat` + Eintrag in `/ventoy/ventoy.json`.
  Ventoy fragt beim Start, ob mit Persistenz gebootet werden soll.
- **1:1-Stick:** neue Partition im freien Platz hinter der ISO (MBR: freier Eintrag; GPT: Eintrag
  ergänzt, Kopf und Sicherungskopie am echten Stick-Ende neu geschrieben). Beim Start muss meist der
  Boot-Parameter `persistent` (Ubuntu/Mint) bzw. `persistence` (Debian) ergänzt werden (im Boot-Menü
  mit `e`), weil die ISO selbst unverändert bleibt.

Labels: `casper-rw` (Ubuntu, Mint, … – casper) bzw. `persistence` + `persistence.conf` mit `/ union`
(Debian, Kali, … – live-boot).

## Sperren

| Sperre | Wirkung |
|---|---|
| Datenträger mit `IsSystem`/`IsBoot`, System-/Boot-Partition | nie Ziel |
| Datenträger mit `%SystemDrive%`, Windows-Ordner oder Auslagerungsdatei | nie Ziel |
| Datenträger, von dem IsoFlasher/Python läuft | nie Ziel |
| Interne Platten (kein USB/SD/MMC, kein Wechseldatenträger) | **nie Ziel** (kein Schalter) |
| Offline, schreibgeschützt, kein Medium (Kartenleser leer) | nicht auswählbar |
| Quelldatei liegt auf dem Ziel, Abbild größer als Stick | nicht auswählbar |

Direkt vor dem Schreiben wird alles **neu abgefragt** und geprüft, ob es noch derselbe Datenträger ist
(Kennung, Seriennummer, Größe). Die Sicherheitslogik ist aus DiskFormatter übernommen und verschärft.

Nur für automatische Tests: Die Umgebungsvariable `ISOFLASHER_TEST_DISKS` (mit `;` getrennte
VHD/VHDX-Pfade) gibt genau diese angehängten Test-VHDX frei (Bustyp „File Backed Virtual“ und
identischer Dateipfad). Echte Platten lassen sich darüber nicht freischalten.

## Externe Open-Source-Helfer

Werden **nur bei Bedarf** und **nur nach Bestätigung** geladen (Dialog mit Quelle, Version, Lizenz,
Größe, SHA-256), in fester Version, mit fest hinterlegter SHA-256-Prüfsumme, nach
`%LOCALAPPDATA%\IsoFlasher\tools`. Stimmt die Prüfsumme nicht, wird die Datei verworfen.

| Helfer | Version | Lizenz | Quelle | Zweck |
|---|---|---|---|---|
| [wimlib](https://wimlib.net/) | 1.14.5 | LGPLv3+/GPLv3+ | wimlib.net | `install.wim` teilen/prüfen |
| [Ventoy](https://github.com/ventoy/Ventoy) | 1.1.18 | GPLv3 | GitHub-Release | Multiboot |
| [Fido](https://github.com/pbatard/Fido) | 1.72 | GPLv3 | GitHub-Release | Windows-ISO-Links von Microsoft |

wimlib.net veröffentlicht keine Prüfsummen; der hinterlegte Wert wurde beim Einbau über HTTPS
ermittelt. Ventoy- und Fido-Werte stammen aus den GitHub-Releases. Die Programme werden nicht
mitgeliefert. Wer sie selbst hinlegen will: `tools\` neben IsoFlasher oder `%LOCALAPPDATA%\IsoFlasher\tools`.

Bewusst nicht genutzt: iVentoy (nur teilweise Open Source). Vorbilder: Rufus (GPLv3, autounattend-
Optionen, wimlib-Ansatz), balenaEtcher (Zurücklesen nach dem Schreiben).

## Kommandozeile / Claude (ToolHub)

`cli.py` gibt JSON aus; `toolhub.json` beschreibt die Befehle (Kategorie „Laufwerke & USB“).

```bash
python cli.py drives [--selectable]
python cli.py iso-info --image ubuntu.iso
python cli.py verify --file ubuntu.iso --expected SHA256SUMS
python cli.py flash --image ubuntu.iso --target 3,4 --confirm-target 3,4 [--persistence-mb 0] [--dry-run]
python cli.py windows --image win11.iso --target 3 --style GPT --bypass-hw --bypass-online --confirm-target 3
python cli.py ventoy-install --target 3 --confirm-target 3 [--allow-download]
python cli.py ventoy-add --target 3 --image ubuntu.iso --persistence-mb 4096 --confirm-target 3
python cli.py backup --target 3 --out stick.img.xz --used-only --confirm-target 3
python cli.py download --key ubuntu --folder D:\ISOs [--dry-run]
```

- Nur lesend: `drives`, `iso-info`, `verify`, `helpers`, `ventoy-list`, `catalog`.
- Ändernd: alle anderen. `--dry-run` zeigt nur den Plan (Sperrgründe, nötige Bestätigung, Schritte,
  bei `windows` die autounattend-Vorschau). Ohne `--dry-run` muss `--confirm-target` exakt passen,
  sonst Exit-Code 2. Fehlen Adminrechte, fragt Windows per UAC; der Vorgang läuft dann in einem
  versteckten Admin-Prozess, das Ergebnis kommt zurück.
- Helfer werden über die CLI nur mit `--allow-download` geladen.
- Fehler: Meldung auf stderr, Exit-Code ≠ 0.

## Tests

```bash
python tests\make_images.py
python -m unittest discover -s tests
```

`make_images.py` erzeugt kleine Test-Abbilder (hybride ISO mit MBR bzw. GPT, nicht-hybride ISO,
`.img` + `.xz`/`.gz`/`.zip`, UDF-Windows-ISO mit echter `install.wim`). Die Unit-Tests prüfen Sperren
und Bestätigung mit nachgebauten Laufwerksdaten, GPT/MBR-Persistenz an einer Datei (CRCs, Sicherung),
ext4-Konsistenz, Prüfsummen, autounattend.xml und die Typ-Erkennung – ohne echte Laufwerke.

```bash
python tests\integration.py [--ext4-python <python mit Paket ext4>]
```

(als Administrator) hängt zwei 1-GB-Test-VHDX an und testet daran über CLI und GUI: Sperren,
1:1 schreiben auf zwei Sticks gleichzeitig mit Prüfsummen-Vergleich, gepackte Abbilder,
Persistenz-Partition (MBR und GPT), Sichern/Zurückschreiben, Abbrechen, Windows-Stick (GPT mit
geteilter WIM und autounattend, MBR mit Bootsektor), Ventoy (installieren, kopieren, auflisten,
löschen, Persistenz-Datei). Vor jeder schreibenden Aktion wird geprüft, dass das Ziel eine Test-VHDX
ist. Mit `--ext4-python` werden die erzeugten ext4-Dateisysteme zusätzlich mit der unabhängigen
Bibliothek [`ext4`](https://pypi.org/project/ext4/) gelesen.

**Grenzen der Tests:** Getestet wird an virtuellen Platten und kleinen Test-Abbildern, nicht an
echten Sticks und nicht durch echtes Booten. Ob ein Windows-Stick tatsächlich startet bzw. Linux die
Persistenz annimmt, ist daher nur über die Struktur (Dateien, Bootsektor, Partitionstabelle,
Dateisystem) geprüft.

> **Achtung:** Dieses Tool verändert Datenträger. Falsch angewendet kann es Daten unwiderruflich löschen. Nutzung auf eigenes Risiko, ohne Gewähr. Systemdatenträger sind gesperrt, trotzdem vorher Backups machen.

## Lizenz

GPLv3 – siehe [LICENSE](LICENSE). Grund: IsoFlasher nutzt Ventoy (GPLv3).
