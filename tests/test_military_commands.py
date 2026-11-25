"""Regress military command token enums, rally records and optional fields."""

import unittest

from ck3_llm.protocol.jomini_codec import JominiCodec


class MilitaryCommandTests(unittest.TestCase):
    def setUp(self):
        self.codec = JominiCodec()
        self.header = {"identity": 10, "sender": 2, "receiver": 0, "attribs": 14}
        self.movement = {"valid_for": "player", "unit": 16777220, "target": 12784,
                         "unit_order_type": "set", "type": "land", "gathering_army": "reset"}

    def test_movement_round_trip(self):
        data = self.codec.encode_command("move_unit", self.movement, self.header)
        result = self.codec.decode_command(data)
        self.assertEqual(result["fields"], self.movement)
        self.assertEqual(result["default_state"], self.header)

    def test_token_enums_reject_unknown_or_numeric_values(self):
        for value in ("unknown", 1, True, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.codec.encode_command("move_unit", {**self.movement, "valid_for": value}, self.header)

    def test_rally_points_are_province_and_color_records(self):
        fields = {"character": 33588240, "rally_points": [{"province": 12786, "color": 0},
                                                         {"province": 12784, "color": 1}],
                  "rally_point": 0, "raise_army_type": "regular"}
        result = self.codec.decode_command(self.codec.encode_command("raise_troops", fields, self.header))
        self.assertEqual(result["fields"], fields)

    def test_rally_points_reject_uninterpreted_fields(self):
        for points in ({"province": 1}, [{"province": 12786, "amount": 100}], [{"province": True, "color": 0}]):
            with self.subTest(points=points), self.assertRaises(ValueError):
                self.codec.encode_command("raise_troops", {"character": 42, "rally_points": points,
                                                          "raise_army_type": "regular"}, self.header)

    def test_native_optional_defaults_are_omitted(self):
        fields = {"character": 42, "rally_points": [], "rally_point": -1,
                  "raise_army_type": "regular", "supply_limit": False}
        result = self.codec.decode_command(self.codec.encode_command("raise_troops", fields, self.header))
        self.assertEqual(result["fields"], {"character": 42, "raise_army_type": "regular"})

    def test_disband_and_stop_gathering_share_reference_fields(self):
        fields = {"valid_for": "player", "army": 16777220}
        for name in ("disband_army", "stop_gathering_army", "detach_commander"):
            with self.subTest(name=name):
                result = self.codec.decode_command(self.codec.encode_command(name, fields, self.header))
                self.assertEqual(result["type"], name)
                self.assertEqual(result["fields"], fields)

    def test_commander_selection_uses_character_and_army_references(self):
        fields = {"valid_for": "player", "character": 33604295, "army": 150995546}
        result = self.codec.decode_command(self.codec.encode_command("select_commander", fields, self.header))
        self.assertEqual(result["type"], "select_commander")
        self.assertEqual(result["fields"], fields)
        for invalid in ({**fields, "character": True}, {**fields, "army": -1},
                        {**fields, "valid_for": 1}, {**fields, "commander": 42}):
            with self.subTest(fields=invalid), self.assertRaises(ValueError):
                self.codec.encode_command("select_commander", invalid, self.header)

    def test_mercenary_hiring_preserves_native_default_flags(self):
        fields = {"character": 65558, "mercenary_company": 153}
        result = self.codec.decode_command(self.codec.encode_command("hire_mercenary", fields, self.header))
        self.assertEqual(result["fields"], fields)
        explicit = {**fields, "flags": 1}
        result = self.codec.decode_command(self.codec.encode_command("hire_mercenary", explicit, self.header))
        self.assertEqual(result["fields"], explicit)


if __name__ == "__main__":
    unittest.main()
