"""Regress native construction timing and the holding query's public fields."""

import unittest
from unittest.mock import patch

from live_objects import DirectWorldReader
from test_siege_progress import SiegeFixture


DATE = 365 * 24 * 5954


class ConstructionTimingFixture(SiegeFixture):
    def __init__(self, base=0x140000000, scratch=0x20000000):
        super().__init__(base, scratch)
        self.building = scratch + 0x1000000
        self.holding_type = scratch + 0xd800
        self.construction = self.province + 0x688
        self.clock = scratch + 0xd400
        self.world.state = {"date": DATE}
        self.world.province_pointer = lambda identifier: self.province
        self.memory.put(base + 0x5c7ddf0, "q", 10000)
        self.memory.put(base + 0x5c7dbe8, "Q", self.clock)
        self.memory.put(self.clock + 8, "I", DATE)
        for pointer, key in ((self.building, "pastures_01"), (self.holding_type, "castle_holding")):
            raw = key.encode("utf-8")
            self.memory.write(pointer + 0x18, raw.ljust(16, b"\0"))
            self.memory.put(pointer + 0x28, "QQ", len(raw), 15)
            self.memory.put(pointer + 0x38, "I", 0x4744624f)
        self.memory.put(self.building + 0x6f510, "Q", self.holding_type)
        self.memory.put(self.holding_type + 0xd8, "H", 0x220)
        self.memory.put(self.holding_type + 0xe0, "H", 0x221)
        self.memory.put(self.province + 0x620, "QB", self.holding_type, 1)
        self.memory.put(self.construction, "QiI", self.building, 0, DATE - 120 * 24)
        self.memory.put(self.construction + 0x18, "q", 9025000)
        self.memory.put(self.construction + 0x70, "I", 42)
        self.timing_modifiers()

    def timing_modifiers(self, *values):
        for container, modifiers in zip(self.containers, list(values) + [{}] * 4):
            keys = self.memory.pointer(container + 0x68)
            array = self.memory.pointer(container + 0xd0)
            identifiers = [0x1d8, 0x1e0, 0x1e5, 0x220, 0x221]
            self.memory.put(container + 0x74, "i", len(identifiers))
            self.memory.put(keys, "5H", *identifiers)
            self.memory.put(array, "5q", *(modifiers.get(key, 0) for key in identifiers))

    def timing(self):
        return self.world.construction_timing(self.province)


class ConstructionTimingTests(unittest.TestCase):
    def test_remaining_base_days_are_read_and_unlocated_results_are_unknown(self):
        fixture = ConstructionTimingFixture()
        result = fixture.timing()
        self.assertEqual(result["base_days_remaining"], 90.25)
        self.assertIsNone(result["time_multiplier"])
        self.assertIsNone(result["days_remaining"])
        self.assertIsNone(result["end_date"])
        self.assertIsNone(result["progress_percent"])
        self.assertEqual(result["reason"], "native_result_not_located")
        fixture.timing_modifiers({0x1e0: -20000}, {0x220: -10000}, {0x1e0: -20000})
        self.assertEqual(fixture.timing()["base_days_remaining"], 90.25)
        self.assertIsNone(fixture.timing()["days_remaining"])

    def test_holding_type_and_modifiers_do_not_trigger_time_recalculation(self):
        fixture = ConstructionTimingFixture()
        fixture.timing_modifiers({0x1e0: 50000, 0x1e5: -20000, 0x221: -30000})
        self.assertIsNone(fixture.timing()["time_multiplier"])
        fixture.memory.put(fixture.province + 0x628, "B", 0)
        self.assertIsNone(fixture.timing()["time_multiplier"])
        fixture.timing_modifiers({0x1e5: -200000})
        self.assertIsNone(fixture.timing()["time_multiplier"])
        self.assertEqual(fixture.timing()["base_days_remaining"], 90.25)

    def test_beneficiary_lease_and_occupation_keep_stored_time_unchanged(self):
        fixture = ConstructionTimingFixture()
        fixture.timing_modifiers({0x1e0: 10000}, {0x1e0: 20000}, {0x1e0: 30000}, {0x1e0: 40000})
        self.assertEqual(fixture.timing()["base_days_remaining"], 90.25)
        fixture.memory.put(fixture.province + 0x73c, "I", 43)
        self.assertEqual(fixture.timing()["base_days_remaining"], 90.25)
        fixture.memory.put(fixture.title + 0x12c, "I", 2)
        self.assertEqual(fixture.timing()["base_days_remaining"], 90.25)
        fixture.memory.put(fixture.title + 0x130, "B", 1)
        self.assertIsNone(fixture.timing()["progress_percent"])

    def test_completed_construction_and_missing_context_are_explicit(self):
        fixture = ConstructionTimingFixture()
        fixture.memory.put(fixture.construction + 0x18, "q", 0)
        self.assertEqual(fixture.timing()["base_days_remaining"], 0)
        self.assertIsNone(fixture.timing()["progress_percent"])
        fixture.memory.put(fixture.province + 0x710, "Q", 0)
        result = fixture.world.holding_buildings(2482)
        self.assertEqual(result["construction"]["timing"]["status"], "unavailable")
        self.assertEqual(result["construction"]["building_key"], "pastures_01")

    def test_public_holding_query_formats_timing_and_selects_fields(self):
        fixture = ConstructionTimingFixture()

        class World:
            def __init__(self, pid=None):
                self.memory = fixture.memory

            def query(self, request):
                return {"data": fixture.world._query(request), "runtime": {"date": DATE, "tick": 1, "world": 100}}

            def close(self):
                pass

        with patch("live_objects.MemoryWorld", World), DirectWorldReader(character_id=42) as reader:
            result = reader.handle({"kind": "holding_buildings", "id": 2482,
                                    "fields": ["construction.timing.days_remaining", "construction.timing.end_date"],
                                    "max_chars": 500})
        self.assertEqual(result["data"], {"construction.timing.days_remaining": None,
                                          "construction.timing.end_date": None})


if __name__ == "__main__":
    unittest.main()
