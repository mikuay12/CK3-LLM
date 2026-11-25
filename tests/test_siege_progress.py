"""Regress native siege morale and model-facing progress queries."""

from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from ck3_llm.world.live_objects import DirectWorldReader
from ck3_llm.world.memory_world import MemoryWorld


class BufferMemory:
    executable = Path("C:/missing/binaries/ck3.exe")

    def __init__(self):
        self.bytes = {}
        self.writes = []

    def write(self, address, data):
        self.writes.append((address, data))
        self.bytes.update((address + index, value) for index, value in enumerate(data))

    def put(self, address, format, *values):
        self.write(address, struct.pack("<" + format, *values))

    def read(self, address, size, **options):
        return bytes(self.bytes.get(address + index, 0) for index in range(size))

    def pointer(self, address, **options):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def u32(self, address, **options):
        return struct.unpack("<I", self.read(address, 4))[0]

    def i32(self, address):
        return struct.unpack("<i", self.read(address, 4))[0]

    def u8(self, address):
        return self.read(address, 1)[0]

    def fixed(self, address):
        return struct.unpack("<q", self.read(address, 8))[0] / 100000

    def string(self, address):
        length = self.pointer(address + 0x10)
        data = address if self.pointer(address + 0x18) < 16 else self.pointer(address)
        return self.read(data, length).decode("utf-8")


class SiegeFixture:
    """Layout shared by ordinary reader tests and offline native execution."""

    def __init__(self, base=0x140000000, scratch=0x20000000):
        self.base = base
        self.memory = BufferMemory()
        self.siege = scratch + 0x1000
        self.province = scratch + 0x2000
        self.regiment = scratch + 0x3000
        self.title = scratch + 0x4000
        self.lease_title = scratch + 0x4400
        self.liege_title = scratch + 0x4800
        self.character = scratch + 0x5000
        self.beneficiary = scratch + 0x5400
        self.alive = scratch + 0x6000
        self.beneficiary_alive = scratch + 0x6400
        self.landed = scratch + 0x7000
        self.county = scratch + 0x8000
        self.cache = scratch + 0x9000
        self.beneficiary_cache = scratch + 0x9800
        dummy_title = scratch + 0xa000
        dummy_character = scratch + 0xa400
        definition = scratch + 0xa800
        rows = {"siege": {1: self.siege}, "regiment": {0: self.regiment},
                "title": {1: self.title, 2: self.lease_title, 3: self.liege_title},
                "character": {42: self.character, 43: self.beneficiary}}
        for index, (kind, global_rva, id_offset, default) in enumerate((
                ("regiment", 0x5d33b68, 0x10, self.regiment), ("title", 0x5d32b08, 0x10, dummy_title),
                ("character", 0x5c7c4f0, 0x18, dummy_character))):
            database = scratch + 0xb800 + index * 0x80
            slots = scratch + (0xb000, 0xb100, 0xb200)[index]
            self.memory.put(base + global_rva, "Q", database)
            # The native fallback pointer precedes the title table and follows the other two.
            fallback_rva = global_rva - 0x18 if kind == "title" else global_rva + 8
            self.memory.put(base + fallback_rva, "Q", default)
            self.memory.put(database + 0x20, "Q", slots)
            self.memory.put(database + 0x2c, "I", max(rows[kind]) + 1)
            for identifier, pointer in rows[kind].items():
                self.memory.put(slots + identifier * 16 + 8, "Q", pointer)
                self.memory.put(pointer + id_offset, "I", identifier)
        self.memory.put(dummy_title + 0x128, "I", 0xffffffff)
        self.memory.put(dummy_character + 0x18, "I", 0xffffffff)
        self.memory.put(self.siege + 8, "II", 1, 0x53696765)
        self.memory.put(self.siege + 0x200, "Q", self.province)
        self.memory.put(self.siege + 0x208, "I", 0xffffffff)
        self.memory.put(self.province + 0x10, "I", 2482)
        self.memory.put(self.province + 0x710, "Q", self.province)
        self.memory.put(self.province + 0x70c, "I", 0)
        self.memory.put(self.province + 0x738, "II", 1, 0xffffffff)
        self.memory.put(self.province + 0x848, "Q", self.county)
        self.memory.put(self.province + 0x850, "i", 3)
        self.memory.put(self.province + 0x730, "i", 300)
        self.memory.put(self.regiment + 0x18, "II", 300, 300)
        self.memory.put(self.title + 0x48, "Q", definition)
        self.memory.put(definition + 0x64, "I", 1)
        self.memory.put(self.title + 0x128, "II", 42, 0xffffffff)
        self.memory.put(self.title + 0xe8, "I", 3)
        self.memory.put(self.lease_title + 0x128, "I", 43)
        self.memory.put(self.liege_title + 0x128, "I", 43)
        self.memory.put(self.character + 0x1c, "I", 0x43686172)
        self.memory.put(self.character + 0x1b0, "Q", self.alive)
        self.memory.put(self.character + 0x1c0, "Q", self.landed)
        self.memory.put(self.landed + 0x1b8, "I", 43)
        self.memory.put(self.alive + 0x258, "Q", self.cache)
        self.memory.put(self.cache + 8, "Q", self.character)
        self.memory.put(self.beneficiary + 0x1c, "I", 0x43686172)
        self.memory.put(self.beneficiary + 0x1b0, "Q", self.beneficiary_alive)
        self.memory.put(self.beneficiary_alive + 0x258, "Q", self.beneficiary_cache)
        self.memory.put(self.beneficiary_cache + 8, "Q", self.beneficiary)
        self.memory.put(self.county + 0x3e0, "I", 0x436f4461)
        self.memory.put(base + 0x5c7e4e4, "i", 25)
        self.memory.put(base + 0x5c7e690, "q", 7500000)
        self.memory.put(base + 0x5c7e698, "q", 10000000)
        self.containers = [self.province + 0x30, self.county + 0x98, self.cache + 0x10, self.beneficiary_cache + 0x10]
        for index, container in enumerate(self.containers):
            keys = scratch + 0xc000 + index * 0x100
            self.memory.put(container + 0x68, "Q", keys)
            self.memory.put(container + 0x74, "i", 3)
            self.memory.put(container + 0xd0, "Q", keys + 0x40)
            self.memory.put(keys, "3H", 10, 0x1d8, 1000)
            self.memory.put(keys + 0x40, "3q", 5000, 0, 6000)
        self.world = MemoryWorld.__new__(MemoryWorld)
        self.world.base = base
        self.world.memory = self.memory
        self.world.lookup = lambda kind, identifier: rows[kind][identifier]
        self.world.province = lambda identifier: {"title": {"id": 1, "holder_id": 42}, "realm": {"ruler": {"id": 42}}}
        self.world.character_name = lambda identifier: {"id": identifier} if identifier is not None else None

    def modifiers(self, *values):
        for container, value in zip(self.containers, values):
            array = self.memory.pointer(container + 0xd0)
            self.memory.put(array + 8, "q", value)

    def progress(self):
        return self.world.siege_progress(self.siege, self.province)


class SiegeProgressTests(unittest.TestCase):
    def test_character_siege_filter_uses_owned_unit_cache_for_foreign_armies(self):
        fixture = SiegeFixture()
        fixture.world.rows = lambda kind: [(1, fixture.siege)]
        fixture.world.liege = lambda pointer: None
        fixture.memory.put(fixture.siege + 0x208, "I", 99)
        # No army 99 exists in the fixture. The native controlled-character
        # unit cache is empty, so this foreign siege requires no army lookup.
        result = fixture.world._query({"kind": "sieges", "character": 43})
        self.assertEqual(result["count"], 0)

    def test_character_siege_filter_reads_owned_army_identity_from_units(self):
        fixture = SiegeFixture()
        unit = fixture.regiment + 0x1000
        array = fixture.regiment + 0x1400
        fixture.memory.put(fixture.landed + 0x278, "QII", array, 1, 1)
        fixture.memory.put(array, "I", 9)
        fixture.memory.put(unit + 0x174, "II", 42, 99)
        fixture.memory.put(fixture.siege + 0x208, "I", 99)
        original = fixture.world.lookup
        fixture.world.lookup = lambda kind, value: unit if kind == "unit" else original(kind, value)
        fixture.world.rows = lambda kind: [(1, fixture.siege)]
        fixture.world.siege = lambda value: {"id": value}
        result = fixture.world._query({"kind": "sieges", "character": 42})
        self.assertEqual(result["items"], [{"id": 1, "side": "attacker"}])

    def test_garrison_is_native_and_progress_results_stay_unknown(self):
        fixture = SiegeFixture()
        fixture.memory.put(fixture.siege + 0x3d0, "q", 16250000)
        progress = fixture.progress()
        self.assertIsNone(progress["points_required"])
        self.assertIsNone(progress["percent"])
        self.assertEqual(progress["garrison_current"], 300)
        fixture.memory.put(fixture.regiment + 0x1c, "I", 150)
        fixture.memory.put(fixture.siege + 0x3d0, "q", 10625000)
        self.assertIsNone(fixture.progress()["points_required"])
        self.assertEqual(fixture.progress()["garrison_current"], 150)

    def test_modifiers_do_not_replace_an_unlocated_native_capacity_result(self):
        fixture = SiegeFixture()
        fixture.modifiers(20000, 30000, 50000, 100000)
        self.assertIsNone(fixture.progress()["garrison_capacity"])
        fixture.memory.put(fixture.province + 0x73c, "I", 43)
        self.assertIsNone(fixture.progress()["garrison_capacity"])
        fixture.memory.put(fixture.province + 0x73c, "I", 0xffffffff)
        fixture.memory.put(fixture.title + 0x12c, "I", 2)
        self.assertIsNone(fixture.progress()["garrison_capacity"])
        fixture.memory.put(fixture.title + 0x130, "B", 1)
        self.assertIsNone(fixture.progress()["garrison_capacity"])
        fixture.memory.put(fixture.title + 0x130, "B", 0)
        fixture.memory.put(fixture.title + 0x128, "II", 0xffffffff, 0xffffffff)
        self.assertIsNone(fixture.progress()["garrison_capacity"])

    def test_empty_garrison_and_special_type_read_the_native_count_fields(self):
        fixture = SiegeFixture()
        fixture.memory.put(fixture.regiment + 0x1c, "I", 0)
        self.assertEqual(fixture.progress()["garrison_current"], 0)
        fixture.memory.put(fixture.regiment + 0x30, "I", 3)
        self.assertEqual(fixture.progress()["garrison_current"], 300)
        fixture.modifiers(-200000, 0, 0, 0)
        self.assertIsNone(fixture.progress()["garrison_capacity"])
        fixture.memory.put(fixture.siege + 0x3d0, "q", 100000000)
        self.assertIsNone(fixture.progress()["percent"])
        fixture.memory.put(fixture.province + 0x70c, "I", 0xffffffff)
        self.assertEqual(fixture.progress()["garrison_current"], 0)

    def test_progress_does_not_evaluate_modifier_tables(self):
        fixture = SiegeFixture()
        fixture.memory.put(fixture.containers[0] + 0x74, "i", -1)
        fixture.world.province_modifier_raw = lambda *args, **kwargs: self.fail("Recomputed garrison capacity")
        self.assertEqual(fixture.progress()["garrison_current"], 300)

    def test_public_siege_query_projects_progress_fields_and_hides_runtime(self):
        fixture = SiegeFixture()
        fixture.memory.put(fixture.siege + 0x3d0, "q", 16250000)

        class World:
            def __init__(self, pid=None):
                self.memory = fixture.memory

            def query(self, request):
                return {"data": fixture.world._query(request), "runtime": {"tick": 10, "world": 100,
                                                                           "date": 365 * 24 * 5954}}

            def close(self):
                pass

        with patch("ck3_llm.world.live_objects.MemoryWorld", World), DirectWorldReader(character_id=42) as reader:
            result = reader.handle({"kind": "siege", "id": 1, "fields": ["progress.percent", "progress.points_required"],
                                    "max_chars": 500})
        self.assertEqual(result["data"], {"progress.percent": None, "progress.points_required": None})
        self.assertNotIn("tick", str(result))
        self.assertNotIn("world", str(result))


if __name__ == "__main__":
    unittest.main()
