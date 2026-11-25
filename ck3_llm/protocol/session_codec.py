"""Directional Jomini session controls recovered from CK3 1.20.1.2.

Native senders allocate more bytes than their packed fields consume. Encoders
zero-fill that unused tail by default. Decoders preserve its exact bytes.
Compatibility values and password digests remain caller-supplied byte fields.
"""

import struct


MAX_SESSION_SIZE = (1 << 20) - 60

# type ID, direction, native allocation size before variable string data, fields
SESSION_SCHEMAS = {
    "ClientRequestHandshake": {
        "type_id": 1, "from_server": False, "native_size": 90,
        "vtable_rva": 0x4952558, "write_rva": 0x380CA70,
        "fields": [
            ("name", "string"), ("compatibility_a_hex", 16),
            ("compatibility_b_hex", 16), ("compatibility_c_hex", 33),
            ("handshake_flag", "u8"), ("platform", "u32"),
        ],
    },
    "ClientRequestConnected": {
        "type_id": 2, "from_server": False, "native_size": 16,
        "vtable_rva": 0x4952498, "fields": [],
    },
    "ClientRequestDisconnect": {
        "type_id": 3, "from_server": False, "native_size": 24,
        "vtable_rva": 0x4952570, "fields": [("reason", "u32")],
    },
    "ClientRespondPassword": {
        "type_id": 4, "from_server": False, "native_size": 36,
        "vtable_rva": 0x4952468, "write_rva": 0x380CD70,
        "fields": [("name", "string"), ("password_digest_hex", 16)],
    },
    "ClientRespondAddPeer": {
        "type_id": 5, "from_server": False, "native_size": 16,
        "vtable_rva": 0x4952480, "fields": [],
    },
    "ClientRespondDisconnect": {
        "type_id": 6, "from_server": False, "native_size": 24,
        "vtable_rva": 0x49524C8, "fields": [("reason", "u32")],
    },
    "ServerRequestPassword": {
        "type_id": 1, "from_server": True, "native_size": 16,
        "vtable_rva": 0x4952540, "fields": [],
    },
    "ServerRequestAddPeer": {
        "type_id": 2, "from_server": True, "native_size": 32,
        "vtable_rva": 0x4952528,
        "fields": [("peer_id", "u32"), ("peer_value", "u32"), ("assign_local", "u8")],
    },
    "ServerRequestRemovePeer": {
        "type_id": 3, "from_server": True, "native_size": 32,
        "vtable_rva": 0x49524B0, "write_rva": 0x380CF00,
        "fields": [("peer_id", "u32"), ("peer_value", "u32"), ("remove_flag", "u32")],
    },
    "ServerRequestDisconnect": {
        "type_id": 4, "from_server": True, "native_size": 24,
        "vtable_rva": 0x49524F8, "fields": [("reason", "u32")],
    },
    "ServerRespondHandshake": {
        "type_id": 5, "from_server": True, "native_size": 24,
        "vtable_rva": 0x4952510, "fields": [("result_code", "u32")],
    },
    "ServerRespondConnected": {
        "type_id": 6, "from_server": True, "native_size": 16,
        "vtable_rva": 0x49524E0, "fields": [],
    },
    "ServerRespondDisconnect": {
        "type_id": 7, "from_server": True, "native_size": 24,
        "vtable_rva": 0x49524F8, "fields": [("reason", "u32")],
    },
}
CLIENT_SESSION_TYPES = {row["type_id"]: name for name, row in SESSION_SCHEMAS.items()
                        if not row["from_server"]}
SERVER_SESSION_TYPES = {row["type_id"]: name for name, row in SESSION_SCHEMAS.items()
                        if row["from_server"]}


def _encode_field(tag, value):
    if tag == "string":
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("Session names require strings without embedded NUL bytes")
        raw = value.encode("utf-8") + b"\0" if value else b""
        if len(raw) > MAX_SESSION_SIZE:
            raise ValueError("Session name exceeds the PDX frame buffer size")
        return struct.pack("<I", len(raw)) + raw
    if type(tag) is int:
        if not isinstance(value, str):
            raise ValueError("Fixed session fields require hex strings")
        raw = bytes.fromhex(value)
        if len(raw) != tag:
            raise ValueError(f"Session field requires exactly {tag} bytes")
        return raw
    limit = 256 if tag == "u8" else 1 << 32
    if type(value) is not int or not 0 <= value < limit:
        raise ValueError(f"Session {tag} field is outside its unsigned integer range")
    return struct.pack("<B" if tag == "u8" else "<I", value)


def encode_session_message(name, fields, *, trailing_hex=None):
    if name not in SESSION_SCHEMAS:
        raise ValueError("Unsupported session control encoder")
    schema = SESSION_SCHEMAS[name]
    if not isinstance(fields, dict) or set(fields) != {row[0] for row in schema["fields"]}:
        raise ValueError("Session fields must exactly match the selected message schema")
    body = bytearray()
    variable_size = 0
    for field, tag in schema["fields"]:
        value = _encode_field(tag, fields[field])
        body.extend(value)
        if tag == "string":
            variable_size += len(value) - 4
    if trailing_hex is None:
        size = schema["native_size"] + variable_size
        body.extend(bytes(size - 5 - len(body)))
    else:
        if not isinstance(trailing_hex, str):
            raise ValueError("Session trailing_hex requires a hex string")
        body.extend(bytes.fromhex(trailing_hex))
        size = 5 + len(body)
    if size > MAX_SESSION_SIZE:
        raise ValueError("Session control exceeds the PDX frame buffer size")
    return struct.pack("<BI", schema["type_id"], size) + body


def decode_session_message(payload, *, from_server=True):
    if len(payload) < 5:
        raise ValueError("Session control message requires a 5-byte header")
    if len(payload) > MAX_SESSION_SIZE:
        raise ValueError("Session control exceeds the PDX frame buffer size")
    message_type, declared_size = struct.unpack_from("<BI", payload)
    if declared_size != len(payload):
        raise ValueError("Session message size does not match its header")
    types = SERVER_SESSION_TYPES if from_server else CLIENT_SESSION_TYPES
    name = types.get(message_type, "unknown")
    result = {
        "type": name, "type_id": message_type, "declared_size": declared_size,
        "body_hex": payload[5:].hex(),
    }
    if name == "unknown":
        return result
    fields = {}
    raw_strings = {}
    position = 5
    for field, tag in SESSION_SCHEMAS[name]["fields"]:
        if tag == "string":
            if position + 4 > len(payload):
                raise ValueError(f"Truncated {name}.{field} length")
            size = struct.unpack_from("<I", payload, position)[0]
            position += 4
        else:
            size = tag if type(tag) is int else (1 if tag == "u8" else 4)
        end = position + size
        if end > len(payload):
            raise ValueError(f"Truncated {name}.{field}")
        raw = payload[position:end]
        position = end
        if tag == "string":
            if raw and raw[-1] != 0:
                raise ValueError(f"Missing NUL terminator in {name}.{field}")
            fields[field] = raw[:-1].decode("utf-8", errors="replace") if raw else ""
            raw_strings[field] = raw.hex()
        elif type(tag) is int:
            fields[field] = raw.hex()
        else:
            fields[field] = int.from_bytes(raw, "little")
    result.update(fields=fields, trailing_hex=payload[position:].hex())
    if raw_strings:
        result["raw_strings"] = raw_strings
    # Preserve the existing public result-code field.
    if name == "ServerRespondHandshake":
        result["result_code"] = fields["result_code"]
    return result


def session_responses(recorded):
    """Reply to native peer registration and host-requested disconnection.

    Registering any peer gets ClientRespondAddPeer. Assignment of our own ID
    also gets ClientRequestConnected. Initial compatibility negotiation is
    separate and remains caller-supplied.
    """
    if (recorded.get("event") != "match_data" or recorded.get("opcode") != 255
            or recorded.get("routing_ignored")):
        return []
    presence = recorded.get("presence") or {}
    source = presence.get("user_id")
    if not source:
        return []
    result = []
    for message in recorded.get("pdx", {}).get("messages", []):
        control = message.get("jomini", {})
        if message.get("state_error") or message.get("state_ignored"):
            continue
        if control.get("type") == "ServerRequestDisconnect":
            result.append({"command": "send_session_message", "type": "ClientRespondDisconnect",
                           "fields": {"reason": control["fields"]["reason"]},
                           "recipient": source, "presences": [presence]})
            continue
        if control.get("type") != "ServerRequestAddPeer":
            continue
        names = ["ClientRespondAddPeer"]
        if control["fields"]["assign_local"]:
            names.append("ClientRequestConnected")
        for name in names:
            result.append({
                "command": "send_session_message", "type": name, "fields": {},
                "recipient": source, "presences": [presence],
            })
    return result
