"""Loopback-only Nakama test peer for the standalone client prototype.

Implements a small JSON/Protobuf realtime subset and WebSocket framing.
All identities, rooms and game payloads produced here are synthetic.
The production client uses the shipped SDK for the full transport protocol.
"""

import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import struct
import threading
import time
import io
import zipfile
from urllib.parse import parse_qs, urlsplit

from ck3_llm.protocol.pdx_nakama_codec import encode_payload, decode_payload
from ck3_llm.protocol.jomini_codec import JominiCodec, _property, _scalar, _word, OPEN, CLOSE, U32, STRING, BOOL
from ck3_llm.protocol.session_codec import encode_session_message
from ck3_llm.protocol.transfer_protocol import decode_chunk_bitmap
from ck3_llm.protocol.snapshot_transfer import TransferChecksum
from ck3_llm.protocol.nakama_wire import _encode_protobuf, _decode_protobuf, _snake_keys, WebSocketReader, _write_frame


MOCK_ROOM = "00000000-0000-0000-0000-000000000001.mock"
MOCK_USER = "00000000-0000-0000-0000-000000000002"
MOCK_HOST = "00000000-0000-0000-0000-000000000003"
SELF_PRESENCE = {"user_id": MOCK_USER, "session_id": "mock-session-b", "username": "Mock B"}
HOST_PRESENCE = {"user_id": MOCK_HOST, "session_id": "mock-session-a", "username": "Mock A"}
def make_world_fixture(*, character=None):
    """Small tokenized world used by the loopback demo and parser regressions."""
    tokens = {name: token for token, name in JominiCodec().tokens.items()}

    def value_bytes(value):
        if isinstance(value, dict):
            properties = b"".join(
                (_scalar(U32, key) if type(key) is int else _word(tokens[key]))
                + _word(1) + value_bytes(item) for key, item in value.items()
            )
            return _word(OPEN) + properties + _word(CLOSE)
        if isinstance(value, list):
            return _word(OPEN) + b"".join(value_bytes(item) for item in value) + _word(CLOSE)
        if isinstance(value, str):
            return _scalar(STRING, value)
        return _scalar(BOOL if type(value) is bool else U32, value)

    fields = {
        "meta_data": {"version": "1.20.1.2", "meta_player_name": "Synthetic AI"},
        "date": 51571296, "speed": 1, "currently_played_characters": [12345],
        "living": {12345: {"first_name": "Synthetic AI", "skill": [6, 8, 6, 3, 8, 5],
                            "alive_data": {"stress": 0}, **(character or {})}},
        "provinces": {1: {"fort_level": 2}},
        "landed_titles": {"landed_titles": {1: {"key": "c_fixture", "holder": 12345}}},
    }
    world = b"".join(_property(tokens[key], value_bytes(value)) for key, value in fields.items())
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        entry = zipfile.ZipInfo("hotjoin_0", date_time=(1980, 1, 1, 0, 0, 0))
        entry.compress_type = zipfile.ZIP_DEFLATED
        output.writestr(entry, world)
    return world, archive.getvalue()


_, SAMPLE_TRANSFER = make_world_fixture()
_sample_checksum = TransferChecksum()
_sample_checksum.update(SAMPLE_TRANSFER)
SAMPLE_TRANSFER_CHECKSUM = _sample_checksum.value
SAMPLE_PAYLOAD = encode_payload(
    MOCK_USER, [{"payload_hex": JominiCodec().encode_command(
        "set_ready_command", {"machineid": 0, "status": True},
        {"identity": 42, "sender": 0, "receiver": 1},
    ).hex(), "word_8": 0},
    {"word_8": 1, "payload_hex": encode_session_message("ServerRequestAddPeer", {
        "peer_id": 1, "peer_value": 123, "assign_local": 1,
    }).hex()},
    {"word_8": 0, "payload_hex": JominiCodec().encode_command(
        "sync_players_command",
        {"machineid": [0, 1], "players": ["Mock A", "Mock B"],
         "character": [12344, 12345], "flags": [0, 8]},
        {"identity": 43, "sender": 0, "receiver": 1},
    ).hex()}],
    42, MOCK_HOST,
)


def make_mock_token():
    def segment(value):
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(encoded).rstrip(b"=")
    content = segment({"alg": "HS256", "typ": "JWT"}) + b"." + segment({
        "uid": MOCK_USER, "usn": "Mock B", "exp": int(time.time()) + 3600,
    })
    signature = hmac.new(b"ck3-loopback-test-only", content, hashlib.sha256).digest()
    return (content + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode("ascii")


class _MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        peer = self.server.peer
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if (parsed.path != "/ws" or peer.mode == "reject_connect"
                or query.get("token") != [peer.token]):
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii"),
        ).digest()).decode("ascii")
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.binary = query.get("format") == ["protobuf"]
        peer.connections.append(self.connection)
        self.close_connection = True
        try:
            self._serve_messages(peer)
        except (EOFError, ConnectionError, OSError):
            pass
        except Exception as error:
            peer.errors.append(str(error))

    def _send(self, message):
        if self.binary:
            _write_frame(self.wfile, 2, _encode_protobuf("envelope", message))
        else:
            _write_frame(self.wfile, 1, json.dumps(message).encode("utf-8"))

    def _serve_messages(self, peer):
        joined = False
        reader = WebSocketReader(self.rfile)
        while True:
            opcode, payload = reader.read()
            if opcode == 8:
                _write_frame(self.wfile, 8, payload)
                return
            if opcode == 9:
                _write_frame(self.wfile, 10, payload)
                continue
            if opcode == 10:
                continue
            if opcode != (2 if self.binary else 1):
                raise ValueError("Unexpected test WebSocket message type")
            if self.binary:
                message = _decode_protobuf("envelope", payload)
            else:
                message = _snake_keys(json.loads(payload))
            peer.received.append(message)
            if "match_join" in message:
                if peer.mode == "silent_join":
                    continue
                if peer.mode == "reject_join" or message["match_join"].get("match_id") != MOCK_ROOM:
                    self._send({"cid": message["cid"], "error": {"code": 4, "message": "Mock room not found"}})
                    continue
                joined = True
                self._send({"cid": message["cid"], "match": {
                    "match_id": MOCK_ROOM, "authoritative": False, "size": 2,
                    "presences": [HOST_PRESENCE], "self": SELF_PRESENCE,
                }})
                self._send({"match_presence_event": {
                    "match_id": MOCK_ROOM, "joins": [HOST_PRESENCE], "leaves": [],
                }})
                self._send({"match_data": {
                    "match_id": MOCK_ROOM, "presence": HOST_PRESENCE, "op_code": "255",
                    "data": base64.b64encode(SAMPLE_PAYLOAD).decode("ascii"),
                }})
                if peer.mode in ("normal", "malformed_data"):
                    codec = JominiCodec()
                    transfer_fields = {
                        "filename": "synthetic-fixture.bin", "size": len(SAMPLE_TRANSFER),
                        "checksum": SAMPLE_TRANSFER_CHECKSUM,
                    }
                    for index, chunk_id in enumerate((1, 0, 1)):
                        chunk = SAMPLE_TRANSFER[20:] if chunk_id else SAMPLE_TRANSFER[:20]
                        game_data = codec.encode_command("filetransfer_fast_cmd_chunk", {
                            **transfer_fields, "chunk_id": chunk_id, "data": {"hex": chunk.hex()},
                        }, {"identity": 44 + index, "sender": 0, "receiver": 1})
                        packet = encode_payload(MOCK_USER, [{"payload_hex": game_data.hex()}], 43 + index, MOCK_HOST)
                        self._send({"match_data": {
                            "match_id": MOCK_ROOM, "presence": HOST_PRESENCE, "op_code": "255",
                            "data": base64.b64encode(packet).decode("ascii"),
                        }})
                if peer.mode == "malformed_data":
                    self._send({"match_data": {
                        "match_id": MOCK_ROOM, "presence": HOST_PRESENCE, "op_code": "255",
                        "data": base64.b64encode(b"invalid PDX frame").decode("ascii"),
                    }})
                if peer.mode == "close_after_join":
                    _write_frame(self.wfile, 8, struct.pack("!H", 1000) + b"Mock remote close")
            elif "match_data_send" in message and joined:
                data = message["match_data_send"]
                if peer.mode == "transfer_protocol" and self._handle_transfer_send(data):
                    continue
                self._send({"match_data": {
                    "match_id": MOCK_ROOM, "presence": HOST_PRESENCE,
                    "op_code": data.get("op_code", 0), "data": data.get("data", ""),
                }})

    def _transfer_fields(self):
        return {"filename": "hotjoin_0", "size": len(SAMPLE_TRANSFER),
                "checksum": SAMPLE_TRANSFER_CHECKSUM}

    def _send_transfer_command(self, name, fields):
        peer = self.server.peer
        peer.transfer_sequence += 1
        payload = JominiCodec().encode_command(name, fields, {
            "identity": peer.transfer_sequence, "sender": 0, "receiver": 1, "attribs": 47,
        })
        packet = encode_payload(MOCK_USER, [{"word_8": 0, "payload_hex": payload.hex()}], peer.transfer_sequence, MOCK_HOST)
        self._send({"match_data": {
            "match_id": MOCK_ROOM, "presence": HOST_PRESENCE, "op_code": "255",
            "data": base64.b64encode(packet).decode("ascii"),
        }})

    def _send_transfer_chunk(self, chunk):
        start = chunk * 64
        self._send_transfer_command("filetransfer_fast_cmd_chunk", {
            **self._transfer_fields(), "chunk_id": chunk,
            "data": {"hex": SAMPLE_TRANSFER[start:start + 64].hex()},
        })

    def _send_session_command(self, name, fields):
        peer = self.server.peer
        peer.transfer_sequence += 1
        packet = encode_payload(MOCK_USER, [{
            "word_8": 1, "payload_hex": encode_session_message(name, fields).hex(),
        }], peer.transfer_sequence, MOCK_HOST)
        self._send({"match_data": {
            "match_id": MOCK_ROOM, "presence": HOST_PRESENCE, "op_code": "255",
            "data": base64.b64encode(packet).decode("ascii"),
        }})

    def _handle_transfer_send(self, data):
        if int(data.get("op_code", 0)) != 255:
            return False
        peer = self.server.peer
        codec = JominiCodec()
        envelope = decode_payload(base64.b64decode(data["data"]))
        for record in envelope["messages"]:
            command = codec.decode_pdx_message(record, from_server=False)
            name = command["type"]
            if name == "ClientRespondAddPeer":
                peer.peer_acknowledged = True
            elif name == "ClientRequestConnected":
                if not peer.peer_acknowledged:
                    raise ValueError("Test peer received Connected before the peer-registration reply")
                peer.session_connected = True
                self._send_session_command("ServerRespondConnected", {})
                self._send_transfer_command("filetransfer_init_command", self._transfer_fields())
            elif name == "filetransfer_fast_cmd_request_handshake":
                if not peer.session_connected:
                    raise ValueError("Test peer received a transfer request before session connection")
                self._send_transfer_command("filetransfer_fast_cmd_handshake", {
                    **self._transfer_fields(), "filetransfer_fast_fld_chunk_size": 64,
                })
            elif name == "filetransfer_fast_cmd_initialize":
                count = (len(SAMPLE_TRANSFER) + 63) // 64
                for chunk in reversed(range(count)):
                    if chunk != 1:
                        self._send_transfer_chunk(chunk)
            elif name == "filetransfer_fast_cmd_chunkstatus":
                count = (len(SAMPLE_TRANSFER) + 63) // 64
                received = decode_chunk_bitmap(bytes.fromhex(command["fields"]["data"]["hex"]), count)
                missing = set(range(count)) - received
                if not missing:
                    peer.transfer_complete = True
                    return False  # Echo only the final bitmap to complete the demo.
                peer.transfer_retransmissions += len(missing)
                for chunk in sorted(missing):
                    self._send_transfer_chunk(chunk)
            else:
                return False
        return True


class MockNakamaServer:
    def __init__(self, mode="normal"):
        self.token = make_mock_token()
        self.mode = mode
        self.received = []
        self.errors = []
        self.connections = []
        self.transfer_sequence = 100
        self.transfer_complete = False
        self.transfer_retransmissions = 0
        self.peer_acknowledged = False
        self.session_connected = False
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
        self._server.daemon_threads = True
        self._server.peer = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
        )

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._server.shutdown()
        for connection in self.connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._server.server_close()
        self._thread.join(timeout=2)
