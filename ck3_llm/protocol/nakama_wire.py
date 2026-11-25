"""Shared Nakama JSON/Protobuf and WebSocket framing for local room services."""

import base64
import json
import struct

# Field numbers from Nakama C++ v2.4.1's generated realtime.pb.h.
# The local service relays game data and handles room/presence controls.
_SCHEMAS = {
    "envelope": {
        1: ("cid", "str"), 11: ("error", "error"), 12: ("match", "match"),
        14: ("match_data", "match_data"), 15: ("match_data_send", "match_data_send"),
        16: ("match_join", "match_join"), 17: ("match_leave", "match_leave"),
        18: ("match_presence_event", "presence_event"),
        24: ("rpc", "rpc"),
        32: ("ping", "empty"), 33: ("pong", "empty"),
    },
    "error": {1: ("code", "int"), 2: ("message", "str")},
    "match": {
        1: ("match_id", "str"), 2: ("authoritative", "int"), 4: ("size", "int"),
        5: ("presences", "presence[]"), 6: ("self", "presence"),
    },
    "presence": {
        1: ("user_id", "str"), 2: ("session_id", "str"), 3: ("username", "str"),
    },
    "match_data": {
        1: ("match_id", "str"), 2: ("presence", "presence"),
        3: ("op_code", "int"), 4: ("data", "bytes"),
    },
    "match_data_send": {
        1: ("match_id", "str"), 2: ("op_code", "int"), 3: ("data", "bytes"),
        4: ("presences", "presence[]"),
    },
    "match_join": {1: ("match_id", "str"), 3: ("metadata", "map")},
    "match_leave": {1: ("match_id", "str")},
    "rpc": {1: ("id", "str"), 2: ("payload", "str"), 3: ("http_key", "str")},
    "empty": {},
    "map_entry": {1: ("key", "str"), 2: ("value", "str")},
    "presence_event": {
        1: ("match_id", "str"), 2: ("joins", "presence[]"), 3: ("leaves", "presence[]"),
    },
}


def _varint(value):
    value &= (1 << 64) - 1
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _read_varint(data, position):
    result = 0
    for shift in range(0, 70, 7):
        if position >= len(data):
            raise ValueError("Truncated test Protobuf varint")
        byte = data[position]
        position += 1
        result |= (byte & 127) << shift
        if not byte & 128:
            return result, position
    raise ValueError("Oversized test Protobuf varint")


def _encode_protobuf(schema, message):
    result = bytearray()
    for number, (name, kind) in _SCHEMAS[schema].items():
        if name not in message:
            continue
        value = message[name]
        if kind == "map":
            values = [{"key": key, "value": item} for key, item in value.items()]
            kind = "map_entry"
        elif kind.endswith("[]"):
            values = value
            kind = kind[:-2]
        else:
            values = [value]
        for item in values:
            if kind == "int":
                result.extend(_varint(number << 3) + _varint(int(item)))
                continue
            if kind == "str":
                encoded = item.encode("utf-8")
            elif kind == "bytes":
                encoded = base64.b64decode(item)
            else:
                encoded = _encode_protobuf(kind, item)
            result.extend(_varint((number << 3) | 2) + _varint(len(encoded)) + encoded)
    return bytes(result)


def _decode_protobuf(schema, data):
    result = {}
    position = 0
    while position < len(data):
        tag, position = _read_varint(data, position)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, position = _read_varint(data, position)
        elif wire == 2:
            size, position = _read_varint(data, position)
            end = position + size
            if end > len(data):
                raise ValueError("Truncated test Protobuf field")
            value, position = data[position:end], end
        elif wire in (1, 5):
            position += 8 if wire == 1 else 4
            if position > len(data):
                raise ValueError("Truncated test Protobuf fixed field")
            continue
        else:
            raise ValueError("Unsupported test Protobuf wire type")
        if number not in _SCHEMAS[schema]:
            continue
        name, kind = _SCHEMAS[schema][number]
        if kind == "map":
            entry = _decode_protobuf("map_entry", value)
            result.setdefault(name, {})[entry["key"]] = entry.get("value", "")
            continue
        repeated = kind.endswith("[]")
        kind = kind.removesuffix("[]")
        if kind == "str":
            value = value.decode("utf-8")
        elif kind == "bytes":
            value = base64.b64encode(value).decode("ascii")
        elif kind != "int":
            value = _decode_protobuf(kind, value)
        if repeated:
            result.setdefault(name, []).append(value)
        else:
            result[name] = value
    return result


def _snake_keys(value):
    if isinstance(value, list):
        return [_snake_keys(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        snake = "".join("_" + char.lower() if char.isupper() else char for char in key)
        result[snake] = item if snake == "metadata" else _snake_keys(item)
    return result


def _read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        chunk = stream.read(size - len(result))
        if not chunk:
            raise EOFError("WebSocket connection closed")
        result.extend(chunk)
    return bytes(result)


class WebSocketReader:
    """Assemble client messages while preserving interleaved control frames."""

    def __init__(self, stream):
        self.stream = stream
        self.opcode = None
        self.fragments = bytearray()

    def _frame(self):
        first, second = _read_exact(self.stream, 2)
        final = bool(first & 0x80)
        opcode = first & 15
        if first & 0x70:
            raise ValueError("WebSocket extensions were not negotiated")
        size = second & 127
        if size == 126:
            size = struct.unpack("!H", _read_exact(self.stream, 2))[0]
        elif size == 127:
            size = struct.unpack("!Q", _read_exact(self.stream, 8))[0]
        if size > 2 << 20:
            raise ValueError("WebSocket frame exceeds the game envelope capacity")
        if opcode >= 8 and (not final or size > 125):
            raise ValueError("Invalid WebSocket control frame")
        if not second & 128:
            raise ValueError("Client WebSocket frame must be masked")
        mask = _read_exact(self.stream, 4)
        payload = _read_exact(self.stream, size)
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return final, opcode, payload

    def read(self):
        while True:
            final, opcode, payload = self._frame()
            if opcode in (8, 9, 10):
                return opcode, payload
            if opcode in (1, 2):
                if self.opcode is not None:
                    raise ValueError("A fragmented WebSocket message is already in progress")
                if final:
                    return opcode, payload
                self.opcode = opcode
            elif opcode != 0 or self.opcode is None:
                raise ValueError("Unexpected WebSocket continuation or opcode")
            self.fragments.extend(payload)
            if len(self.fragments) > 2 << 20:
                raise ValueError("WebSocket message exceeds the game envelope capacity")
            if final:
                result = self.opcode, bytes(self.fragments)
                self.opcode = None
                self.fragments.clear()
                return result


def _write_frame(stream, opcode, payload):
    header = bytes([128 | opcode])
    if len(payload) < 126:
        header += bytes([len(payload)])
    elif len(payload) < 65536:
        header += b"\x7e" + struct.pack("!H", len(payload))
    else:
        header += b"\x7f" + struct.pack("!Q", len(payload))
    stream.write(header + payload)
    stream.flush()


