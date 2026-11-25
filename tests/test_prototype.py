"""Offline regression checks for game bytes, native callbacks and CLI behavior."""

from contextlib import redirect_stdout
import gc
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from ck3_llm.paths import PROJECT_ROOT
from ck3_llm.client import EventWriter, load_local_profile, replay_capture, run_connection
from ck3_llm.version import SUPPORTED_EXE_SHA256
from ck3_llm.protocol.jomini_codec import JominiCodec, decode_session_message
from ck3_llm.protocol.session_codec import SESSION_SCHEMAS, encode_session_message, session_responses
from ck3_llm.protocol.session_state import SessionState, SynchronizationState
from ck3_llm.protocol.pdx_nakama_codec import encode_payload
from ck3_llm.protocol.mock_nakama import (
    HOST_PRESENCE, MOCK_ROOM, SAMPLE_TRANSFER, SAMPLE_TRANSFER_CHECKSUM,
    MockNakamaServer, make_mock_token,
)
from ck3_llm.protocol.nakama_native import NakamaClient, NakamaSDK, validate_token
from ck3_llm.protocol.snapshot_transfer import SnapshotAssembler, TransferChecksum


ROOT = PROJECT_ROOT
HEADER = {"identity": 7, "sender": 1, "receiver": 0, "attribs": 8}
# Output of CK3's compiled serializer executed by verify_jomini_native.py.
READY_NATIVE_VECTOR = bytes.fromhex(
    "a60601000300f30101000300db000100140007000000400201000c0001000000"
    "c60001000c00000000009c060100140008000000040041000100030016000100"
    "0c0001000000d00001000e000104000400"
)
# Real host commands captured before snapshot transfer on 2026-11-14.
HOTJOIN_CAPTURE_VECTOR = bytes.fromhex(
    "a30601000300f30101000300db000100140056680000400201000c0001000000"
    "c60001000c00000000009c0601001400080000000400410001000300f0000100"
    "03000b00010003000c00020000000400850301001400edd50400ec0401000e00"
    "009c0701000e0001040004000400"
)
PING_CAPTURE_VECTOR = bytes.fromhex(
    "882701000300f30101000300db00010014006a680000400201000c0001000000"
    "c60001000c00000000009c06010014002900000004004100010003008a270100"
    "1400edd504001600010014000100000004000400"
)


class GameCodecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.codec = JominiCodec()

    def test_ready_matches_native_bytes(self):
        payload = self.codec.encode_command("set_ready_command", {"machineid": 1, "status": True}, HEADER)
        self.assertEqual(payload, READY_NATIVE_VECTOR)
        decoded = self.codec.decode_command(READY_NATIVE_VECTOR)
        self.assertEqual(decoded["default_state"], HEADER)
        self.assertEqual(decoded["fields"], {"machineid": 1, "status": True})

    def test_playable_omits_false_optional_field(self):
        for leave_ai_on in (False, True):
            with self.subTest(leave_ai_on=leave_ai_on):
                fields = {"machineid": -1, "playable": 0xFFFFFFFF, "leave_ai_on": leave_ai_on}
                payload = self.codec.encode_command("set_character_controller", fields, HEADER)
                expected = fields if leave_ai_on else {"machineid": -1, "playable": 0xFFFFFFFF}
                self.assertEqual(self.codec.decode_command(payload)["fields"], expected)

    def test_transfer_binary_data_including_token_bytes(self):
        fields = {
            "filename": "世界.ck3", "size": 12, "checksum": -1,
            "chunk_id": 0, "data": {"hex": "0000010003000400ff0000ff"},
        }
        payload = self.codec.encode_command("filetransfer_fast_cmd_chunk", fields, HEADER)
        self.assertEqual(self.codec.decode_command(payload)["fields"], fields)

    def test_truncated_commands_are_rejected(self):
        for length in range(len(READY_NATIVE_VECTOR)):
            with self.subTest(length=length), self.assertRaises(ValueError):
                self.codec.decode_command(READY_NATIVE_VECTOR[:length])

    def test_invalid_fields_are_rejected(self):
        for fields in (
            {"machineid": 1, "status": 1}, {"machineid": 1 << 31, "status": True},
            {"machineid": 1, "status": True, "typo": 0},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.codec.encode_command("set_ready_command", fields, HEADER)

    def test_session_header_and_direction(self):
        response = struct.pack("<BII", 5, 9, 2)
        self.assertEqual(decode_session_message(response)["type"], "ServerRespondHandshake")
        self.assertEqual(decode_session_message(response)["result_code"], 2)
        self.assertEqual(decode_session_message(struct.pack("<BI", 2, 5), from_server=False)["type"], "ClientRequestConnected")
        with self.assertRaises(ValueError):
            decode_session_message(response[:-1])

    def test_player_sync_arrays_round_trip(self):
        fields = {"machineid": [0, 1, 0xFFFFFFFF], "players": ["host", "\u79bb\u7ebf AI", ""],
                  "character": [123, 456, 0xFFFFFFFF], "flags": [0, 8, 255]}
        payload = self.codec.encode_command("sync_players_command", fields, HEADER)
        self.assertEqual(self.codec.decode_command(payload)["fields"], fields)
        empty = self.codec.encode_command("sync_players_command", {}, HEADER)
        self.assertEqual(self.codec.decode_command(empty)["fields"], {})
        with self.assertRaises(ValueError):
            self.codec.encode_command("sync_players_command", {"flags": [256]}, HEADER)
        with self.assertRaises(ValueError):
            self.codec.encode_command("sync_players_command", {"players": "host"}, HEADER)

    def test_gameplay_and_player_operations_round_trip(self):
        commands = {
            "add_perk": {"target": 12345, "perk": "befriend_perk"},
            "set_character_focus": {"target": 12345, "focus_type": "martial_strategy_focus", "actor": 12345},
            "execute_decision": {"character": 12345, "decision": "hold_mystical_communion_decision", "puppeteer": 6789},
            "select_event_option": {"data": 4, "option": 0},
            "pause_game_command": {"player": -1, "value": True},
            "set_speed": {"speed": 3},
            "client_ping": {"turn": 316909, "machineid": 2},
            "add_player": {"name": "\u672c\u5730\u73a9\u5bb6", "machineid": 1, "is_host": False},
            "set_player_here_command": {"machineid": 1},
            "set_observer_command": {"machineid": 1, "flag": False},
        }
        for name, fields in commands.items():
            with self.subTest(name=name):
                payload = self.codec.encode_command(name, fields, HEADER)
                self.assertEqual(self.codec.decode_command(payload)["fields"], fields)

    def test_captured_gameplay_messages_reencode_exactly(self):
        # Original MatchData command bytes from the real host on 2026-11-14.
        vectors = {
            "client_ping": PING_CAPTURE_VECTOR.hex(),
            "execute_decision": (
                "642e01000300f30101000300db00010014006c680000400201000c0001000000"
                "c60001000c00000000009c0601001400000000000400410001000300ec060100"
                "1400b8740000652e01000f002000686f6c645f6d7973746963616c5f636f6d6d"
                "756e696f6e5f6465636973696f6e04000400"
            ),
            "select_event_option": (
                "d42701000300f30101000300db00010014007f680000400201000c0001000000"
                "c60001000c00000000009c0601001400200000000400410001000300f0000100"
                "0c0004000000cf0601000c000000000004000400"
            ),
            "set_character_focus": (
                "3c2c01000300f30101000300db0001001400c56e0000400201000c0001000000"
                "c60001000c00000000009c06010014002000000004004100010003006b000100"
                "140003320000022c01000f0016006d61727469616c5f73747261746567795f66"
                "6f637573aa28010014000332000004000400"
            ),
        }
        for name, payload_hex in vectors.items():
            with self.subTest(name=name):
                payload = bytes.fromhex(payload_hex)
                decoded = self.codec.decode_command(payload)
                self.assertEqual(decoded["type"], name)
                self.assertEqual(self.codec.encode_command(name, decoded["fields"], decoded["default_state"]), payload)

    def test_decision_optional_puppeteer_and_parameter_scope(self):
        fields = {"character": 12345, "decision": "test_decision"}
        for puppeteer in (0xFFFFFFFF, 0, 11190):
            with self.subTest(puppeteer=puppeteer):
                spec = {**fields, "puppeteer": puppeteer}
                payload = self.codec.encode_command("execute_decision", spec, HEADER)
                expected = fields if puppeteer == 0xFFFFFFFF else spec
                self.assertEqual(self.codec.decode_command(payload)["fields"], expected)
        payload = self.codec.encode_command("execute_decision", {**fields, "parameters": {}}, HEADER)
        self.assertEqual(self.codec.decode_command(payload)["fields"], {**fields, "parameters": {}})
        for parameters in ({"local_variables": {}}, {"event_targets": {"target": {"type": "char", "identity": 1}}}):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                self.codec.encode_command("execute_decision", {**fields, "parameters": parameters}, HEADER)

    def test_decision_boolean_parameters_match_captured_bytes(self):
        payload = bytes.fromhex(
            "642e01000300f30101000300db0001001400e96a0000400201000c0001000000"
            "c60001000c00000000009c0601001400000000000400410001000300ec060100"
            "14005b3b0000652e01000f0013007061795f686f6d6167655f6465636973696f6e"
            "fa2f01000300f806010003000f0015007061795f686f6d6167655f7375626d6973"
            "73696f6e01000300e10001000005db0001009c02010000000000000004000f000f"
            "007061795f686f6d6167655f686f6f6b01000300e1000100000504000f001300"
            "7061795f686f6d6167655f636f6e747261637401000300e1000100000504000f000f"
            "007061795f686f6d6167655f676f6c6401000300e1000100000504000400040004000400"
        )
        decoded = self.codec.decode_command(payload)
        self.assertEqual(decoded["fields"]["decision"], "pay_homage_decision")
        targets = decoded["fields"]["parameters"]["event_targets"]
        self.assertEqual(targets["pay_homage_submission"], {"type": "boolean", "identity": 1})
        self.assertEqual(targets["pay_homage_gold"], {"type": "boolean"})
        self.assertEqual(self.codec.encode_command("execute_decision", decoded["fields"], decoded["default_state"]), payload)
        fields = {"character": 12345, "decision": "test_decision", "parameters": {
            "event_targets": {"test_flag": {"type": "boolean", "identity": 0}},
        }}
        encoded = self.codec.encode_command("execute_decision", fields, HEADER)
        self.assertEqual(self.codec.decode_command(encoded)["fields"]["parameters"], {
            "event_targets": {"test_flag": {"type": "boolean"}},
        })

    def test_invalid_player_arrays_raise_decode_errors(self):
        scalar_body = b"\xa5\x06" + READY_NATIVE_VECTOR[2:]
        with self.assertRaises(ValueError):
            self.codec.decode_command(scalar_body)
        payload = self.codec.encode_command("sync_players_command", {"machineid": [1]}, HEADER)
        array_property = bytes.fromhex("1600010003001400010000000400")
        self.assertIn(array_property, payload)
        repeated = payload.replace(array_property, array_property * 2)
        with self.assertRaises(ValueError):
            self.codec.decode_command(repeated)


class SessionCodecTests(unittest.TestCase):
    def handshake_fields(self):
        return {"name": "\u672c\u5730 AI", "compatibility_a_hex": "00" * 16,
                "compatibility_b_hex": "01" * 16, "compatibility_c_hex": "02" * 33,
                "handshake_flag": 1, "platform": 0x4E4D5453}

    def test_all_directional_controls_round_trip(self):
        for name, schema in SESSION_SCHEMAS.items():
            fields = {}
            for field, tag in schema["fields"]:
                if tag == "string":
                    fields[field] = "\u79bb\u7ebf AI"
                elif type(tag) is int:
                    fields[field] = "ab" * tag
                else:
                    fields[field] = 1
            with self.subTest(name=name):
                payload = encode_session_message(name, fields)
                decoded = decode_session_message(payload, from_server=schema["from_server"])
                self.assertEqual(decoded["type"], name)
                self.assertEqual(decoded["fields"], fields)
                self.assertEqual(encode_session_message(name, fields, trailing_hex=decoded["trailing_hex"]), payload)

    def test_native_lengths_and_opaque_tail(self):
        request = encode_session_message("ClientRequestConnected", {})
        self.assertEqual(request, struct.pack("<BI", 2, 16) + bytes(11))
        fields = self.handshake_fields()
        request = encode_session_message("ClientRequestHandshake", fields)
        self.assertEqual(len(request), 90 + len(fields["name"].encode("utf-8")) + 1)
        decoded = decode_session_message(request, from_server=False)
        self.assertEqual(decoded["trailing_hex"], "00" * 11)
        custom = encode_session_message("ClientRequestHandshake", fields, trailing_hex="ff00a5")
        self.assertEqual(decode_session_message(custom, from_server=False)["trailing_hex"], "ff00a5")

    def test_invalid_controls_and_truncated_fields(self):
        fields = self.handshake_fields()
        for changes in ({"compatibility_a_hex": "00"}, {"platform": True}, {"handshake_flag": 256},
                        {"name": "a\0b"}, {"unknown": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                encode_session_message("ClientRequestHandshake", {**fields, **changes})
        payload = encode_session_message("ClientRequestHandshake", fields, trailing_hex="")
        for length in range(5, len(payload)):
            shortened = bytearray(payload[:length])
            struct.pack_into("<I", shortened, 1, length)
            with self.subTest(length=length), self.assertRaises(ValueError):
                decode_session_message(shortened, from_server=False)

    def test_empty_names_and_unknown_types_preserve_raw_data(self):
        fields = self.handshake_fields()
        fields["name"] = ""
        payload = encode_session_message("ClientRequestHandshake", fields)
        self.assertEqual(len(payload), 90)
        self.assertEqual(payload[5:9], bytes(4))
        self.assertEqual(decode_session_message(payload, from_server=False)["fields"], fields)
        unknown = struct.pack("<BI", 255, 8) + b"\x00\xff\x01"
        self.assertEqual(decode_session_message(unknown)["body_hex"], "00ff01")
        with self.assertRaises(ValueError):
            encode_session_message("unknown", {})


class SessionStateTests(unittest.TestCase):
    def player_sync(self):
        return {"type": "sync_players_command", "fields": {
            "machineid": [0, 1], "players": ["host", "local-ai"],
            "character": [100, 200], "flags": [0, 8],
        }}

    def test_identity_roster_and_disconnect(self):
        state = SessionState()
        add = {"type": "ServerRequestAddPeer", "fields": {
            "peer_id": 1, "peer_value": 123, "assign_local": 1,
        }}
        state.feed(add)
        self.assertEqual(state.local_machineid, 1)
        self.assertEqual(state.feed(add), [])
        state.feed({"type": "ServerRespondConnected", "fields": {}})
        events = state.feed(self.player_sync())
        self.assertTrue(events[0]["session_connected"])
        self.assertEqual(events[0]["players"][1], {"machineid": 1, "name": "local-ai", "playable": 200, "flags": 8})
        events[0]["players"][1]["name"] = "changed"
        self.assertEqual(state.players[1]["name"], "local-ai")
        self.assertEqual(state.feed(self.player_sync()), [])
        state.feed({"type": "ServerRequestDisconnect", "fields": {"reason": 1}})
        self.assertEqual(state.snapshot(), {"local_machineid": None, "session_connected": False,
                                           "peers": [], "players": []})

    def test_invalid_sync_preserves_previous_roster(self):
        state = SessionState()
        state.feed(self.player_sync())
        previous = state.snapshot()
        invalid = self.player_sync()
        invalid["fields"]["flags"] = []
        with self.assertRaises(ValueError):
            state.feed(invalid)
        self.assertEqual(state.snapshot(), previous)
        invalid = self.player_sync()
        invalid["fields"]["machineid"] = [0, 0]
        with self.assertRaises(ValueError):
            state.feed(invalid)
        self.assertEqual(state.snapshot(), previous)

    def test_received_character_loaded_and_ready_commands_update_roster(self):
        state = SessionState()
        state.feed(self.player_sync())
        state.feed({"type": "set_character_controller", "fields": {"machineid": 1, "playable": 300}})
        state.feed({"type": "set_player_here_command", "fields": {"machineid": 1}})
        state.feed({"type": "set_ready_command", "fields": {"machineid": 1, "status": True}})
        self.assertEqual(state.players[1]["playable"], 300)
        self.assertEqual(state.players[1]["flags"], 8 | 0x40 | 2)
        state.feed({"type": "set_ready_command", "fields": {"machineid": 1, "status": False}})
        self.assertEqual(state.players[1]["flags"], 8 | 0x40)
        previous = state.snapshot()
        with self.assertRaises(ValueError):
            state.feed({"type": "set_character_controller", "fields": {"machineid": 1, "playable": -1}})
        self.assertEqual(state.snapshot(), previous)

    def test_received_packets_emit_structured_session_state(self):
        codec = JominiCodec()
        sync = self.player_sync()
        messages = [
            {"word_8": 1, "payload_hex": encode_session_message("ServerRequestAddPeer", {
                "peer_id": 1, "peer_value": 123, "assign_local": 1,
            }).hex()},
            {"word_8": 0, "payload_hex": codec.encode_command(sync["type"], sync["fields"], HEADER).hex()},
        ]
        packet = encode_payload("local-ai", messages, 1, "host")
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()) as stdout:
            with EventWriter(Path(folder) / "capture") as writer:
                writer.write({"event": "match_data", "opcode": 255, "payload": packet})
                self.assertEqual(writer.session_state.local_machineid, 1)
                writer.write({"event": "disconnected", "code": 1000})
        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        updates = [event for event in events if event["event"] == "session_state"]
        self.assertEqual(updates[1]["players"][1]["playable"], 200)
        self.assertIsNone(updates[-1]["local_machineid"])
        self.assertEqual(updates[-1]["players"], [])


class SynchronizationStateTests(unittest.TestCase):
    def test_announced_positions_tick_counts_and_disconnect(self):
        state = SynchronizationState()
        self.assertEqual(state.feed({"type": "daily_tick", "fields": {}}), [])
        codec = JominiCodec()
        hotjoin = codec.decode_command(HOTJOIN_CAPTURE_VECTOR)
        state.feed(hotjoin)
        self.assertEqual(state.snapshot()["hotjoin"], {
            "tick": 316909, "peer_ids": [2], "sender": 1,
            "pause_game": False, "host_has_started_game": True,
        })
        self.assertEqual(state.feed(hotjoin), [])
        state.feed({"type": "daily_tick", "fields": {}})
        state.feed({"type": "event_tick", "fields": {}})
        ping = codec.decode_command(PING_CAPTURE_VECTOR)
        state.feed(ping)
        self.assertEqual(state.feed(ping), [])
        snapshot = state.snapshot()
        self.assertEqual(snapshot["received_ticks_since_hotjoin"], {"daily_tick": 1, "event_tick": 1})
        self.assertEqual(snapshot["peer_turns"], [{"machineid": 1, "turn": 316909}])
        snapshot["hotjoin"]["peer_ids"].append(99)
        self.assertEqual(state.hotjoin["peer_ids"], [2])
        state.feed({"type": "ServerRespondDisconnect", "fields": {"reason": 0}})
        self.assertIsNone(state.hotjoin)
        self.assertEqual(state.peer_turns, {})
        self.assertEqual(state.received_ticks, {"daily_tick": 0, "event_tick": 0})

    def test_invalid_announcements_preserve_previous_state(self):
        state = SynchronizationState()
        codec = JominiCodec()
        state.feed(codec.decode_command(HOTJOIN_CAPTURE_VECTOR))
        previous = state.snapshot()
        for field, value in (("tick", -1), ("pause_game", 1), ("id", [2, "bad"])):
            message = codec.decode_command(HOTJOIN_CAPTURE_VECTOR)
            message["fields"]["data"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                state.feed(message)
            self.assertEqual(state.snapshot(), previous)
        with self.assertRaises(ValueError):
            state.feed({"type": "client_ping", "fields": {"machineid": 1, "turn": True}})
        self.assertEqual(state.snapshot(), previous)


class SessionResponseTests(unittest.TestCase):
    def packet(self, assign_local):
        message = decode_session_message(encode_session_message("ServerRequestAddPeer", {
            "peer_id": 2, "peer_value": 123, "assign_local": assign_local,
        }))
        return {"event": "match_data", "opcode": 255, "presence": HOST_PRESENCE,
                "pdx": {"messages": [{"jomini": message}]}}

    def test_peer_registration_responses_follow_assignment(self):
        for assigned in (0, 1, 255):
            with self.subTest(assigned=assigned):
                responses = session_responses(self.packet(assigned))
                expected = ["ClientRespondAddPeer"]
                if assigned:
                    expected.append("ClientRequestConnected")
                self.assertEqual([response["type"] for response in responses], expected)
                for response in responses:
                    self.assertEqual(response["recipient"], HOST_PRESENCE["user_id"])
                    self.assertEqual(response["presences"], [HOST_PRESENCE])
                    self.assertEqual(response["fields"], {})

    def test_session_responses_ignore_missing_sources_and_invalid_state(self):
        packet = self.packet(1)
        packet.pop("presence")
        self.assertEqual(session_responses(packet), [])
        packet = self.packet(1)
        packet["pdx"]["messages"][0]["state_error"] = "invalid peer registration"
        self.assertEqual(session_responses(packet), [])
        packet = self.packet(1)
        packet["pdx"]["messages"][0]["jomini"] = decode_session_message(
            encode_session_message("ServerRespondHandshake", {"result_code": 1}),
        )
        self.assertEqual(session_responses(packet), [])

    def test_host_disconnect_is_acknowledged_with_its_reason(self):
        control = decode_session_message(encode_session_message("ServerRequestDisconnect", {"reason": 7}))
        packet = {"event": "match_data", "opcode": 255, "presence": HOST_PRESENCE,
                  "pdx": {"messages": [{"jomini": control}]}}
        responses = session_responses(packet)
        self.assertEqual(responses, [{"command": "send_session_message", "type": "ClientRespondDisconnect",
                                     "fields": {"reason": 7}, "recipient": HOST_PRESENCE["user_id"],
                                     "presences": [HOST_PRESENCE]}])
        packet["routing_ignored"] = "different_recipient"
        self.assertEqual(session_responses(packet), [])


class SessionShutdownTests(unittest.TestCase):
    class Peer:
        def __init__(self, writer, commands, mode):
            self.writer = writer
            self.commands = commands
            self.mode = mode
            self.pending = []
            self.sent = []

        def connect(self):
            self.pending.append({"event": "connected", "user_id": "local-ai"})

        def join(self, room_id, metadata):
            self.pending.append({"event": "room_joined", "match_id": room_id})

        def poll(self):
            events, self.pending = self.pending, []
            return events

        def receive(self, name, fields):
            packet = encode_payload("local-ai", [{"word_8": 1,
                                    "payload_hex": encode_session_message(name, fields).hex()}], 1, "host")
            self.pending.append({"event": "match_data", "opcode": 255, "payload": packet,
                                 "presence": {"user_id": "host"}})

        def send_envelope(self, room_id, recipient, messages, magic=0xCAFE, presences=None):
            command = self.writer.game_codec.decode_pdx_message(messages[0], from_server=False)
            self.sent.append(command["type"])
            packet = encode_payload(recipient, messages, len(self.sent), "local-ai")
            self.pending.append({"event": "send_submitted", "opcode": 255, "payload": packet})
            if command["type"] == "ClientRequestHandshake":
                if self.mode == "silent":
                    return packet
                self.receive("ServerRequestAddPeer", {"peer_id": 2, "peer_value": 7, "assign_local": 1})
                self.receive("ServerRespondConnected", {})
                if self.mode == "host":
                    self.receive("ServerRequestDisconnect", {"reason": 7})
                elif self.mode in ("stop", "timeout"):
                    self.commands.put({"command": "stop"})
            elif command["type"] == "ClientRequestDisconnect" and self.mode != "timeout":
                self.receive("ServerRespondDisconnect", {"reason": 0xFFFFFFFF})
            return packet

    def run_peer(self, mode, timeout=15):
        import queue
        commands = queue.SimpleQueue()
        handshake = {"command": "send_session_message", "type": "ClientRequestHandshake",
                     "recipient": "host", "fields": SessionCodecTests().handshake_fields()}
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with EventWriter(Path(folder) / "capture", quiet=True) as writer:
                peer = self.Peer(writer, commands, mode)
                result = run_connection(peer, "room", writer, handshake=handshake, commands=commands,
                                        duration=0.06, timeout=timeout, disconnect_timeout=0.04)
                return result, peer.sent, writer.session_state.snapshot()

    def test_stop_and_duration_complete_game_disconnect(self):
        for mode in ("stop", "duration"):
            with self.subTest(mode=mode):
                result, sent, state = self.run_peer(mode)
                self.assertEqual(result["reason"], "stop_command" if mode == "stop" else "duration_elapsed")
                self.assertEqual(result["native_disconnect"], "confirmed")
                self.assertEqual(sent.count("ClientRequestDisconnect"), 1)
                self.assertFalse(state["session_connected"])
                self.assertIsNone(state["local_machineid"])

    def test_host_disconnect_is_acknowledged_before_exit(self):
        result, sent, state = self.run_peer("host")
        self.assertEqual(result["reason"], "server_disconnect")
        self.assertEqual(result["native_disconnect"], "host_requested")
        self.assertEqual(sent[-1], "ClientRespondDisconnect")
        self.assertNotIn("ClientRequestDisconnect", sent)
        self.assertFalse(state["session_connected"])

    def test_missing_disconnect_response_has_bounded_timeout(self):
        started = time.monotonic()
        result, sent, _ = self.run_peer("timeout")
        self.assertEqual(result["native_disconnect"], "timed_out")
        self.assertEqual(sent.count("ClientRequestDisconnect"), 1)
        self.assertLess(time.monotonic() - started, 2)

    def test_silent_game_handshake_times_out_after_transport_join(self):
        with self.assertRaisesRegex(TimeoutError, "negotiating_game_session"):
            self.run_peer("silent", timeout=0.04)

    def test_foreign_and_unsolicited_disconnects_preserve_both_states(self):
        def control(name, fields):
            return {"word_8": 1, "payload_hex": encode_session_message(name, fields).hex()}

        def receive(writer, messages, recipient="local-ai", source="host"):
            return writer.write({"event": "match_data", "opcode": 255,
                                 "payload": encode_payload(recipient, messages, 1, source),
                                 "presence": {"user_id": source}})

        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with EventWriter(Path(folder) / "capture", quiet=True) as writer:
                writer.write({"event": "connected", "user_id": "local-ai"})
                receive(writer, [control("ServerRequestAddPeer", {"peer_id": 2, "peer_value": 7, "assign_local": 1}),
                                 control("ServerRespondConnected", {}),
                                 {"word_8": 0, "payload_hex": HOTJOIN_CAPTURE_VECTOR.hex()}])
                session = writer.session_state.snapshot()
                sync = writer.synchronization_state.snapshot()
                response = [control("ServerRespondDisconnect", {"reason": 0xFFFFFFFF})]
                ignored = receive(writer, response, recipient="other-ai")
                self.assertEqual(ignored["routing_ignored"], "different_recipient")
                self.assertEqual(session_responses(ignored), [])
                receive(writer, [control("ServerRequestAddPeer", {"peer_id": 9, "peer_value": 8, "assign_local": 1})],
                        recipient="other-ai")
                ignored = receive(writer, response)
                self.assertEqual(ignored["pdx"]["messages"][0]["state_ignored"], "unsolicited_disconnect_response")
                self.assertEqual(writer.session_state.snapshot(), session)
                self.assertEqual(writer.synchronization_state.snapshot(), sync)
                writer.write({"event": "send_submitted", "opcode": 255,
                              "payload": encode_payload("host", [control("ClientRequestDisconnect", {"reason": 0})],
                                                        1, "local-ai")})
                receive(writer, response, source="other-host")
                self.assertFalse(writer.disconnect_confirmed)
                receive(writer, response)
                self.assertTrue(writer.disconnect_confirmed)
                self.assertFalse(writer.session_state.connected)
                self.assertIsNone(writer.synchronization_state.hotjoin)

    def test_replay_restores_recipient_and_disconnect_request_context(self):
        def packet(recipient, name, fields, source="host"):
            return encode_payload(recipient, [{"word_8": 1,
                                  "payload_hex": encode_session_message(name, fields).hex()}], 1, source)

        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            source = Path(folder) / "source"
            destination = Path(folder) / "replay"
            with EventWriter(source, quiet=True) as writer:
                writer.write({"event": "connected", "user_id": "local-ai"})
                for recipient, machine in (("local-ai", 2), ("other-ai", 9)):
                    writer.write({"event": "match_data", "opcode": 255, "presence": {"user_id": "host"},
                                  "payload": packet(recipient, "ServerRequestAddPeer",
                                                    {"peer_id": machine, "peer_value": 7, "assign_local": 1})})
                writer.write({"event": "send_submitted", "opcode": 255,
                              "payload": packet("host", "ClientRequestDisconnect", {"reason": 0}, "local-ai")})
                writer.write({"event": "match_data", "opcode": 255, "presence": {"user_id": "host"},
                              "payload": packet("local-ai", "ServerRespondDisconnect", {"reason": 0})})
            replay_capture(source, destination, quiet=True)
            events = [json.loads(line) for line in (destination / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            states = [event["local_machineid"] for event in events if event["event"] == "session_state"]
            self.assertEqual(states, [2, None])
            received = [event for event in events if event["event"] == "match_data"]
            self.assertEqual(received[1]["routing_ignored"], "different_recipient")


class TransferTests(unittest.TestCase):
    def chunk(self, chunk_id, data):
        return {"type": "filetransfer_fast_cmd_chunk", "fields": {
            "filename": "../world.ck3", "size": 4, "checksum": 0x018A03D4,
            "chunk_id": chunk_id, "data": {"hex": data.hex()},
        }}

    def test_out_of_order_and_retransmission(self):
        with tempfile.TemporaryDirectory() as folder:
            assembler = SnapshotAssembler(Path(folder) / "transfers")
            self.assertEqual(assembler.feed(self.chunk(1, b"cd")), [])
            self.assertEqual(assembler.feed(self.chunk(1, b"cd")), [])
            event, = assembler.feed(self.chunk(0, b"ab"))
            self.assertEqual(Path(event["file"]).read_bytes(), b"abcd")
            self.assertEqual(event["checksum_verification"], "passed")
            self.assertTrue(Path(event["file"]).is_relative_to(folder))
            self.assertEqual(assembler.feed(self.chunk(0, b"ab")), [])

    def test_conflicting_chunk_preserves_original(self):
        with tempfile.TemporaryDirectory() as folder:
            assembler = SnapshotAssembler(Path(folder) / "transfers")
            assembler.feed(self.chunk(0, b"ab"))
            with self.assertRaises(ValueError):
                assembler.feed(self.chunk(0, b"xy"))
            event, = assembler.feed(self.chunk(1, b"cd"))
            self.assertEqual(Path(event["file"]).read_bytes(), b"abcd")

    def test_incomplete_chunk_sequence_stays_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            assembler = SnapshotAssembler(Path(folder) / "transfers")
            self.assertEqual(assembler.feed(self.chunk(1, b"abcd")), [])

    def test_empty_transfer_and_empty_chunk_are_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            assembler = SnapshotAssembler(Path(folder) / "transfers")
            metadata = {"filename": "empty.bin", "size": 0, "checksum": 0}
            event, = assembler.feed({"type": "filetransfer_fast_cmd_initialize", "fields": metadata})
            self.assertEqual(Path(event["file"]).read_bytes(), b"")
            self.assertEqual(assembler.feed({"type": "filetransfer_fast_cmd_chunk", "fields": {
                **metadata, "chunk_id": 0, "data": {"hex": ""},
            }}), [])

    def test_negotiated_invalid_chunk_does_not_pollute_reassembly(self):
        with tempfile.TemporaryDirectory() as folder:
            assembler = SnapshotAssembler(Path(folder) / "transfers")
            fields = self.chunk(0, b"ab")["fields"]
            assembler.feed({"type": "filetransfer_fast_cmd_handshake", "fields": {
                "filename": fields["filename"], "size": fields["size"], "checksum": fields["checksum"],
                "filetransfer_fast_fld_chunk_size": 2,
            }})
            for chunk_id, data in ((0, b"a"), (0, b"abc"), (2, b"ab"), (1, b"")):
                with self.subTest(chunk_id=chunk_id, data=data), self.assertRaises(ValueError):
                    assembler.feed(self.chunk(chunk_id, data))
            self.assertEqual(assembler.feed(self.chunk(0, b"ab")), [])
            event, = assembler.feed(self.chunk(1, b"cd"))
            self.assertEqual(event["checksum_verification"], "passed")

    def test_sources_have_independent_transfers(self):
        with tempfile.TemporaryDirectory() as folder:
            assembler = SnapshotAssembler(Path(folder) / "transfers")
            assembler.feed(self.chunk(0, b"ab"), source="host-a")
            assembler.feed(self.chunk(1, b"cd"), source="host-b")
            self.assertEqual(len(assembler.transfers), 2)
            first, = assembler.feed(self.chunk(1, b"cd"), source="host-a")
            second, = assembler.feed(self.chunk(0, b"ab"), source="host-b")
            self.assertNotEqual(first["file"], second["file"])
            self.assertEqual(first["source_user_id"], "host-a")
            self.assertEqual(second["source_user_id"], "host-b")

    def test_checksum_failure_keeps_bytes_and_skips_world_decoding(self):
        codec = JominiCodec()
        command = codec.encode_command("filetransfer_fast_cmd_chunk", {
            "filename": "hotjoin_0", "size": len(SAMPLE_TRANSFER),
            "checksum": SAMPLE_TRANSFER_CHECKSUM ^ 1,
            "chunk_id": 0, "data": {"hex": SAMPLE_TRANSFER.hex()},
        }, HEADER)
        payload = encode_payload("local", [{"payload_hex": command.hex()}], 1, "host")
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(stdout):
            with EventWriter(Path(folder) / "capture") as writer:
                writer.write({"event": "match_data", "opcode": 255, "payload": payload,
                              "presence": {"user_id": "host"}})
                writer.finish_world()
                self.assertEqual(writer.decoded_worlds, 0)
            events = [json.loads(line) for line in stdout.getvalue().splitlines()]
            transfer = next(event for event in events if event["event"] == "transfer_reassembled")
            self.assertEqual(transfer["checksum_verification"], "failed")
            self.assertEqual(transfer["computed_checksum"], SAMPLE_TRANSFER_CHECKSUM)
            self.assertEqual(transfer["world_state_decoding"], "skipped")
            self.assertEqual(Path(transfer["file"]).read_bytes(), SAMPLE_TRANSFER)


class NativeTransportTests(unittest.TestCase):
    def run_cli(self, arguments, stdin=None):
        result = subprocess.run(
            [sys.executable, str(ROOT / "ck3_client.py"), *arguments],
            input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return result, events

    def test_replay_cli_uses_normal_snapshot_and_synchronization_pipeline(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source"
            codec = JominiCodec()
            transfer = codec.encode_command("filetransfer_fast_cmd_chunk", {
                "filename": "world.ck3", "size": len(SAMPLE_TRANSFER), "checksum": SAMPLE_TRANSFER_CHECKSUM,
                "chunk_id": 0, "data": {"hex": SAMPLE_TRANSFER.hex()},
            }, HEADER)
            with redirect_stdout(io.StringIO()), EventWriter(source, quiet=True) as writer:
                for sequence, payloads in enumerate(([HOTJOIN_CAPTURE_VECTOR], [PING_CAPTURE_VECTOR, transfer])):
                    packet = encode_payload("local-ai", [
                        {"word_8": 0, "payload_hex": payload.hex()} for payload in payloads
                    ], sequence, "host")
                    writer.write({"event": "match_data", "opcode": 255, "payload": packet,
                                  "presence": HOST_PRESENCE, "time": "2026-11-14T04:09:31+00:00"})
            result, events = self.run_cli([
                "replay", str(source), "--capture-dir", str(Path(folder) / "replayed"), "--quiet",
            ])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = events[-1]
            self.assertEqual(report["event"], "replay_completed")
            self.assertEqual(report["received_frames"], 2)
            self.assertEqual(report["decode_errors"], 0)
            self.assertEqual((report["reassembled_files"], report["decoded_worlds"]), (1, 1))
            self.assertEqual(report["last_announced_synchronization"]["peer_turns"], [{"machineid": 1, "turn": 316909}])
            output_log = Path(folder) / "replayed" / "events.jsonl"
            recorded = [json.loads(line) for line in output_log.read_text(encoding="utf-8").splitlines()]
            packets = [event for event in recorded if event["event"] == "match_data"]
            self.assertEqual(packets[0]["time"], "2026-11-14T04:09:31+00:00")

    def test_replay_cli_reports_checksum_and_world_decode_failures(self):
        invalid_world = b"invalid world payload"
        world_checksum = TransferChecksum()
        world_checksum.update(invalid_world)
        cases = (
            (SAMPLE_TRANSFER, 0, "failed_transfers"),
            (invalid_world, world_checksum.value, "world_decode_errors"),
        )
        for data, checksum, error_field in cases:
            with self.subTest(error_field=error_field), tempfile.TemporaryDirectory() as folder:
                codec = JominiCodec()
                transfer = codec.encode_command("filetransfer_fast_cmd_chunk", {
                    "filename": "world.ck3", "size": len(data), "checksum": checksum,
                    "chunk_id": 0, "data": {"hex": data.hex()},
                }, HEADER)
                source = Path(folder) / "source"
                with redirect_stdout(io.StringIO()), EventWriter(source, quiet=True) as writer:
                    packet = encode_payload("local-ai", [{"word_8": 0, "payload_hex": transfer.hex()}], 1, "host")
                    writer.write({"event": "match_data", "opcode": 255, "payload": packet})
                result, events = self.run_cli([
                    "replay", str(source), "--capture-dir", str(Path(folder) / "replayed"), "--quiet",
                ])
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(events[-1][error_field], 1)

    def test_replay_rejects_changed_packet_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source"
            source.mkdir()
            (source / "packet.bin").write_bytes(b"changed packet bytes")
            (source / "events.jsonl").write_text(json.dumps({
                "event": "match_data", "opcode": 255, "payload_file": "packet.bin", "payload_sha256": "0" * 64,
            }) + "\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "checksum"):
                replay_capture(source, Path(folder) / "replayed", quiet=True)

    def test_demo_protobuf_and_json(self):
        for protocol in ("protobuf", "json"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as folder:
                result, events = self.run_cli([
                    "demo", "--protocol", protocol, "--timeout", "3",
                    "--capture-dir", str(Path(folder) / "capture"),
                ])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(events[-1]["event"], "demo_passed")
                reassembled = [event for event in events if event["event"] == "transfer_reassembled"]
                self.assertEqual(len(reassembled), 1)
                self.assertEqual(Path(reassembled[0]["file"]).read_bytes(), SAMPLE_TRANSFER)
                sent = next(event for event in events if event["event"] == "send_submitted")
                game_command = sent["pdx"]["messages"][0]["jomini"]
                self.assertEqual(game_command["type"], "set_character_controller")
                self.assertEqual(game_command["fields"], {"machineid": 1, "playable": 12345})
                states = [event for event in events if event["event"] == "session_state" and event["players"]]
                self.assertTrue(states)
                self.assertEqual(states[0]["local_machineid"], 1)
                self.assertEqual(states[0]["players"][1]["playable"], 12345)
                worlds = [event for event in events if event["event"] == "world_snapshot_decoded"]
                self.assertEqual(len(worlds), 1)
                self.assertEqual(worlds[0]["observation"]["character"]["id"], 12345)
                self.assertEqual(worlds[0]["observation"]["held_titles"][0]["key"], "c_fixture")

    def test_connection_join_disconnect_and_timeout_failures(self):
        for mode, expected in (
            ("reject_connect", "disconnected"), ("reject_join", "join_error"),
            ("close_after_join", "disconnected"), ("silent_join", "failed"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder, MockNakamaServer(mode) as peer:
                token_file = Path(folder) / "token.txt"
                token_file.write_text(peer.token, encoding="utf-8")
                result, events = self.run_cli([
                    "connect", "--host", "127.0.0.1", "--port", str(peer.port), "--no-ssl",
                    "--room-id", MOCK_ROOM, "--token-file", str(token_file),
                    "--timeout", "0.25", "--duration", "1",
                    "--capture-dir", str(Path(folder) / "capture"),
                ])
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(expected, [event["event"] for event in events])
                self.assertNotIn(peer.token, result.stdout + result.stderr)
                self.assertEqual(peer.errors, [])

    def test_active_network_transfer_protocol(self):
        for protocol in ("protobuf", "json"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as folder:
                result, events = self.run_cli([
                    "demo", "--transfer-protocol", "--protocol", protocol, "--timeout", "3",
                    "--capture-dir", str(Path(folder) / "capture"),
                ])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                sent = [event["pdx"]["messages"][0]["jomini"] for event in events if event["event"] == "send_submitted"]
                self.assertEqual([command["type"] for command in sent[:4]], [
                    "ClientRespondAddPeer", "ClientRequestConnected",
                    "filetransfer_fast_cmd_request_handshake", "filetransfer_fast_cmd_initialize",
                ])
                count = (len(SAMPLE_TRANSFER) + 63) // 64
                full = ((1 << count) - 1).to_bytes((count + 7) // 8, "little").hex()
                statuses = [command["fields"]["data"]["hex"] for command in sent
                            if command["type"] == "filetransfer_fast_cmd_chunkstatus"]
                self.assertNotEqual(statuses[0], full)
                self.assertEqual(statuses[-1], full)
                worlds = [event for event in events if event["event"] == "world_snapshot_decoded"]
                self.assertEqual(len(worlds), 1)
                self.assertEqual(worlds[0]["observation"]["character"]["id"], 12345)
                reassembled = next(event for event in events if event["event"] == "transfer_reassembled")
                self.assertEqual(reassembled["checksum_verification"], "passed")
                self.assertEqual(reassembled["computed_checksum"], SAMPLE_TRANSFER_CHECKSUM)
                connected = [event for event in events if event["event"] == "session_state"
                             and event["session_connected"]]
                self.assertTrue(connected)
                self.assertEqual(events[-1]["event"], "demo_passed")

    def test_stdin_commands_and_malformed_receive_are_recorded(self):
        with tempfile.TemporaryDirectory() as folder, MockNakamaServer("malformed_data") as peer:
            token_file = Path(folder) / "token.json"
            token_file.write_text(json.dumps({"token": peer.token}), encoding="utf-8")
            metadata = Path(folder) / "metadata.json"
            metadata.write_text(json.dumps({"CaseSensitiveKey": "value"}), encoding="utf-8")
            commands = "\n".join([
                "invalid json", json.dumps({"command": "send_raw", "opcode": 99, "payload_hex": "00ff"}),
                json.dumps({"command": "stop"}),
            ]) + "\n"
            result, events = self.run_cli([
                "connect", "--host", "127.0.0.1", "--port", str(peer.port), "--no-ssl",
                "--protocol", "json", "--room-id", MOCK_ROOM, "--token-file", str(token_file),
                "--metadata-file", str(metadata), "--stdin", "--timeout", "3",
                "--capture-dir", str(Path(folder) / "capture"),
            ], stdin=commands)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("command_error", [event["event"] for event in events])
            self.assertTrue(any("pdx_decode_error" in event for event in events))
            self.assertTrue(any(event["event"] == "send_submitted" and event["opcode"] == 99 for event in events))
            self.assertEqual(peer.received[0]["match_join"]["metadata"], {"CaseSensitiveKey": "value"})
            self.assertEqual(events[-1]["reason"], "stop_command")
            self.assertNotIn(peer.token, result.stdout + result.stderr)

    def test_native_callbacks_own_their_data_and_survive_gc(self):
        sdk = NakamaSDK()
        with MockNakamaServer() as peer:
            with NakamaClient(sdk, "127.0.0.1", peer.port, False, peer.token) as client:
                client.connect()
                gc.collect()
                deadline = time.monotonic() + 3
                received = None
                while time.monotonic() < deadline and received is None:
                    for event in client.poll():
                        if event["event"] == "connected":
                            client.join(MOCK_ROOM)
                        elif event["event"] == "room_joined":
                            client.send_raw(MOCK_ROOM, (1 << 63) - 1, b"", [HOST_PRESENCE])
                            gc.collect()
                        elif event["event"] == "match_data" and event["opcode"] == (1 << 63) - 1:
                            received = event
                    time.sleep(0.01)
                self.assertIsNotNone(received)
                gc.collect()
                self.assertEqual(received["payload"], b"")
            client.close()
            with self.assertRaises(RuntimeError):
                client.poll()
            self.assertEqual(peer.errors, [])

    def test_stdin_session_and_gameplay_commands(self):
        with tempfile.TemporaryDirectory() as folder, MockNakamaServer() as peer:
            token_file = Path(folder) / "token.txt"
            token_file.write_text(peer.token, encoding="utf-8")
            commands = [
                {"command": "send_session_message", "recipient": HOST_PRESENCE["user_id"],
                 "type": "ClientRequestConnected", "fields": {}},
                {"command": "send_game_command", "recipient": HOST_PRESENCE["user_id"],
                 "type": "add_perk", "header": HEADER,
                 "fields": {"target": 12345, "perk": "befriend_perk"}},
                {"command": "send_game_command", "recipient": HOST_PRESENCE["user_id"],
                 "type": "set_character_focus", "header": HEADER,
                 "fields": {"target": 12345, "focus_type": "martial_strategy_focus", "actor": 12345}},
                {"command": "send_game_command", "recipient": HOST_PRESENCE["user_id"],
                 "type": "execute_decision", "header": HEADER,
                 "fields": {"character": 12345, "decision": "hold_mystical_communion_decision"}},
                {"command": "send_game_command", "recipient": HOST_PRESENCE["user_id"],
                 "type": "select_event_option", "header": HEADER,
                 "fields": {"data": 4, "option": 0}},
                {"command": "stop"},
            ]
            result, events = self.run_cli([
                "connect", "--host", "127.0.0.1", "--port", str(peer.port), "--no-ssl",
                "--room-id", MOCK_ROOM, "--token-file", str(token_file), "--stdin",
                "--timeout", "3", "--capture-dir", str(Path(folder) / "capture"),
            ], stdin="\n".join(json.dumps(command) for command in commands) + "\n")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            sent = [event["pdx"]["messages"][0]["jomini"] for event in events
                    if event["event"] == "send_submitted"]
            self.assertEqual([message["type"] for message in sent], [command["type"] for command in commands[:-1]])
            self.assertEqual(sent[0]["declared_size"], 16)
            for message, command in zip(sent[1:], commands[1:-1]):
                self.assertEqual(message["fields"], command["fields"])


class CredentialHandlingTests(unittest.TestCase):
    def test_automatic_command_headers_follow_explicit_identity_and_assigned_machine(self):
        class Peer:
            def __init__(self):
                self.pending = []
                self.sent = []

            def connect(self):
                self.pending.append({"event": "connected"})

            def join(self, room_id, metadata):
                self.pending.append({"event": "room_joined", "match_id": room_id})
                control = encode_session_message("ServerRequestAddPeer", {"peer_id": 2, "peer_value": 7, "assign_local": 1})
                packet = encode_payload("local-ai", [{"word_8": 1, "payload_hex": control.hex()}], 1, "host")
                self.pending.append({"event": "match_data", "opcode": 255, "payload": packet, "presence": {"user_id": "host"}})

            def poll(self):
                events, self.pending = self.pending, []
                return events

            def send_envelope(self, room_id, recipient, messages, magic=0xCAFE, presences=None):
                self.sent.append((recipient, JominiCodec().decode_pdx_message(messages[0], from_server=False)))
                return b"submitted"

        import queue
        commands = queue.SimpleQueue()
        commands.put({"command": "send_game_command", "recipient": "host", "type": "set_ready_command",
                      "fields": {"machineid": 2, "status": True},
                      "header": {"identity": 40, "sender": 2, "receiver": 0, "attribs": 14}})
        commands.put({"command": "send_game_command", "type": "set_ready_command",
                      "fields": {"machineid": 2, "status": False}, "header": {"attribs": 14}})
        commands.put({"command": "stop"})
        handshake = {"command": "send_session_message", "type": "ClientRequestHandshake", "recipient": "host",
                     "fields": SessionCodecTests().handshake_fields()}
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with EventWriter(Path(folder) / "capture", quiet=True) as writer:
                peer = Peer()
                run_connection(peer, "room", writer, handshake=handshake, commands=commands)
        game = [(recipient, row) for recipient, row in peer.sent if row["channel"] == "command"]
        self.assertEqual([row["default_state"]["identity"] for _, row in game], [40, 41])
        self.assertEqual([row["default_state"]["sender"] for _, row in game], [2, 2])
        self.assertEqual([recipient for recipient, _ in game], ["host", "host"])

    def test_received_turn_acknowledgements_filter_sources_and_avoid_echo_loops(self):
        class Peer:
            def __init__(self, writer):
                self.writer = writer
                self.pending = []
                self.sent = []

            def connect(self):
                self.pending.append({"event": "connected"})

            def join(self, room_id, metadata):
                self.pending.append({"event": "room_joined", "match_id": room_id})

            def poll(self):
                events, self.pending = self.pending, []
                return events

            def receive(self, messages, source="host"):
                payload = encode_payload("local-ai", messages, 1, source)
                self.pending.append({"event": "match_data", "opcode": 255, "payload": payload,
                                     "presence": {"user_id": source}})

            def send_envelope(self, room_id, recipient, messages, magic=0xCAFE, presences=None):
                command = self.writer.game_codec.decode_pdx_message(messages[0], from_server=False)
                self.sent.append(command)
                if command["type"] == "ClientRequestHandshake":
                    controls = [encode_session_message("ServerRequestAddPeer", {
                        "peer_id": 2, "peer_value": 7, "assign_local": 1,
                    }), encode_session_message("ServerRespondConnected", {})]
                    self.receive([{"word_8": 1, "payload_hex": payload.hex()} for payload in controls])
                elif command["type"] == "add_player":
                    self.writer.decoded_worlds = 1
                    self.receive([{"word_8": 0, "payload_hex": data.hex()}
                                  for data in (HOTJOIN_CAPTURE_VECTOR, PING_CAPTURE_VECTOR, PING_CAPTURE_VECTOR)])
                    self.receive([{"word_8": 0, "payload_hex": PING_CAPTURE_VECTOR.hex()}], "other-user")
                elif command["type"] == "client_ping":
                    self.receive(messages)
                    if command["fields"]["turn"] == 316909:
                        for turn in (316910, 316908):
                            data = self.writer.game_codec.encode_command("client_ping", {"turn": turn, "machineid": 1}, HEADER)
                            self.receive([{"word_8": 0, "payload_hex": data.hex()}])
                return b"submitted"

        fields = SessionCodecTests().handshake_fields()
        handshake = {"command": "send_session_message", "type": "ClientRequestHandshake",
                     "recipient": "host", "fields": fields}
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
                with EventWriter(Path(directory) / "capture", quiet=True) as writer:
                    peer = Peer(writer)
                    run_connection(peer, "room", writer, handshake=handshake, player_name="LocalAI",
                                   duration=0.3, ack_received_turn=enabled)
            pings = [row for row in peer.sent if row["type"] == "client_ping"]
            self.assertEqual([row["fields"]["turn"] for row in pings], [316909, 316910] if enabled else [])
            games = [row for row in peer.sent if row["channel"] == "command"]
            ids = [row["default_state"]["identity"] for row in games]
            self.assertEqual(len(ids), len(set(ids)))

    def test_local_profile_validates_handshake_and_uses_chosen_name(self):
        profile = {"exe_sha256": SUPPORTED_EXE_SHA256, "host": "127.0.0.1", "port": 7350,
                   "ssl": False, "match_id": "local-room", "host_user_id": "local-host",
                   "handshake": {"compatibility_a_hex": "ab" * 16, "compatibility_b_hex": "cd" * 16,
                                 "compatibility_c_hex": "00" * 33, "handshake_flag": 1, "platform": 0x4E4D5453}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session_profile.json"
            path.write_text(json.dumps(profile), encoding="utf-8")
            _, command = load_local_profile(path, "ChosenPlayer")
            self.assertEqual(command["recipient"], "local-host")
            self.assertEqual(command["fields"]["name"], "ChosenPlayer")
            profile["handshake"]["compatibility_a_hex"] = "ab"
            path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_local_profile(path, "ChosenPlayer")

    def test_quiet_output_preserves_raw_capture(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as stdout:
            capture = Path(directory) / "capture"
            with EventWriter(capture, quiet=True) as writer:
                writer.write({"event": "match_data", "opcode": 99, "payload": b"packet"})
                writer.write({"event": "connected"})
            self.assertNotIn("match_data", stdout.getvalue())
            self.assertIn("connected", stdout.getvalue())
            events = [json.loads(line) for line in (capture / "events.jsonl").read_text("utf-8").splitlines()]
            self.assertEqual((capture / events[0]["payload_file"]).read_bytes(), b"packet")

    def test_game_registration_and_world_loaded_ack_use_distinct_identities(self):
        class Peer:
            def __init__(self):
                self.pending = []
                self.sent = []

            def connect(self):
                self.pending.append({"event": "connected"})

            def join(self, room_id, metadata):
                self.pending.append({"event": "room_joined", "match_id": room_id})

            def poll(self):
                events, self.pending = self.pending, []
                return events

            def send_envelope(self, room_id, recipient, messages, magic=0xCAFE, presences=None):
                message = writer.game_codec.decode_pdx_message(messages[0], from_server=False)
                self.sent.append(message)
                if message["type"] == "ClientRequestHandshake":
                    controls = [encode_session_message("ServerRequestAddPeer", {
                        "peer_id": 2, "peer_value": 7, "assign_local": 1,
                    }), encode_session_message("ServerRespondConnected", {})]
                    payload = encode_payload("local-ai", [{"word_8": 1, "payload_hex": data.hex()}
                                                          for data in controls], 0, "host")
                    self.pending.append({"event": "match_data", "opcode": 255, "payload": payload,
                                         "presence": {"user_id": "host"}})
                elif message["type"] == "add_player":
                    writer.decoded_worlds = 1
                return b"submitted"

        fields = SessionCodecTests().handshake_fields()
        handshake = {"command": "send_session_message", "type": "ClientRequestHandshake",
                     "recipient": "host", "fields": fields}
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            with EventWriter(Path(directory) / "capture", quiet=True) as writer:
                peer = Peer()
                run_connection(peer, "room", writer, handshake=handshake, player_name="LocalAI", duration=0.15)
        commands = [row for row in peer.sent if row["channel"] == "command"]
        self.assertEqual([row["type"] for row in commands], ["add_player", "set_player_here_command"])
        self.assertEqual([row["default_state"]["identity"] for row in commands], [0, 1])
        self.assertEqual(commands[-1]["fields"]["machineid"], 2)

    def test_join_pause_is_released_after_host_confirms_world_loaded(self):
        class Peer:
            def __init__(self):
                self.pending = []
                self.sent = []

            def connect(self):
                self.pending.append({"event": "connected"})

            def join(self, room_id, metadata):
                self.pending.append({"event": "room_joined", "match_id": room_id})

            def poll(self):
                events, self.pending = self.pending, []
                return events

            def receive(self, messages):
                packet = encode_payload("local-ai", messages, 1, "host")
                self.pending.append({"event": "match_data", "opcode": 255, "payload": packet,
                                     "presence": {"user_id": "host"}})

            def send_envelope(self, room_id, recipient, messages, magic=0xCAFE, presences=None):
                message = writer.game_codec.decode_pdx_message(messages[0], from_server=False)
                self.sent.append(message)
                if message["type"] == "ClientRequestHandshake":
                    controls = [encode_session_message("ServerRequestAddPeer", {
                        "peer_id": 2, "peer_value": 7, "assign_local": 1,
                    }), encode_session_message("ServerRespondConnected", {})]
                    self.receive([{"word_8": 1, "payload_hex": item.hex()} for item in controls])
                elif message["type"] == "add_player":
                    writer.decoded_worlds = 1
                    sync = writer.game_codec.encode_command("sync_players_command", {
                        "machineid": [1, 2], "players": ["Host", "LocalAI"],
                        "character": [12345, 0xFFFFFFFF], "flags": [0, 5],
                    }, {"identity": 10, "sender": 1, "receiver": 0})
                    pause = writer.game_codec.encode_command("pause_game_command", {
                        "player": 2, "value": True,
                    }, {"identity": 11, "sender": 1, "receiver": 0, "attribs": 8})
                    self.receive([{"word_8": 0, "payload_hex": item.hex()} for item in (sync, pause)])
                elif message["type"] == "set_player_here_command":
                    self.receive(messages)
                return b"submitted"

        handshake = {"command": "send_session_message", "type": "ClientRequestHandshake",
                     "recipient": "host", "fields": SessionCodecTests().handshake_fields()}
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with EventWriter(Path(folder) / "capture", quiet=True) as writer:
                peer = Peer()
                run_connection(peer, "room", writer, handshake=handshake,
                               player_name="LocalAI", duration=0.2)
        commands = [row for row in peer.sent if row["channel"] == "command"]
        self.assertEqual([row["type"] for row in commands],
                         ["add_player", "set_player_here_command", "pause_game_command"])
        self.assertEqual(commands[-1]["fields"], {"player": 2, "value": False})
        self.assertEqual(commands[-1]["default_state"]["attribs"], 8)
        self.assertEqual(len({row["default_state"]["identity"] for row in commands}), 3)

    def test_invalid_tokens_are_rejected_before_native_calls(self):
        for token in (None, "", "a.b.c", "not a jwt"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                validate_token(token)

    def test_authenticated_urls_are_redacted(self):
        token = make_mock_token()
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()) as stdout:
            with EventWriter(Path(folder) / "capture", secrets=[token]) as writer:
                event = writer.write({"event": "error", "message": "ws://localhost/ws?token=" + token + "&status=false"})
            self.assertNotIn(token, stdout.getvalue())
            self.assertIn("[redacted]", event["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
