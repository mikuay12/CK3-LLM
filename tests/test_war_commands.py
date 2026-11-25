"""Regress typed war declarations against retained native multiplayer bytes."""

from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
import queue
from pathlib import Path
import unittest
import tempfile

from ck3_llm.paths import PROJECT_ROOT
from ck3_llm.protocol.jomini_codec import JominiCodec, declare_war_fields
from ck3_llm.protocol.pdx_nakama_codec import decode_payload


class WarCommandTests(unittest.TestCase):
    def setUp(self):
        self.codec = JominiCodec()
        self.header = {"identity": 1, "sender": 2, "receiver": 0, "attribs": 14}
        self.fields = {
            "character_interaction": {
                "character_interaction": "declare_war_interaction", "actor": 42, "recipient": 99,
                "puppet_or_actor": 42,
                "scope": {"root": {}, "seed": -1, "event_targets": {
                    "actor": {"type": "char", "identity": 42}, "recipient": {"type": "char", "identity": 99},
                    "hook": {"type": "boolean"}, "target": {"type": "boolean", "identity": 1}}},
                "original_scope": {"root": {}, "seed": -1}, "target": {"type": "boolean", "identity": 1},
                "options": [False, True, False],
                "declare_war_interaction": {"casus_belli": "county_conquest_cb", "titles": [2997],
                                             "claimant": 0xffffffff, "uses_hook": False, "has_hook": False},
            }, "ai_intermediary_accept": 100.0, "ai_accept": 1.0,
        }

    def test_declaration_round_trip_preserves_scopes_options_and_war_targets(self):
        payload = self.codec.encode_command("send_character_interaction", self.fields, self.header)
        result = self.codec.decode_command(payload)
        self.assertEqual(result["fields"], self.fields)
        self.assertEqual(result["default_state"], self.header)

    def test_call_ally_war_target_matches_native_serializer(self):
        from ck3_llm.protocol.jomini_codec import simple_interaction_fields
        from ck3_llm.protocol.nakama_native import DEFAULT_GAME_DIR
        from ck3_llm.tools.verify_jomini_native import NativeSerializerOracle

        executable = DEFAULT_GAME_DIR / "binaries/ck3.exe"
        if not executable.exists():
            self.skipTest("ck3.exe is not installed locally")
        target = {"type": "war", "identity": 100663342}
        fields = simple_interaction_fields(33336, 32396, "call_ally_interaction",
                                          ai_accept=100.0, target=target)
        payload = self.codec.encode_command("send_character_interaction", fields, self.header)
        oracle = NativeSerializerOracle(executable)
        self.assertEqual(payload, oracle.encode("send_character_interaction", fields, self.header))
        data = self.codec.decode_command(payload)["fields"]["character_interaction"]
        self.assertEqual(data["target"], target)
        self.assertEqual(data["scope"]["event_targets"]["target"], target)

    def test_declaration_omits_native_empty_arrays_and_default_references(self):
        fields = deepcopy(self.fields)
        data = fields["character_interaction"]
        data.update(secondary_actor=0xffffffff, intermediary=0xffffffff, options=[])
        data["declare_war_interaction"]["titles"] = []
        decoded = self.codec.decode_command(self.codec.encode_command("send_character_interaction", fields, self.header))["fields"]
        expected = deepcopy(self.fields)
        expected["character_interaction"].pop("options")
        expected["character_interaction"]["declare_war_interaction"].pop("titles")
        self.assertEqual(decoded, expected)

    def test_unsupported_or_malformed_interaction_inputs_are_rejected(self):
        for replacement in ({"options": [1]}, {"actor": True}, {"recipient": -1}, {"unknown": 1},
                            {"character_interaction": "invite_to_council_position_interaction"},
                            {"scope": {"root": {"type": "army", "identity": 1}}},
                            {"scope": {"root": {"type": "boolean", "identity": 2}}},
                            {"declare_war_interaction": {"casus_belli": "debug_war", "titles": [True]}},
                            {"declare_war_interaction": {"casus_belli": "debug_war", "uses_hook": 1}}):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                fields = deepcopy(self.fields)
                fields["character_interaction"].update(replacement)
                self.codec.encode_command("send_character_interaction", fields, self.header)

    def test_fixed_point_fields_validate_finite_native_range(self):
        for value in (True, float("nan"), float("inf"), 10 ** 1000, 1e308):
            with self.subTest(value=str(value)[:40]), self.assertRaises(ValueError):
                self.codec.encode_command("send_character_interaction", {**self.fields, "ai_accept": value}, self.header)

    def test_recorded_native_war_command_reencodes_exactly(self):
        root = PROJECT_ROOT
        evidence = root / "captures" / "notifications-20261115" / "war_declaration_example.json"
        if not evidence.exists():
            self.skipTest("Local recorded native command is unavailable")
        record = json.loads(evidence.read_text(encoding="utf-8"))[0]
        path = root / Path(record["capture"]).parent / record["packet"]
        if not path.exists():
            self.skipTest("Local recorded native packet is unavailable")
        found = False
        for message in decode_payload(path.read_bytes())["messages"]:
            if message["word_8"] != 0:
                continue
            payload = bytes.fromhex(message["payload_hex"])
            command = self.codec.decode_command(payload)
            if command["default_state"] != record["header"] or command["type"] != "send_character_interaction":
                continue
            encoded = self.codec.encode_command(command["type"], command["fields"], command["default_state"])
            self.assertEqual(encoded, payload)
            data = command["fields"]["character_interaction"]
            war = data["declare_war_interaction"]
            built = declare_war_fields(data["actor"], data["recipient"], war["casus_belli"], titles=war["titles"])
            self.assertEqual(self.codec.encode_command(command["type"], built, command["default_state"]), payload)
            found = True
        self.assertTrue(found)

    def test_simple_declare_war_request_uses_normal_client_loop_and_local_character(self):
        from ck3_llm.client import EventWriter, run_connection

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
        commands.put({"command": "declare_war", "target": 99, "casus_belli": "county_conquest_cb", "titles": [2997]})
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with EventWriter(Path(folder) / "client", quiet=True) as writer:
                writer.session_state.connected = True
                writer.session_state.local_machineid = 2
                writer.observations.set_character(42)
                peer = Peer()
                run_connection(peer, "room", writer, commands=commands, duration=0.05,
                               handshake={"command": "send_session_message", "recipient": "host",
                                          "type": "ClientRequestConnected", "fields": {}})
        gameplay = [(recipient, command) for recipient, command in peer.sent if command["channel"] == "command"]
        self.assertEqual(len(gameplay), 1)
        recipient, command = gameplay[0]
        self.assertEqual(recipient, "host")
        self.assertEqual(command["type"], "send_character_interaction")
        self.assertEqual(command["default_state"]["sender"], 2)
        self.assertEqual(command["default_state"]["attribs"], 14)
        data = command["fields"]["character_interaction"]
        self.assertEqual((data["actor"], data["recipient"]), (42, 99))
        self.assertEqual(data["declare_war_interaction"]["titles"], [2997])


if __name__ == "__main__":
    unittest.main()
