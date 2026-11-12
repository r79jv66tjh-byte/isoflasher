"""Kleine Test-Abbilder erzeugen (kein Admin nötig).

  hybrid_mbr.iso   ISO 9660/Joliet „Ubuntu“-ähnlich (/casper), hybrid mit MBR-Partitionstabelle
  hybrid_gpt.iso   dasselbe mit GPT (Schutz-MBR, Kopf + Sicherung am Ende)
  plain.iso        ISO ohne Partitionstabelle (-> Ventoy empfohlen)
  raw.img          Datenträger-Abbild mit MBR (Raspberry-Pi-artig) + .img.xz, .img.gz, .zip
  windows.iso      nur UDF (wie Microsofts ISOs) mit echter install.wim (wimlib), boot.wim, bootmgr,
                   efi/boot/bootx64.efi, boot/bootsect.exe (Kopie aus System32)

    python tests\\make_images.py <ordner>
"""
from __future__ import annotations

import gzip
import lzma
import os
import shutil
import struct
import subprocess
import sys
import uuid
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from isoflasher import helpers  # noqa: E402

_IMAPI = r"""
param($src, $out, $label, $fs)
if (-not ('IsoWriter' -as [type])) { Add-Type -TypeDefinition @'
public class IsoWriter {
  public static void Write(string path, object stream, int blockSize, int totalBlocks) {
    var i = stream as System.Runtime.InteropServices.ComTypes.IStream;
    byte[] buf = new byte[blockSize];
    using (var o = System.IO.File.Create(path)) {
      while (totalBlocks-- > 0) { i.Read(buf, blockSize, System.IntPtr.Zero); o.Write(buf, 0, blockSize); }
    }
  }
}
'@ }
$fsi = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
$fsi.FileSystemsToCreate = [int]$fs
$fsi.FreeMediaBlocks = 4000000
$fsi.VolumeName = $label
$fsi.Root.AddTree($src, $false)
$r = $fsi.CreateResultImage()
[IsoWriter]::Write($out, $r.ImageStream, $r.BlockSize, $r.TotalBlocks)
"""


def make_iso(src: Path, out: Path, label: str, fs: int):
    script = out.parent / "_imapi.ps1"
    script.write_text(_IMAPI, encoding="utf-8")
    p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                        str(src), str(out), label, str(fs)], capture_output=True, timeout=600)
    script.unlink()
    if p.returncode != 0 or not out.exists():
        raise RuntimeError("IMAPI: " + p.stderr.decode("utf-8", "replace")[-800:])


def rnd(path: Path, size: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        while size > 0:
            n = min(size, 1 << 20)
            f.write(os.urandom(n))
            size -= n


def mbr_entry(boot: int, ptype: int, start: int, count: int) -> bytes:
    return bytes([boot, 0xFE, 0xFF, 0xFF, ptype, 0xFE, 0xFF, 0xFF]) + struct.pack("<II", start, count)


def bootcode() -> bytes:
    return (b"\xFA\x31\xC0" + b"TESTBOOT" * 60)[:440]


def patch_hybrid_mbr(path: Path):
    size = path.stat().st_size
    mbr = bytearray(512)
    mbr[:440] = bootcode()
    mbr[446:462] = mbr_entry(0x80, 0x17, 0, size // 512)
    mbr[510:512] = b"\x55\xaa"
    with open(path, "r+b") as f:
        f.write(mbr)


def patch_hybrid_gpt(path: Path):
    # Bild auf 1 MiB runden + Platz für Sicherungs-GPT
    with open(path, "r+b") as f:
        f.seek(0, 2)
        size = f.tell()
        total = (size // 512) + 33
        total = (total + 2047) // 2048 * 2048
        f.truncate(total * 512)
        last = total - 1
        ents = bytearray(128 * 128)
        e = (uuid.UUID("0FC63DAF-8483-4772-8E79-3D69D8477DE4").bytes_le + uuid.uuid4().bytes_le
             + struct.pack("<QQQ", 64, size // 512 - 1, 0) + "ISO9660".encode("utf-16-le").ljust(72, b"\0"))
        ents[:128] = e
        ecrc = zlib.crc32(bytes(ents)) & 0xFFFFFFFF
        guid = uuid.uuid4().bytes_le

        def hdr(my, alt, elba):
            b = struct.pack("<8sIIIIQQQQ16sQIII", b"EFI PART", 0x10000, 92, 0, 0, my, alt, 34, last - 33,
                            guid, elba, 128, 128, ecrc)
            return b[:16] + struct.pack("<I", zlib.crc32(b) & 0xFFFFFFFF) + b[20:]
        mbr = bytearray(512)
        mbr[:440] = bootcode()
        mbr[446:462] = mbr_entry(0, 0xEE, 1, min(total - 1, 0xFFFFFFFF))
        mbr[510:512] = b"\x55\xaa"
        f.seek(0)
        f.write(mbr)
        f.write(hdr(1, last, 2).ljust(512, b"\0"))
        f.write(ents)
        f.seek((last - 32) * 512)
        f.write(ents)
        f.write(hdr(last, 1, last - 32).ljust(512, b"\0"))


def make_raw(out: Path, mb: int = 48):
    rnd(out, mb << 20)
    mbr = bytearray(512)
    mbr[:440] = bootcode()
    mbr[440:444] = os.urandom(4)          # Datenträger-Signatur wie bei echten Pi-Abbildern (PARTUUID)
    mbr[446:462] = mbr_entry(0x80, 0x0C, 2048, (mb << 20) // 512 - 2048)
    mbr[510:512] = b"\x55\xaa"
    with open(out, "r+b") as f:
        f.write(mbr)
    data = out.read_bytes()
    with lzma.open(str(out) + ".xz", "wb", preset=0) as f:
        f.write(data)
    with gzip.open(str(out) + ".gz", "wb", compresslevel=1) as f:
        f.write(data)
    with zipfile.ZipFile(str(out)[:-4] + ".zip", "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        z.writestr("raspi-test.img", data)


def make_linux_tree(d: Path, label_marker: str):
    rnd(d / "casper" / "filesystem.squashfs", 24 << 20)
    rnd(d / "casper" / "vmlinuz", 2 << 20)
    (d / "boot" / "grub").mkdir(parents=True, exist_ok=True)
    (d / "boot" / "grub" / "grub.cfg").write_text("menuentry 'Test' { linux /casper/vmlinuz }\n")
    (d / "README.diskdefines").write_text(label_marker)


def make_windows(work: Path, out: Path):
    wimlib = helpers.require("wimlib", allow_download=False)
    tree = work / "win"
    rnd(tree / "bootmgr", 400 << 10)
    rnd(tree / "efi" / "boot" / "bootx64.efi", 1 << 20)
    rnd(tree / "sources" / "boot.wim", 2 << 20)
    rnd(tree / "sources" / "setup.exe", 300 << 10)
    (tree / "boot").mkdir(parents=True, exist_ok=True)
    shutil.copy(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "bootsect.exe"),
                tree / "boot" / "bootsect.exe")
    cap = work / "wimsrc"
    rnd(cap / "Windows" / "System32" / "big1.bin", 30 << 20)
    rnd(cap / "Windows" / "System32" / "big2.bin", 25 << 20)
    (cap / "Windows" / "readme.txt").write_text("Testabbild\n" * 1000)
    p = subprocess.run([str(wimlib), "capture", str(cap), str(tree / "sources" / "install.wim"),
                        "Test Windows", "--compress=none"], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("wimlib capture: " + p.stdout.decode(errors="replace")[-400:])
    make_iso(tree, out, "CCCOMA_X64FRE_DE-DE_DV9", 4)     # 4 = nur UDF


def main(folder: str):
    w = Path(folder)
    w.mkdir(parents=True, exist_ok=True)
    work = w / "_src"
    shutil.rmtree(work, ignore_errors=True)
    make_linux_tree(work / "linux", "Ubuntu test")
    make_iso(work / "linux", w / "hybrid_mbr.iso", "Ubuntu 26.04 Test amd64", 3)
    patch_hybrid_mbr(w / "hybrid_mbr.iso")
    make_iso(work / "linux", w / "hybrid_gpt.iso", "Ubuntu 26.04 Test amd64", 3)
    patch_hybrid_gpt(w / "hybrid_gpt.iso")
    make_iso(work / "linux", w / "plain.iso", "TOOLDISK", 3)
    make_raw(w / "raw.img")
    make_windows(work, w / "windows.iso")
    shutil.rmtree(work, ignore_errors=True)
    for f in sorted(w.iterdir()):
        print(f.name, f.stat().st_size)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "tests" / "_work" / "images"))
