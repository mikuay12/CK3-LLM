"""Local Nakama-compatible authentication, rooms and opaque multiplayer relay."""

import argparse
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

from nakama_wire import _decode_protobuf, _encode_protobuf, WebSocketReader, _snake_keys, _write_frame


class LocalNakamaServer:
    def __init__(self, host="127.0.0.1", port=7350, server_key="ck3-local", capture_dir=None):
        self.lock = threading.RLock()
        self.log_lock = threading.Lock()
        self.secret = secrets.token_bytes(32)
        self.server_key = server_key
        self.users = {}
        self.rooms = {}
        self.peers = {}
        self.capture_dir = Path(capture_dir).resolve() if capture_dir else None
        self.packet_number = 0
        if self.capture_dir:
            self.capture_dir.mkdir(parents=True, exist_ok=False)
        self.http = ThreadingHTTPServer((host, port), LocalNakamaHandler)
        self.http.daemon_threads = True
        self.http.service = self
        self.host, self.port = self.http.server_address
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def record(self, event, **fields):
        record = {"event": event, "time": time.time(), **fields}
        with self.log_lock:
            payload = record.pop("payload", None)
            if payload is not None:
                self.packet_number += 1
                filename = f"{self.packet_number:06d}.bin"
                record.update(payload_size=len(payload), payload_sha256=hashlib.sha256(payload).hexdigest())
                if self.capture_dir:
                    (self.capture_dir / filename).write_bytes(payload)
                    record["payload_file"] = filename
            line = json.dumps(record, ensure_ascii=True)
            if self.capture_dir:
                with (self.capture_dir / "events.jsonl").open("a", encoding="utf-8") as output:
                    output.write(line + "\n")
            print(line, flush=True)

    def issue_token(self, user, *, refresh=False):
        def segment(value):
            raw = json.dumps(value, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=")
        now = int(time.time())
        content = segment({"alg": "HS256", "typ": "JWT"}) + b"." + segment({
            "uid": user["id"], "usn": user["username"], "iat": now,
            "exp": now + (30 * 86400 if refresh else 86400), "refresh": refresh,
        })
        signature = base64.urlsafe_b64encode(hmac.new(self.secret, content, hashlib.sha256).digest()).rstrip(b"=")
        return (content + b"." + signature).decode()

    def verify_token(self, token, *, refresh=False):
        content, signature = token.rsplit(".", 1)
        actual = base64.urlsafe_b64encode(hmac.new(self.secret, content.encode(), hashlib.sha256).digest()).rstrip(b"=")
        if not hmac.compare_digest(actual.decode(), signature):
            raise ValueError("Invalid local session signature")
        claims = content.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
        if claims["exp"] <= time.time() or claims.get("refresh", False) != refresh:
            raise ValueError("Expired or mismatched local session")
        with self.lock:
            return dict(self.users[claims["uid"]])

    def authenticate(self, identity, username):
        user_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "ck3-local:" + identity))
        with self.lock:
            created = user_id not in self.users
            if created:
                self.users[user_id] = {"id": user_id, "username": username or "LocalPlayer"}
            user = dict(self.users[user_id])
        return {"created": created, "token": self.issue_token(user),
                "refresh_token": self.issue_token(user, refresh=True)}

    def rpc(self, name, payload, user):
        self.record("rpc", name=name, user_id=user["id"], data=payload)
        if name == "create_jomini_match":
            match_id = str(uuid.uuid4()) + ".local"
            room = {"match_id": match_id, "owner": user["id"], "details": payload,
                    "participants": {}, "created_at": int(time.time())}
            with self.lock:
                self.rooms[match_id] = room
            return {"match_id": match_id}
        if name == "list_matches":
            with self.lock:
                return {"matches": {
                    match_id: {"match_id": match_id, "host_id": room["owner"],
                               "size": len(room["participants"]), "data": room["details"],
                               **(room["details"] if isinstance(room["details"], dict) else {})}
                    for match_id, room in self.rooms.items() if room["participants"]
                }}
        raise ValueError("Unsupported local RPC " + name)

    def start(self):
        self.thread.start()
        self.record("local_server_ready", host=self.host, port=self.port,
                    capture_dir=str(self.capture_dir) if self.capture_dir else None)
        return self

    def close(self):
        self.http.shutdown()
        with self.lock:
            peers = list(self.peers.values())
        for peer in peers:
            try:
                peer.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.http.server_close()
        self.thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()


class LocalNakamaHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def json_response(self, value, status=200):
        data = json.dumps(value, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def read_json(self):
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size)) if size else {}

    def user(self):
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise ValueError("A local session is required")
        return self.server.service.verify_token(authorization[7:])

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path in ("/", "/healthcheck"):
            return self.json_response({"status": "ok", "backend": "ck3-local"})
        if parts.path == "/ws":
            return self.websocket(parse_qs(parts.query))
        if parts.path == "/v2/account":
            try:
                return self.json_response({"user": self.user(), "wallet": "{}"})
            except (ValueError, KeyError):
                return self.json_response({"code": 16, "message": "Invalid local session"}, 401)
        self.json_response({"code": 5, "message": "Unknown local endpoint"}, 404)

    def do_POST(self):
        service = self.server.service
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        try:
            body = self.read_json()
            if parts.path == "/v2/account/authenticate/custom":
                basic = "Basic " + base64.b64encode((service.server_key + ":").encode()).decode()
                if not hmac.compare_digest(self.headers.get("Authorization", ""), basic):
                    return self.json_response({"code": 16, "message": "Invalid local server key"}, 401)
                identity = body.get("id")
                if not isinstance(identity, str) or not identity:
                    raise ValueError("Custom local identity is required")
                response = service.authenticate(identity, query.get("username", ["LocalPlayer"])[0])
                service.record("local_user_authenticated", created=response["created"])
                return self.json_response(response)
            if parts.path == "/v2/session/refresh":
                user = service.verify_token(body["token"], refresh=True)
                return self.json_response({"token": service.issue_token(user),
                                           "refresh_token": service.issue_token(user, refresh=True)})
            if parts.path == "/v2/session/logout":
                return self.json_response({})
            if parts.path.startswith("/v2/rpc/"):
                user = self.user()
                payload = body
                if isinstance(payload, dict) and "payload" in payload:
                    payload = payload["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload) if payload else {}
                result = service.rpc(unquote(parts.path.rsplit("/", 1)[1]), payload, user)
                return self.json_response({"payload": json.dumps(result, separators=(",", ":"))})
            service.record("unknown_http_endpoint", method="POST", path=parts.path)
            self.json_response({"code": 5, "message": "Unknown local endpoint"}, 404)
        except (ValueError, KeyError, TypeError) as error:
            service.record("http_request_error", path=parts.path, message=str(error))
            self.json_response({"code": 3, "message": str(error)}, 400)

    def send_envelope(self, message):
        with self.send_lock:
            if self.binary:
                _write_frame(self.wfile, 2, _encode_protobuf("envelope", message))
            else:
                _write_frame(self.wfile, 1, json.dumps(message).encode())

    def presence(self):
        return {"user_id": self.local_user["id"], "username": self.local_user["username"],
                "session_id": self.session_id}

    def websocket(self, query):
        service = self.server.service
        try:
            self.local_user = service.verify_token(query.get("token", [""])[0])
        except (ValueError, KeyError, TypeError):
            return self.json_response({"code": 16, "message": "Invalid local session"}, 401)
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            return self.json_response({"message": "WebSocket key is required"}, 400)
        self.binary = query.get("format", ["json"])[0] == "protobuf"
        self.session_id = str(uuid.uuid4())
        self.joined_rooms = set()
        self.send_lock = threading.Lock()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode())
        self.end_headers()
        self.wfile.flush()
        with service.lock:
            service.peers[self.session_id] = self
        service.record("socket_connected", **self.presence(), protocol="protobuf" if self.binary else "json")
        reader = WebSocketReader(self.rfile)
        try:
            while True:
                opcode, payload = reader.read()
                if opcode == 8:
                    with self.send_lock:
                        _write_frame(self.wfile, 8, payload)
                    break
                if opcode == 9:
                    with self.send_lock:
                        _write_frame(self.wfile, 10, payload)
                    continue
                if opcode == 10:
                    continue
                if opcode != (2 if self.binary else 1):
                    raise ValueError("Unexpected WebSocket data type for the selected Nakama format")
                message = _decode_protobuf("envelope", payload) if self.binary else _snake_keys(json.loads(payload))
                self.handle_envelope(message)
        except (EOFError, OSError, ValueError) as error:
            service.record("socket_closed", session_id=self.session_id, message=str(error))
        finally:
            for match_id in list(self.joined_rooms):
                self.leave(match_id)
            with service.lock:
                service.peers.pop(self.session_id, None)
            self.close_connection = True

    def handle_envelope(self, message):
        service = self.server.service
        cid = message.get("cid", "")
        if "rpc" in message:
            request = message["rpc"]
            try:
                payload = request.get("payload", "")
                payload = json.loads(payload) if payload else {}
                result = service.rpc(request["id"], payload, self.local_user)
                self.send_envelope({"cid": cid, "rpc": {
                    "id": request["id"], "payload": json.dumps(result, separators=(",", ":")),
                }})
            except (ValueError, KeyError, TypeError) as error:
                service.record("rpc_error", name=request.get("id"), message=str(error))
                self.send_envelope({"cid": cid, "error": {"code": 3, "message": str(error)}})
        elif "match_join" in message:
            match_id = message["match_join"].get("match_id")
            with service.lock:
                room = service.rooms.get(match_id)
                if room is None:
                    self.send_envelope({"cid": cid, "error": {"code": 4, "message": "Local room not found"}})
                    return
                others = [peer for peer in room["participants"].values() if peer is not self]
                room["participants"][self.session_id] = self
                self.joined_rooms.add(match_id)
                size = len(room["participants"])
            self.send_envelope({"cid": cid, "match": {
                "match_id": match_id, "authoritative": False, "size": size,
                "presences": [peer.presence() for peer in others], "self": self.presence(),
            }})
            for peer in others:
                peer.send_envelope({"match_presence_event": {
                    "match_id": match_id, "joins": [self.presence()], "leaves": [],
                }})
            service.record("room_joined", match_id=match_id, size=size, **self.presence())
        elif "match_data_send" in message:
            data = message["match_data_send"]
            match_id = data["match_id"]
            targets = data.get("presences", [])
            with service.lock:
                room = service.rooms.get(match_id)
                if room is None or match_id not in self.joined_rooms:
                    raise ValueError("Cannot send outside a joined local room")
                peers = [peer for peer in room["participants"].values() if peer is not self]
            if targets:
                peers = [peer for peer in peers if any(
                    peer.local_user["id"] == target.get("user_id")
                    and (not target.get("session_id") or peer.session_id == target["session_id"])
                    for target in targets
                )]
            payload = base64.b64decode(data.get("data", ""), validate=True)
            service.record("match_data", match_id=match_id, opcode=int(data.get("op_code", 0)),
                           source=self.presence(), recipients=[peer.presence() for peer in peers], payload=payload)
            for peer in peers:
                peer.send_envelope({"match_data": {
                    "match_id": match_id, "presence": self.presence(),
                    "op_code": data.get("op_code", 0), "data": data.get("data", ""),
                }})
        elif "match_leave" in message:
            self.leave(message["match_leave"]["match_id"])
            self.send_envelope({"cid": cid})
        elif "ping" in message:
            self.send_envelope({"cid": cid, "pong": {}})
        else:
            service.record("unsupported_envelope", keys=list(message))
            self.send_envelope({"cid": cid, "error": {"code": 3, "message": "Unsupported local realtime operation"}})

    def leave(self, match_id):
        service = self.server.service
        with service.lock:
            room = service.rooms.get(match_id)
            self.joined_rooms.discard(match_id)
            if room is None:
                return
            room["participants"].pop(self.session_id, None)
            others = list(room["participants"].values())
            if not others:
                service.rooms.pop(match_id, None)
        for peer in others:
            try:
                peer.send_envelope({"match_presence_event": {
                    "match_id": match_id, "joins": [], "leaves": [self.presence()],
                }})
            except OSError:
                pass
        service.record("room_left", match_id=match_id, **self.presence())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7350)
    parser.add_argument("--key", default="ck3-local")
    parser.add_argument("--capture-dir", type=Path)
    args = parser.parse_args()
    try:
        with LocalNakamaServer(args.host, args.port, args.key, args.capture_dir):
            threading.Event().wait()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
