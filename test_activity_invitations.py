"""Regress native invitation ownership and model-selected activity responses."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import patch

from game_actions import action
from game_notifications import ActivityInvitationNotifications
from jomini_codec import JominiCodec, travel_plan_fields
from live_objects import DirectWorldReader
from memory_world import MemoryWorld
from model_runtime import ModelConfig, ModelRuntime
from test_siege_progress import BufferMemory


class ActivityFixture:
    def __init__(self):
        self.world = MemoryWorld.__new__(MemoryWorld)
        self.world.base = 0x140000000
        self.memory = self.world.memory = BufferMemory()
        self.world.tables = {}
        self.world.references = set()
        self.character = 0x20002000
        self.alive = 0x20003000
        self.invitations = 0x20004000
        self.activity = 0x20005000
        self.definition = 0x20006000
        self.phases = 0x20007000
        self.identifier = 570425364
        self.memory.put(self.world.base + 0x5c7c4f0, "Q", 0x20000000)
        self.memory.put(0x20000020, "Q", 0x20001000)
        self.memory.put(0x2000002c, "I", 100)
        for identifier, pointer, name in ((42, self.character, "Richard"), (70, 0x20002800, "Host")):
            self.memory.put(0x20001000 + identifier * 16 + 8, "Q", pointer)
            self.memory.put(pointer, "Q", self.world.base + 0x47748a0)
            self.memory.put(pointer + 0x18, "I", identifier)
            self.string(pointer + 0x20, name)
        self.memory.put(self.character + 0x1b0, "Q", self.alive)
        self.memory.put(self.alive + 0x550, "QII", self.invitations, 2, 1)
        self.memory.put(self.invitations, "I", self.identifier)
        self.memory.put(self.world.base + 0x5d32c90, "Q", 0x20008000)
        self.memory.put(0x20008020, "Q", 0x20009000)
        self.memory.put(0x2000802c, "I", 64)
        self.memory.put(0x20009000 + (self.identifier & 0xffffff) * 16 + 8, "Q", self.activity)
        self.memory.put(self.activity, "QII", self.world.base + 0x473ea08, self.identifier, 0x41637469)
        self.memory.put(self.activity + 0x10, "Q", self.world.base + 0x473e9d0)
        self.memory.put(self.activity + 0x3a0, "QI", self.definition, 70)
        self.definition_key(self.definition, "activity_coronation")
        self.memory.put(self.activity + 0x3c8, "QII", self.phases, 3, 3)
        self.memory.put(self.activity + 0x3e0, "i", 0)
        self.memory.put(self.activity + 0x3ec, "I", 54295008)
        self.memory.put(self.activity + 0x3f4, "I", 54296760)
        self.memory.put(self.activity + 0x420, "2B", 0, 0)
        for index, key in enumerate(("preparations", "coronation", "feast")):
            definition = self.definition + (index + 1) * 0x100
            self.definition_key(definition, key)
            self.memory.put(self.phases + index * 0x38, "QI", definition, 2575)
        self.memory.put(self.activity + 0x528, "QII", 0x20004100, 2, 2)
        self.memory.put(0x20004100, "2I", 70, 99)

    def string(self, address, value):
        raw = value.encode("utf-8")
        if len(raw) > 15:
            self.memory.put(address, "Q", address + 0x10000)
            self.memory.write(address + 0x10000, raw)
        else:
            self.memory.write(address, raw.ljust(16, b"\0"))
        self.memory.put(address + 0x10, "QQ", len(raw), max(15, len(raw)))

    def definition_key(self, address, key):
        self.string(address + 0x18, key)
        self.memory.put(address + 0x38, "I", 0x4744624f)

    def query(self, **request):
        return self.world._query({"kind": "activity_invitations", "character": 42, **request})


class ActivityInvitationTests(unittest.TestCase):
    def test_list_uses_native_character_invitations(self):
        fixture = ActivityFixture()
        rows = fixture.query()["items"]
        self.assertEqual([row["id"] for row in rows], [570425364])
        self.assertEqual(rows[0]["activity"]["key"], "activity_coronation")
        self.assertEqual(rows[0]["host"], {"id": 70, "name": "Host"})
        self.assertEqual(rows[0]["active_start_date"], 54296760)
        self.assertFalse(rows[0]["active"])
        self.assertEqual(fixture.query(character=70)["count"], 0)

    def test_world_activity_directory_uses_native_instances_and_separate_invitation_status(self):
        fixture = ActivityFixture()
        fixture.memory.put(fixture.alive + 0x55c, "I", 0)
        result = fixture.query(kind="activities")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["source"], "native_activity_database")
        self.assertEqual(result["items"][0]["details_query"],
                         {"kind": "activity", "id": fixture.identifier, "character": 42})
        detail = fixture.world._query(result["items"][0]["details_query"])
        self.assertNotIn("invitation_state", detail)
        self.assertEqual([option["id"] for option in detail["response_options"]], ["join"])
        self.assertIsNone(detail["response_options"][0]["available"])
        self.assertEqual(fixture.query()["count"], 0)

    def test_world_activity_list_filters_native_completed_flag(self):
        fixture = ActivityFixture()
        fixture.memory.put(fixture.activity + 0x421, "B", 1)
        self.assertEqual(fixture.query(kind="activities")["count"], 0)
        self.assertEqual(fixture.query(kind="activities", completed=True)["count"], 1)
        with self.assertRaisesRegex(ValueError, "completed"):
            fixture.query(kind="activities", completed=1)

    def test_observer_can_read_activity_without_selecting_a_character(self):
        fixture = ActivityFixture()
        result = fixture.query(kind="activity", id=fixture.identifier, character=None)
        self.assertIsNone(result["character_id"])
        self.assertNotIn("character", result["details_query"])
        self.assertNotIn("character", result["response_options"][0]["action"]["parameters"])

    def test_detail_preserves_native_phases_and_attendees(self):
        fixture = ActivityFixture()
        detail = fixture.query(kind="activity_invitation", id=fixture.identifier)
        self.assertEqual(detail["current_phase"], 0)
        self.assertEqual([row["key"] for row in detail["phases"]], ["preparations", "coronation", "feast"])
        self.assertTrue(all(row["province_id"] == 2575 for row in detail["phases"]))
        self.assertEqual(detail["attending_character_ids"], [70, 99])
        self.assertIsNone(detail["display"]["description"])
        self.assertTrue(all(row["available"] is None for row in detail["response_options"]))
        self.assertEqual([row["id"] for row in detail["response_options"]], ["join", "decline"])
        self.assertEqual(detail["response_options"][0]["action"]["parameters"]["activity"], fixture.identifier)

    def test_foreign_removed_and_reused_activity_references_are_rejected(self):
        fixture = ActivityFixture()
        with self.assertRaisesRegex(ValueError, "unavailable for the character"):
            fixture.query(kind="activity_invitation", id=fixture.identifier, character=70)
        fixture.memory.put(fixture.activity + 8, "I", fixture.identifier + 0x1000000)
        with self.assertRaisesRegex(ValueError, "expired"):
            fixture.query(kind="activity_invitation", id=fixture.identifier)
        fixture.memory.put(fixture.alive + 0x55c, "I", 0)
        self.assertEqual(fixture.query()["count"], 0)

    def test_changes_to_invitation_list_during_reading_are_rejected(self):
        fixture = ActivityFixture()
        original = fixture.memory.read
        changed = False

        def read(address, size, **options):
            nonlocal changed
            result = original(address, size, **options)
            if address == fixture.activity + 0x3a0 and not changed:
                changed = True
                fixture.memory.put(fixture.alive + 0x55c, "I", 0)
            return result

        fixture.memory.read = read
        with self.assertRaisesRegex(ValueError, "invitations changed"):
            fixture.query()

    def test_malformed_phase_vector_is_rejected(self):
        fixture = ActivityFixture()
        fixture.memory.put(fixture.activity + 0x3d4, "I", 4)
        with self.assertRaisesRegex(ValueError, "phase vector"):
            fixture.query(kind="activity_invitation", id=fixture.identifier)

    def test_attendee_changes_during_detail_read_are_rejected(self):
        fixture = ActivityFixture()
        original = fixture.memory.read
        phase_reads = 0

        def read(address, size, **options):
            nonlocal phase_reads
            result = original(address, size, **options)
            if address == fixture.phases:
                phase_reads += 1
                if phase_reads == 2:
                    fixture.memory.put(0x20004100, "I", 42)
            return result

        fixture.memory.read = read
        with self.assertRaisesRegex(ValueError, "attendees changed"):
            fixture.query(kind="activity_invitation", id=fixture.identifier)

    def test_public_reader_defaults_recipient_formats_dates_and_selects_fields(self):
        fixture = ActivityFixture()

        class World:
            def __init__(self, pid=None):
                self.memory = fixture.memory

            def query(self, request):
                return {"data": fixture.world._query(request), "runtime": {"date": 54296040, "tick": 1, "world": 100}}

            def close(self):
                pass

        with patch("live_objects.MemoryWorld", World), DirectWorldReader(character_id=42) as reader:
            listed = reader.handle({"kind": "activity_invitations", "fields": ["id", "active_start_date"]})
            self.assertEqual(listed["data"]["items"], [{"id": fixture.identifier, "active_start_date": "1198.4.6"}])
            detail = reader.handle({"kind": "activity_invitation", "id": fixture.identifier,
                                    "fields": ["response_options"], "max_chars": 3000})
            self.assertEqual(len(detail["data"]["response_options"]), 2)
            schema = reader.handle({"command": "schema", "kind": "activity_invitation"})
            self.assertIn("response_options", schema["data"])
            activities = reader.handle({"kind": "activities", "fields": ["id", "details_query"]})
            query = activities["data"]["items"][0]["details_query"]
            self.assertEqual(query["character"], 42)
            self.assertEqual(query["kind"], "activity")
            self.assertEqual(reader.handle(query)["data"]["host"]["id"], 70)

    def test_notifications_are_recipient_scoped_deduplicated_and_do_not_choose(self):
        class Reader:
            rows = [{"id": 10, "character_id": 42}, {"id": 11, "character_id": 99}]

            def handle(self, request):
                return {"date": "1198.2.15", "data": {"items": self.rows, "next_offset": None}}

        reader = Reader()
        source = ActivityInvitationNotifications(reader)
        self.assertEqual([row["data"]["id"] for row in source.poll(42)], [10])
        self.assertEqual(source.poll(42), [])
        reader.rows = []
        self.assertEqual(source.poll(42), [])
        reader.rows = [{"id": 10, "character_id": 42}]
        self.assertEqual(len(source.poll(42)), 1)

    def test_response_commands_encode_native_optional_fields(self):
        codec = JominiCodec()
        header = {"sender": 2, "receiver": 0, "identity": 3, "attribs": 14}
        for name in ("join_activity_command", "decline_activity_invite_command"):
            fields = {"activity": 570425364, "character": 42}
            encoded = codec.encode_command(name, fields, header)
            self.assertEqual(codec.decode_command(encoded)["fields"], fields)
            self.assertTrue(action(name)["parameters"]["activity"]["required"])
        fields.update(travel="no", check_start_date=False)
        encoded = codec.encode_command("join_activity_command", fields, header)
        self.assertEqual(codec.decode_command(encoded)["fields"], fields)
        draft = travel_plan_fields(54296040, 12, destinations=[45], waypoints=[[45]])
        with_draft = codec.encode_command("join_activity_command", {**fields, "travel_plan_draft_created": draft}, header)
        self.assertEqual(codec.decode_command(with_draft)["fields"]["travel_plan_draft_created"]["destinations"], [45])
        with self.assertRaisesRegex(ValueError, "Unknown fields"):
            codec.encode_command("join_activity_command", {"unknown_field": {}}, header)

    def test_client_loop_dispatches_start_activity_and_set_intent_with_dedup(self):
        from ck3_client import EventWriter, run_connection

        class Peer:
            def __init__(self):
                self.events = []
                self.sent = []

            def connect(self):
                self.events.append({"event": "connected"})

            def join(self, room_id, metadata):
                self.events.append({"event": "room_joined", "match_id": room_id})

            def poll(self):
                result, self.events = self.events, []
                return result

            def send_envelope(self, room_id, recipient, messages, magic=0xcafe, presences=None):
                self.sent.append((recipient, JominiCodec().decode_pdx_message(messages[0], from_server=False)))
                return b"sent"

        commands = queue.SimpleQueue()
        start_request = {
            "command": "start_activity_command",
            "request_id": "act-1",
            "type": "activity_feast",
            "start_date": 54296760,
            "phases": [{"phase": "feast_phase_meal", "province": 12}],
            "options": {"feast_option_food": "feast_food_good"},
        }
        commands.put(start_request)
        commands.put(dict(start_request))
        commands.put({"command": "set_activity_intent_command", "request_id": "intent-1", "intent": "recreation_intent", "target": 99})
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with EventWriter(Path(folder) / "client", quiet=True) as writer:
                writer.session_state.connected = True
                writer.session_state.local_machineid = 2
                writer.observations.set_character(42)
                peer = Peer()
                run_connection(peer, "room", writer, commands=commands, duration=0.05,
                               handshake={"command": "send_session_message", "recipient": "host",
                                          "type": "ClientRequestConnected", "fields": {}})
        gameplay = [command for _, command in peer.sent if command["channel"] == "command"]
        self.assertEqual([command["type"] for command in gameplay], ["start_activity_command", "set_activity_intent_command"])
        self.assertEqual(gameplay[0]["fields"]["data"]["host"], 42)
        self.assertEqual(gameplay[0]["fields"]["data"]["type"], "activity_feast")
        self.assertEqual(gameplay[1]["fields"], {"character": 42, "intent": {"type": "recreation_intent", "target": 99}})

    def test_model_must_explicitly_choose_a_response_and_target(self):
        settings = ModelConfig(endpoint="http://127.0.0.1:1/v1/chat/completions", model="offline")
        with redirect_stdout(io.StringIO()):
            runtime = ModelRuntime(settings, model=object())
            try:
                for name in ("join_activity_command", "decline_activity_invite_command"):
                    with self.assertRaisesRegex(ValueError, "activity"):
                        runtime._action({"action": name, "request_id": "activity-1", "parameters": {"character": 42}})
                    request = runtime._action({"action": name, "request_id": "activity-2",
                                               "parameters": {"activity": 570425364, "character": 42}})
                    self.assertEqual(request["type"], name)
                    self.assertEqual(request["fields"]["activity"], 570425364)
            finally:
                runtime.close()
