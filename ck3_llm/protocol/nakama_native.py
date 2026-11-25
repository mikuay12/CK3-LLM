"""Python bindings for the Nakama 2.4.1 C API shipped with CK3.

All methods on a client must run on its creating thread. Call poll regularly
to dispatch SDK callbacks. Events own copies of the native callback data.
"""

import base64
import ctypes as ct
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import threading
import time

from ck3_llm.protocol.pdx_nakama_codec import GAME_DATA_OPCODE, encode_payload


DEFAULT_GAME_DIR = Path(r"C:\Games\Crusader Kings III")
SUPPORTED_SDK_VERSION = "2.4.1"


class NClientParameters(ct.Structure):
    _fields_ = [
        ("server_key", ct.c_char_p), ("host", ct.c_char_p),
        ("port", ct.c_int32), ("ssl", ct.c_bool),
    ]


class NRtClientParameters(ct.Structure):
    _fields_ = [
        ("host", ct.c_char_p), ("port", ct.c_int32), ("ssl", ct.c_bool),
    ]


class NBytes(ct.Structure):
    _fields_ = [("bytes", ct.POINTER(ct.c_uint8)), ("size", ct.c_uint32)]


class NUserPresence(ct.Structure):
    _fields_ = [
        ("user_id", ct.c_char_p), ("session_id", ct.c_char_p),
        ("username", ct.c_char_p), ("persistence", ct.c_bool),
        ("status", ct.c_char_p),
    ]


class NMatch(ct.Structure):
    _fields_ = [
        ("match_id", ct.c_char_p), ("authoritative", ct.c_bool),
        ("label", ct.c_char_p), ("size", ct.c_int32),
        ("presences", ct.POINTER(NUserPresence)),
        ("presences_count", ct.c_uint16), ("self", NUserPresence),
    ]


class NMatchData(ct.Structure):
    _fields_ = [
        ("match_id", ct.c_char_p), ("presence", NUserPresence),
        ("op_code", ct.c_int64), ("data", NBytes),
    ]


class NRtError(ct.Structure):
    _fields_ = [
        ("code", ct.c_int), ("message", ct.c_char_p), ("context", ct.c_void_p),
    ]


class NDisconnectInfo(ct.Structure):
    _fields_ = [
        ("code", ct.c_uint16), ("reason", ct.c_char_p), ("remote", ct.c_bool),
    ]


class NMatchPresenceEvent(ct.Structure):
    _fields_ = [
        ("match_id", ct.c_char_p), ("joins", ct.POINTER(NUserPresence)),
        ("joins_count", ct.c_uint16), ("leaves", ct.POINTER(NUserPresence)),
        ("leaves_count", ct.c_uint16),
    ]


CONNECT_CALLBACK = ct.CFUNCTYPE(None, ct.c_void_p)
DISCONNECT_CALLBACK = ct.CFUNCTYPE(None, ct.c_void_p, ct.POINTER(NDisconnectInfo))
ERROR_CALLBACK = ct.CFUNCTYPE(None, ct.c_void_p, ct.POINTER(NRtError))
JOIN_CALLBACK = ct.CFUNCTYPE(None, ct.c_void_p, ct.c_void_p, ct.POINTER(NMatch))
JOIN_ERROR_CALLBACK = ct.CFUNCTYPE(
    None, ct.c_void_p, ct.c_void_p, ct.POINTER(NRtError),
)
DATA_CALLBACK = ct.CFUNCTYPE(None, ct.c_void_p, ct.POINTER(NMatchData))
PRESENCE_CALLBACK = ct.CFUNCTYPE(
    None, ct.c_void_p, ct.POINTER(NMatchPresenceEvent),
)
LOG_CALLBACK = ct.CFUNCTYPE(None, ct.c_int, ct.c_char_p, ct.c_char_p)


@LOG_CALLBACK
def _discard_native_log(level, message, function):
    # SDK transport logs can contain authenticated WebSocket URLs.
    pass


def _text(value):
    return (value or b"").decode("utf-8", errors="replace")


def _cstring(value, field):
    if not isinstance(value, str) or "\0" in value:
        raise ValueError(f"{field} must be a string without NUL bytes")
    return value.encode("utf-8")


def _presence(value):
    return {
        "user_id": _text(value.user_id),
        "session_id": _text(value.session_id),
        "username": _text(value.username),
        "persistence": bool(value.persistence),
        "status": _text(value.status),
    }


def validate_token(token):
    """Validate JWT structure locally. The remote server verifies its signature."""
    if not isinstance(token, str) or any(char.isspace() for char in token):
        raise ValueError("Session token must be a single JWT string")
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError("Session token must contain three JWT segments")
    try:
        claims = json.loads(base64.b64decode(
            parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True,
        ))
    except (ValueError, UnicodeError) as error:
        raise ValueError("Session token has an invalid JWT payload") from error
    if not isinstance(claims, dict):
        raise ValueError("Session token payload must be a JSON object")
    expiry = claims.get("exp")
    if type(expiry) is not int or not 0 < expiry < (1 << 63) // 1000:
        raise ValueError("Session token requires an integer exp claim")
    if expiry <= time.time():
        raise ValueError("Session token has expired")
    if not isinstance(claims.get("uid"), str) or not claims["uid"]:
        raise ValueError("Session token requires a uid claim")
    _cstring(claims["uid"], "JWT uid")
    return claims


def load_token(path):
    content = Path(path).read_text(encoding="utf-8-sig").strip()
    if content.startswith("{"):
        content = json.loads(content).get("token")
    validate_token(content)
    return content


class NakamaSDK:
    """Load the installed DLL and declare its public C ABI."""

    def __init__(self, game_dir=DEFAULT_GAME_DIR):
        if os.name != "nt" or ct.sizeof(ct.c_void_p) != 8:
            raise RuntimeError("This binding requires 64-bit Python on Windows")
        self.dll_path = Path(game_dir).resolve() / "binaries" / "nakama-cpp.dll"
        if not self.dll_path.is_file():
            raise FileNotFoundError(f"Nakama DLL was not found at {self.dll_path}")
        self._dll_directory = os.add_dll_directory(str(self.dll_path.parent))
        self.dll = ct.CDLL(str(self.dll_path))
        self._bind("getNakamaSdkVersion", [], ct.c_char_p)
        self.version = _text(self.dll.getNakamaSdkVersion())
        if self.version != SUPPORTED_SDK_VERSION:
            raise RuntimeError(f"Unsupported SDK version {self.version!r}, expected 2.4.1")
        self._bind("NLogger_init", [LOG_CALLBACK, ct.c_int])
        self.dll.NLogger_init(_discard_native_log, 4)
        self._bind("createDefaultNakamaClient", [ct.POINTER(NClientParameters)], ct.c_void_p)
        self._bind("destroyNakamaClient", [ct.c_void_p])
        self._bind("NClient_createRtClientEx", [ct.c_void_p, ct.POINTER(NRtClientParameters)], ct.c_void_p)
        self._bind("restoreNakamaSession", [ct.c_char_p], ct.c_void_p)
        self._bind("NSession_destroy", [ct.c_void_p])
        self._bind("NSession_getUserId", [ct.c_void_p], ct.c_char_p)
        self._bind("NSession_getUsername", [ct.c_void_p], ct.c_char_p)
        self._bind("NRtClient_connect", [ct.c_void_p, ct.c_void_p, ct.c_bool, ct.c_int])
        self._bind("NRtClient_isConnected", [ct.c_void_p], ct.c_bool)
        self._bind("NRtClient_tick", [ct.c_void_p])
        self._bind("NRtClient_disconnect", [ct.c_void_p])
        self._bind("NRtClient_destroy", [ct.c_void_p])
        self._bind("NRtClient_setConnectCallback", [ct.c_void_p, CONNECT_CALLBACK])
        self._bind("NRtClient_setDisconnectCallback", [ct.c_void_p, DISCONNECT_CALLBACK])
        self._bind("NRtClient_setErrorCallback", [ct.c_void_p, ERROR_CALLBACK])
        self._bind("NRtClient_setMatchDataCallback", [ct.c_void_p, DATA_CALLBACK])
        self._bind("NRtClient_setMatchPresenceCallback", [ct.c_void_p, PRESENCE_CALLBACK])
        self._bind("NRtClient_joinMatch", [
            ct.c_void_p, ct.c_char_p, ct.c_void_p, ct.c_void_p,
            JOIN_CALLBACK, JOIN_ERROR_CALLBACK,
        ])
        self._bind("NRtClient_sendMatchData", [
            ct.c_void_p, ct.c_char_p, ct.c_int64, ct.POINTER(NBytes),
            ct.POINTER(NUserPresence), ct.c_uint16,
        ])
        self._bind("NStringMap_create", [], ct.c_void_p)
        self._bind("NStringMap_setValue", [ct.c_void_p, ct.c_char_p, ct.c_char_p])
        self._bind("NStringMap_destroy", [ct.c_void_p])

    def _bind(self, name, arguments, result=None):
        function = getattr(self.dll, name)
        function.argtypes = arguments
        function.restype = result


class NakamaClient:
    """Standalone Nakama room transport with copied, structured events.

    Joining here completes the Nakama room layer. CK3's Jomini login,
    character selection and snapshot protocol require further implementation.
    """

    def __init__(self, sdk, host, port, ssl, token, protocol="protobuf"):
        validate_token(token)
        if protocol not in ("protobuf", "json"):
            raise ValueError("Protocol must be protobuf or json")
        if not 1 <= port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        host_bytes = _cstring(host, "Host")
        if not host or any(char in host for char in "/?#@"):
            raise ValueError("Host must contain a hostname without a URL scheme or path")
        self.sdk = sdk
        self._dll = sdk.dll
        self._thread_id = threading.get_ident()
        self._events = queue.SimpleQueue()
        self._client = None
        self._rt = None
        self._session = None
        self._connect_started = False
        self.connected = False
        self.rooms = set()
        self._sequence = 0
        self.protocol = protocol
        self._callbacks = {
            "connect": CONNECT_CALLBACK(self._guard(self._on_connect)),
            "disconnect": DISCONNECT_CALLBACK(self._guard(self._on_disconnect)),
            "error": ERROR_CALLBACK(self._guard(self._on_error)),
            "joined": JOIN_CALLBACK(self._guard(self._on_joined)),
            "join_error": JOIN_ERROR_CALLBACK(self._guard(self._on_join_error)),
            "data": DATA_CALLBACK(self._guard(self._on_data)),
            "presence": PRESENCE_CALLBACK(self._guard(self._on_presence)),
        }
        try:
            self._session = self._dll.restoreNakamaSession(token.encode("ascii"))
            parameters = NClientParameters(b"", host_bytes, port, ssl)
            self._client = self._dll.createDefaultNakamaClient(ct.byref(parameters))
            if not self._session or not self._client:
                raise RuntimeError("The SDK could not create a session or client")
            rt_parameters = NRtClientParameters(host_bytes, port, ssl)
            self._rt = self._dll.NClient_createRtClientEx(self._client, ct.byref(rt_parameters))
            if not self._rt:
                raise RuntimeError("The SDK could not create a realtime client")
            self.user_id = _text(self._dll.NSession_getUserId(self._session))
            self.username = _text(self._dll.NSession_getUsername(self._session))
            for name, function in (
                ("connect", "NRtClient_setConnectCallback"),
                ("disconnect", "NRtClient_setDisconnectCallback"),
                ("error", "NRtClient_setErrorCallback"),
                ("data", "NRtClient_setMatchDataCallback"),
                ("presence", "NRtClient_setMatchPresenceCallback"),
            ):
                getattr(self._dll, function)(self._rt, self._callbacks[name])
        except BaseException:
            self.close()
            raise

    def _guard(self, handler):
        def callback(*args):
            try:
                handler(*args)
            except Exception as error:
                self._emit("callback_error", message=str(error))
        return callback

    def _emit(self, event, **values):
        self._events.put({
            "event": event,
            "time": datetime.now(timezone.utc).isoformat(),
            **values,
        })

    def _check_thread(self):
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("Use the client from its creating thread")

    def _check_open(self):
        self._check_thread()
        if not self._rt:
            raise RuntimeError("Client is closed")

    def connect(self):
        self._check_open()
        if self._connect_started:
            raise RuntimeError("Create a new client for another connection attempt")
        self._connect_started = True
        self._dll.NRtClient_connect(
            self._rt, self._session, False, int(self.protocol == "json"),
        )

    def join(self, match_id, metadata=None):
        self._check_open()
        if not self.connected:
            raise RuntimeError("Connect before joining a room")
        match_bytes = _cstring(match_id, "Match ID")
        if not match_id:
            raise ValueError("Match ID is empty")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("Metadata must be a dictionary of strings")
        entries = [(_cstring(key, "Metadata key"), _cstring(value, "Metadata value"))
                   for key, value in (metadata or {}).items()]
        native_map = self._dll.NStringMap_create()
        if not native_map:
            raise RuntimeError("The SDK could not allocate metadata")
        try:
            for key, value in entries:
                self._dll.NStringMap_setValue(native_map, key, value)
            self._dll.NRtClient_joinMatch(
                self._rt, match_bytes, native_map, None,
                self._callbacks["joined"], self._callbacks["join_error"],
            )
        finally:
            self._dll.NStringMap_destroy(native_map)

    def send_raw(self, match_id, opcode, payload, presences=None):
        self._check_open()
        if not self.connected or match_id not in self.rooms:
            raise RuntimeError("Join this room before sending data")
        if type(opcode) is not int or not -(1 << 63) <= opcode < (1 << 63):
            raise ValueError("Opcode must fit a signed 64-bit integer")
        payload = bytes(payload)
        if len(payload) > 0xFFFFFFFF:
            raise ValueError("Payload is too large for the SDK C API")
        targets = presences or []
        if len(targets) > 0xFFFF:
            raise ValueError("Too many target presences")
        target_data = (NUserPresence * len(targets))()
        for index, target in enumerate(targets):
            target_data[index] = NUserPresence(
                _cstring(target["user_id"], "Target user ID"),
                _cstring(target["session_id"], "Target session ID"),
                _cstring(target.get("username", ""), "Target username"),
                False, b"",
            )
        buffer = (ct.c_uint8 * len(payload)).from_buffer_copy(payload)
        native_data = NBytes(buffer, len(payload))
        self._dll.NRtClient_sendMatchData(
            self._rt, _cstring(match_id, "Match ID"), opcode,
            ct.byref(native_data), target_data, len(targets),
        )
        self._emit("send_submitted", match_id=match_id, opcode=opcode, payload=payload)

    def send_envelope(self, match_id, recipient, messages, magic=0xCAFE, presences=None):
        payload = encode_payload(
            recipient, messages, self._sequence, self.user_id, magic,
        )
        self.send_raw(match_id, GAME_DATA_OPCODE, payload, presences)
        self._sequence += 1
        return payload

    def poll(self):
        self._check_open()
        self._dll.NRtClient_tick(self._rt)
        events = []
        while not self._events.empty():
            events.append(self._events.get_nowait())
        return events

    def close(self):
        self._check_thread()
        if self._rt:
            self._dll.NRtClient_disconnect(self._rt)
            self._dll.NRtClient_destroy(self._rt)
            self._rt = None
        if self._client:
            self._dll.destroyNakamaClient(self._client)
            self._client = None
        if self._session:
            self._dll.NSession_destroy(self._session)
            self._session = None
        self.connected = False
        self.rooms.clear()

    def _on_connect(self, client):
        self.connected = True
        self._emit("connected", protocol=self.protocol, user_id=self.user_id)

    def _on_disconnect(self, client, pointer):
        info = pointer.contents
        self.connected = False
        self.rooms.clear()
        self._emit("disconnected", code=info.code, reason=_text(info.reason), remote=bool(info.remote))

    def _on_error(self, client, pointer):
        error = pointer.contents
        self._emit("error", code=error.code, message=_text(error.message))

    def _on_join_error(self, client, request, pointer):
        error = pointer.contents
        self._emit("join_error", code=error.code, message=_text(error.message))

    def _on_joined(self, client, request, pointer):
        match = pointer.contents
        match_id = _text(match.match_id)
        self.rooms.add(match_id)
        self._emit(
            "room_joined", match_id=match_id, authoritative=bool(match.authoritative),
            size=match.size, self_presence=_presence(match.self),
            presences=[_presence(match.presences[index])
                       for index in range(match.presences_count)],
        )

    def _on_data(self, client, pointer):
        data = pointer.contents
        payload = ct.string_at(data.data.bytes, data.data.size)
        self._emit(
            "match_data", match_id=_text(data.match_id), opcode=data.op_code,
            presence=_presence(data.presence), payload=payload,
        )

    def _on_presence(self, client, pointer):
        event = pointer.contents
        self._emit(
            "match_presence", match_id=_text(event.match_id),
            joins=[_presence(event.joins[index]) for index in range(event.joins_count)],
            leaves=[_presence(event.leaves[index]) for index in range(event.leaves_count)],
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
