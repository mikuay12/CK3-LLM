"""Regress passive military notifications and delivery acknowledgement."""

from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from live_objects import DirectNotifications


class NotificationReader:
    def __init__(self):
        self.reports = [{"id": 7, "end_date": "943.2.6", "outcome": "defeat"}]
        self.unit = {"id": 42, "path": [], "retreating": False, "province_id": 10}
        self.wars = [3]
        self.fail_unit = False
        self.combats = []

    def handle(self, request):
        if request["kind"] == "combat_results":
            return {"data": {"items": deepcopy(self.reports), "next_offset": None}}
        if request["kind"] == "character":
            return {"date": "943.2.6", "data": {"military": {"unit_ids": [42], "war_ids": list(self.wars)}}}
        if request["kind"] == "combats":
            return {"date": "943.2.6", "data": {"items": deepcopy(self.combats), "next_offset": None}}
        if self.fail_unit:
            raise OSError("A unit changed during the read")
        return {"data": deepcopy(self.unit)}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.reader = NotificationReader()
        self.notifications = DirectNotifications(self.reader)

    def test_retained_battle_report_is_delivered_once(self):
        events = self.notifications.poll(1)
        self.assertEqual(events[0]["type"], "battle_result")
        self.assertEqual(events[0]["data"]["outcome"], "defeat")
        self.assertEqual(self.notifications.poll(1), [])

    def test_failed_observation_does_not_acknowledge_reports(self):
        self.reader.fail_unit = True
        with self.assertRaises(OSError):
            self.notifications.poll(1)
        self.reader.fail_unit = False
        self.assertEqual(self.notifications.poll(1)[0]["type"], "battle_result")

    def test_retreat_and_movement_stop_are_reported(self):
        self.notifications.poll(1)
        self.reader.unit.update(path=[20], retreating=True)
        self.assertEqual(self.notifications.poll(1)[0]["type"], "retreat_started")
        self.reader.unit.update(path=[], retreating=False, province_id=20)
        event = self.notifications.poll(1)[0]
        self.assertEqual(event["type"], "retreat_ended")
        self.assertEqual(event["data"]["province_id"], 20)

    def test_war_changes_are_reported(self):
        self.notifications.poll(1)
        self.reader.wars = [4]
        events = self.notifications.poll(1)
        self.assertEqual({event["type"] for event in events}, {"war_started", "war_ended"})

    def test_character_change_resets_notification_state(self):
        self.notifications.poll(1)
        events = self.notifications.poll(2)
        self.assertEqual(events[0]["character_id"], 2)
        self.assertEqual(events[0]["type"], "battle_result")

    def test_delivered_unit_changes_cannot_mutate_baseline(self):
        self.notifications.poll(1)
        self.reader.unit["path"] = [20]
        event = self.notifications.poll(1)[0]
        event["data"]["path"].clear()
        self.reader.unit["path"] = []
        self.assertEqual(self.notifications.poll(1)[0]["type"], "movement_stopped")

    def test_active_battle_and_phase_changes_are_reported_once(self):
        self.notifications.poll(1)
        self.reader.combats = [{"id": 8, "phase": "maneuver", "attacker_advantage": -20}]
        event = self.notifications.poll(1)[0]
        self.assertEqual(event["type"], "battle_started")
        self.assertEqual(event["data"]["attacker_advantage"], -20)
        self.assertEqual(self.notifications.poll(1), [])
        self.reader.combats[0]["phase"] = "main"
        self.assertEqual(self.notifications.poll(1)[0]["type"], "battle_phase_changed")

    def test_failed_poll_does_not_acknowledge_battle_start(self):
        self.notifications.poll(1)
        self.reader.combats = [{"id": 8, "phase": "main"}]
        self.reader.fail_unit = True
        with self.assertRaises(OSError):
            self.notifications.poll(1)
        self.reader.fail_unit = False
        self.assertEqual(self.notifications.poll(1)[0]["type"], "battle_started")


class MilitaryDecisionPauseTests(unittest.TestCase):
    def run_events(self, enabled):
        from ck3_client import EventWriter, run_connection
        from jomini_codec import JominiCodec

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

            def send_envelope(self, room_id, recipient, messages, magic=0xcafe, presences=None):
                self.sent.append(JominiCodec().decode_pdx_message(messages[0], from_server=False))
                return b"submitted"

        class Notifications:
            def __init__(self):
                self.sent = False

            def poll(self, character_id=None):
                if self.sent:
                    return []
                self.sent = True
                return [{"event": "game_notification", "type": "battle_result", "character_id": 42},
                        {"event": "game_notification", "type": "retreat_ended", "character_id": 42},
                        {"event": "game_notification", "type": "battle_started", "character_id": 43}]

            def finish(self):
                return []

        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with EventWriter(Path(folder) / "capture", quiet=True) as writer:
                writer.session_state.connected = True
                writer.session_state.local_machineid = 2
                writer.observations.set_character(42)
                writer.live_memory_pid = 123
                writer.live_observations = Notifications()
                peer = Peer()
                run_connection(peer, "room", writer, duration=0.03,
                               handshake={"command": "send_session_message", "recipient": "host",
                                          "type": "ClientRequestConnected", "fields": {}},
                               pause_on_military_event=enabled)
            events = [json.loads(line) for line in
                      (Path(folder) / "capture" / "events.jsonl").read_text().splitlines()]
        return [command for command in peer.sent if command["channel"] == "command"], events

    def test_military_pause_is_optional(self):
        commands, events = self.run_events(False)
        self.assertEqual(commands, [])
        self.assertFalse(any(event["event"] == "military_decision_pause_requested" for event in events))

    def test_current_character_events_pause_once_per_batch(self):
        commands, events = self.run_events(True)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["type"], "pause_game_command")
        self.assertEqual(commands[0]["fields"], {"player": 2, "value": True})
        pauses = [event for event in events if event["event"] == "military_decision_pause_requested"]
        self.assertEqual(pauses[0]["causes"], ["battle_result", "retreat_ended"])


if __name__ == "__main__":
    unittest.main()
