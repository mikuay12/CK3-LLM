"""Extract CK3's compiled token-name table without loading its executable."""

import argparse
import hashlib
import json
from pathlib import Path
import struct

from inspect_network import PortableExecutable


SUPPORTED_EXE_SHA256 = "52c4678588c11ef6dd49c815198bc0108134d69699a30b90bc66f080268e8110"


def extract_tokens(executable):
    pe = PortableExecutable(Path(executable))
    digest = hashlib.sha256(pe.data).hexdigest()
    if digest != SUPPORTED_EXE_SHA256:
        raise ValueError("This command schema is verified for CK3 1.20.1.2's executable hash")
    anchor = pe.data.index(b"set_ready_command\0")
    section = next(row for row in pe.sections
                   if row["raw_offset"] <= anchor < row["raw_offset"] + row["raw_size"])
    anchor_rva = section["rva"] + anchor - section["raw_offset"]
    pointer = struct.pack("<Q", pe.image_base + anchor_rva)
    position = pe.data.find(pointer)
    while position >= 8 and pe.u64(position - 8) != 0x6A6:
        position = pe.data.find(pointer, position + 1)
    if position < 8:
        raise ValueError("Ready command token table anchor was not found")

    def entry(offset):
        token, address = struct.unpack_from("<QQ", pe.data, offset)
        if not 0 <= token <= 0xFFFF:
            raise ValueError("End of token table")
        string_offset = pe.file_offset(address - pe.image_base)
        end = pe.data.find(b"\0", string_offset, string_offset + 512)
        if end <= string_offset:
            raise ValueError("End of token table")
        value = pe.data[string_offset:end].decode("ascii")
        if not all(32 <= ord(char) <= 126 for char in value):
            raise ValueError("End of token table")
        return token, value

    start = position - 8
    while start >= 16:
        try:
            entry(start - 16)
        except (ValueError, struct.error):
            break
        start -= 16
    tokens = {}
    cursor = start
    while cursor + 16 <= len(pe.data):
        try:
            token, value = entry(cursor)
        except (ValueError, struct.error):
            break
        if token in tokens:
            raise ValueError("Token table has a duplicate numeric token")
        tokens[token] = value
        cursor += 16
    if tokens.get(0x6A6) != "set_ready_command" or tokens.get(0x6DE) != "set_character_controller":
        raise ValueError("The command token anchors disagree with the schema")
    section = next(row for row in pe.sections
                   if row["raw_offset"] <= start < row["raw_offset"] + row["raw_size"])
    return {
        "game_version": "1.20.1.2", "exe_sha256": digest,
        "table_rva": hex(section["rva"] + start - section["raw_offset"]),
        "token_count": len(tokens),
        "tokens": {hex(token): value for token, value in tokens.items()},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("jomini_tokens.json"))
    args = parser.parse_args()
    report = extract_tokens(args.executable)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "tokens"}))


if __name__ == "__main__":
    main()
