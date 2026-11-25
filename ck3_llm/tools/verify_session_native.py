"""Verify session fields using CK3's native senders and inline writer blocks.

The emulator replaces memcpy, stack probing and transport submission with local
stubs. Stack bytes are cleared before execution so the unwritten native tail is
deterministic. This verification checks encoding, independently of room access.
"""

import argparse
import json
from pathlib import Path
import random
import struct

from ck3_llm.tools.verify_jomini_native import NativeSerializerOracle
from unicorn import UC_HOOK_CODE
from unicorn.x86_const import (
    UC_X86_REG_RAX, UC_X86_REG_RBP, UC_X86_REG_RBX, UC_X86_REG_RCX,
    UC_X86_REG_RDX, UC_X86_REG_RDI, UC_X86_REG_R8, UC_X86_REG_R9,
    UC_X86_REG_R14, UC_X86_REG_R15, UC_X86_REG_RIP, UC_X86_REG_RSP,
)

from ck3_llm.protocol.nakama_native import DEFAULT_GAME_DIR
from ck3_llm.protocol.session_codec import SESSION_SCHEMAS, decode_session_message, encode_session_message


class NativeSessionOracle(NativeSerializerOracle):
    def __init__(self, executable):
        super().__init__(executable)
        self.send_stub = self.scratch + 0xE020
        self.cpu.mem_write(self.send_stub, b"\xc3")
        self.cpu.hook_add(UC_HOOK_CODE, self._capture_send,
                          begin=self.send_stub, end=self.send_stub)
        self.copy_stub = self.base + 0x42361E0
        self.cpu.mem_write(self.copy_stub, b"\xc3")
        self.cpu.hook_add(UC_HOOK_CODE, self._copy,
                          begin=self.copy_stub, end=self.copy_stub)
        # The emulated stack is already mapped for every bounded fixture.
        self.cpu.mem_write(self.base + 0x4234130, b"\xc3")
        self.connection = self.scratch + 0x500
        interface = self.scratch + 0x600
        table = self.scratch + 0x700
        self.cpu.mem_write(self.connection + 0x70, struct.pack("<Q", interface))
        self.cpu.mem_write(interface, struct.pack("<Q", table))
        self.cpu.mem_write(table + 0x48, struct.pack("<Q", self.send_stub))

    def _copy(self, cpu, address, size, user_data):
        destination = cpu.reg_read(UC_X86_REG_RCX)
        source = cpu.reg_read(UC_X86_REG_RDX)
        length = cpu.reg_read(UC_X86_REG_R8)
        if length > 1 << 16:
            raise ValueError("Unexpected native session string length")
        cpu.mem_write(destination, bytes(cpu.mem_read(source, length)))
        cpu.reg_write(UC_X86_REG_RAX, destination)

    def _capture_send(self, cpu, address, size, user_data):
        pointer = cpu.reg_read(UC_X86_REG_R8)
        length = cpu.reg_read(UC_X86_REG_R9)
        if length > 1 << 16:
            raise ValueError("Unexpected native session send length")
        self.output.extend(cpu.mem_read(pointer, length))

    def _fragment(self, begin, end, object_pointer, output_register, length_register):
        self.cpu.reg_write(UC_X86_REG_RSP, self.stack + 0xF008)
        self.cpu.reg_write(UC_X86_REG_RBP, object_pointer)
        self.cpu.emu_start(self.base + begin, self.base + end, timeout=2_000_000, count=100_000)
        if self.cpu.reg_read(UC_X86_REG_RIP) != self.base + end:
            raise RuntimeError("Native session writer block exceeded its execution bound")
        pointer = self.cpu.reg_read(output_register)
        length = self.cpu.reg_read(length_register)
        return bytes(self.cpu.mem_read(pointer, length))

    def encode(self, name, fields):
        schema = SESSION_SCHEMAS[name]
        self.allocation = self.scratch + 0x2000
        self.cpu.mem_write(self.stack, bytes(0x10000))
        command = self.scratch + 0x1000
        self.cpu.mem_write(command, bytes(0x100))
        self.cpu.mem_write(command, struct.pack("<Q", self.base + schema["vtable_rva"]))
        self.cpu.mem_write(command + 8, bytes([schema["type_id"]]))
        offset = 0x10
        for field, tag in schema["fields"]:
            if tag == "string":
                value = fields[field].encode("utf-8") + b"\0" if fields[field] else b""
                self.cpu.mem_write(command + offset, struct.pack("<I", len(value)))
                self.cpu.mem_write(command + offset + 8, struct.pack("<Q", self._allocate(value)))
                offset += 16
            elif type(tag) is int:
                self.cpu.mem_write(command + offset, bytes.fromhex(fields[field]))
                offset += tag
            else:
                if field == "platform":
                    offset = 0x64  # Native object alignment after the byte flag.
                raw = struct.pack("<B" if tag == "u8" else "<I", fields[field])
                self.cpu.mem_write(command + offset, raw)
                offset += len(raw)
        self.output.clear()
        if "write_rva" in schema:
            self._call(schema["write_rva"], self.connection, command, self.scratch + 0x800)
            return bytes(self.output)
        if not schema["fields"]:
            # Header-only writer in the ServerRequestPassword send path.
            return self._fragment(0x37FF99E, 0x37FFA06, command, UC_X86_REG_RBX, UC_X86_REG_RDI)
        if name == "ServerRequestAddPeer":
            return self._fragment(0x37FFB21, 0x37FFBBE, command, UC_X86_REG_R14, UC_X86_REG_R15)
        # The shared packed u32 layout from ServerRespondHandshake's writer.
        return self._fragment(0x37FF79C, 0x37FF81C, command - 0x78, UC_X86_REG_R14, UC_X86_REG_R15)

    def peer_registration_responses(self, fields):
        """Execute the complete native add-peer handler with local side effects stubbed."""
        for rva in (
            0x3812E80, 0x3F9E410, 0x25B6F50, 0x886250, 0x886100,
            0x25B7070, 0x368EF50, 0x368EE70,
        ):
            self.cpu.mem_write(self.base + rva, b"\xc3")
        self.cpu.mem_write(self.stack, bytes(0x10000))
        session = self.scratch + 0x3000
        self.cpu.mem_write(session, bytes(0x500))
        self.cpu.mem_write(session + 0x3EC, struct.pack("<I", 0xFFFFFFFF))
        payload = self.scratch + 0x8000
        encoded = encode_session_message("ServerRequestAddPeer", fields)
        self.cpu.mem_write(payload, encoded)
        self.cpu.mem_write(self.stack + 0xF030, struct.pack("<Q", self.connection))
        self.output.clear()
        self._call(0x3800DB0, session, payload, len(encoded), self.scratch + 0x800)
        machine = struct.unpack("<i", self.cpu.mem_read(session + 0x3EC, 4))[0]
        return bytes(self.output), machine


def verify(game_dir=DEFAULT_GAME_DIR, cases=128):
    oracle = NativeSessionOracle(Path(game_dir) / "binaries" / "ck3.exe")
    generator = random.Random(0xCAFE01)
    comparisons = 0
    for index in range(cases):
        for name, schema in SESSION_SCHEMAS.items():
            fields = {}
            for field, tag in schema["fields"]:
                if tag == "string":
                    fields[field] = ("", "local-ai", "\u79bb\u7ebf\u73a9\u5bb6", "synthetic-long-client-name")[index % 4]
                elif type(tag) is int:
                    fields[field] = generator.randbytes(tag).hex()
                else:
                    fields[field] = generator.randrange(256 if tag == "u8" else 1 << 32)
            expected = oracle.encode(name, fields)
            actual = encode_session_message(name, fields)
            if actual != expected:
                raise AssertionError(f"Session encoding mismatch for {name}: {actual.hex()} != {expected.hex()}")
            decoded = decode_session_message(expected, from_server=schema["from_server"])
            assert decoded["type"] == name
            assert decoded["fields"] == fields
            comparisons += 1
    for index in range(cases):
        fields = {"peer_id": generator.randrange(1 << 31),
                  "peer_value": generator.randrange(1 << 32), "assign_local": index % 2}
        actual, machine = oracle.peer_registration_responses(fields)
        expected = encode_session_message("ClientRespondAddPeer", {})
        if fields["assign_local"]:
            expected += encode_session_message("ClientRequestConnected", {})
        assert actual == expected
        assert machine == (fields["peer_id"] if fields["assign_local"] else -1)
    return {"native_session_comparisons": comparisons, "result": "passed",
            "native_peer_registration_checks": cases,
            "native_unused_tail": "zero-initialized", "live_ck3_admission": "pending"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, default=DEFAULT_GAME_DIR)
    parser.add_argument("--cases", type=int, default=128)
    args = parser.parse_args()
    if args.cases <= 0:
        parser.error("cases must be positive")
    print(json.dumps(verify(args.game_dir, args.cases)))


if __name__ == "__main__":
    main()
