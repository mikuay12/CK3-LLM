"""Run CK3's actual argument parser to verify local Nakama option syntax.

Only insertion into the final option table is intercepted after the original
key/value trimming. The original scanner, quoted strings and delimiters run.
This checks parsing in emulated memory, independently of game startup/login.
"""

import json
from pathlib import Path
import struct

from ck3_llm.tools.verify_session_native import NativeSessionOracle
from unicorn import UC_HOOK_CODE
from unicorn.x86_const import UC_X86_REG_RAX, UC_X86_REG_RBX, UC_X86_REG_RDI, UC_X86_REG_RIP, UC_X86_REG_RSP

from ck3_llm.protocol.nakama_native import DEFAULT_GAME_DIR


class NativeArgumentOracle(NativeSessionOracle):
    def __init__(self, executable):
        super().__init__(executable)
        point = self.base + 0x3F6625A
        self.cpu.hook_add(UC_HOOK_CODE, self._capture_pair, begin=point, end=point)
        self.arguments = []

    def _read_string(self, address):
        size, capacity = struct.unpack("<QQ", self.cpu.mem_read(address + 0x10, 16))
        pointer = struct.unpack("<Q", self.cpu.mem_read(address, 8))[0] if capacity >= 16 else address
        return bytes(self.cpu.mem_read(pointer, size)).decode("utf-8")

    def _capture_pair(self, cpu, address, size, user_data):
        pair = cpu.reg_read(UC_X86_REG_RDI)
        self.arguments.append((self._read_string(pair), self._read_string(pair + 0x20)))
        # Continue through the real clear/destructor/return path after capture.
        cpu.reg_write(UC_X86_REG_RIP, self.base + 0x3F66382)

    def parse(self, command_line):
        self.allocation = self.scratch + 0x2000
        source = self.scratch + 0x1000
        self.cpu.mem_write(source, bytes(32))
        self._string(source, command_line)
        output = self.scratch + 0x8000
        self.cpu.mem_write(output, bytes(64))
        self.arguments.clear()
        self._call(0x3F65CB0, output, source)
        if self.cpu.reg_read(UC_X86_REG_RAX) != 1:
            raise ValueError("Native argument parser rejected the fixture")
        return dict(self.arguments)


class NativeFrontendOracle(NativeSessionOracle):
    def check_gate(self, action, identity_blocked, game_ready):
        sites = {"host": (0x189293A, 0x1892952, 0x189299A),
                 "join": (0x18923F2, 0x189240A, 0x1892453)}
        begin, accepted, rejected = sites[action]
        game = self.scratch + 0x4000
        table = self.scratch + 0x5000
        ready = self.scratch + 0xE100
        self.cpu.mem_write(self.base + 0x5C7F548, struct.pack("<Q", game))
        self.cpu.mem_write(game, struct.pack("<Q", table))
        self.cpu.mem_write(table + 0xB0, struct.pack("<Q", ready))
        self.cpu.mem_write(game + 0x20, bytes([int(game_ready)]))
        self.cpu.mem_write(ready, b"\x48\xb8" + struct.pack("<Q", game + 0x20) + b"\x8a\x00\xc3")
        self.cpu.reg_write(UC_X86_REG_RSP, self.stack + 0xF008)
        self.cpu.reg_write(UC_X86_REG_RBX, int(identity_blocked))
        reached = []

        def stop_at_branch(cpu, address, size, user_data):
            if address in (self.base + accepted, self.base + rejected):
                reached.append(address == self.base + accepted)
                cpu.emu_stop()

        hook = self.cpu.hook_add(UC_HOOK_CODE, stop_at_branch,
                                 begin=self.base + begin, end=self.base + rejected)
        try:
            self.cpu.emu_start(self.base + begin, self.stop, count=1000)
        finally:
            self.cpu.hook_del(hook)
        if len(reached) != 1:
            raise RuntimeError("Native frontend gate did not reach an expected branch")
        return reached[0]


def main():
    oracle = NativeArgumentOracle(Path(DEFAULT_GAME_DIR) / "binaries" / "ck3.exe")
    comparisons = 0
    for prefix in ("-", "--"):
        for port in (7350, 19735):
            for ssl in (0, 1):
                fields = {"nakama_key": "ck3-local", "nakama_host": "127.0.0.1",
                          "nakama_port": str(port), "nakama_ssl": str(ssl)}
                line = " ".join(prefix + key + "=" + value for key, value in fields.items())
                assert oracle.parse(line) == fields
                comparisons += 1
    frontend = NativeFrontendOracle(Path(DEFAULT_GAME_DIR) / "binaries" / "ck3.exe")
    frontend_checks = 0
    for action in ("host", "join"):
        for blocked in (False, True):
            for ready in (False, True):
                assert frontend.check_gate(action, blocked, ready) == (not blocked and ready)
                frontend_checks += 1
    print(json.dumps({"native_argument_checks": comparisons, "native_frontend_gate_checks": frontend_checks,
                      "result": "passed",
                      "verified_prefixes": ["-", "--"], "game_frontend_login": "pending"}))


if __name__ == "__main__":
    main()
