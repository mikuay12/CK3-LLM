"""Verify binary world scalars and fixed-point scale using compiled CK3 code."""

import argparse
from decimal import Decimal
import json
from pathlib import Path
import random
import struct

from verify_session_native import NativeSessionOracle
from unicorn.x86_const import (
    UC_X86_REG_XMM1, UC_X86_REG_RAX, UC_X86_REG_RBX, UC_X86_REG_RSI,
    UC_X86_REG_RIP, UC_X86_REG_RBP, UC_X86_REG_R8, UC_X86_REG_RDI,
)

from jomini_codec import F32, FIXED_POINT, JominiCodec, U64
from nakama_native import DEFAULT_GAME_DIR
from transfer_protocol import encode_chunk_bitmap, decode_chunk_bitmap
from snapshot_transfer import TransferChecksum


class NativeWorldOracle(NativeSessionOracle):
    def transfer_checksum(self, data):
        pointer = self.scratch + 0x5000
        self.cpu.mem_write(pointer, data or b"\0")
        self._call(0x382FAC0, pointer, len(data))
        value = self.cpu.reg_read(UC_X86_REG_RAX) & 0xFFFFFFFF
        return value - (1 << 32) if value >= 1 << 31 else value

    def hotjoin_checksum(self, data):
        pointer = self.scratch + 0x5000
        frame = self.scratch + 0x1000
        self.cpu.mem_write(pointer, data or b"\0")
        self.cpu.mem_write(frame - 0x100, bytes(0x200))
        for register, value in (
            (UC_X86_REG_RBP, frame), (UC_X86_REG_RBX, 0),
            (UC_X86_REG_RDI, 0), (UC_X86_REG_RSI, len(data)), (UC_X86_REG_R8, pointer),
        ):
            self.cpu.reg_write(register, value)
        self.cpu.emu_start(self.base + 0x369AA9A, self.base + 0x369AADD, count=100_000)
        if self.cpu.reg_read(UC_X86_REG_RIP) != self.base + 0x369AADD:
            raise RuntimeError("Native hotjoin checksum loop did not finish")
        return struct.unpack("<i", self.cpu.mem_read(frame + 0x74, 4))[0]

    def chunk_bitmap(self, count, chunks):
        transfer = self.scratch + 0x1000
        status = self.scratch + 0x2000
        bitmap = self.scratch + 0x3000
        size = (count + 7) // 8
        self.cpu.mem_write(transfer, bytes(0x100))
        self.cpu.mem_write(status, bytes(0x80))
        self.cpu.mem_write(bitmap, bytes(max(size, 1)))
        self.cpu.mem_write(transfer + 0xA0, struct.pack("<Q", status))
        self.cpu.mem_write(status, struct.pack("<QII", bitmap, size, size))
        for chunk in chunks:
            self.cpu.reg_write(UC_X86_REG_RBX, transfer)
            self.cpu.reg_write(UC_X86_REG_RSI, chunk)
            self.cpu.emu_start(self.base + 0x3826C9B, self.base + 0x3826CC3, count=32)
            if self.cpu.reg_read(UC_X86_REG_RIP) != self.base + 0x3826CC3:
                raise RuntimeError("Native received-chunk bit setter did not finish")
        raw = bytes(self.cpu.mem_read(bitmap, size))
        self._call(0x3827EA0, status)
        assert self.cpu.reg_read(UC_X86_REG_RAX) == len(set(chunks))
        return raw

    def scalar(self, tag, value):
        self.cpu.mem_write(self.stack, bytes(0x10000))
        self.cpu.mem_write(self.writer, bytes(0x40))
        self.cpu.mem_write(self.writer + 8, struct.pack("<Q", self.scratch + 0x100))
        self.cpu.mem_write(self.writer + 0x14, b"\x01")
        self.output.clear()
        if tag == F32:
            bits = struct.unpack("<I", struct.pack("<f", value))[0]
            self.cpu.reg_write(UC_X86_REG_XMM1, bits)
            self._call(0x3F9BB70, self.writer)
        elif tag == FIXED_POINT:
            pointer = self.scratch + 0x8000
            self.cpu.mem_write(pointer, struct.pack("<q", value))
            self._call(0x3F9BF20, self.writer, pointer)
        elif tag == U64:
            self._call(0x3F9BDC0, self.writer, value)
        else:
            raise ValueError("Unknown native world scalar fixture")
        return bytes(self.output)

    def fixed_point_text(self, raw):
        self.cpu.mem_write(self.stack, bytes(0x10000))
        result = self.scratch + 0x9000
        buffer = self.scratch + 0xA000
        self._call(0x3F7FEA0, result, buffer, buffer + 128, raw & ((1 << 64) - 1))
        end, status = struct.unpack("<QI", self.cpu.mem_read(result, 12))
        if status or not buffer <= end <= buffer + 128:
            raise ValueError("Native fixed-point formatter did not produce a bounded string")
        return bytes(self.cpu.mem_read(buffer, end - buffer)).decode("ascii")


def verify(game_dir=DEFAULT_GAME_DIR, cases=64):
    oracle = NativeWorldOracle(Path(game_dir) / "binaries" / "ck3.exe")
    codec = JominiCodec()
    generator = random.Random(0x3155)
    comparisons = 0
    for index in range(cases):
        fixed = generator.randrange(-(1 << 63), 1 << 63)
        integer = generator.randrange(1 << 64)
        fraction = generator.randrange(-1000, 1001) / 4
        for tag, value in ((FIXED_POINT, fixed), (U64, integer), (F32, fraction)):
            payload = oracle.scalar(tag, value)
            parsed = codec.decode_document(payload)[0]["value"]
            if tag == FIXED_POINT:
                assert parsed["raw_integer"] == value
                assert parsed["scale"] == 100000
            else:
                assert parsed["value"] == value
            comparisons += 1
        text = oracle.fixed_point_text(fixed)
        assert Decimal(text) == Decimal(fixed) / 100000
        count = generator.randrange(1, 1025)
        chunks = generator.sample(range(count), generator.randrange(count + 1))
        bitmap = oracle.chunk_bitmap(count, chunks)
        assert encode_chunk_bitmap(count, chunks) == bitmap
        assert decode_chunk_bitmap(bitmap, count) == set(chunks)
        if index < 4:
            checksum_data = (b"", bytes(257), b"\xff" * 257, b"\xff" * 258)[index]
        else:
            checksum_data = generator.randbytes(index * 53 % 4097)
        checksum = TransferChecksum()
        checksum.update(checksum_data)
        assert checksum.value == oracle.transfer_checksum(checksum_data)
        assert checksum.value == oracle.hotjoin_checksum(checksum_data)
        incremental = TransferChecksum()
        for start in range(0, len(checksum_data), 31):
            incremental.update(checksum_data[start:start + 31])
        assert incremental.value == checksum.value
    return {"native_scalar_checks": comparisons, "native_fixed_point_text_checks": cases,
            "native_bitmap_checks": cases, "native_bitmap_popcount_checks": cases,
            "native_file_checksum_checks": cases, "native_hotjoin_checksum_checks": cases,
            "result": "passed", "live_hotjoin_snapshot": "pending"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, default=DEFAULT_GAME_DIR)
    parser.add_argument("--cases", type=int, default=64)
    args = parser.parse_args()
    if args.cases <= 0:
        parser.error("cases must be positive")
    print(json.dumps(verify(args.game_dir, args.cases)))


if __name__ == "__main__":
    main()
