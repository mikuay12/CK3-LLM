"""Regress native request ownership and explicit model-selected replies."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import queue
import struct
import tempfile
import unittest
from unittest.mock import patch

from ck3_client import EventWriter, run_connection
from game_actions import action
from game_notifications import PendingInteractionNotifications
from jomini_codec import INTERACTION_REPLIES, JominiCodec
from live_objects import DirectWorldReader
from memory_world import MemoryWorld
from model_runtime import ModelConfig, ModelRuntime
from pdx_nakama_codec import encode_payload
from test_siege_progress import BufferMemory


class RequestFixture:
    def __init__(self):
        self.world = MemoryWorld.__new__(MemoryWorld)
        self.world.base = 0x140000000
        self.memory = self.world.memory = BufferMemory()
        self.world.tables = {}
        self.world.references = set()
        self.world._scope_tokens = None
        self.db = 0x20000000
        self.slots = 0x20001000
        self.character_db = 0x20002000
        self.character_slots = 0x20003000
        self.definition = 0x20005000
        self.pointer = 0x20010000
        self.identifier = 0x1000001
        self.date = 365 * 24 * 5954
        self.memory.put(self.world.base + 0x5d33c78, "Q", self.db)
        self.memory.put(self.db + 0x20, "Q", self.slots)
        self.memory.put(self.db + 0x2c, "I", 4)
        self.memory.put(self.world.base + 0x5c7c4f0, "Q", self.character_db)
        self.memory.put(self.character_db + 0x20, "Q", self.character_slots)
        self.memory.put(self.character_db + 0x2c, "I", 100)
        for identifier, name in ((42, "Receiver"), (70, "Delegate"), (99, "Sender")):
            pointer = 0x20100000 + identifier * 0x100
            self.memory.put(self.character_slots + identifier * 16 + 8, "Q", pointer)
            self.memory.put(pointer, "Q", self.world.base + 0x47748a0)
            self.memory.put(pointer + 0x18, "I", identifier)
            self.string(pointer + 0x20, name)
        self.string(self.definition + 0x18, "gift_interaction")
        self.memory.put(self.definition + 0x38, "I", 0x4744624f)
        self.install(self.identifier, self.pointer, actor=99, recipient=42)
        self.install(0x2000002, self.pointer + 0x1000, actor=42, recipient=99)
        self.install(0x3000003, self.pointer + 0x2000, actor=99, recipient=70, intermediary=42, status=1)

    def string(self, address, value):
        raw = value.encode("utf-8")
        if len(raw) > 15:
            data = address + 0x1000000
            self.memory.put(address, "Q", data)
            self.memory.write(data, raw)
        else:
            self.memory.write(address, raw.ljust(16, b"\0"))
        self.memory.put(address + 0x10, "QQ", len(raw), max(15, len(raw)))

    def install(self, identifier, pointer, *, actor, recipient, intermediary=0xffffffff, status=0):
        self.memory.put(self.slots + (identifier & 0xffffff) * 16 + 8, "Q", pointer)
        self.memory.put(pointer, "Q", self.world.base + 0x4769590)
        self.memory.put(pointer + 0x10, "IIQ", identifier, 0x4368496e, self.definition)
        self.memory.put(pointer + 0x2f0, "6I", actor, recipient, 0xffffffff, 0xffffffff, intermediary, actor)
        self.memory.put(pointer + 0x5b8, "3I3B", 7, 4, status, 1, 0, 0)

    def query(self, **request):
        return self.world._query({"kind": "incoming_interactions", "character": 42, **request})

    def install_text_cache(self, identifier=None):
        from building_economy import APPLICATION_RVA, HOLDING_VIEW_CHAIN

        address = self.world.base + APPLICATION_RVA
        for index, (vtable, next_offset) in enumerate(HOLDING_VIEW_CHAIN[:-1]):
            interface = 0x20400000 + index * 0x1000
            self.memory.put(address, "Q", interface)
            self.memory.put(interface, "Q", self.world.base + vtable)
            address = interface + next_offset
        window = 0x20410000
        self.cache = 0x20420000
        self.memory.put(interface + 0xf8, "Q", window)
        self.memory.put(window, "Q", self.world.base + 0x455ce98)
        self.memory.put(window + 0x3f8, "QII", self.cache, 1, 1)
        self.memory.put(self.cache, "I", self.identifier if identifier is None else identifier)
        for index, value in enumerate(("Delegate opening", "Delegate body", "Recipient opening",
                                       "Recipient body", "Signed", "Native signature")):
            self.string(self.cache + 8 + index * 0x20, value)


class PendingInteractionTests(unittest.TestCase):
    def test_incoming_queue_includes_recipient_and_pending_intermediary(self):
        fixture = RequestFixture()
        result = fixture.query()
        self.assertEqual([row["id"] for row in result["items"]], [0x1000001, 0x3000003])
        self.assertEqual([row["role"] for row in result["items"]], ["recipient", "intermediary"])
        self.assertTrue(all(row["character_id"] == 42 for row in result["items"]))
        fixture.memory.put(fixture.pointer + 0x2000 + 0x5c0, "I", 2)
        self.assertEqual(fixture.query()["count"], 1)

    def test_detail_preserves_native_values_and_explicit_unknown_display(self):
        fixture = RequestFixture()
        result = fixture.query(kind="incoming_interaction", id=fixture.identifier)
        self.assertEqual(result["actor"], {"id": 99, "name": "Sender"})
        self.assertEqual(result["recipient"]["id"], 42)
        self.assertEqual(result["days_in_queue"], 7)
        self.assertEqual(result["ai_reply_delay"], 4)
        self.assertEqual(result["interaction"]["key"], "gift_interaction")
        self.assertEqual(result["scope"], {"root": None, "targets": {}})
        self.assertIsNone(result["display"]["description"])
        self.assertTrue(all(row["available"] is None for row in result["response_options"]))
        self.assertEqual([row["id"] for row in result["response_options"]], list(INTERACTION_REPLIES))

    def test_native_text_cache_uses_exact_request_and_native_reply_stage(self):
        fixture = RequestFixture()
        fixture.install_text_cache()
        display = fixture.query(kind="incoming_interaction", id=fixture.identifier)["display"]
        self.assertEqual(display["opening"], "Recipient opening")
        self.assertEqual(display["description"], "Recipient body")
        self.assertEqual(display["signature"], "Native signature")
        self.assertEqual(display["status"], "partially_cached")
        self.assertIsNone(display["title"])
        fixture.memory.put(fixture.cache, "I", 0x3000003)
        display = fixture.query(kind="incoming_interaction", id=0x3000003)["display"]
        self.assertEqual(display["opening"], "Delegate opening")
        self.assertEqual(display["description"], "Delegate body")
        self.assertIsNone(fixture.query(kind="incoming_interaction", id=fixture.identifier)["display"]["description"])

    def test_changing_native_text_cache_returns_unknown_and_keeps_request(self):
        fixture = RequestFixture()
        fixture.install_text_cache()
        original = fixture.memory.read
        changed = False

        def read(address, size, **options):
            nonlocal changed
            data = original(address, size, **options)
            if not changed and address == fixture.cache + 8 + 0xa0:
                fixture.string(fixture.cache + 8 + 0x60, "New body")
                changed = True
            return data

        fixture.memory.read = read
        detail = fixture.query(kind="incoming_interaction", id=fixture.identifier)
        self.assertEqual(detail["actor"]["id"], 99)
        self.assertIsNone(detail["display"]["description"])
        self.assertEqual(detail["display"]["status"], "unavailable")
        self.assertIn("text cache changed", detail["display"]["reason"])

    def test_detail_rejects_foreign_recipient_and_reused_native_id(self):
        fixture = RequestFixture()
        with self.assertRaisesRegex(ValueError, "another recipient"):
            fixture.query(kind="incoming_interaction", id=0x2000002)
        fixture.memory.put(fixture.pointer + 0x10, "I", 0x5000001)
        with self.assertRaisesRegex(ValueError, "expired"):
            fixture.query(kind="incoming_interaction", id=fixture.identifier)

    def test_ownership_change_during_reading_is_rejected(self):
        fixture = RequestFixture()
        original = fixture.memory.read
        changed = False

        def read(address, size, **options):
            nonlocal changed
            data = original(address, size, **options)
            if not changed and address == fixture.pointer + 0x18:
                fixture.memory.put(fixture.pointer + 0x2f4, "I", 99)
                changed = True
            return data

        fixture.memory.read = read
        with self.assertRaisesRegex(ValueError, "changed during reading"):
            fixture.query(kind="incoming_interaction", id=fixture.identifier)

    def test_cached_recipient_and_reply_stage_cannot_override_fresh_snapshot(self):
        fixture = RequestFixture()
        original = fixture.memory.read

        def read(address, size, **options):
            if not options.get("fresh") and address == fixture.pointer + 0x2f4:
                return struct.pack("<I", 42)
            if not options.get("fresh") and address == fixture.pointer + 0x5c0:
                return struct.pack("<I", 1)
            return original(address, size, **options)

        fixture.memory.read = read
        fixture.memory.put(fixture.pointer + 0x2f4, "I", 99)
        with self.assertRaisesRegex(ValueError, "another recipient"):
            fixture.query(kind="incoming_interaction", id=fixture.identifier)
        fixture.memory.put(fixture.pointer + 0x2f4, "I", 42)
        fixture.memory.put(fixture.pointer + 0x5c0, "I", 3)
        detail = fixture.query(kind="incoming_interaction", id=fixture.identifier)
        self.assertEqual(detail["intermediary_status"], "decline")

    def test_public_reader_defaults_to_controlled_recipient_and_paginates(self):
        fixture = RequestFixture()

        class World:
            def __init__(self, pid=None):
                self.memory = fixture.memory

            def query(self, request):
                return {"data": fixture.world._query(request),
                        "runtime": {"date": fixture.date, "tick": 1, "world": 100}}

            def close(self):
                pass

        with patch("live_objects.MemoryWorld", World), DirectWorldReader(character_id=42) as reader:
            first = reader.handle({"kind": "incoming_interactions", "limit": 1, "fields": ["id", "role"]})
            self.assertEqual(first["data"]["items"], [{"id": fixture.identifier, "role": "recipient"}])
            self.assertEqual(first["data"]["next_offset"], 1)
            detail = reader.handle({"kind": "incoming_interaction", "id": fixture.identifier,
                                    "fields": ["actor", "response_options"], "max_chars": 3000})
            self.assertEqual(detail["data"]["actor"]["id"], 99)

    def test_notification_is_role_scoped_deduplicated_and_resets_after_removal(self):
        class Reader:
            rows = [{"id": 10, "character_id": 42, "role": "recipient"},
                    {"id": 11, "character_id": 99, "role": "recipient"}]

            def handle(self, request):
                return {"date": "1198.2.15", "data": {"items": self.rows, "next_offset": None}}

        reader = Reader()
        source = PendingInteractionNotifications(reader)
        self.assertEqual([row["data"]["id"] for row in source.poll(42)], [10])
        self.assertEqual(source.poll(42), [])
        reader.rows = []
        self.assertEqual(source.poll(42), [])
        reader.rows = [{"id": 10, "character_id": 42, "role": "intermediary"}]
        self.assertEqual(source.poll(42)[0]["data"]["role"], "intermediary")

    def test_reply_encoding_uses_signed_integer_wire_values_for_named_choices(self):
        codec = JominiCodec()
        header = {"sender": 2, "receiver": 0, "identity": 3, "attribs": 14}
        for response, value in INTERACTION_REPLIES.items():
            with self.subTest(response=response):
                fields = {"character_interaction": 0x90000001, "accept": response}
                encoded = codec.encode_command("reply_character_interaction", fields, header)
                self.assertEqual(codec.decode_command(encoded)["fields"], fields)
                self.assertIn(struct.pack("<HHHi", 0x28b0, 1, 0xc, value), encoded)
        with self.assertRaises(ValueError):
            codec.encode_command("reply_character_interaction", {"character_interaction": 1}, header)
        self.assertEqual(action("reply_character_interaction")["parameters"]["accept"]["choices"], list(INTERACTION_REPLIES))
        encoded = codec.encode_command("reply_character_interaction", {"character_interaction": 1, "accept": "accept"}, header)
        reply_value = struct.pack("<HHHi", 0x28b0, 1, 0xc, 0)
        unknown = encoded.replace(reply_value, struct.pack("<HHHi", 0x28b0, 1, 0xc, 3))
        self.assertEqual(codec.decode_command(unknown)["fields"]["accept"], 3)
        boolean = encoded.replace(reply_value, struct.pack("<HHH?", 0x28b0, 1, 0xe, True))
        self.assertIs(codec.decode_command(boolean)["fields"]["accept"], True)

    def test_model_must_supply_its_own_reply_choice(self):
        settings = ModelConfig(endpoint="http://127.0.0.1:1/v1/chat/completions", model="offline")
        with redirect_stdout(io.StringIO()):
            runtime = ModelRuntime(settings, model=object())
            try:
                with self.assertRaisesRegex(ValueError, "accept"):
                    runtime._action({"action": "reply_character_interaction", "request_id": "reply-1",
                                     "parameters": {"character_interaction": 10}})
                request = runtime._action({"action": "reply_character_interaction", "request_id": "reply-2",
                                           "parameters": {"character_interaction": 10, "accept": "reject"}})
                self.assertEqual(request["fields"]["accept"], "reject")
                self.assertEqual(request["type"], "reply_character_interaction")
            finally:
                runtime.close()

    def test_window_responses_follow_normal_sdk_dispatch_deduplication_and_host_echo(self):
        commands = queue.SimpleQueue()
        responses = [
            ("reply_character_interaction", {"character_interaction": 0x1000001, "accept": "reject"}),
            ("join_activity_command", {"activity": 570425364, "character": 42, "travel": "yes"}),
            ("decline_activity_invite_command", {"activity": 570425364, "character": 42}),
        ]
        for name, parameters in responses:
            request = {"command": "send_game_command", "type": name, "fields": parameters,
                       "header": action(name)["header"], "request_id": name}
            commands.put(request)
            commands.put(request)

        class Peer:
            def __init__(self):
                self.events = []
                self.sent = []

            def connect(self):
                self.events.append({"event": "connected"})

            def join(self, room_id, metadata):
                self.events.append({"event": "room_joined", "match_id": room_id})

            def poll(self):
                if len(self.sent) == len(responses):
                    commands.put({"command": "stop"})
                result, self.events = self.events, []
                return result

            def send_envelope(self, room_id, recipient, messages, magic=0xcafe, presences=None):
                command = JominiCodec().decode_pdx_message(messages[0], from_server=False)
                if command["channel"] == "command":
                    self.sent.append(command)
                    self.events.append({"event": "match_data", "opcode": 255,
                                        "payload": encode_payload("observer", messages, len(self.sent), "host"),
                                        "presence": {"user_id": "host"}})
                return b"sent"

        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            directory = Path(folder) / "capture"
            with EventWriter(directory, quiet=True) as writer:
                writer.session_state.connected = True
                writer.session_state.local_machineid = 2
                writer.observations.set_character(42)
                peer = Peer()
                run_connection(peer, "room", writer, commands=commands, duration=1,
                               handshake={"command": "send_session_message", "recipient": "host",
                                          "type": "ClientRequestConnected", "fields": {}})
                for name, _ in responses:
                    self.assertEqual(writer.actions.status(name)["status"], "host_echoed")
            self.assertEqual([command["type"] for command in peer.sent], [name for name, _ in responses])
            self.assertEqual(peer.sent[0]["fields"]["accept"], "reject")
            events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sum(bool(event.get("duplicate")) for event in events), 3)
