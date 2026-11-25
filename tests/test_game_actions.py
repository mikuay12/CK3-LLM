"""Regress model-facing action discovery against the actual command encoders."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ck3_llm.gameplay.game_actions import ACTION_LABELS, action, actions
from ck3_llm.protocol.jomini_codec import JominiCodec
from ck3_llm.world.live_objects import DirectWorldReader


class ActionTests(unittest.TestCase):
    def test_every_native_action_describes_encoder_required_fields(self):
        codec = JominiCodec()
        for name in ACTION_LABELS:
            if action(name)["entry_point"] != "send_game_command":
                continue
            with self.subTest(name=name):
                schema = action(name)
                fields = {}
                for key, parameter in schema["parameters"].items():
                    if not parameter["required"]:
                        continue
                    if parameter["type"] == "enum":
                        fields[key] = parameter["choices"][0]
                    elif parameter["type"] == "string":
                        fields[key] = "example"
                    elif parameter["type"] == "boolean":
                        fields[key] = True
                    else:
                        fields[key] = 1
                header = {"identity": 1, "sender": 2, "receiver": 0, **schema["header"]}
                payload = codec.encode_command(schema["command_type"], fields, header)
                self.assertEqual(codec.decode_command(payload)["type"], name)

    def test_defaults_optional_objects_enums_and_distinct_entity_ids(self):
        move = action("move_unit")
        self.assertIn("unit", move["parameters"]["unit"]["description"])
        self.assertIn("set", move["parameters"]["unit_order_type"]["choices"])
        self.assertEqual(action("hire_mercenary")["parameters"]["flags"]["default"], 3)
        self.assertFalse(action("execute_decision")["parameters"]["parameters"]["required"])
        self.assertEqual(action("declare_war")["parameters"]["character"]["default_source"], "controlled_character")
        self.assertEqual(action("pause_game_command")["header"]["attribs"], 8)
        self.assertIn("event_options", action("select_event_option")["parameters"]["option"]["description"])
        self.assertIn("start_date", action("start_activity_command")["parameters"])
        self.assertEqual(action("start_activity_command")["entry_point"], "start_activity_command")
        self.assertEqual(action("set_activity_intent_command")["entry_point"], "set_activity_intent_command")
        self.assertIn("intent", action("set_activity_intent_command")["parameters"])
        self.assertIn("travel_plan_draft_created", action("join_activity_command")["parameters"])
        self.assertEqual(action("change_council_task")["parameters"]["target"]["type"], "scope_value_object")
        self.assertFalse(action("change_council_task")["parameters"]["target"]["required"])
        self.assertEqual(action("stop")["parameters"]["units"]["type"], "unsigned_integer_array")
        self.assertFalse(action("stop")["parameters"]["units"]["required"])
        self.assertIn("scheme", action("end_scheme")["parameters"])
        self.assertIn("faction", action("join_faction")["parameters"])
        self.assertIn("landed_title", action("create_title")["parameters"])
        self.assertIn("holy_order", action("hire_holy_order")["parameters"])
        self.assertIn("travel_plan", action("cancel_travel_plan_command")["parameters"])
        self.assertEqual(len(ACTION_LABELS), 79)
        move["parameters"]["unit_order_type"]["choices"].clear()
        self.assertIn("set", action("move_unit")["parameters"]["unit_order_type"]["choices"])

    def test_paging_search_and_unknown_names(self):
        result = actions({"limit": 3})
        self.assertEqual(result["count"], len(ACTION_LABELS))
        self.assertEqual(result["next_offset"], 3)
        self.assertEqual(actions({"search": "event"})["items"][0]["id"], "select_event_option")
        for invalid in (None, [], True, "pursue_army", "client_ping"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                action(invalid)

    def test_queries_need_no_memory_reads_and_support_field_budgets(self):
        class World:
            def __init__(self, pid=None):
                self.memory = type("Memory", (), {"executable": Path("C:/missing/binaries/ck3.exe")})()

            def query(self, request):
                raise AssertionError("Action discovery should use the existing encoder schemas")

            def close(self):
                pass
        with patch("ck3_llm.world.live_objects.MemoryWorld", World), DirectWorldReader() as reader:
            response = reader.handle({"kind": "action", "id": "move_unit", "fields": ["parameters.unit"]})
            self.assertTrue(response["data"]["parameters.unit"]["required"])
            response = reader.handle({"kind": "actions", "limit": 2, "fields": ["id", "label"], "max_chars": 500})
            self.assertEqual(len(response["data"]["items"]), 2)
            self.assertNotIn("rva", json.dumps(response))
            self.assertNotIn("tick", json.dumps(response))


if __name__ == "__main__":
    unittest.main()
