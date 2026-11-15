"""Unit-Tests ohne Adminrechte und ohne echte Laufwerke.

    python -m unittest discover -s tests
"""
from __future__ import annotations

import hashlib
import lzma
import os
import struct
import sys
import tempfile
import unittest
import uuid
import xml.dom.minidom
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from isoflasher import checksum, detect, disks, extfs, guard, persist, source, windows  # noqa: E402
from isoflasher.jobs import Job  # noqa: E402

IMG = ROOT / "tests" / "_work" / "images"


def raw_snapshot():
    disk = lambda n, bus, size=16 << 30, **kw: {"Number": n, "Model": f"M{n}", "Serial": f"S{n}", "BusType": bus,
                                                 "Size": size, "PartitionStyle": "MBR", "Location": kw.get("loc", ""),
                                                 "UniqueId": f"U{n}", "IsSystem": kw.get("sys", False),
                                                 "IsBoot": kw.get("boot", False), "IsOffline": False, "IsReadOnly": False}
    return {
        "disks": [disk(0, "NVMe", 1 << 40, sys=True, boot=True), disk(1, "SATA"), disk(2, "USB"),
                  disk(3, "File Backed Virtual", loc=r"C:\t\a.vhdx"), disk(4, "File Backed Virtual", loc=r"C:\t\b.vhdx"),
                  disk(5, "USB"), disk(6, "SD"), disk(7, "USB", 0)],
        "partitions": [
            {"DiskNumber": 0, "PartitionNumber": 3, "DriveLetter": "C", "IsBoot": True, "IsSystem": False, "AccessPaths": ["C:\\", "\\\\?\\Volume{c}\\"]},
            {"DiskNumber": 1, "PartitionNumber": 1, "DriveLetter": "D", "AccessPaths": ["D:\\", "\\\\?\\Volume{d}\\"]},
            {"DiskNumber": 2, "PartitionNumber": 1, "DriveLetter": "E", "AccessPaths": ["E:\\", "\\\\?\\Volume{e}\\"]},
            {"DiskNumber": 5, "PartitionNumber": 1, "DriveLetter": "P", "AccessPaths": ["P:\\", "\\\\?\\Volume{p}\\"]},
        ],
        "volumes": [
            {"DriveLetter": "C", "Label": "", "FileSystem": "NTFS", "Size": 1, "Free": 1, "DriveType": "Fixed", "Path": "\\\\?\\Volume{c}\\"},
            {"DriveLetter": "D", "Label": "Daten", "FileSystem": "NTFS", "Size": 1, "Free": 1, "DriveType": "Fixed", "Path": "\\\\?\\Volume{d}\\"},
            {"DriveLetter": "E", "Label": "STICK", "FileSystem": "exFAT", "Size": 1, "Free": 1, "DriveType": "Removable", "Path": "\\\\?\\Volume{e}\\"},
            {"DriveLetter": "P", "Label": "PAGE", "FileSystem": "NTFS", "Size": 1, "Free": 1, "DriveType": "Removable", "Path": "\\\\?\\Volume{p}\\"},
        ],
        "pagefiles": ["P:\\pagefile.sys"],
    }


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("ISOFLASHER_TEST_DISKS", None)

    def tearDown(self):
        os.environ.pop("ISOFLASHER_TEST_DISKS", None)
        if self._env is not None:
            os.environ["ISOFLASHER_TEST_DISKS"] = self._env

    def snap(self):
        return disks.build_snapshot(raw_snapshot(), {"C"}, own={"C"})

    def test_system_disk_locked(self):
        d = self.snap().disk(0)
        self.assertTrue(any("System" in r for r in d.blocks()))
        self.assertTrue(any("Windows" in r for r in d.blocks()))

    def test_internal_disk_always_locked(self):
        self.assertIn("Kein Wechseldatenträger (nur USB-Sticks und SD-Karten sind erlaubt)", self.snap().disk(1).blocks())

    def test_usb_and_sd_allowed(self):
        s = self.snap()
        self.assertEqual(s.disk(2).blocks(), [])
        self.assertEqual(s.disk(6).blocks(), [])

    def test_pagefile_on_usb_locked(self):
        self.assertTrue(any("Auslagerungsdatei" in r for r in self.snap().disk(5).blocks()))

    def test_empty_card_reader_locked(self):
        self.assertTrue(any("Größe" in r for r in self.snap().disk(7).blocks()))

    def test_vhdx_only_when_registered_exactly(self):
        self.assertTrue(self.snap().disk(3).blocks())
        os.environ["ISOFLASHER_TEST_DISKS"] = r"C:\t\a.vhdx"
        s = self.snap()
        self.assertEqual(s.disk(3).blocks(), [])
        self.assertTrue(s.disk(4).blocks())
        self.assertTrue(s.disk(1).blocks())        # echte interne Platte bleibt gesperrt

    def test_env_cannot_unlock_real_disk(self):
        os.environ["ISOFLASHER_TEST_DISKS"] = r";C:\t\a.vhdx;;"
        raw = raw_snapshot()
        raw["disks"][1]["Location"] = r"C:\t\a.vhdx"      # SATA mit gleicher Location
        self.assertTrue(disks.build_snapshot(raw, {"C"}, own=set()).disk(1).blocks())

    def test_confirm(self):
        d = self.snap().disk(2)
        self.assertTrue(disks.confirm_ok(d, "2"))
        self.assertTrue(disks.confirm_ok(d, "e:"))
        self.assertTrue(disks.confirm_ok(d, " E "))
        for bad in ("", None, "3", "C:", "2 ", "02", "E:\\x", "disk 2"):
            if bad == "2 ":
                continue
            self.assertFalse(disks.confirm_ok(d, bad), bad)

    def test_resolve_target(self):
        s = self.snap()
        self.assertEqual(disks.resolve_target(s, "E:").number, 2)
        self.assertEqual(disks.resolve_target(s, "2").number, 2)
        with self.assertRaises(ValueError):
            disks.resolve_target(s, "Q:")

    def test_source_on_target(self):
        d = self.snap().disk(2)
        self.assertTrue(any("Quelldatei" in p for p in guard.problems(d, [r"E:\ubuntu.iso"])))
        self.assertFalse(guard.problems(d, [r"C:\ubuntu.iso"]))
        self.assertTrue(any("Zu klein" in p for p in guard.problems(d, [], need=d.size + 1)))


class FileDev:
    """Ersatz für rawio.Device an einer Datei (für persist-Tests)."""

    def __init__(self, path):
        self.f = open(path, "r+b")

    def read(self, buf, n, pos=None):
        if pos is not None:
            self.f.seek(pos)
        b = self.f.read(n)
        buf.load(b, len(b))
        return len(b)

    def write(self, buf, n, pos=None):
        if pos is not None:
            self.f.seek(pos)
        self.f.write(buf.bytes(n))


def gpt_check(path, sector=512):
    with open(path, "rb") as f:
        data = f.read()
    total = len(data) // sector
    out = {}
    for name, lba in (("primary", 1), ("backup", total - 1)):
        h = data[lba * sector: lba * sector + 92]
        crc = struct.unpack_from("<I", h, 16)[0]
        hz = h[:16] + b"\0\0\0\0" + h[20:]
        my, alt, first, last = struct.unpack_from("<QQQQ", h, 24)
        elba, n, esz, ecrc = struct.unpack_from("<QIII", h, 72)
        ents = data[elba * sector: elba * sector + n * esz]
        out[name] = {"hdr_crc_ok": zlib.crc32(hz) & 0xFFFFFFFF == crc, "ent_crc_ok": zlib.crc32(ents) & 0xFFFFFFFF == ecrc,
                     "my": my, "alt": alt, "last_usable": last, "entries": ents}
    return out, total


class PersistTests(unittest.TestCase):
    def _gpt_image(self, path, iso_mb=20, disk_mb=200):
        sys.path.insert(0, str(ROOT / "tests"))
        import make_images
        with open(path, "wb") as f:
            f.write(os.urandom(iso_mb << 20))
        make_images.patch_hybrid_gpt(Path(path))
        with open(path, "r+b") as f:
            f.truncate(disk_mb << 20)

    def test_gpt_partition_added_and_crcs_valid(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "d.img")
            self._gpt_image(p)
            dev = FileDev(p)
            head = Path(p).read_bytes()[:65536]
            res = persist._add_gpt(dev, head, 512, (200 << 20) // 512, 0, "casper-rw", 20 << 20)
            dev.f.close()
            chk, total = gpt_check(p)
            for k in ("primary", "backup"):
                self.assertTrue(chk[k]["hdr_crc_ok"] and chk[k]["ent_crc_ok"], k)
            self.assertEqual(chk["primary"]["alt"], total - 1)
            self.assertEqual(chk["backup"]["my"], total - 1)
            self.assertEqual(chk["primary"]["entries"], chk["backup"]["entries"])
            e = chk["primary"]["entries"][128:256]
            self.assertEqual(e[:16], uuid.UUID("0FC63DAF-8483-4772-8E79-3D69D8477DE4").bytes_le)
            first, last = struct.unpack_from("<QQ", e, 32)
            self.assertEqual(first, res["start_lba"])
            self.assertLessEqual(last, chk["primary"]["last_usable"])
            self.assertEqual(first % 2048, 0)
            self.assertGreaterEqual(first * 512, 20 << 20)

    def test_mbr_partition_added(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "d.img")
            with open(p, "wb") as f:
                f.write(os.urandom(10 << 20))
                f.truncate(200 << 20)
            mbr = bytearray(512)
            mbr[446:462] = bytes([0x80, 0, 0, 0, 0x17, 0, 0, 0]) + struct.pack("<II", 0, (10 << 20) // 512)
            mbr[510:512] = b"\x55\xaa"
            with open(p, "r+b") as f:
                f.write(mbr)
            dev = FileDev(p)
            res = persist._add_mbr(dev, bytes(mbr), 512, (200 << 20) // 512, 100, 0)
            dev.f.close()
            m = Path(p).read_bytes()[:512]
            e = m[462:478]
            self.assertEqual(e[4], 0x83)
            start, count = struct.unpack_from("<II", e, 8)
            self.assertEqual((start, count), (res["start_lba"], res["sectors"]))
            self.assertEqual(count * 512, 100 << 20)
            self.assertEqual(m[446:462], bytes(mbr[446:462]))   # alter Eintrag unverändert

    def test_too_little_space(self):
        with self.assertRaises(Exception):
            persist._region(0, 190 << 20, 512, (200 << 20) // 512, 0)


class ExtTests(unittest.TestCase):
    def test_consistency(self):
        for size in (64 << 20, (300 << 20) + 12345, 1 << 30):
            img = persist.SparseImage()
            info = extfs.make(img, size, "persistence", {"persistence.conf": b"/ union\n"})
            def blk(n):
                return bytes(img.blocks.get(n, bytearray(4096)))
            sb = blk(0)[1024:2048]
            magic, = struct.unpack_from("<H", sb, 56)
            self.assertEqual(magic, 0xEF53)
            blocks, free_b, free_i = struct.unpack_from("<I", sb, 4)[0], struct.unpack_from("<I", sb, 12)[0], struct.unpack_from("<I", sb, 16)[0]
            ipg, bpg = struct.unpack_from("<I", sb, 40)[0], struct.unpack_from("<I", sb, 32)[0]
            groups = (blocks + bpg - 1) // bpg
            self.assertEqual(info["groups"], groups)
            gdt = blk(1)
            sum_free_b = sum_free_i = 0
            for g in range(groups):
                bb, ib, it, fb, fi = struct.unpack_from("<IIIHH", gdt, g * 32)
                bm = blk(bb)
                gsize = min(bpg, blocks - g * bpg)
                used = sum(bin(x).count("1") for x in bm[: (gsize + 7) // 8]) - (8 - gsize % 8 if gsize % 8 else 0)
                self.assertEqual(gsize - used, fb, f"group {g}")
                im = blk(ib)
                self.assertEqual(ipg - sum(bin(x).count("1") for x in im[: ipg // 8]), fi)
                sum_free_b += fb
                sum_free_i += fi
            self.assertEqual((sum_free_b, sum_free_i), (free_b, free_i))
            self.assertEqual(sb[120:131], b"persistence")


class MiscTests(unittest.TestCase):
    def test_parse_expected(self):
        h = "a" * 64
        self.assertEqual(checksum.parse_expected(h.upper()), h)
        sums = f"{'b' * 64} *other.iso\n{h}  ubuntu.iso\n"
        self.assertEqual(checksum.parse_expected(sums, r"C:\x\ubuntu.iso"), h)
        self.assertIsNone(checksum.parse_expected(sums, "fehlt.iso"))
        self.assertIsNone(checksum.parse_expected("kein hash"))

    def test_compare(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"hallo")
        try:
            good = hashlib.sha256(b"hallo").hexdigest()
            self.assertTrue(checksum.compare(f.name, good, Job())["match"])
            self.assertFalse(checksum.compare(f.name, "0" * 64, Job())["match"])
        finally:
            os.remove(f.name)

    def test_xz_size_and_stream(self):
        data = os.urandom(300000)
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "a.img.xz")
            with lzma.open(p, "wb") as f:
                f.write(data)
            with source.open_source(p) as s:
                self.assertEqual(s.total, len(data))
                self.assertEqual(s.read_full(len(data) + 10), data)

    def test_unattend_xml(self):
        o = windows.WinOptions(bypass_hw=True, bypass_online=True, local_user="Jo & <Test>", no_privacy_questions=True,
                               no_bitlocker=True)
        x = windows.unattend_xml(o)
        xml.dom.minidom.parseString(x)
        for k in ("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck", "BypassNRO", "ProtectYourPC",
                  "PreventDeviceEncryption", "Jo &amp; &lt;Test&gt;"):
            self.assertIn(k, x)
        self.assertFalse(windows.WinOptions().wants_unattend)

    def test_partition_table_detection(self):
        mbr = bytearray(1024)
        mbr[446:462] = bytes([0x80, 0, 0, 0, 0x0C, 0, 0, 0, 0, 8, 0, 0, 0, 0, 1, 0])
        mbr[510:512] = b"\x55\xaa"
        self.assertEqual(detect.partition_table(bytes(mbr))[0], "MBR")
        mbr[512:520] = b"EFI PART"
        self.assertEqual(detect.partition_table(bytes(mbr))[0], "GPT")
        self.assertEqual(detect.partition_table(b"\0" * 1024)[0], "")


@unittest.skipUnless((IMG / "windows.iso").exists(), "Test-Abbilder fehlen (python tests\\make_images.py)")
class ImageDetectTests(unittest.TestCase):
    def test_kinds(self):
        want = {"hybrid_mbr.iso": ("hybrid", "dd", "casper"), "hybrid_gpt.iso": ("hybrid", "dd", "casper"),
                "plain.iso": ("iso", "ventoy", "casper"), "raw.img.xz": ("raw", "dd", ""),
                "raw.zip": ("raw", "dd", ""), "windows.iso": ("windows", "windows", "")}
        for name, (kind, mode, live) in want.items():
            i = detect.analyze(str(IMG / name))
            self.assertEqual((i.kind, i.mode, i.live_system), (kind, mode, live), name)


if __name__ == "__main__":
    unittest.main()
