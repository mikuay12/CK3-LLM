"""Regress pending event details, script metadata and bounded option queries."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from event_catalog import EventCatalog, script_fields
from event_evaluation import evaluate_condition, render_text
from live_objects import DirectObservationQueue, DirectWorldReader
from memory_world import MemoryWorld
from notification_catalog import definitions


SCRIPT = '''namespace = sample
sample.1 = {
    type = character_event
    title = sample.1.t
    desc = sample.1.desc
    option = {
        name = sample.1.a
        trigger = { gold >= 10 }
        add_gold = -10
        # A comment with braces { }
        custom_tooltip = "quoted } brace"
    }
    option = { name = sample.1.b add_prestige = 20 }
}
sample.2 = {
    type = letter_event
    desc = { first_valid = { triggered_desc = { trigger = { always = yes } desc = sample.1.desc } } }
}
'''


class EventFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.executable = self.root / "binaries/ck3.exe"
        scripts = self.root / "game/events"
        scripts.mkdir(parents=True)
        self.script = scripts / "sample_events.txt"
        self.script.write_text(SCRIPT, encoding="utf-8")
        locale = self.root / "game/localization/simp_chinese"
        locale.mkdir(parents=True)
        (locale / "events_l_simp_chinese.yml").write_text(
            'l_simp_chinese:\n sample.1.t: "A pending event"\n'
            ' sample.1.desc: "Hello [ROOT.Char.GetName]"\n'
            ' sample.1.a: "Pay the cost"\n sample.1.b: "Gain prestige"\n', encoding="utf-8")


class CatalogTests(EventFixture):
    def test_live_event_details_do_not_run_the_reference_condition_interpreter(self):
        self.script.write_text(SCRIPT.replace("gold >= 10", "always = yes"), encoding="utf-8")
        catalog = EventCatalog(self.executable)
        with patch("event_evaluation.evaluate_condition", side_effect=AssertionError("Evaluated a game predicate")):
            _, options = catalog.describe({"event": "sample.1", "option_count": 2, "scope": {}})
        self.assertIsNone(options[0]["condition_results"][0]["value"])
        self.assertEqual(options[0]["condition_results"][0]["basis"], "native_result_not_located")

    def test_scope_names_resolve_without_changing_cached_templates(self):
        catalog = EventCatalog(self.executable)
        event = {"event": "sample.1", "option_count": 2,
                 "scope": {"root": {"type": "char", "id": 42, "name": "Juma"}, "targets": {}}}
        definition, options = catalog.describe(event)
        description = definition["descriptions"][0]
        self.assertEqual(description["text"], "Hello Juma")
        self.assertTrue(description["resolved"])
        self.assertEqual(description["template"], "Hello [ROOT.Char.GetName]")
        self.assertEqual(catalog.lookup("sample.1")["descriptions"][0]["text"], description["template"])
        self.assertEqual(options[0]["condition_results"][0]["value"], None)
        self.assertEqual(options[1]["condition_results"], [])
        event["title_override"] = "sample.1.b"
        self.assertEqual(catalog.describe(event)[0]["title"]["text"], "Gain prestige")

    def test_parser_preserves_order_conditions_quotes_and_effects(self):
        catalog = EventCatalog(self.executable)
        row = catalog.lookup("sample.1")
        self.assertEqual(row["title"]["text"], "A pending event")
        self.assertTrue(row["title"]["resolved"])
        self.assertFalse(row["descriptions"][0]["resolved"])
        self.assertEqual([item["index"] for item in row["options"]], [0, 1])
        self.assertEqual([item["name"]["text"] for item in row["options"]], ["Pay the cost", "Gain prestige"])
        self.assertIn("gold >= 10", row["options"][0]["conditions"][0])
        self.assertIn("add_gold = -10", row["options"][0]["script"])
        self.assertFalse(row["options"][1]["conditional"])
        row["options"][0]["name"]["text"] = "Changed"
        self.assertEqual(catalog.lookup("sample.1")["options"][0]["name"]["text"], "Pay the cost")

    def test_conditional_description_keeps_branches_and_localization(self):
        result = EventCatalog(self.executable).lookup("sample.2")
        self.assertFalse(result["descriptions"][0]["resolved"])
        self.assertIn("Hello [ROOT.Char.GetName]", json.dumps(result))
        self.assertIn("trigger", result["descriptions"][0]["script"])

    def test_native_option_count_mismatch_and_missing_sources_are_explicit(self):
        catalog = EventCatalog(self.executable)
        definition, options = catalog.describe({"event": "sample.1", "option_count": 3})
        self.assertEqual(definition["status"], "option_count_mismatch")
        self.assertEqual(options, [])
        definition, options = catalog.describe({"event": "modded.1", "option_count": 1})
        self.assertEqual(definition["status"], "unavailable")
        self.assertEqual(options, [])

    def test_existing_notification_parser_stays_compact_by_default(self):
        self.assertNotIn("body", definitions(self.script)[0])
        self.assertIn("body", definitions(self.script, include_body=True)[0])
        with self.assertRaises(ValueError):
            script_fields("option = { name = missing")


class EventEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.context = {"root": {"type": "char", "id": 42, "name": "Juma"}, "targets": {
            "recipient": {"type": "char", "id": 99, "name": "Recipient"},
            "enabled": {"type": "boolean", "value": True},
            "unresolved": {"type": "unknown", "resolved": False},
            "empty": {"type": "char", "id": None},
            "title": {"type": "lt", "title": {"key": "k_test"}}}}
        self.labels = type("Labels", (), {"name": lambda self, key: "Test Kingdom" if key == "k_test" else None})()

    def test_character_and_title_scope_names_are_rendered(self):
        text, unresolved = render_text(
            "[ROOT.Char.GetName] [scope:recipient.Char.GetFirstNameNoTooltip] [scope:title.GetName]",
            self.context, self.labels)
        self.assertEqual(text, "Juma Recipient Test Kingdom")
        self.assertEqual(unresolved, [])

    def test_unknown_expressions_and_missing_scopes_are_preserved(self):
        text = "[ROOT.Char.Custom('Title')] [scope:missing.GetName] [scope:empty.GetName]"
        rendered, unresolved = render_text(text, self.context, self.labels)
        self.assertEqual(rendered, text)
        self.assertEqual(len(unresolved), 3)

    def test_boolean_conditions_and_logic_use_three_valued_results(self):
        cases = {
            "always = yes": True, "always = no": False,
            "exists = root exists = scope:recipient": True,
            "exists = scope:missing": False, "exists = scope:empty": False,
            "exists = scope:unresolved": None, "scope:enabled = yes": True,
            "scope:enabled = no": False, "NOT = { always = no }": True,
            "AND = { always = yes gold >= 10 }": None,
            "AND = { always = no gold >= 10 }": False,
            "OR = { always = yes gold >= 10 }": True,
            "NOR = { always = no always = no }": True,
            "NAND = { always = yes always = no }": True,
            "NOT = { always = yes gold >= 10 }": None,
            "unknown_trigger = yes": None,
        }
        for script, expected in cases.items():
            with self.subTest(script=script):
                self.assertIs(evaluate_condition(script, self.context), expected)

    def test_unsupported_and_malformed_syntax_cannot_be_silently_dropped(self):
        for script in ("always = yes gold >= 10", "always = yes !", "always = yes }", 'always = yes "',
                       "AND = { always = yes", "always = yes unknown_trigger = yes"):
            with self.subTest(script=script):
                self.assertIsNone(evaluate_condition(script, self.context))
        self.assertIsNone(evaluate_condition("AND = {" * 70 + "always = yes" + "}" * 70, self.context))


class NativeEventTests(unittest.TestCase):
    def setUp(self):
        self.world = MemoryWorld.__new__(MemoryWorld)
        self.present = True
        self.scope = {"root": {"type": "char", "id": 42, "name": "Ruler"},
                      "targets": {"recipient": {"type": "char", "id": 99, "name": "Recipient"}}}
        class Memory:
            def u32(self, address, *, fresh=False):
                return {0x11bc: 7, 0x11b8: 42, 0x11a8: 365 * 24 * 5030}[address]

            def pointer(self, address, *, fresh=False):
                return {0x11b0: 0x5000}[address]

            def string(self, address):
                return {0x5010: "sample.1", 0x1188: ""}[address]

            def u8(self, address):
                return {0x11c0: 1, 0x11c1: 0}[address]
        self.world.memory = Memory()
        self.world.world_data = lambda: 0x2000
        self.world.scope_context = lambda pointer: deepcopy(self.scope)
        self.world.lookup = lambda kind, value: value
        def vector(address, width=4, *, fresh=False):
            self.assertEqual(width, 8)
            if address == 0x51a0:
                return [0x6000, 0x7000]
            self.assertEqual(address, 0x38398)
            return [0x1000] if not fresh or self.present else []
        self.world.vector = vector

    def test_event_reads_native_count_scope_and_recipient(self):
        event = self.world._query({"kind": "event", "id": 7, "character": 42})
        self.assertEqual(event["option_count"], 2)
        self.assertEqual(event["scope"], self.scope)
        self.assertTrue(event["primary"])
        self.assertEqual(event["options_query"], {"kind": "event_options", "id": 7, "character": 42})
        self.assertEqual(self.world.events(42)[0]["details_query"], {"kind": "event", "id": 7, "character": 42})

    def test_other_recipient_ended_and_invalid_events_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "another character"):
            self.world.event(7, 99)
        with self.assertRaisesRegex(ValueError, "already ended"):
            self.world.event(8)
        with self.assertRaises(ValueError):
            self.world.event(True)
        self.present = False
        with self.assertRaisesRegex(ValueError, "changed during reading"):
            self.world.event(7)

    def test_fresh_recipient_overrides_cached_event_ownership(self):
        original = self.world.memory.u32

        def u32(address, *, fresh=False):
            if address == 0x11b8 and fresh:
                return 99
            return original(address, fresh=fresh)

        self.world.memory.u32 = u32
        with self.assertRaisesRegex(ValueError, "another character"):
            self.world.event(7, 42)
        self.assertEqual(self.world.events(42), [])
        self.assertEqual(self.world.events(99)[0]["character_id"], 99)


class EventQueryTests(EventFixture):
    def setUp(self):
        super().setUp()
        executable = self.executable
        class World:
            def __init__(self, pid=None):
                self.memory = type("Memory", (), {"executable": executable, "is_running": lambda self: True})()
                self.event = {"id": 7, "character_id": 42, "event": "sample.1", "option_count": 2,
                              "expiration_date": 365 * 24 * 5030,
                              "scope": {"root": {"type": "char", "id": 42}, "targets": {}},
                              "options_query": {"kind": "event_options", "id": 7, "character": 42}}
                self.requests = []

            def query(self, request):
                self.requests.append(deepcopy(request))
                if request["kind"] == "events":
                    return {"data": {"items": [], "count": 0, "offset": 0, "next_offset": None},
                            "runtime": {"date": 365 * 24 * 5030, "tick": 1}}
                if request.get("character", 42) != 42:
                    raise ValueError("The pending event belongs to another character")
                return {"data": deepcopy(self.event), "runtime": {"date": 365 * 24 * 5030, "tick": 1}}

            def close(self):
                pass
        patcher = patch("live_objects.MemoryWorld", World)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.reader = DirectWorldReader(character_id=42)
        self.addCleanup(self.reader.close)

    def test_detail_localization_default_recipient_and_selected_fields(self):
        result = self.reader.handle({"kind": "event", "id": 7})
        self.assertEqual(result["data"]["definition"]["title"]["text"], "A pending event")
        self.assertEqual(result["data"]["expiration_date"], "30.1.1")
        self.assertEqual(self.reader.memory.requests[-1]["character"], 42)
        result = self.reader.handle({"kind": "event", "id": 7, "fields": ["option_count", "scope"]})
        self.assertEqual(result["data"]["option_count"], 2)
        with self.assertRaisesRegex(ValueError, "another character"):
            self.reader.handle({"kind": "event", "id": 7, "character": 99})

    def test_pending_event_list_defaults_to_controlled_character_and_accepts_explicit_recipient(self):
        self.reader.handle({"kind": "events"})
        self.assertEqual(self.reader.memory.requests[-1]["id"], 42)
        self.reader.handle({"kind": "events", "character": 99})
        self.assertEqual(self.reader.memory.requests[-1]["id"], 99)
        self.reader.handle({"kind": "events", "id": 70})
        self.assertEqual(self.reader.memory.requests[-1]["id"], 70)

    def test_option_summaries_page_and_full_effect_query(self):
        result = self.reader.handle({"kind": "event_options", "id": 7, "limit": 1})
        self.assertEqual(result["data"]["count"], 2)
        self.assertEqual(result["data"]["next_offset"], 1)
        option = result["data"]["items"][0]
        self.assertEqual(option["name"]["text"], "Pay the cost")
        self.assertNotIn("script", option)
        details = self.reader.handle(option["details_query"])
        self.assertIn("add_gold = -10", details["data"]["items"][0]["script"])
        self.assertEqual(details["data"]["items"][0]["availability"], "requires_game_evaluation")
        self.assertEqual(details["data"]["items"][0]["condition_results"][0]["value"], None)

    def test_public_event_names_and_unavailable_option_details(self):
        self.reader.memory.event["scope"]["root"]["name"] = "Juma"
        result = self.reader.handle({"kind": "event", "id": 7, "fields": ["definition.descriptions"]})
        self.assertEqual(result["data"]["definition.descriptions"][0]["text"], "Hello Juma")
        self.reader.memory.event["event"] = "modded.1"
        summary = self.reader.handle({"kind": "event_options", "id": 7, "limit": 1})
        result = self.reader.handle(summary["data"]["items"][0]["details_query"])
        self.assertEqual(result["data"]["items"][0]["condition_results"], [])
        self.assertIsNone(result["data"]["items"][0]["script"])

    def test_budget_continuation_and_missing_metadata_keep_native_indices(self):
        one = self.reader.handle({"kind": "event_options", "id": 7, "limit": 1})
        budget = len(json.dumps(one, ensure_ascii=False, separators=(",", ":"))) + 10
        result = self.reader.handle({"kind": "event_options", "id": 7, "max_chars": budget})
        self.assertEqual(len(result["data"]["items"]), 1)
        self.assertEqual(result["data"]["next_offset"], 1)
        self.reader.memory.event["event"] = "modded.1"
        result = self.reader.handle({"kind": "event_options", "id": 7})
        self.assertEqual(result["data"]["definition_status"], "unavailable")
        self.assertEqual([item["index"] for item in result["data"]["items"]], [0, 1])
        self.assertIsNone(result["data"]["items"][0]["name"])

    def test_sdk_background_queue_uses_same_event_api(self):
        connection = DirectObservationQueue(123)
        connection.submit({"command": "query", "kind": "event_options", "id": 7,
                           "fields": ["index", "name"], "request_id": "options"}, 42)
        events = connection.finish()
        result = next(event for event in events if event["event"] == "interface_response")
        self.assertEqual(result["request_id"], "options")
        self.assertEqual([item["index"] for item in result["result"]["data"]["items"]], [0, 1])
        self.assertFalse(any(event["event"] == "command_error" for event in events))


if __name__ == "__main__":
    unittest.main()
