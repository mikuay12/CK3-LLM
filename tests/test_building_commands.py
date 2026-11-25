"""Regress construction commands, live definition IDs and building slots."""

import struct
import unittest
from unittest.mock import patch

from ck3_llm.gameplay.game_actions import action
from ck3_llm.protocol.jomini_codec import JominiCodec
from ck3_llm.world.live_objects import DirectWorldReader
from ck3_llm.world.memory_world import MemoryWorld
from tests.test_siege_progress import BufferMemory


class BuildingFixture:
    def __init__(self, base=0x140000000, scratch=0x20000000):
        self.memory = BufferMemory()
        self.database = scratch + 0x1000
        self.types = [scratch + 0x3000, scratch + 0x4000]
        self.holding_type = scratch + 0x5000
        self.province = scratch + 0x6000
        self.holding = self.province + 0x620
        self.slots = scratch + 0x8000
        self.dummy = scratch + 0x9000
        self.memory.put(base + 0x5c7c4c8, "Q", self.database)
        self.memory.put(self.database + 0x50, "QII", scratch + 0x2000, 2, 2)
        self.memory.put(scratch + 0x2000, "2Q", *self.types)
        for pointer, key, days in ((self.types[0], "pastures_01", 365), (self.types[1], "orchards_01", 730),
                                   (self.holding_type, "castle_holding", 0)):
            raw = key.encode("utf-8")
            self.memory.write(pointer + 0x18, raw.ljust(16, b"\0"))
            self.memory.put(pointer + 0x28, "QQ", len(raw), 15)
            self.memory.put(pointer + 0x38, "I", 0x4744624f)
            self.memory.put(pointer + 0x58, "i", days)
        self.memory.put(self.holding, "Q", self.holding_type)
        self.memory.put(self.holding + 0x10, "QII", self.slots, 2, 2)
        self.memory.put(self.slots, "QBB6xQBB6x", self.types[0], 1, 1, 0, 0, 0)
        self.memory.put(self.holding + 0x28, "Q", self.dummy)
        self.memory.put(self.holding + 0x38, "Q", self.dummy)
        self.memory.put(self.holding + 0x48, "Q", self.dummy)
        self.memory.put(self.holding + 0x68, "QiI", self.types[1], 1, 365 * 24 * 5954)
        self.memory.put(self.holding + 0xd8, "I", 42)
        self.memory.put(self.holding + 0xe8, "II", 0xffffffff, 0xffffffff)
        self.world = MemoryWorld.__new__(MemoryWorld)
        self.world.base = base
        self.world.memory = self.memory
        self.world.province_pointer = lambda identifier: self.province


class BuildingTests(unittest.TestCase):
    def test_construction_commands_round_trip_and_describe_native_parameters(self):
        codec = JominiCodec()
        header = {"identity": 1, "sender": 2, "receiver": 0, "attribs": 14}
        fields = {"character": 42, "province": 2482, "index": 1, "type": 0}
        for name in ("add_construction", "cancel_construction"):
            with self.subTest(name=name):
                parameters = fields if name == "add_construction" else {key: fields[key] for key in ("character", "province")}
                decoded = codec.decode_command(codec.encode_command(name, parameters, header))
                self.assertEqual(decoded["fields"], parameters)
                self.assertEqual(decoded["type"], name)
        self.assertIn("building_types", action("add_construction")["parameters"]["type"]["description"])
        with self.assertRaises(ValueError):
            codec.encode_command("add_construction", {**fields, "type": "pastures_01"}, header)

    def test_building_type_ids_come_from_current_memory_order(self):
        fixture = BuildingFixture()
        world = fixture.world
        self.assertEqual(world.building_type("pastures_01")["id"], 0)
        self.assertEqual(world.building_type(1)["base_construction_days"], 730)
        result = world.building_types({"limit": 1})
        self.assertEqual(result["next_offset"], 1)
        self.assertEqual(result["items"][0]["key"], "pastures_01")
        self.assertEqual(world.building_types({"search": "orchard"})["items"][0]["id"], 1)
        fixture.memory.put(fixture.database + 0x50, "QII", 0x20002000, 2, 2)
        fixture.memory.put(0x20002000, "2Q", *reversed(fixture.types))
        self.assertEqual(world.building_type("pastures_01")["id"], 1)
        for invalid in (None, True, -1, 2, "missing"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                world.building_type(invalid)

    def test_holding_slots_and_active_construction_are_semantic(self):
        fixture = BuildingFixture()
        result = fixture.world.holding_buildings(2482)
        self.assertEqual(result["slot_count"], 2)
        self.assertEqual(result["slots"][0], {"index": 0, "building_key": "pastures_01", "disabled": True, "ruined": True})
        self.assertIsNone(result["slots"][1]["building_key"])
        self.assertEqual(result["construction"]["slot_index"], 1)
        self.assertEqual(result["construction"]["building_key"], "orchards_01")
        self.assertEqual(result["construction"]["character_id"], 42)
        fixture.memory.put(fixture.holding + 0x68, "Q", 0)
        self.assertIsNone(fixture.world.holding_buildings(2482)["construction"])

    def test_corrupt_building_vectors_fail_explicitly(self):
        fixture = BuildingFixture()
        fixture.memory.put(fixture.holding + 0x1c, "I", 3)
        with self.assertRaisesRegex(ValueError, "building slots"):
            fixture.world.holding_buildings(2482)
        fixture.memory.put(fixture.database + 0x5c, "I", 3)
        with self.assertRaisesRegex(ValueError, "vector"):
            fixture.world.building_types({})

    def test_public_queries_use_localization_projection_and_budgets(self):
        fixture = BuildingFixture()

        class World:
            def __init__(self, pid=None):
                self.memory = fixture.memory

            def query(self, request):
                return {"data": fixture.world._query(request), "runtime": {"date": 365 * 24 * 5954, "tick": 10, "world": 100}}

            def close(self):
                pass

        with patch("ck3_llm.world.live_objects.MemoryWorld", World), DirectWorldReader(character_id=42) as reader:
            reader.labels.values["building_type_pastures_01"] = "牧牛场"
            response = reader.handle({"kind": "building_types", "search": "牧牛", "fields": ["id", "name"], "max_chars": 500})
            self.assertEqual(response["data"]["items"], [{"id": 0, "name": "牧牛场"}])
            response = reader.handle({"kind": "holding_buildings", "id": 2482, "fields": ["slots", "construction.start_date"],
                                      "max_chars": 1000})
        self.assertEqual(response["data"]["slots"][0]["building_name"], "牧牛场")
        self.assertEqual(response["data"]["construction.start_date"], "954.1.1")
        for internal in ("tick", "world", "pointer"):
            self.assertNotIn(internal, str(response))


if __name__ == "__main__":
    unittest.main()
