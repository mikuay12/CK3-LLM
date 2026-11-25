"""Inspect local CK3 PE files without loading or modifying the game."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct


NETWORK_TERMS = re.compile(
    r"nakama|CSession|net.?message|net.?command|save.?game.?transfer|"
    r"file.?transfer|hot.?join|lobby|socket|pdx.?mp|network.?manager|"
    r"message.?type|checksum|synchronization|rendezvous|peer.?id|"
    r"multiplayer|\.proto|CNet|CMessage|Packet|SessionMessage|"
    r"join_game|join_server|set_ready_command|set_character_controller|"
    r"start_game_from_lobby|queue_save_transfer|sync_players_command|"
    r"set_observer_command|scripted_gui_command|game_command|"
    r"modules\\session\\source|JominiSession|clientmessages|"
    r"connectionshandler|commanddispatcher|SetReadyStatusCommand|"
    r"SetPlayableCommand|SyncPlayersCommand",
    re.IGNORECASE,
)


class PortableExecutable:
    def __init__(self, path):
        self.path = path
        self.data = path.read_bytes()
        if self.data[:2] != b"MZ":
            raise ValueError(f"Invalid DOS signature in {path}")
        pe_offset = self.u32(0x3C)
        if self.data[pe_offset:pe_offset + 4] != b"PE\0\0":
            raise ValueError(f"Invalid PE signature in {path}")
        file_header = pe_offset + 4
        self.machine = self.u16(file_header)
        section_count = self.u16(file_header + 2)
        optional_size = self.u16(file_header + 16)
        optional = file_header + 20
        magic = self.u16(optional)
        if magic != 0x20B:
            raise ValueError(f"Expected PE32+ in {path}, got {magic:#x}")
        self.image_base = self.u64(optional + 24)
        self.header_size = self.u32(optional + 60)
        directory_count = self.u32(optional + 108)
        self.directories = [
            struct.unpack_from("<II", self.data, optional + 112 + index * 8)
            for index in range(min(directory_count, 16))
        ]
        self.sections = []
        for index in range(section_count):
            offset = optional + optional_size + index * 40
            name = self.data[offset:offset + 8].rstrip(b"\0").decode("ascii")
            virtual_size, rva, raw_size, raw_offset = struct.unpack_from(
                "<IIII", self.data, offset + 8
            )
            self.sections.append({
                "name": name,
                "rva": rva,
                "virtual_size": virtual_size,
                "raw_offset": raw_offset,
                "raw_size": raw_size,
            })

    def u16(self, offset):
        return struct.unpack_from("<H", self.data, offset)[0]

    def u32(self, offset):
        return struct.unpack_from("<I", self.data, offset)[0]

    def u64(self, offset):
        return struct.unpack_from("<Q", self.data, offset)[0]

    def file_offset(self, rva):
        if rva < self.header_size:
            return rva
        for section in self.sections:
            displacement = rva - section["rva"]
            if 0 <= displacement < section["raw_size"]:
                return section["raw_offset"] + displacement
        raise ValueError(f"RVA {rva:#x} has no file-backed section")

    def c_string(self, offset):
        end = self.data.find(b"\0", offset)
        if end == -1:
            raise ValueError(f"Unterminated string at {offset:#x}")
        return self.data[offset:end].decode("ascii", errors="replace")

    def imports(self):
        directory_rva, _ = self.directories[1]
        if not directory_rva:
            return []
        descriptor = self.file_offset(directory_rva)
        libraries = []
        while any(self.data[descriptor:descriptor + 20]):
            original_thunk, _, _, name_rva, first_thunk = struct.unpack_from(
                "<IIIII", self.data, descriptor
            )
            library = self.c_string(self.file_offset(name_rva))
            thunk = self.file_offset(original_thunk or first_thunk)
            functions = []
            index = 0
            while value := self.u64(thunk + index * 8):
                if value & (1 << 63):
                    name = f"ordinal:{value & 0xFFFF}"
                else:
                    name = self.c_string(self.file_offset(value) + 2)
                functions.append({
                    "name": name,
                    "iat_rva": hex(first_thunk + index * 8),
                })
                index += 1
            libraries.append({"library": library, "functions": functions})
            descriptor += 20
        return libraries

    def exports(self):
        directory_rva, directory_size = self.directories[0]
        if not directory_rva:
            return []
        directory = self.file_offset(directory_rva)
        ordinal_base = self.u32(directory + 16)
        names_count = self.u32(directory + 24)
        function_table = self.file_offset(self.u32(directory + 28))
        name_table = self.file_offset(self.u32(directory + 32))
        ordinal_table = self.file_offset(self.u32(directory + 36))
        exports = []
        for index in range(names_count):
            name_rva = self.u32(name_table + index * 4)
            ordinal = self.u16(ordinal_table + index * 2)
            function_rva = self.u32(function_table + ordinal * 4)
            entry = {
                "name": self.c_string(self.file_offset(name_rva)),
                "ordinal": ordinal_base + ordinal,
                "rva": hex(function_rva),
            }
            if directory_rva <= function_rva < directory_rva + directory_size:
                entry["forwarder"] = self.c_string(self.file_offset(function_rva))
            exports.append(entry)
        return exports

    def network_strings(self):
        matches = []
        for section in self.sections:
            if section["name"] not in (".rdata", ".data"):
                continue
            start = section["raw_offset"]
            end = start + section["raw_size"]
            for match in re.finditer(rb"[\x20-\x7E]{5,}", self.data[start:end]):
                value = match.group().decode("ascii")
                if not NETWORK_TERMS.search(value):
                    continue
                matches.append({
                    "text": value,
                    "rva": hex(section["rva"] + match.start()),
                    "file_offset": hex(start + match.start()),
                })
        return matches

    def report(self):
        return {
            "path": str(self.path),
            "size": len(self.data),
            "sha256": hashlib.sha256(self.data).hexdigest(),
            "machine": hex(self.machine),
            "image_base": hex(self.image_base),
            "sections": self.sections,
            "imports": self.imports(),
            "exports": self.exports(),
            "network_strings": self.network_strings(),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("install", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    launcher = json.loads(
        (args.install / "launcher" / "launcher-settings.json").read_text("utf-8-sig")
    )
    report = {"game_version": launcher.get("rawVersion"), "files": []}
    for name in ("ck3.exe", "nakama-cpp.dll", "PDXSDK.dll"):
        entry = PortableExecutable(args.install / "binaries" / name).report()
        report["files"].append(entry)
        print(json.dumps({
            "file": name,
            "sha256": entry["sha256"],
            "imports": len(entry["imports"]),
            "exports": len(entry["exports"]),
            "matching_strings": len(entry["network_strings"]),
        }))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
