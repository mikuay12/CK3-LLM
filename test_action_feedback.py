"""Regress retry-safe action submission and actual host echo correlation."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import patch

from action_feedback import ActionFeedback
from ck3_client import EventWriter, run_connection
from jomini_codec import JominiCodec
from pdx_nakama_codec import encode_payload


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.codec = JominiCodec()
        self.feedback = ActionFeedback()
        self.context = {"room": "room", "host": "host", "machineid": 2, "character_id": 42,
                        "world": 100, "world_data": 200, "connected": True, "active": True, "tick": 10}
        self.feedback.synchronize(self.context)
        self.request = {"command": "send_game_command", "request_id": "hire-1", "type": "hire_mercenary",
                        "fields": {"character": 42, "mercenary_company": 95, "flags": 3}}
        self.native = self.codec.decode_command(self.codec.encode_command(
            self.request["type"], self.request["fields"], {"identity": 10, "sender": 2, "receiver": 0, "attribs": 14}))

    def prepare(self, request=None, native=None):
        self.feedback.prepare(request or self.request, native or self.native)
        self.feedback.submitted((request or self.request)["request_id"])

    def echo(self, native=None, source="host"):
        return self.feedback.observe({"channel": "command", **(native or self.native)}, source)

    def test_retry_reuses_feedback_and_conflicting_request_fails(self):
        self.prepare()
        replay = self.feedback.replay(self.request)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["status"], "submitted")
        with self.assertRaisesRegex(ValueError, "different action"):
            self.feedback.replay({**self.request, "fields": {"character": 42, "mercenary_company": 96}})

    def test_echo_requires_host_header_type_and_canonical_fields(self):
        self.prepare()
        self.assertNotIn("flags", self.native["fields"])
        self.assertEqual(self.echo(source="other-player"), [])
        self.assertEqual(self.echo({**self.native, "type": "disband_army"}), [])
        self.assertEqual(self.echo({**self.native, "fields": {"character": 42, "mercenary_company": 96}}), [])
        self.assertEqual(self.echo()[0]["status"], "host_echoed")
        self.assertEqual(self.echo(), [])
        visible = json.dumps(self.feedback.status("hire-1"))
        for internal in ("identity", "sender", "world", "tick"):
            self.assertNotIn(internal, visible)
        self.assertEqual(self.feedback.status("hire-1")["effect_verification"], "model_state_query_required")

    def test_timeout_reports_unknown_delivery_and_accepts_late_echo(self):
        with patch("action_feedback.time.monotonic", return_value=0):
            self.prepare()
        with patch("action_feedback.time.monotonic", return_value=31):
            self.assertEqual(self.feedback.poll()[0]["status"], "delivery_unconfirmed")
            self.assertEqual(self.feedback.poll(), [])
        self.assertEqual(self.echo()[0]["status"], "host_echoed")

    def test_world_character_disconnect_and_rewind_cancel_pending_feedback(self):
        for changes in ({"world": 101}, {"character_id": 99}, {"connected": False}, {"tick": 9}):
            with self.subTest(changes=changes):
                self.setUp()
                self.prepare()
                events = self.feedback.synchronize({**self.context, **changes})
                self.assertEqual(events[0]["status"], "session_changed")
                self.assertEqual(self.echo(), [])
                self.assertEqual(self.feedback.replay(self.request)["status"], "session_changed")

    def test_send_error_and_early_echo_preserve_observed_delivery(self):
        self.feedback.prepare(self.request, self.native)
        self.assertEqual(self.echo()[0]["status"], "host_echoed")
        self.assertIsNone(self.feedback.submitted("hire-1"))
        self.assertIsNone(self.feedback.failed("hire-1", RuntimeError("late send callback")))

    def test_failed_send_retains_identity_for_a_late_echo(self):
        self.prepare()
        self.feedback.failed("hire-1", RuntimeError("send failed"))
        with self.assertRaisesRegex(ValueError, "already uses"):
            self.feedback.prepare({**self.request, "request_id": "hire-2"}, self.native)
        self.assertEqual(self.echo()[0]["request_id"], "hire-1")

    def test_storage_keeps_unconfirmed_ids_and_reused_header_mapping(self):
        self.feedback.capacity = 2
        self.prepare()
        self.echo()
        second = {**self.request, "request_id": "hire-2"}
        self.prepare(second)
        third = {**self.request, "request_id": "hire-3"}
        native = deepcopy(self.native)
        native["default_state"]["identity"] += 1
        self.prepare(third, native)
        self.assertEqual(self.echo()[0]["request_id"], "hire-2")
        with self.assertRaises(ValueError):
            self.feedback.status("hire-1")
        self.feedback = ActionFeedback(capacity=1)
        self.feedback.synchronize(self.context)
        self.prepare()
        with self.assertRaisesRegex(ValueError, "unconfirmed"):
            self.prepare(second, native)


class ConnectionFeedbackTests(unittest.TestCase):
    def test_failed_send_reserves_identity_before_the_next_action(self):
        commands = queue.SimpleQueue()
        for identifier in ("focus-1", "focus-2"):
            commands.put({"command": "send_game_command", "request_id": identifier, "type": "set_character_focus",
                          "fields": {"actor": 42, "target": 42, "focus_type": "martial_strategy_focus"},
                          "header": {"attribs": 14}})
        commands.put({"command": "stop"})
        sent = []

        class Peer:
            def __init__(self):
                self.events = []

            def connect(self):
                self.events.append({"event": "connected"})

            def join(self, room, metadata):
                self.events.append({"event": "room_joined", "match_id": room})

            def poll(self):
                result, self.events = self.events, []
                return result

        def send(client, room, command, codec):
            sent.append(deepcopy(command))
            if len(sent) == 1:
                raise OSError("Send failed after submission")
            return b"sent"

        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with EventWriter(Path(folder) / "capture", quiet=True) as writer, patch("ck3_client.execute_command", side_effect=send):
                writer.session_state.connected = True
                writer.session_state.local_machineid = 2
                writer.observations.set_character(42)
                run_connection(Peer(), "room", writer, commands=commands, duration=1, disconnect_timeout=0.04,
                               handshake={"command": "send_session_message", "recipient": "host",
                                          "type": "ClientRequestConnected", "fields": {}})
                actions = [command for command in sent if command["command"] == "send_game_command"]
                self.assertEqual([command["header"]["identity"] for command in actions], [0, 1])
                self.assertEqual(writer.actions.status("focus-1")["status"], "send_failed")
                self.assertEqual(writer.actions.status("focus-2")["status"], "submitted")
                first = writer.actions.records["focus-1"]["native"]
                writer.actions.observe({"channel": "command", **first}, "host")
                self.assertEqual(writer.actions.status("focus-1")["status"], "host_echoed")
                self.assertEqual(writer.actions.status("focus-2")["status"], "submitted")

    def test_normal_sdk_loop_deduplicates_actions_and_receives_real_encoded_echo(self):
        commands = queue.SimpleQueue()
        request = {"command": "send_game_command", "request_id": "focus-1", "type": "set_character_focus",
                   "fields": {"actor": 42, "target": 42, "focus_type": "martial_strategy_focus"},
                   "header": {"attribs": 14}}
        commands.put(request)
        commands.put(deepcopy(request))
        commands.put({**request, "fields": {**request["fields"], "focus_type": "martial_authority_focus"}})
        commands.put({"command": "query", "kind": "action_status", "id": "focus-1", "request_id": "before"})

        class Peer:
            def __init__(self):
                self.events = []
                self.sent = []
                self.final_query = False

            def connect(self):
                self.events.append({"event": "connected"})

            def join(self, room_id, metadata):
                self.events.append({"event": "room_joined", "match_id": room_id})

            def poll(self):
                if self.sent and not self.final_query:
                    commands.put({"command": "query", "kind": "action_status", "id": "focus-1", "request_id": "after"})
                    commands.put({"command": "stop"})
                    self.final_query = True
                result, self.events = self.events, []
                return result

            def send_envelope(self, room_id, recipient, messages, magic=0xcafe, presences=None):
                command = JominiCodec().decode_pdx_message(messages[0], from_server=False)
                if command["channel"] == "command":
                    self.sent.append(command)
                    self.events.append({"event": "match_data", "opcode": 255,
                                        "payload": encode_payload("observer", messages, len(self.sent), "host"),
                                        "presence": {"user_id": "host"}})
                return b"submitted"

        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with EventWriter(Path(folder) / "capture", quiet=True) as writer:
                writer.session_state.connected = True
                writer.session_state.local_machineid = 2
                writer.observations.set_character(42)
                peer = Peer()
                result = run_connection(peer, "room", writer, commands=commands, duration=1,
                                        handshake={"command": "send_session_message", "recipient": "host",
                                                   "type": "ClientRequestConnected", "fields": {}})
                self.assertEqual(writer.actions.status("focus-1")["status"], "host_echoed")
            events = [json.loads(line) for line in (Path(folder) / "capture/events.jsonl").read_text().splitlines()]
        self.assertEqual(result["reason"], "stop_command")
        self.assertEqual(len(peer.sent), 1)
        replies = {event["request_id"]: event["result"]["data"] for event in events if event["event"] == "interface_response"}
        self.assertEqual(replies["before"]["status"], "submitted")
        self.assertEqual(replies["after"]["status"], "host_echoed")
        errors = [event for event in events if event["event"] == "command_error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("different action", errors[0]["message"])

    def test_start_activity_echo_ignores_native_empty_tenet_and_doctrine_padding(self):
        feedback = ActionFeedback()
        feedback.synchronize({"room": "r", "host": "host-1", "machineid": 2, "character_id": 42,
                              "world": 1, "world_data": 1, "connected": True, "active": True, "tick": 1})
        request = {"command": "start_activity_command", "request_id": "feast-1", "type": "activity_feast"}
        native = {
            "channel": "command", "type": "start_activity_command",
            "default_state": {"identity": 10, "sender": 2, "receiver": 0, "attribs": 14},
            "fields": {"data": {"type": "activity_feast", "host": 42, "start_date": 51959040,
                                "phases": [{"phase": "feast_phase_meal", "province": 2214}],
                                "invitations": [[70]]}},
        }
        feedback.prepare(request, native)
        feedback.submitted("feast-1")
        echoed = {
            **native,
            "fields": {"data": {**native["fields"]["data"], "tenets": ["", "", ""], "doctrines": ["", "", ""]}},
        }
        events = feedback.observe(echoed, "host-1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "host_echoed")
        self.assertEqual(feedback.status("feast-1")["status"], "host_echoed")


if __name__ == "__main__":
    unittest.main()
