"""Read a local CK3 process using Windows query and read permissions."""

import ctypes
from ctypes import wintypes
import hashlib
from pathlib import Path
import struct

from ck3_llm.version import SUPPORTED_EXE_SHA256


class ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


class ModuleInfo(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("size", wintypes.DWORD), ("entry", ctypes.c_void_p)]


class ProcessMemory:
    def __init__(self, pid=None):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateToolhelp32Snapshot": (wintypes.HANDLE, [wintypes.DWORD, wintypes.DWORD]),
            "Process32FirstW": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]),
            "Process32NextW": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]),
            "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
            "OpenProcess": (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
            "GetExitCodeProcess": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]),
            "QueryFullProcessImageNameW": (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD,
                                                          wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]),
            "K32EnumProcessModules": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE),
                                                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]),
            "K32GetModuleInformation": (wintypes.BOOL, [wintypes.HANDLE, wintypes.HMODULE,
                                                       ctypes.POINTER(ModuleInfo), wintypes.DWORD]),
            "ReadProcessMemory": (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(self.api, name)
            function.restype = result
            function.argtypes = arguments
        candidates = self._processes()
        if pid is not None:
            candidates = [value for value in candidates if value == pid]
        if len(candidates) != 1:
            raise ValueError("Start CK3 or specify the PID of one running CK3 process")
        self.pid = candidates[0]
        self.handle = self.api.OpenProcess(0x410, False, self.pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            path = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(path))
            if not self.api.QueryFullProcessImageNameW(self.handle, 0, path, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.executable = Path(path.value)
            if hashlib.sha256(self.executable.read_bytes()).hexdigest() != SUPPORTED_EXE_SHA256:
                raise ValueError("This CK3 executable differs from the verified object layouts")
            modules = (wintypes.HMODULE * 1024)()
            needed = wintypes.DWORD()
            if not self.api.K32EnumProcessModules(self.handle, modules, ctypes.sizeof(modules), ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            info = ModuleInfo()
            if not self.api.K32GetModuleInformation(self.handle, modules[0], ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.base = info.base
        except Exception:
            self.close()
            raise
        self.cache = {}
        self.bytes_read = 0
        self.read_calls = 0

    def _processes(self):
        snapshot = self.api.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            valid = self.api.Process32FirstW(snapshot, ctypes.byref(entry))
            result = []
            while valid:
                if entry.szExeFile.lower() == "ck3.exe":
                    result.append(entry.th32ProcessID)
                valid = self.api.Process32NextW(snapshot, ctypes.byref(entry))
            return result
        finally:
            self.api.CloseHandle(snapshot)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def is_running(self):
        if not self.handle:
            return False
        code = wintypes.DWORD()
        if not self.api.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value == 259

    def reset(self):
        self.cache.clear()
        self.bytes_read = 0
        self.read_calls = 0

    def read(self, address, size, *, fresh=False):
        if not self.handle:
            raise ValueError("Process memory reader is closed")
        if address <= 0 or size < 0 or size > 1048576:
            raise ValueError("Invalid bounded memory read")
        if not size:
            return b""
        if fresh:
            buffer = ctypes.create_string_buffer(size)
            read = ctypes.c_size_t()
            if not self.api.ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(read)) or read.value != size:
                raise OSError(ctypes.get_last_error(), "Game object memory became unavailable")
            self.bytes_read += size
            self.read_calls += 1
            return buffer.raw
        result = bytearray()
        end = address + size
        while address < end:
            page = address & ~255
            if page not in self.cache:
                self.cache[page] = self.read(page, 256, fresh=True)
            count = min(end - address, 256 - (address - page))
            result.extend(self.cache[page][address - page:address - page + count])
            address += count
        return bytes(result)

    def u8(self, address):
        return self.read(address, 1)[0]

    def i16(self, address):
        return struct.unpack("<h", self.read(address, 2))[0]

    def u32(self, address, *, fresh=False):
        return struct.unpack("<I", self.read(address, 4, fresh=fresh))[0]

    def i32(self, address):
        return struct.unpack("<i", self.read(address, 4))[0]

    def pointer(self, address, *, fresh=False):
        return struct.unpack("<Q", self.read(address, 8, fresh=fresh))[0]

    def fixed(self, address):
        return struct.unpack("<q", self.read(address, 8))[0] / 100000

    def string(self, address):
        length = self.pointer(address + 0x10)
        capacity = self.pointer(address + 0x18)
        if length > capacity or length > 65536:
            raise ValueError("Invalid game string")
        data = address if capacity < 16 else self.pointer(address)
        return self.read(data, length).decode("utf-8", errors="strict") if length else ""
