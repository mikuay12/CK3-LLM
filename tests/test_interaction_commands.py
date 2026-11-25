"""Regress marriage and ordinary interactions through query, model and SDK paths."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import patch

from ck3_llm.client import EventWriter, run_connection
from ck3_llm.gameplay.game_actions import action
from ck3_llm.gameplay.interaction_catalog import INTERACTION_FILES, InteractionCatalog
from ck3_llm.protocol.jomini_codec import JominiCodec, SIMPLE_INTERACTIONS
from ck3_llm.world.live_objects import DirectWorldReader
from ck3_llm.model.model_runtime import ModelConfig, ModelRuntime
from ck3_llm.protocol.pdx_nakama_codec import encode_payload


class InteractionFixture(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.executable = self.root / "binaries/ck3.exe"
        scripts = self.root / "game/common/character_interactions"
        scripts.mkdir(parents=True)
        self.flags = {"arrange_marriage_interaction": ["grand_wedding_promise", "matrilineal", "hook"],
                      "gift_interaction": [], "offer_vassalization_interaction": ["normal_obligations"],
                      "invite_to_court_interaction": ["cover_travel_expenses", "hook"]}
        for name, filename in INTERACTION_FILES.items():
            special = "special_interaction = arrange_marriage_interaction" if name == "arrange_marriage_interaction" else ""
            options = "\n".join(f"send_option = {{ flag = {flag} localization = {flag}_label }}" for flag in self.flags[name])
            (scripts / filename).write_text(f"{name} = {{ {special} is_valid = {{ always = yes }} {options} }}", encoding="utf-8")
        locale = self.root / "game/localization/simp_chinese"
        locale.mkdir(parents=True)
        (locale / "interactions.yml").write_text('l_simp_chinese:\n matrilineal_label: "母系婚姻"\n', encoding="utf-8")
        self.catalog = InteractionCatalog(self.executable)
        self.codec = JominiCodec()
        self.header = {"identity": 1, "sender": 2, "receiver": 0, "attribs": 14}

    def test_supported_interactions_preserve_characters_options_and_acceptance(self):
        for name in SIMPLE_INTERACTIONS:
            with self.subTest(name=name):
                fields = self.catalog.build(42, 99, name, ai_accept=-15.25, secondary_actor=43, secondary_recipient=100,
                                            option_flags={flag: True for flag in self.flags[name]})
                command = self.codec.decode_command(self.codec.encode_command("send_character_interaction", fields, self.header))
                self.assertEqual(command["fields"]["ai_accept"], -15.25)
                data = command["fields"]["character_interaction"]
                self.assertEqual((data["actor"], data["recipient"], data["secondary_actor"], data["secondary_recipient"]),
                                 (42, 99, 43, 100))
                self.assertEqual(data.get("options", []), [True] * len(self.flags[name]))
                self.assertEqual("arrange_marriage_interaction" in data, name == "arrange_marriage_interaction")

    def test_flags_use_installed_order_and_reject_unknown_or_nonboolean_values(self):
        fields = self.catalog.build(42, 99, "arrange_marriage_interaction", ai_accept=0,
                                    secondary_actor=43, secondary_recipient=100, option_flags={"hook": True})
        self.assertEqual(fields["character_interaction"]["options"], [False, False, True])
        self.assertEqual(fields["character_interaction"]["scope"]["event_targets"]["hook"]["identity"], 1)
        for options in ({"unknown": True}, {"hook": 1}, []):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.catalog.build(42, 99, "invite_to_court_interaction", ai_accept=0, option_flags=options)
        with self.assertRaises(ValueError):
            self.catalog.build(42, 99, "arrange_marriage_interaction", ai_accept=0)
        with self.assertRaises(TypeError):
            self.catalog.build(42, 99, "gift_interaction")

    def test_query_returns_localized_flags_and_bounded_details(self):
        fixture = self

        class World:
            def __init__(self, pid=None):
                self.memory = type("Memory", (), {"executable": fixture.executable})()

            def query(self, request):
                raise AssertionError("Static interaction discovery requires no world reads")

            def close(self):
                pass

        with patch("ck3_llm.world.live_objects.MemoryWorld", World), DirectWorldReader(character_id=42) as reader:
            result = reader.handle({"kind": "interaction", "id": "arrange_marriage_interaction", "fields": ["send_options"],
                                    "max_chars": 2000})
            self.assertEqual(result["data"]["send_options"][1]["label"], "母系婚姻")
            self.assertNotIn("script", result["data"]["send_options"][1])
            details = reader.handle(result["data"]["send_options"][1]["details_query"])
            self.assertIn("flag = matrilineal", details["data"]["send_options.1.script"])
            result = reader.handle({"kind": "interactions", "limit": 1, "fields": ["id"], "max_chars": 300})
            self.assertEqual(result["data"]["next_offset"], 1)
            self.assertEqual(result["data"]["items"][0]["id"], "arrange_marriage_interaction")

    def test_large_option_scripts_leave_default_flag_queries_usable(self):
        path = self.catalog.directory / INTERACTION_FILES["arrange_marriage_interaction"]
        source = path.read_text(encoding="utf-8")
        source = source.replace("flag = matrilineal", "flag = matrilineal is_shown = {" + " unknown_trigger = yes" * 600 + " }")
        path.write_text(source, encoding="utf-8")
        result = self.catalog.query({"kind": "interaction", "id": "arrange_marriage_interaction"})
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), 6000)
        self.assertEqual([item["flag"] for item in result["send_options"]], self.flags["arrange_marriage_interaction"])

    def test_model_dispatch_forbids_model_acceptance_override_and_uses_high_level_request(self):
        runtime = ModelRuntime(ModelConfig(endpoint="http://127.0.0.1:1/v1/chat/completions", model="test"))
        self.addCleanup(runtime.close)
        parameters = {"interaction": "gift_interaction", "recipient": 99}
        result = runtime._action({"action": "send_interaction", "parameters": parameters, "request_id": "gift-1"})
        self.assertEqual(result, {"command": "send_interaction", **parameters, "request_id": "gift-1"})
        with self.assertRaises(ValueError):
            runtime._action({"action": "send_interaction",
                             "parameters": {"interaction": "gift_interaction", "recipient": 99, "ai_accept": -20},
                             "request_id": "gift-2"})
        with self.assertRaises(ValueError):
            runtime._action({"action": "send_interaction",
                             "parameters": {"interaction": "gift_interaction", "recipient": 99, "ai_intermediary_accept": 50},
                             "request_id": "gift-3"})
        schema_params = action("send_interaction")["parameters"]
        self.assertNotIn("ai_accept", schema_params)
        self.assertNotIn("ai_intermediary_accept", schema_params)

    def test_sdk_loop_tracks_deduplicates_and_matches_host_echo(self):
        commands = queue.SimpleQueue()
        request = {"command": "send_interaction", "interaction": "arrange_marriage_interaction", "recipient": 99,
                   "secondary_actor": 43, "secondary_recipient": 100, "option_flags": {"matrilineal": True},
                   "ai_accept": -15, "request_id": "marriage-1"}
        commands.put(request)
        commands.put(deepcopy(request))

        class Peer:
            def __init__(self):
                self.events = []
                self.sent = []

            def connect(self):
                self.events.append({"event": "connected"})

            def join(self, room_id, metadata):
                self.events.append({"event": "room_joined", "match_id": room_id})

            def poll(self):
                if self.sent:
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

        with redirect_stdout(io.StringIO()), EventWriter(self.root / "capture", quiet=True) as writer:
            writer.session_state.connected = True
            writer.session_state.local_machineid = 2
            writer.observations.set_character(42)
            writer.interactions = self.catalog
            peer = Peer()
            run_connection(peer, "room", writer, commands=commands, duration=1,
                           handshake={"command": "send_session_message", "recipient": "host",
                                      "type": "ClientRequestConnected", "fields": {}})
            self.assertEqual(writer.actions.status("marriage-1")["status"], "host_echoed")
        self.assertEqual(len(peer.sent), 1)
        data = peer.sent[0]["fields"]["character_interaction"]
        self.assertEqual((data["actor"], data["secondary_actor"], data["secondary_recipient"]), (42, 43, 100))
        self.assertEqual(data["options"], [False, True, False])
        events = [json.loads(line) for line in (self.root / "capture/events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(event.get("duplicate") for event in events))

    def test_special_interactions_roundtrip_and_catalog_metadata(self):
        scripts = self.root / "game/common/character_interactions"
        (scripts / "00_special_interactions.txt").write_text(
            "\n".join([
                "grant_titles_interaction = { special_interaction = grant_titles_interaction target_type = title }",
                "revoke_title_interaction = { special_interaction = revoke_title_interaction target_type = title }",
                "liege_modify_vassal_contract_interaction = { special_interaction = liege_modify_vassal_contract }",
                "grant_court_position = { special_interaction = grant_court_position target_type = court_position_type }",
                "invite_to_council_position_interaction = { special_interaction = invite_to_council_interaction }",
                "end_war_attacker_victory_interaction = { special_interaction = end_war_attacker_victory_interaction }",
                "ransom_interaction = { special_interaction = ransom_interaction send_option = { flag = gold localization = gold_lbl } }",
                "request_culture_conversion_interaction = { special_interaction = request_culture_conversion_interaction }",
            ]),
            encoding="utf-8",
        )
        special_catalog = InteractionCatalog(self.executable)
        codec = JominiCodec()
        cases = [
            ("grant_titles_interaction", 42, 99, {"type": "landed_title", "identity": 401},
             None, {"titles": [401, 402], "government": "feudal_government"}, {},
             lambda fields: self.assertEqual(fields["character_interaction"]["grant_titles_interaction"],
                                             {"titles": [401, 402], "government": "feudal_government"})),
            ("revoke_title_interaction", 42, 99, {"type": "landed_title", "identity": 405},
             None, None, {},
             lambda fields: self.assertEqual(fields["character_interaction"]["revoke_title_interaction"], {"title": 405})),
            ("liege_modify_vassal_contract_interaction", 42, 99, None,
             None, {"types": ["feudal_government_taxes", "feudal_government_levies"], "level": [2, 1]}, {},
             lambda fields: self.assertEqual(fields["character_interaction"]["liege_modify_vassal_contract"],
                                             {"types": ["feudal_government_taxes", "feudal_government_levies"], "level": [2, 1]})),
            ("grant_court_position", 42, 99, None,
             None, {"court_position": "court_physician_court_position"}, {},
             lambda fields: self.assertEqual(fields["character_interaction"]["grant_court_position"],
                                             {"court_position": "court_physician_court_position"})),
            ("invite_to_council_position_interaction", 42, 99, None,
             None, {"active_council_task": 55}, {},
             lambda fields: self.assertEqual(fields["character_interaction"]["invite_to_council_interaction"],
                                             {"active_council_task": 55})),
            ("end_war_attacker_victory_interaction", 42, 99, None,
             None, None, {},
             lambda fields: self.assertEqual(fields["character_interaction"]["end_war_attacker_victory_interaction"], {})),
            ("end_war_attacker_victory_interaction", 42, 99, {"type": "war", "identity": 12},
             None, None, {},
             lambda fields: (
                 self.assertEqual(fields["character_interaction"]["scope"]["root"], {"type": "war", "identity": 12}),
                 self.assertEqual(fields["character_interaction"]["scope"]["event_targets"]["war"], {"type": "war", "identity": 12}),
                 self.assertNotIn("target", fields["character_interaction"]),
                 self.assertNotIn("target", fields["character_interaction"]["scope"]["event_targets"]),
             )),
            ("ransom_interaction", 42, 99, None,
             {"secondary_recipient": 120}, None, {"gold": True},
             lambda fields: self.assertNotIn("ransom_interaction", fields["character_interaction"])),
            ("request_culture_conversion_interaction", 42, 99, None,
             None, {"traits": {"traditions": ["tradition_only_the_strong"], "ethos": "ethos_bellicose"}}, {},
             lambda fields: self.assertEqual(
                 fields["character_interaction"]["request_culture_conversion_interaction"]["traits"],
                 {"traditions": ["tradition_only_the_strong"], "ethos": "ethos_bellicose"},
             )),
        ]
        for name, actor, recipient, target, extra, special_fields, option_flags, check in cases:
            with self.subTest(interaction=name):
                details = special_catalog.query({"kind": "interaction", "id": name})
                self.assertIn("special_interaction", details)
                self.assertIn("special_parameters", details)
                fields = special_catalog.build(
                    actor, recipient, name,
                    target=target,
                    option_flags=option_flags,
                    special_fields=special_fields,
                    ai_accept=12.0,
                    **(extra or {}),
                )
                payload = codec.encode_command("send_character_interaction", fields,
                                               {"identity": 1, "sender": 2, "receiver": 0, "attribs": 14})
                decoded = codec.decode_command(payload)
                check(decoded["fields"])

        contract = special_catalog.query({"kind": "interaction", "id": "liege_modify_vassal_contract_interaction"})
        self.assertEqual(contract["special_interaction"], "liege_modify_vassal_contract")
        self.assertEqual(contract["special_parameters"], ["types", "level"])
        self.assertIn("special_fields", action("send_interaction")["parameters"])
        self.assertEqual(len(special_catalog.index), 12)


if __name__ == "__main__":
    unittest.main()
