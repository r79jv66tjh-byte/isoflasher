"""Direkter Zugriff auf \\\\.\\PhysicalDriveN (ungepuffert, sektorgenau) und Volume-Sperren."""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_WRITE_THROUGH = 0x80000000
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_UNLOCK_VOLUME = 0x0009001C
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140
IOCTL_DISK_DELETE_DRIVE_LAYOUT = 0x0007C100
MEM_COMMIT_RESERVE, MEM_RELEASE, PAGE_READWRITE = 0x3000, 0x8000, 0x04
FILE_BEGIN = 0
INVALID = ctypes.c_void_p(-1).value

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong),
                                 wintypes.DWORD]
k32.VirtualAlloc.restype = ctypes.c_void_p
k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]


def _err(what: str) -> OSError:
    e = ctypes.get_last_error()
    return OSError(e, f"{what}: {ctypes.FormatError(e).strip()} (Fehler {e})")


class AlignedBuffer:
    """Seitenausgerichteter Puffer (nötig für FILE_FLAG_NO_BUFFERING)."""

    def __init__(self, size: int):
        self.size = size
        self.ptr = k32.VirtualAlloc(None, size, MEM_COMMIT_RESERVE, PAGE_READWRITE)
        if not self.ptr:
            raise _err("Speicher anfordern")

    def load(self, data: bytes, pad_to: int) -> int:
        """data hineinkopieren und mit Nullen bis pad_to auffüllen; gibt pad_to zurück."""
        n = len(data)
        ctypes.memmove(self.ptr, data, n)
        if pad_to > n:
            ctypes.memset(self.ptr + n, 0, pad_to - n)
        return pad_to

    def bytes(self, n: int) -> bytes:
        return ctypes.string_at(self.ptr, n)

    def free(self):
        if self.ptr:
            k32.VirtualFree(self.ptr, 0, MEM_RELEASE)
            self.ptr = None


class Device:
    """\\\\.\\PhysicalDriveN oder \\\\.\\X: öffnen (ungepuffert)."""

    def __init__(self, path: str, write: bool):
        access = GENERIC_READ | (GENERIC_WRITE if write else 0)
        flags = FILE_FLAG_NO_BUFFERING | (FILE_FLAG_WRITE_THROUGH if write else 0)
        self.path = path
        self.h = k32.CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                 OPEN_EXISTING, flags, None)
        if self.h in (None, INVALID):
            self.h = None
            raise _err(f"{path} öffnen")

    def length(self) -> int:
        ret = wintypes.DWORD()
        length = ctypes.c_longlong()
        if not k32.DeviceIoControl(self.h, IOCTL_DISK_GET_LENGTH_INFO, None, 0, ctypes.byref(length),
                                   ctypes.sizeof(length), ctypes.byref(ret), None):
            raise _err("Größe lesen")
        return length.value

    def seek(self, pos: int):
        if not k32.SetFilePointerEx(self.h, pos, None, FILE_BEGIN):
            raise _err(f"Position {pos} setzen")

    def write(self, buf: AlignedBuffer, n: int, pos: int | None = None):
        if pos is not None:
            self.seek(pos)
        done = wintypes.DWORD()
        if not k32.WriteFile(self.h, buf.ptr, n, ctypes.byref(done), None) or done.value != n:
            raise _err(f"Schreiben ({n} Bytes)")

    def read(self, buf: AlignedBuffer, n: int, pos: int | None = None) -> int:
        if pos is not None:
            self.seek(pos)
        done = wintypes.DWORD()
        if not k32.ReadFile(self.h, buf.ptr, n, ctypes.byref(done), None):
            raise _err(f"Lesen ({n} Bytes)")
        return done.value

    def flush(self):
        k32.FlushFileBuffers(self.h)

    def delete_layout(self):
        """Partitionstabelle im Speicher und auf dem Datenträger löschen (Volumes verschwinden)."""
        ret = wintypes.DWORD()
        if not k32.DeviceIoControl(self.h, IOCTL_DISK_DELETE_DRIVE_LAYOUT, None, 0, None, 0, ctypes.byref(ret), None):
            raise _err("Partitionstabelle entfernen")
        self.update_properties()

    def update_properties(self):
        ret = wintypes.DWORD()
        k32.DeviceIoControl(self.h, IOCTL_DISK_UPDATE_PROPERTIES, None, 0, None, 0, ctypes.byref(ret), None)

    def close(self):
        if self.h is not None:
            k32.CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def lock_dismount(path: str, tries: int = 10):
    """Volume (\\\\.\\E: oder \\\\?\\Volume{..}) sperren und aushängen. Gibt den Handle zurück
    (offen lassen, solange geschrieben wird) oder None, wenn es das Volume nicht (mehr) gibt."""
    p = path.rstrip("\\")
    h = k32.CreateFileW(p, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                        OPEN_EXISTING, 0, None)
    if h in (None, INVALID):
        return None
    ret = wintypes.DWORD()
    for _ in range(tries):
        if k32.DeviceIoControl(h, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(ret), None):
            break
        time.sleep(0.5)
    k32.DeviceIoControl(h, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0, ctypes.byref(ret), None)
    return h


def release(handles):
    ret = wintypes.DWORD()
    for h in handles:
        if h:
            k32.DeviceIoControl(h, FSCTL_UNLOCK_VOLUME, None, 0, None, 0, ctypes.byref(ret), None)
            k32.CloseHandle(h)
