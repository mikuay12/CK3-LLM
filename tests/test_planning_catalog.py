"""Regress decision discovery and activity planning metadata without evaluating rules."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from ck3_llm.protocol.jomini_codec import JominiCodec, travel_plan_fields
from ck3_llm.world.live_objects import DirectWorldReader
from ck3_llm.world.memory_world import MemoryWorld
from ck3_llm.model.model_runtime import ModelConfig, ModelRuntime
from ck3_llm.gameplay.planning_catalog import PlanningCatalog
from tests.test_siege_progress import BufferMemory


DECISIONS = '''
@cost = 50
sample_decision = {
    desc = sample_decision_desc
    confirm_text = sample_decision_confirm
    selection_tooltip = sample_decision_tooltip
    is_shown = { is_adult = yes }
    is_valid = { gold >= @cost }
    cost = { gold = @cost }
    cooldown = { years = 2 }
    effect = { add_trait = generous }
}
other_decision = { desc = { first_valid = { desc = dynamic_desc } } widget = sample_widget }
'''
ACTIVITIES = '''
activity_sample = {
    is_shown = { is_landed = yes }
    can_start_showing_failures_only = { is_available_adult = yes }
    cost = { gold = host_activity_cost }
    host_intents = { default = { intent = recreation_intent } }
    phases = {
        sample_meal = { order = 1 is_predefined = yes on_enter_phase = { add_prestige = 10 } }
        sample_toast = { order = 2 is_valid = { always = yes } }
    }
    options = {
        food = {
            simple_food = { default = { always = yes } cost = { gold = 10 } }
            fine_food = { is_shown = { has_dlc_feature = advanced_activities } cost = { gold = 20 } }
        }
    }
}
activity_other = { phases = { other_phase = { order = 1 } } }
'''


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.executable = self.root / "binaries/ck3.exe"
        self.decisions = self.root / "game/common/decisions"
        self.activities = self.root / "game/common/activities/activity_types"
        self.decisions.mkdir(parents=True)
        self.activities.mkdir(parents=True)
        (self.decisions / "samples.txt").write_text(DECISIONS, encoding="utf-8")
        (self.activities / "samples.txt").write_text(ACTIVITIES, encoding="utf-8")
        locale = self.root / "game/localization/simp_chinese"
        locale.mkdir(parents=True)
        (locale / "samples.yml").write_text(
            'l_simp_chinese:\n sample_decision: "Practice generosity"\n'
            ' sample_decision_desc: "Give as [ROOT.Char.GetName]"\n'
            ' sample_decision_confirm: "Give"\n activity_sample: "Sample feast"\n'
            ' simple_food: "Simple food"\n fine_food: "Fine food"\n', encoding="utf-8")
        self.catalog = PlanningCatalog(self.executable)

    def reader(self):
        executable = self.executable

        class World:
            def __init__(self, pid=None):
                self.memory = type("Memory", (), {"executable": executable})()

            def query(self, request):
                raise AssertionError("Installed planning metadata should use the catalog")

            def close(self):
                pass

        patcher = patch("ck3_llm.world.live_objects.MemoryWorld", World)
        patcher.start()
        self.addCleanup(patcher.stop)
        reader = DirectWorldReader(character_id=42)
        self.addCleanup(reader.close)
        return reader

    def test_decision_directory_searches_names_and_keeps_native_availability_unknown(self):
        result = self.catalog.query({"kind": "decisions", "search": "generosity", "character": 42})
        self.assertEqual([row["id"] for row in result["items"]], ["sample_decision"])
        row = result["items"][0]
        self.assertIsNone(row["shown"])
        self.assertIsNone(row["available"])
        self.assertEqual(row["details_query"], {"kind": "decision", "id": "sample_decision", "character": 42})
        self.assertEqual(result["source"], "installed_game_scripts")
        self.assertEqual(self.catalog.query({"kind": "decisions", "limit": 1})["next_offset"], 1)

    def test_decision_retains_conditions_effects_and_explicit_action_without_calculating_cost(self):
        with patch("ck3_llm.gameplay.event_evaluation.evaluate_condition", side_effect=AssertionError("Evaluated a game predicate")):
            result = self.catalog.query({"kind": "decision", "id": "sample_decision", "character": 42})
        self.assertIn("gold >= @cost", result["conditions"][1])
        self.assertIn("add_trait = generous", result["effects"][0])
        self.assertIsNone(result["cost"]["gold"])
        self.assertIn("gold = @cost", result["cost"]["script"][0])
        self.assertIsNone(result["cooldown"]["remaining_days"])
        self.assertFalse(result["description"]["resolved"])
        self.assertEqual(result["confirmation"]["text"], "Give")
        self.assertEqual(result["action"], {"action": "execute_decision",
                                            "parameters": {"decision": "sample_decision", "character": 42}})

    def test_dynamic_description_and_widget_stay_explicit(self):
        result = self.catalog.query({"kind": "decision", "id": "other_decision"})
        self.assertFalse(result["description"]["resolved"])
        self.assertIn("first_valid", result["description"]["script"])
        self.assertEqual(result["widget"], "sample_widget")
        self.assertEqual(result["parameter_schema_status"], "native_result_not_located")

    def test_activity_directory_and_detail_expose_choices_and_hosting_gap(self):
        result = self.catalog.query({"kind": "activity_types", "search": "feast"})
        self.assertEqual([row["id"] for row in result["items"]], ["activity_sample"])
        detail = self.catalog.query(result["items"][0]["details_query"])
        self.assertEqual([row["id"] for row in detail["phases"]], ["sample_meal", "sample_toast"])
        choices = detail["options"][0]["choices"]
        self.assertEqual([row["id"] for row in choices], ["simple_food", "fine_food"])
        self.assertTrue(all(row["available"] is None for row in choices))
        self.assertNotIn("script", choices[0])
        self.assertEqual(detail["hosting_status"], "requires_game_validation")
        self.assertEqual(detail["action"], {"action": "start_activity_command", "parameters": {"type": "activity_sample"}})
        detail_for_char = self.catalog.query({"kind": "activity_type", "id": "activity_sample", "character": 42})
        self.assertEqual(detail_for_char["action"], {"action": "start_activity_command", "parameters": {"type": "activity_sample", "host": 42}})

    def test_choice_details_preserve_default_rules_without_selecting_them(self):
        reader = self.reader()
        detail = reader.handle({"kind": "activity_type", "id": "activity_sample", "max_chars": 10000})
        choice = detail["data"]["options"][0]["choices"][0]
        result = reader.handle(choice["details_query"])
        selected = result["data"]["options.0.choices.0"]
        self.assertIn("default = { always = yes }", selected["script"])
        self.assertIsNone(selected["available"])
        self.assertNotIn("selected", selected)
        self.assertNotIn("default_choice", result)
        summary = reader.handle({"kind": "activity_type", "id": "activity_sample", "fields": ["phases", "options"]})
        self.assertNotIn("script", summary["data"]["options"][0]["choices"][0])
        self.assertEqual(len(summary["data"]["phases"]), 2)

    def test_normal_reader_defaults_character_selects_fields_and_preserves_continuation(self):
        reader = self.reader()
        listed = reader.handle({"kind": "decisions", "limit": 1, "fields": ["id", "details_query"]})
        self.assertEqual(listed["data"]["count"], 2)
        self.assertEqual(listed["data"]["next_offset"], 1)
        self.assertEqual(listed["data"]["items"][0]["details_query"]["character"], 42)
        detail = reader.handle({"kind": "decision", "id": "sample_decision", "fields": ["action", "cost"]})
        self.assertEqual(detail["data"]["action"]["parameters"]["character"], 42)
        self.assertIsNone(detail["data"]["cost"]["gold"])
        schema = reader.handle({"command": "schema", "kind": "decision"})
        self.assertIn("action", schema["data"])

    def test_model_explicitly_selects_a_discovered_decision_and_encoder_accepts_it(self):
        detail = self.reader().handle({"kind": "decision", "id": "sample_decision", "fields": ["action"]})
        selected = detail["data"]["action"]
        config = ModelConfig(endpoint="http://127.0.0.1:1/v1/chat/completions", model="offline")
        with redirect_stdout(io.StringIO()):
            runtime = ModelRuntime(config, model=object())
            try:
                command = runtime._action({**selected, "request_id": "decision-1"})
            finally:
                runtime.close()
        codec = JominiCodec()
        encoded = codec.encode_command(command["type"], command["fields"],
                                       {**command["header"], "identity": 1, "sender": 2, "receiver": 0})
        self.assertEqual(codec.decode_command(encoded)["fields"], {"character": 42, "decision": "sample_decision"})

    def test_cache_isolated_and_broken_source_is_reported(self):
        original = self.catalog.lookup("decision", "sample_decision")
        original["action"]["parameters"]["decision"] = "changed"
        self.assertEqual(self.catalog.lookup("decision", "sample_decision")["action"]["parameters"]["decision"], "sample_decision")
        (self.decisions / "broken.txt").write_text("broken_decision = {", encoding="utf-8")
        catalog = PlanningCatalog(self.executable)
        result = catalog.query({"kind": "decisions"})
        self.assertEqual(result["count"], 2)
        self.assertEqual(len(result["definition_errors"]), 1)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            catalog.lookup("decision", "missing")

    def test_unknown_native_cost_and_cooldown_cannot_become_zero_or_available(self):
        for kind, identifier in (("decision", "sample_decision"), ("activity_type", "activity_sample")):
            with self.subTest(kind=kind):
                result = self.catalog.query({"kind": kind, "id": identifier, "character": 42})
                self.assertIsNone(result["available"])
                self.assertIsNone(result["cooldown"]["remaining_days"])
                self.assertTrue(all(result["cost"][key] is None for key in ("gold", "prestige", "piety", "treasury")))

    def test_model_can_submit_start_activity_and_set_activity_intent_commands(self):
        from ck3_llm.protocol.jomini_codec import activity_plan_fields

        detail = self.reader().handle({"kind": "activity_type", "id": "activity_sample", "fields": ["action"]})
        selected = detail["data"]["action"]
        config = ModelConfig(endpoint="http://127.0.0.1:1/v1/chat/completions", model="offline")
        codec = JominiCodec()
        header = {"attribs": 14, "identity": 1, "sender": 2, "receiver": 0}
        with redirect_stdout(io.StringIO()):
            runtime = ModelRuntime(config, model=object())
            try:
                start_request = runtime._action({
                    "action": selected["action"],
                    "request_id": "start-act-1",
                    "parameters": {
                        **selected["parameters"],
                        "start_date": 54296760,
                        "phases": [{"phase": "sample_meal", "province": 12}],
                        "options": {"food": "simple_food"},
                        "intent": {"type": "recreation_intent"},
                        "travel_plan": travel_plan_fields(54296040, 12, destinations=[12], waypoints=[[12]]),
                    },
                })
                intent_request = runtime._action({
                    "action": "set_activity_intent_command",
                    "request_id": "intent-1",
                    "parameters": {"character": 42, "intent": "recreation_intent", "target": 99},
                })
            finally:
                runtime.close()
        self.assertEqual(start_request["command"], "start_activity_command")
        start_fields = activity_plan_fields(
            start_request["type"], start_request["host"], start_request["start_date"], start_request["phases"],
            options=start_request["options"], intent=start_request["intent"], travel_plan=start_request["travel_plan"],
        )
        start_decoded = codec.decode_command(codec.encode_command("start_activity_command", start_fields, header))
        self.assertEqual(start_decoded["type"], "start_activity_command")
        self.assertEqual(start_decoded["fields"]["data"]["host"], 42)
        self.assertEqual(start_decoded["fields"]["data"]["options"], {"food": "simple_food"})
        self.assertEqual(start_decoded["fields"]["data"]["travel_plan"]["destinations"], [12])
        self.assertEqual(intent_request["command"], "set_activity_intent_command")
        intent_fields = {"character": intent_request["character"], "intent": {"type": intent_request["intent"], "target": intent_request["target"]}}
        intent_decoded = codec.decode_command(codec.encode_command("set_activity_intent_command", intent_fields, header))
        self.assertEqual(intent_decoded["fields"], intent_fields)

    def test_direct_world_reader_attaches_native_planning_cooldowns(self):
        executable = self.executable
        calls = []

        class World:
            def __init__(self, pid=None):
                self.memory = type("Memory", (), {"executable": executable})()

            def planning_cooldown(self, kind, identifier, character_id):
                calls.append((kind, identifier, character_id))
                return {"remaining_days": 12, "remaining_hours": 288, "on_cooldown": True,
                        "expiration_date": 54296328, "current_date": 54296040, "status": "native_memory"}

            def close(self):
                pass

        with patch("ck3_llm.world.live_objects.MemoryWorld", World), DirectWorldReader(character_id=42) as reader:
            dec = reader.handle({"kind": "decision", "id": "sample_decision", "fields": ["cooldown"]})
            act = reader.handle({"kind": "activity_type", "id": "activity_sample", "fields": ["cooldown"]})
        self.assertEqual(calls, [("decision", "sample_decision", 42), ("activity_type", "activity_sample", 42)])
        self.assertEqual(dec["data"]["cooldown"]["remaining_days"], 12)
        self.assertTrue(act["data"]["cooldown"]["on_cooldown"])

    def test_world_planning_cooldown_reads_robin_hood_table_and_rejects_mutations(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.base = 0x140000000
        memory = world.memory = BufferMemory()
        world.tables = {}
        world.references = set()
        world.state = {}
        world.title_index = None
        world.primary_cache = {}
        world.summary_cache = {}
        world.province_index = None
        world.ruler_cache = {}
        world._scope_tokens = None
        world._world_identity = None
        world._last_clock = None
        world._map_data = None
        world._navigation_nodes = {}

        world_ptr, world_data, sync_ptr = 0x20000100, 0x20000200, 0x20000300
        char_db, char_slots, char_ptr, alive_ptr, landed_ptr = 0x20000400, 0x20001000, 0x20002000, 0x20003000, 0x20004000
        dec_def, act_def = 0x20005000, 0x20005200
        dec_buckets, act_buckets = 0x20006000, 0x20007000

        def write_str(address, text):
            raw = text.encode("utf-8")
            memory.write(address, raw.ljust(16, b"\0"))
            memory.put(address + 0x10, "QQ", len(raw), 15)

        memory.put(world.base + 0x5C7DBE8, "Q", world_ptr)
        memory.put(world.base + 0x5C7F410, "Q", sync_ptr)
        memory.put(world_ptr + 0xA0, "Q", world_data)
        memory.put(world_ptr + 8, "I", 54296040)
        memory.put(sync_ptr + 0x68, "I", 10)
        memory.put(world.base + 0x5C7C4F0, "Q", char_db)
        memory.put(char_db + 0x20, "QI", char_slots, 0)
        memory.put(char_db + 0x2C, "I", 100)
        memory.put(char_slots + 42 * 16 + 8, "Q", char_ptr)
        memory.put(char_ptr, "Q", world.base + 0x47748A0)
        memory.put(char_ptr + 0x18, "I", 42)
        memory.put(char_ptr + 0x1B0, "QQ", alive_ptr, 0)
        memory.put(char_ptr + 0x1C0, "Q", landed_ptr)
        for defn, key in ((dec_def, "sample_decision"), (act_def, "activity_sample")):
            write_str(defn + 0x18, key)
            memory.put(defn + 0x38, "I", 0x4744624F)

        # Single-bucket table (capacity_mask=0, probe_limit=2) -> home slot is always 0.
        memory.put(landed_ptr + 0x438, "QIIB", dec_buckets, 1, 0, 2)
        memory.put(dec_buckets + 4, "I", 1)
        memory.put(dec_buckets + 8, "QI", dec_def, 54296040 + 48)
        memory.put(alive_ptr + 0x508, "QIIB", act_buckets, 1, 0, 2)
        memory.put(act_buckets + 4, "I", 1)
        memory.put(act_buckets + 8, "QI", act_def, 54296040)  # equal == expired

        dec_res = world.planning_cooldown("decision", "sample_decision", 42)
        self.assertEqual(dec_res["remaining_days"], 2)
        self.assertTrue(dec_res["on_cooldown"])
        self.assertEqual(world.planning_cooldown("decision", "missing_decision", 42)["remaining_days"], 0)
        act_res = world.planning_cooldown("activity_type", "activity_sample", 42)
        self.assertEqual(act_res["remaining_days"], 0)
        self.assertFalse(act_res["on_cooldown"])

        memory.put(char_ptr + 0x1C0, "Q", 0)
        self.assertEqual(world.planning_cooldown("decision", "sample_decision", 42)["reason"], "character_not_landed")
        memory.put(char_ptr + 0x1B0, "Q", 0)
        self.assertEqual(world.planning_cooldown("activity_type", "activity_sample", 42)["reason"], "character_dead")

        # Restore and verify mid-read world advance raises.
        memory.put(char_ptr + 0x1B0, "Q", alive_ptr)
        memory.put(char_ptr + 0x1C0, "Q", landed_ptr)
        orig_u32 = memory.u32

        def mutating_u32(address, **options):
            val = orig_u32(address, **options)
            if address == world_ptr + 8:
                memory.put(world_ptr + 8, "I", val + 1)
            return val

        memory.u32 = mutating_u32
        with self.assertRaisesRegex(ValueError, "kept changing during the cooldown query"):
            world.planning_cooldown("decision", "sample_decision", 42)

    def test_direct_world_reader_attaches_shadow_decision_and_activity_evaluation(self):
        executable = self.executable

        class World:
            def __init__(self, pid=None):
                self.memory = type("Memory", (), {"executable": executable})()

            def evaluate_decision(self, identifier, character_id):
                return {"shown": True, "valid": True, "can_afford": False, "available": False,
                        "cost": {"gold": 100.0, "prestige": 0.0, "piety": 50.0, "treasury": 0.0},
                        "source": "native_shadow_unicorn"}

            def evaluate_activity_type(self, identifier, character_id):
                return {"shown": True, "available": True, "can_start": True, "source": "native_shadow_unicorn"}

            def close(self):
                pass

        with patch("ck3_llm.world.live_objects.MemoryWorld", World), DirectWorldReader(character_id=42) as reader:
            dec = reader.handle({"command": "query", "kind": "decision", "id": "sample_decision"})["data"]
            act = reader.handle({"command": "query", "kind": "activity_type", "id": "activity_sample"})["data"]
        self.assertTrue(dec["shown"])
        self.assertFalse(dec["available"])
        self.assertEqual(dec["availability"], "native_shadow_unicorn")
        self.assertEqual(dec["cost"]["gold"], 100.0)
        self.assertEqual(dec["cost"]["piety"], 50.0)
        self.assertEqual(dec["cost"]["status"], "native_shadow_unicorn")
        self.assertTrue(act["shown"])
        self.assertTrue(act["available"])
        self.assertEqual(act["availability"], "native_shadow_unicorn")
