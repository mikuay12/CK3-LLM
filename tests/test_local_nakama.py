"""Local room-service regressions using the shipped Nakama SDK."""

import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from ck3_llm.protocol.local_nakama import LocalNakamaServer, LocalNakamaHandler
from ck3_llm.protocol.nakama_native import NakamaClient, NakamaSDK
from ck3_llm.protocol.nakama_wire import WebSocketReader, _encode_protobuf, _decode_protobuf


class LocalNakamaTests(unittest.TestCase):
    def test_heartbeat_and_leave_protobuf_fields(self):
        for envelope in ({"cid": "1", "ping": {}}, {"cid": "1", "pong": {}},
                         {"cid": "2", "match_leave": {"match_id": "local-room"}},
                         {"cid": "3", "rpc": {"id": "create_jomini_match", "payload": "{}"}}):
            encoded = _encode_protobuf("envelope", envelope)
            self.assertEqual(_decode_protobuf("envelope", encoded), envelope)

    def test_realtime_rpc_creates_owned_room_and_preserves_correlation(self):
        with redirect_stdout(io.StringIO()), LocalNakamaServer(port=0) as service:
            session = service.authenticate("local-a", "LocalA")
            handler = object.__new__(LocalNakamaHandler)
            handler.server = service.http
            handler.local_user = service.verify_token(session["token"])
            responses = []
            handler.send_envelope = responses.append
            envelope = {"cid": "create-1", "rpc": {"id": "create_jomini_match", "payload": "{}"}}
            handler.handle_envelope(_decode_protobuf("envelope", _encode_protobuf("envelope", envelope)))
            self.assertEqual(responses[0]["cid"], "create-1")
            self.assertEqual(responses[0]["rpc"]["id"], "create_jomini_match")
            match_id = json.loads(responses[0]["rpc"]["payload"])["match_id"]
            self.assertEqual(service.rooms[match_id]["owner"], handler.local_user["id"])
            handler.handle_envelope({"cid": "unknown-1", "rpc": {"id": "unknown", "payload": "{}"}})
            self.assertEqual(responses[-1]["cid"], "unknown-1")
            self.assertEqual(responses[-1]["error"]["code"], 3)

    def test_fragmented_message_with_interleaved_ping(self):
        def frame(final, opcode, payload):
            header = bytes([(128 if final else 0) | opcode])
            if len(payload) < 126:
                header += bytes([128 | len(payload)])
            else:
                header += b"\xfe" + struct.pack("!H", len(payload))
            mask = b"abcd"
            return header + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        payload = bytes(range(256)) * 300
        stream = io.BytesIO(frame(False, 2, payload[:40000]) + frame(True, 9, b"ping")
                            + frame(True, 0, payload[40000:]))
        reader = WebSocketReader(stream)
        self.assertEqual(reader.read(), (9, b"ping"))
        self.assertEqual(reader.read(), (2, payload))

    def post(self, service, route, body, token=None):
        authorization = "Bearer " + token if token else "Basic " + base64.b64encode(b"ck3-local:").decode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{service.port}" + route,
            data=json.dumps(body).encode(), headers={
                "Authorization": authorization, "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    def test_authentication_refresh_and_signature_rejection(self):
        with redirect_stdout(io.StringIO()), LocalNakamaServer(port=0) as service:
            session = self.post(service, "/v2/account/authenticate/custom?username=LocalA", {"id": "local-a"})
            self.assertTrue(session["created"])
            user = service.verify_token(session["token"])
            self.assertEqual(user["username"], "LocalA")
            again = self.post(service, "/v2/account/authenticate/custom", {"id": "local-a"})
            self.assertFalse(again["created"])
            self.assertEqual(service.verify_token(again["token"])["id"], user["id"])
            refreshed = self.post(service, "/v2/session/refresh", {"token": session["refresh_token"]})
            self.assertEqual(service.verify_token(refreshed["token"])["id"], user["id"])
            with self.assertRaises(ValueError):
                service.verify_token(session["token"] + "x")
            with self.assertRaises(ValueError):
                service.verify_token(session["refresh_token"])

    def test_cross_protocol_room_join_and_bidirectional_relay(self):
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with LocalNakamaServer(port=0, capture_dir=Path(folder) / "capture") as service:
                first = self.post(service, "/v2/account/authenticate/custom?username=LocalA", {"id": "local-a"})
                second = self.post(service, "/v2/account/authenticate/custom?username=LocalB", {"id": "local-b"})
                created = self.post(service, "/v2/rpc/create_jomini_match", json.dumps({"name": "Local room"}), first["token"])
                room = json.loads(created["payload"])["match_id"]
                sdk = NakamaSDK()
                received = [[], []]
                with NakamaClient(sdk, "127.0.0.1", service.port, False, first["token"], "protobuf") as a:
                    with NakamaClient(sdk, "127.0.0.1", service.port, False, second["token"], "json") as b:
                        clients = (a, b)
                        for client in clients:
                            client.connect()
                        deadline = time.monotonic() + 5
                        while not all(room in client.rooms for client in clients):
                            self.assertLess(time.monotonic(), deadline)
                            for client in clients:
                                for event in client.poll():
                                    if event["event"] == "connected":
                                        client.join(room)
                                    self.assertNotIn(event["event"], ("error", "join_error", "callback_error"))
                            time.sleep(0.01)
                        a.send_raw(room, 255, b"from-a")
                        b.send_raw(room, 99, b"from-b")
                        while not all(received):
                            self.assertLess(time.monotonic(), deadline)
                            for index, client in enumerate(clients):
                                received[index].extend(event for event in client.poll() if event["event"] == "match_data")
                            time.sleep(0.01)
                        self.assertEqual(received[0][0]["payload"], b"from-b")
                        self.assertEqual(received[1][0]["payload"], b"from-a")
                        self.assertEqual(received[0][0]["presence"]["user_id"], b.user_id)
                        listed = self.post(service, "/v2/rpc/list_matches", {}, first["token"])
                        self.assertEqual(json.loads(listed["payload"])["matches"][room]["size"], 2)
            log = Path(folder) / "capture" / "events.jsonl"
            events = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
            packets = [event for event in events if event["event"] == "match_data"]
            self.assertEqual(len(packets), 2)
            self.assertEqual({(log.parent / packet["payload_file"]).read_bytes() for packet in packets},
                             {b"from-a", b"from-b"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
