"""Prototype codec for the PDX Nakama payload in CK3 1.20.1.2.

The layout comes from static analysis of the local executable and was verified
with a real host during handshake, world transfer and player-command exchange.
Input is Nakama MatchData.data after its outer transport has been decoded.
"""

import argparse
import json
from pathlib import Path
import struct


GAME_DATA_OPCODE = 255
FRAME_HEADER_SIZE = 48
SUFFIX_SIZE = 64
MAX_FRAME_SIZE = 1 << 20
KNOWN_MAGICS = (0xCAFE, 0xBCA5)


def encode_payload(recipient, messages, sequence, source_user, magic=0xCAFE):
    """Build the statically identified envelope around opaque game messages.

    messages contains payload_hex, word_4 and word_8. The latter two names
    deliberately preserve offsets whose full semantics are still unresolved.
    """
    if magic not in KNOWN_MAGICS:
        raise ValueError("Unsupported PDX frame magic")
    if not isinstance(recipient, str) or not isinstance(source_user, str):
        raise ValueError("Recipient and source user must be strings")
    if type(sequence) is not int or not 0 <= sequence < (1 << 64):
        raise ValueError("Sequence must fit an unsigned 64-bit integer")
    if not isinstance(messages, (list, tuple)):
        raise ValueError("Messages must be a list")
    recipient_bytes = recipient.encode("ascii")
    user_bytes = source_user.encode("ascii")
    if b"\0" in recipient_bytes or len(recipient_bytes) >= 40:
        raise ValueError("Recipient must fit the 40-byte terminated field")
    if b"\0" in user_bytes or len(user_bytes) > 48:
        raise ValueError("Source user must fit the 48-byte suffix field")
    if len(messages) > 0xFFFF:
        raise ValueError("Too many messages in a frame")
    records = bytearray()
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("payload_hex"), str):
            raise ValueError("Each message requires a payload_hex string")
        words = [message.get("word_4", 5), message.get("word_8", 0)]
        if any(type(word) is not int or not 0 <= word < (1 << 32) for word in words):
            raise ValueError("Message words must fit unsigned 32-bit integers")
        payload = bytes.fromhex(message["payload_hex"])
        if FRAME_HEADER_SIZE + len(records) + 12 + len(payload) > MAX_FRAME_SIZE:
            raise ValueError("Frame exceeds the observed 1 MiB writer limit")
        records.extend(struct.pack(
            "<III", len(payload) + 8, *words,
        ))
        records.extend(payload)
    frame_size = FRAME_HEADER_SIZE + len(records)
    if frame_size > MAX_FRAME_SIZE:
        raise ValueError("Frame exceeds the observed 1 MiB writer limit")
    header = bytearray(FRAME_HEADER_SIZE)
    struct.pack_into("<HHI", header, 0, magic, len(messages), frame_size)
    if magic == 0xCAFE:
        header[8:8 + len(recipient_bytes)] = recipient_bytes
    suffix = bytearray(SUFFIX_SIZE)
    struct.pack_into("<H", suffix, 0, SUFFIX_SIZE)
    struct.pack_into("<Q", suffix, 8, sequence)
    suffix[16:16 + len(user_bytes)] = user_bytes
    return bytes(header + records + suffix + b"\0")


def decode_payload(data):
    """Decode one complete opcode-255 data payload with a PDX suffix."""
    minimum_size = FRAME_HEADER_SIZE + SUFFIX_SIZE + 1
    if len(data) < minimum_size:
        raise ValueError("Payload is too short for the observed envelope")
    suffix_start = len(data) - SUFFIX_SIZE - 1
    if data[-1] != 0:
        raise ValueError("Missing observed trailing zero byte")
    suffix_size = struct.unpack_from("<H", data, suffix_start)[0]
    if suffix_size != SUFFIX_SIZE:
        raise ValueError("Unexpected PDX suffix size")
    magic, count, frame_size = struct.unpack_from("<HHI", data)
    if magic not in KNOWN_MAGICS:
        raise ValueError("Unknown PDX frame magic")
    if frame_size != suffix_start or frame_size > MAX_FRAME_SIZE:
        raise ValueError("Frame size does not match the payload boundaries")
    recipient = data[8:48].split(b"\0", 1)[0].decode("ascii")
    sequence = struct.unpack_from("<Q", data, suffix_start + 8)[0]
    source_user = data[suffix_start + 16:suffix_start + 64]
    source_user = source_user.split(b"\0", 1)[0].decode("ascii")
    messages = []
    cursor = FRAME_HEADER_SIZE
    for _ in range(count):
        if cursor + 12 > frame_size:
            raise ValueError("Truncated message header")
        record_size, word_4, word_8 = struct.unpack_from("<III", data, cursor)
        end = cursor + 4 + record_size
        if record_size < 8 or end > frame_size:
            raise ValueError("Invalid message record length")
        messages.append({
            "word_4": word_4,
            "word_8": word_8,
            "payload_hex": data[cursor + 12:end].hex(),
        })
        cursor = end
    if cursor != frame_size:
        raise ValueError("Message count leaves unparsed frame bytes")
    return {
        "opcode": GAME_DATA_OPCODE,
        "magic": hex(magic),
        "recipient": recipient,
        "sequence": sequence,
        "source_user": source_user,
        "frame_size": frame_size,
        "messages": messages,
    }


def self_test():
    """Exercise framing and corrupt-length rejection with synthetic data."""
    messages = [
        {"payload_hex": "00010203", "word_4": 5, "word_8": 1},
        {"payload_hex": "ff0041", "word_4": 5, "word_8": 0},
    ]
    for magic in KNOWN_MAGICS:
        encoded = encode_payload("recipient", messages, 123, "source-user", magic)
        decoded = decode_payload(encoded)
        assert decoded["messages"] == messages
        assert decoded["sequence"] == 123
        assert decoded["source_user"] == "source-user"
        assert decoded["recipient"] == ("recipient" if magic == 0xCAFE else "")
    encoded = encode_payload("", [], 0, "")
    assert decode_payload(encoded)["messages"] == []
    corruptions = [encoded[:-1], encoded + b"\0"]
    bad_length = bytearray(encode_payload("recipient", messages, 1, "source-user"))
    struct.pack_into("<I", bad_length, FRAME_HEADER_SIZE, 0xFFFFFFFF)
    corruptions.append(bytes(bad_length))
    for data in corruptions:
        try:
            decode_payload(data)
        except ValueError:
            continue
        raise AssertionError("Corrupted framing was accepted")
    print("Synthetic round-trip and corrupt-length checks passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    decode_command = commands.add_parser("decode")
    decode_command.add_argument("payload", type=Path)
    encode_command = commands.add_parser("encode")
    encode_command.add_argument("spec", type=Path)
    encode_command.add_argument("output", type=Path)
    commands.add_parser("self-test")
    args = parser.parse_args()
    if args.command == "self-test":
        self_test()
    elif args.command == "decode":
        print(json.dumps(decode_payload(args.payload.read_bytes()), indent=2))
    else:
        spec = json.loads(args.spec.read_text("utf-8"))
        payload = encode_payload(
            spec["recipient"], spec["messages"], spec["sequence"],
            spec["source_user"], int(spec.get("magic", "0xcafe"), 0),
        )
        args.output.write_bytes(payload)
        print(f"Wrote {len(payload)} payload bytes")


if __name__ == "__main__":
    main()
