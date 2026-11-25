"""Regressions for compact AI context, delta baselines and selective queries."""

from contextlib import closing, redirect_stdout
import io
import json
import sqlite3
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

from ck3_llm.world.ai_observation import ObservationInterface
from ck3_llm.client import EventWriter
from ck3_llm.protocol.jomini_codec import JominiCodec
from ck3_llm.protocol.jomini_codec import CLOSE, OPEN, STRING, U32, _property, _scalar, _word
from ck3_llm.protocol.mock_nakama import HOST_PRESENCE, SAMPLE_PAYLOAD, make_world_fixture
from ck3_llm.protocol.pdx_nakama_codec import encode_payload
from ck3_llm.protocol.snapshot_transfer import TransferChecksum
from ck3_llm.world.world_snapshot import WorldSnapshot


def make_personal_world(stress=0):
    raw, archive = make_world_fixture(character={
        "dna": "unused-genetic-data" * 400,
        "family_data": {"primary_spouse": 12346, "child": [12347]},
        "alive_data": {
            "gold": {"value": 208}, "prestige": {"currency": 80},
            "piety": {"currency": 200}, "stress": stress,
            "focus": {"type": "martial_authority_focus", "date": 51571296},
            "variables": {"data": []},
        },
        "landed_data": {"domain": [1], "current_strength": 2488, "levy": 2073, "succession": [12347]},
    })
    return WorldSnapshot(raw), archive


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.world, self.archive = make_personal_world()
        self.interface = ObservationInterface(self.world, 12345)

    def test_compact_personal_context_and_unchanged_delta(self):
        first = self.interface.observe()
        text = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
        self.assertNotIn("genetic", text)
        self.assertNotIn("variables", text)
        self.assertEqual(first["personal"]["resources"]["gold"], 208)
        self.assertEqual(first["personal"]["military"]["current_strength"], 2488)
        self.assertEqual(first["personal"]["domain"]["titles"], [{"id": 1, "key": "c_fixture"}])
        self.assertTrue(first["should_decide"])
        first["personal"]["resources"]["gold"] = 999
        second = self.interface.observe()
        self.assertEqual(second["mode"], "delta")
        self.assertEqual(second["personal"], {})
        self.assertFalse(second["should_decide"])
        self.assertEqual(self.interface.observe(changes_only=False)["personal"]["resources"]["gold"], 208)

    def test_character_event_queries_handle_absent_single_and_repeated_records(self):
        tokens = {name: token for token, name in JominiCodec().tokens.items()}

        def event_record(event_id, character_id):
            fields = b"".join([
                _property(tokens["id"], _scalar(U32, event_id)),
                _property(tokens["character"], _scalar(U32, character_id)),
                _property(tokens["event"], _scalar(STRING, "clan_event.2002")),
                _property(tokens["expiration_date"], _scalar(U32, 51573456)),
            ])
            return _property(tokens["player_event"], _word(OPEN) + fields + _word(CLOSE))

        for suffix, expected in ((b"", 0), (event_record(7, 12345), 1),
                                 (event_record(7, 12345) + event_record(8, 999), 1)):
            with self.subTest(records=len(suffix)):
                world = WorldSnapshot(self.world.data + suffix)
                interface = ObservationInterface(world, 12345)
                result = interface.query("events", 12345, max_chars=1000)
                self.assertEqual(result["data"]["count"], expected)
                if expected:
                    self.assertEqual(result["data"]["events"][0]["id"], 7)
                    selected = interface.query("events", 12345, fields=["events.0.id", "events.0.event"])
                    self.assertEqual(selected["data"], {"events.0.id": 7, "events.0.event": "clan_event.2002"})
                with tempfile.TemporaryDirectory() as folder:
                    from ck3_llm.world.world_store import SQLiteWorldStore
                    database = Path(folder) / "world.sqlite3"
                    with SQLiteWorldStore(database) as store:
                        store.import_snapshot(world)
                        store.connection.execute("DELETE FROM root_values WHERE key = 'player_event'")
                        store.connection.commit()
                        self.assertTrue(store.import_snapshot(world)["reused"])
                    with SQLiteWorldStore(database) as store:
                        self.assertEqual(ObservationInterface(store).query("events", 12345)["data"]["count"], expected)

    def test_new_snapshot_emits_only_changed_groups(self):
        self.interface.observe()
        changed, _ = make_personal_world(stress=21)
        self.interface.set_snapshot(changed)
        result = self.interface.observe()
        self.assertEqual(set(result["personal"]), {"resources"})
        self.assertEqual(result["personal"]["resources"]["stress"], 21)
        self.assertEqual(result["snapshot"]["sha256"], changed.source_sha256)

    def test_budget_omissions_remain_available_on_next_observation(self):
        full = self.interface.observe()
        interface = ObservationInterface(self.world, 12345)
        base = {**full, "personal": {}, "omitted_fields": list(full["personal"])}
        budget = len(json.dumps(base, ensure_ascii=False, separators=(",", ":"))) + 90
        limited = interface.observe(max_chars=budget)
        self.assertLessEqual(len(json.dumps(limited, ensure_ascii=False, separators=(",", ":"))), budget)
        self.assertTrue(limited["omitted_fields"])
        next_result = interface.observe()
        self.assertTrue(set(limited["omitted_fields"]).issubset(next_result["personal"]))

    def test_relevant_commands_are_filtered_deduplicated_and_consumed(self):
        self.interface.observe()
        for target in (999, 12345, 12346, 12347):
            command = {"channel": "command", "type": "set_character_focus",
                       "default_state": {"identity": target, "sender": 1},
                       "fields": {"target": target, "actor": target, "focus_type": "martial_strategy_focus"}}
            self.interface.feed(command)
            self.interface.feed(command)
        result = self.interface.observe()
        self.assertEqual(len(result["observed_commands"]), 3)
        self.assertEqual(result["personal"], {})
        self.assertTrue(result["snapshot"]["refresh_needed"])
        self.assertEqual(self.interface.observe()["observed_commands"], [])

    def test_reported_host_progress_marks_snapshot_for_refresh(self):
        self.interface.set_snapshot(self.world, 200)
        self.interface.set_synchronization({"hotjoin": {"sender": 1}, "peer_turns": [{"machineid": 1, "turn": 201}]})
        self.assertTrue(self.interface.observe()["snapshot"]["refresh_needed"])
        self.interface.set_snapshot(self.world, 201)
        result = self.interface.observe()
        self.assertFalse(result["snapshot"]["refresh_needed"])
        self.assertEqual(result["snapshot"]["latest_host_turn"], 201)

    def test_selective_queries_resolve_entities_and_field_paths(self):
        title = self.interface.query("title", "c_fixture")
        self.assertEqual(title["data"]["holder"], 12345)
        self.assertEqual(self.interface.query("province", 1)["data"], {"fort_level": 2})
        person = self.interface.query("character", 12345, fields=["first_name", "alive_data.gold.value", "missing"])
        self.assertEqual(person["data"], {"first_name": "Synthetic AI", "alive_data.gold.value": 208})
        self.assertEqual(person["missing_fields"], ["missing"])
        path = self.interface.query("path", path=["date"])
        self.assertEqual(path["data"]["value"], 51571296)
        limited = self.interface.query("character", 12345, fields=["dna", "first_name"], max_chars=800)
        self.assertEqual(limited["omitted_fields"], ["dna"])
        self.assertEqual(limited["data"]["first_name"], "Synthetic AI")

    def test_queries_preserve_observation_baseline_and_character_switch_resets_it(self):
        self.interface.query("character", 12345)
        self.assertEqual(self.interface.observe()["mode"], "full")
        self.interface.query("title", 1)
        self.assertEqual(self.interface.observe()["personal"], {})
        self.interface.set_character(None)
        self.interface.set_character(12345)
        self.assertEqual(self.interface.observe()["mode"], "full")

    def test_invalid_requests_preserve_unconsumed_observation(self):
        interface = ObservationInterface()
        self.assertFalse(interface.handle({"command": "observation_status"})["world_ready"])
        with self.assertRaises(ValueError):
            interface.query("character", 12345)
        for budget in (True, 0, -1, 5):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                self.interface.observe(max_chars=budget)
        self.assertEqual(self.interface.observe()["mode"], "full")
        with self.assertRaises(ValueError):
            self.interface.query("character", 12345, fields="first_name")

    def test_retried_request_returns_same_delta_without_consuming_next_observation(self):
        request = {"command": "observe", "request_id": "observation-1"}
        first = self.interface.handle(request)
        retry = self.interface.handle(dict(reversed(list(request.items()))))
        self.assertEqual(first, retry)
        retry["personal"]["resources"]["gold"] = 999
        self.assertEqual(self.interface.handle(request)["personal"]["resources"]["gold"], 208)
        with self.assertRaises(ValueError):
            self.interface.handle({**request, "max_chars": 1000})
        self.assertEqual(self.interface.handle({"command": "observe", "request_id": "observation-2"})["personal"], {})

    def test_cli_outputs_utf8_json(self):
        _, archive = make_world_fixture(character={"first_name": "\u8def\u5fb7\u7ef4\u5e0c \u00c9udes"})
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "world.bin"
            snapshot.write_bytes(archive)
            result = subprocess.run([
                sys.executable, "-m", "ck3_llm.world.ai_observation", str(snapshot), "--character", "12345",
            ], capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["personal"]["identity"]["first_name"], "\u8def\u5fb7\u7ef4\u5e0c \u00c9udes")

    def test_normal_receive_pipeline_retains_world_and_follows_local_character(self):
        checksum = TransferChecksum()
        checksum.update(self.archive)
        codec = JominiCodec()
        command = codec.encode_command("filetransfer_fast_cmd_chunk", {
            "filename": "world.ck3", "size": len(self.archive), "checksum": checksum.value,
            "chunk_id": 0, "data": {"hex": self.archive.hex()},
        }, {"identity": 45, "sender": 0, "receiver": 1})
        packet = encode_payload("local-ai", [{"word_8": 0, "payload_hex": command.hex()}], 43, "host")
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with EventWriter(Path(folder) / "capture", quiet=True) as writer:
                writer.write({"event": "match_data", "opcode": 255, "payload": SAMPLE_PAYLOAD})
                writer.write({"event": "match_data", "opcode": 255, "payload": packet, "presence": HOST_PRESENCE})
                writer.finish_world()
                self.assertEqual(writer.observations.world.source_sha256,
                                 writer._world.latest_snapshot.source_sha256)
                self.assertTrue((Path(folder) / "capture" / "world.sqlite3").is_file())
                result = writer.observations.handle({"command": "observe"})
                self.assertEqual(result["character_id"], 12345)
                self.assertEqual(result["personal"]["resources"]["gold"], 208)
                writer.write({"event": "match_data", "opcode": 255, "payload": SAMPLE_PAYLOAD,
                              "presence": HOST_PRESENCE})
                with closing(sqlite3.connect(Path(folder) / "capture" / "world.sqlite3")) as database:
                    kinds = {row[0] for row in database.execute(
                        "SELECT command_type FROM observed_commands")}
                    ready_count = database.execute(
                        "SELECT COUNT(*) FROM observed_commands WHERE command_type = 'set_ready_command'").fetchone()[0]
                self.assertIn("set_ready_command", kinds)
                self.assertEqual(ready_count, 2)  # One packet before import, one after.


if __name__ == "__main__":
    unittest.main()
