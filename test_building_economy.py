"""Regress direct native quotes, cache ownership and construction payments."""

import struct
import unittest
from unittest.mock import patch

from building_economy import APPLICATION_RVA, CACHE_FIELDS, HOLDING_VIEW_CHAIN, TRACK_CACHE_FIELDS
from live_objects import DirectWorldReader
from test_construction_timing import ConstructionTimingFixture, DATE


class EconomyFixture(ConstructionTimingFixture):
    def __init__(self, base=0x140000000, scratch=0x20000000):
        super().__init__(base, scratch)
        self.terrain = self.building + 0x72000
        self.government = self.building + 0x73000
        self.slots = scratch + 0xd100
        terrain_context = self.building + 0x71000
        self.memory.put(self.province + 0x20, "Q", terrain_context)
        self.memory.put(terrain_context + 0xb8, "Q", self.terrain)
        self.memory.put(self.building + 0x58, "i", 365)
        self.memory.put(self.building + 0x6f518, "3q", 10000000, 2000000, 1000000)
        self.memory.put(self.building + 0x6ffe8, "i", 0)
        self.memory.put(self.construction, "Q", 0)
        self.memory.put(self.province + 0x630, "QII", self.slots, 1, 1)
        self.memory.put(self.landed + 0x1e0, "QII", scratch + 0xd200, 1, 1)
        self.memory.put(scratch + 0xd200, "I", 1)
        self.memory.put(self.title + 0x14, "I", 0x4c616e64)
        self.memory.put(self.title + 0x318, "q", 30000000)
        self.memory.put(self.landed + 0x3f8, "Q", self.government)
        self.memory.put(self.government + 0x38, "I", 0x4744624f)
        self.memory.put(self.government + 0x40, "Q", 1 << 43)
        for offset, value in ((0x100, 50000000), (0x130, 10000000), (0x110, 5000000)):
            self.memory.put(self.alive + offset, "q", value)
        for offset, identifier in ((0xda, 0x222), (0xe2, 0x223), (0xdc, 0x224),
                                   (0xe4, 0x225), (0xde, 0x226), (0xe6, 0x227)):
            self.memory.put(self.holding_type + offset, "H", identifier)
        for offset, identifier in ((0x77e, 0x230), (0x780, 0x231), (0x782, 0x232),
                                   (0x784, 0x233), (0x786, 0x234), (0x788, 0x235)):
            self.memory.put(self.terrain + offset, "H", identifier)
        # Native unlanded/default-character modifier reads use an empty aggregate cache.
        dummy_character = scratch + 0xa400
        self.memory.put(dummy_character + 0x1b0, "Q", scratch + 0xd300)
        self.memory.put(scratch + 0xd558, "Q", scratch + 0xd600)
        self.memory.put(scratch + 0xd608, "Q", dummy_character)
        self.world.building_pointers = lambda: [self.building]
        self.economy_modifiers()
        self.install_gui()

    def install_gui(self):
        self.gui_objects = [self.building + 0x80000 + index * 0x1000 for index in range(5)]
        self.view = self.gui_objects[-1]
        self.item_array = self.view + 0x1000
        self.item = self.view + 0x2000
        address = self.base + APPLICATION_RVA
        for pointer, (vtable, next_offset) in zip(self.gui_objects, HOLDING_VIEW_CHAIN):
            self.memory.put(address, "Q", pointer)
            self.memory.put(pointer, "Q", self.base + vtable)
            if next_offset is not None:
                address = pointer + next_offset
        self.memory.put(self.view + 0xd8, "Q", self.province)
        self.memory.put(self.view + 0x1c0, "Q", self.province)
        self.memory.put(self.view + 0xe8, "QII", self.item_array, 1, 1)
        self.memory.put(self.item_array, "Q", self.item)
        self.memory.put(self.item, "QQi", self.view, self.building, 0)
        self.cached_values = {"gold_or_treasury": 12345678, "prestige": 1700003,
                              "piety": 0, "construction_days": 27312547}
        for name, value in self.cached_values.items():
            self.cache_value(name, value)
        for name, unit in (("gold_or_treasury", "@gold!"), ("prestige", "@prestige!"), ("piety", "@piety!")):
            address = self.item + CACHE_FIELDS[name] + 0x38
            self.memory.write(address, unit.encode().ljust(16, b"\0"))
            self.memory.put(address + 0x10, "QQ", len(unit), 15)

    def cache_value(self, name, value, initialized=True):
        state = 0x28 if name == "construction_days" else 8
        self.memory.put(self.item + CACHE_FIELDS[name] + 0x78, "qqIHB", value, 100000,
                        0 if initialized else 1, 0x2b, state if initialized else 0xc)

    def install_track(self):
        self.memory.put(self.view + 0xf4, "I", 0)
        self.track = self.view + 0x1c8
        self.current_building = self.building + 0x200000
        self.memory.put(self.track + 0x450, "QQQQi", self.view, self.current_building,
                        self.building, self.current_building, 0)
        for name, offset in TRACK_CACHE_FIELDS.items():
            self.track_value(offset, self.cached_values[name], duration=name == "construction_days")
        self.track_value(0x558, 0, initialized=False)
        for name, unit in (("gold_or_treasury", "@gold!"), ("prestige", "@prestige!"), ("piety", "@piety!")):
            address = self.track + TRACK_CACHE_FIELDS[name] + 0x38
            self.memory.write(address, unit.encode().ljust(16, b"\0"))
            self.memory.put(address + 0x10, "QQ", len(unit), 15)

    def track_value(self, offset, value, *, initialized=True, duration=False):
        state = 0x28 if duration else 8
        self.memory.put(self.track + offset + 0x78, "qqIHB", value, 100000,
                        0 if initialized else 1, 0x2b, state if initialized else 0xc)

    def economy_modifiers(self, *values):
        identifiers = [0x1d8, *range(0x1e0, 0x1e9), *range(0x220, 0x228), *range(0x230, 0x236)]
        for container, modifiers in zip(self.containers, list(values) + [{}] * 4):
            keys = self.memory.pointer(container + 0x68)
            array = self.memory.pointer(container + 0xd0)
            self.memory.put(container + 0x74, "i", len(identifiers))
            self.memory.put(keys, f"{len(identifiers)}H", *identifiers)
            self.memory.put(array, f"{len(identifiers)}q", *(modifiers.get(key, 0) for key in identifiers))

    def quote(self, **changes):
        request = {"kind": "construction_quote", "id": 2482, "building": 0, "character": 42, "index": 0, **changes}
        return self.world._query(request)


class BuildingEconomyTests(unittest.TestCase):
    def test_upgrade_track_reads_cached_next_building_costs_and_time(self):
        fixture = EconomyFixture()
        fixture.install_track()
        result = fixture.quote()
        self.assertEqual(result["cost"], {"gold_or_treasury": 123.45678, "prestige": 17.00003, "piety": 0})
        self.assertEqual(result["timing"]["construction_days"], 273.12547)
        self.assertEqual(result["quote_cache"]["cache_kind"], "upgrade_track")
        self.assertEqual(result["quote_cache"]["status"], "cached")
        self.assertEqual(result["live_validation"], "pending")
        fixture.memory.put(fixture.track + 0x468, "Q", fixture.building)
        self.assertEqual(fixture.quote()["cost"], result["cost"])

    def test_upgrade_track_reads_native_treasury_cache_and_units(self):
        fixture = EconomyFixture()
        fixture.install_track()
        fixture.track_value(0x478, 0, initialized=False)
        fixture.track_value(0x558, 7250000)
        address = fixture.track + 0x558 + 0x38
        unit = "@treasury!"
        fixture.memory.write(address, unit.encode().ljust(16, b"\0"))
        fixture.memory.put(address + 0x10, "QQ", len(unit), 15)
        result = fixture.quote()
        self.assertEqual(result["cost"]["gold_or_treasury"], 72.5)
        self.assertEqual(result["cost_units"]["gold_or_treasury"], unit)

    def test_upgrade_track_checks_target_parent_slot_and_preview(self):
        for offset, value in ((0x450, 0), (0x460, 0), (0x470, 2)):
            with self.subTest(offset=offset):
                fixture = EconomyFixture()
                fixture.install_track()
                fixture.memory.put(fixture.track + offset, "I" if offset == 0x470 else "Q", value)
                self.assertEqual(fixture.quote()["quote_cache"]["reason"], "quote_not_cached")
        fixture = EconomyFixture()
        fixture.install_track()
        fixture.memory.put(fixture.track + 0x468, "Q", fixture.building + 0x300000)
        self.assertEqual(fixture.quote()["quote_cache"]["reason"], "ambiguous_track_quote_target")

    def test_upgrade_track_rejects_two_initialized_currency_caches(self):
        fixture = EconomyFixture()
        fixture.install_track()
        fixture.track_value(0x558, 0)
        result = fixture.quote()
        self.assertEqual(result["quote_cache"]["reason"], "ambiguous_cached_currency")
        self.assertIsNone(result["cost"]["gold_or_treasury"])

    def test_upgrade_track_preserves_unevaluated_fields(self):
        fixture = EconomyFixture()
        fixture.install_track()
        fixture.track_value(0x478, 0, initialized=False)
        fixture.track_value(0x7f8, 0, initialized=False, duration=True)
        result = fixture.quote()
        self.assertEqual(result["quote_cache"]["status"], "partially_cached")
        self.assertIsNone(result["cost"]["gold_or_treasury"])
        self.assertIsNone(result["timing"]["construction_days"])
        self.assertEqual(result["cost"]["piety"], 0)

    def test_upgrade_track_rejects_changed_context_and_price(self):
        for offset in (0x450, 0x460, 0x470, 0x4f0):
            with self.subTest(offset=offset):
                fixture = EconomyFixture()
                fixture.install_track()
                original = fixture.memory.read
                changed = False

                def read(address, size, **options):
                    nonlocal changed
                    data = original(address, size, **options)
                    if not changed and address == fixture.track + 0x7f8 + 0x78:
                        fixture.memory.write(fixture.track + offset, b"\xff")
                        changed = True
                    return data

                fixture.memory.read = read
                result = fixture.quote()
                self.assertEqual(result["quote_cache"]["reason"], "native_cache_changed_during_read")
                self.assertIsNone(result["cost"]["gold_or_treasury"])

    def test_candidate_quote_reads_native_results_and_preserves_freshness(self):
        result = EconomyFixture().quote()
        self.assertEqual(result["cost"], {"gold_or_treasury": 123.45678, "prestige": 17.00003, "piety": 0})
        self.assertEqual(result["timing"]["construction_days"], 273.12547)
        self.assertEqual(result["quote_cache"]["status"], "cached")
        self.assertEqual(result["quote_cache"]["freshness"], "untracked")
        self.assertEqual(result["cost_units"]["gold_or_treasury"], "@gold!")
        self.assertEqual(result["availability"]["status"], "requires_game_evaluation")

    def test_quote_has_no_cost_or_candidate_duration_recalculation(self):
        fixture = EconomyFixture()
        fixture.world.modifier_raw = lambda *args: self.fail("Quote tried to evaluate a price modifier")
        fixture.world.province_modifier_raw = lambda *args: self.fail("Quote tried to evaluate duration modifiers")
        fixture.memory.put(fixture.province + 0x20, "Q", 0)
        with patch.object(fixture.world, "construction_timing", side_effect=AssertionError("Recomputed candidate time")):
            self.assertEqual(fixture.quote()["cost"]["gold_or_treasury"], 123.45678)
        fixture.memory.put(fixture.base + APPLICATION_RVA, "Q", 0)
        self.assertIsNone(fixture.quote()["cost"]["gold_or_treasury"])

    def test_missing_cache_retains_direct_definition_values(self):
        fixture = EconomyFixture()
        fixture.memory.put(fixture.base + APPLICATION_RVA, "Q", 0)
        result = fixture.quote()
        self.assertEqual(result["base_cost"], {"gold": 100, "prestige": 20, "piety": 10})
        self.assertEqual(result["building_type"]["base_construction_days"], 365)
        self.assertTrue(all(value is None for value in result["cost"].values()))
        self.assertIsNone(result["timing"]["construction_days"])
        self.assertEqual(result["quote_cache"]["reason"], "holding_view_unavailable")

    def test_every_gui_link_requires_its_verified_vtable(self):
        for index in range(len(HOLDING_VIEW_CHAIN)):
            with self.subTest(link=index):
                fixture = EconomyFixture()
                fixture.memory.put(fixture.gui_objects[index], "Q", fixture.base + 0x1234)
                self.assertEqual(fixture.quote()["quote_cache"]["reason"], "holding_view_unavailable")

    def test_pending_scene_field_is_independent_of_the_active_scene(self):
        fixture = EconomyFixture()
        application = fixture.gui_objects[0]
        fixture.memory.put(application + 0x78, "Q", 0)
        self.assertEqual(fixture.quote()["quote_cache"]["status"], "cached")
        fixture.memory.put(application + 0x78, "Q", fixture.gui_objects[1])
        fixture.memory.put(application + 0x28, "Q", 0)
        self.assertEqual(fixture.quote()["quote_cache"]["reason"], "holding_view_unavailable")

    def test_quote_rejects_other_province_and_other_payer_contexts(self):
        for offset in (0xd8, 0x1c0):
            with self.subTest(offset=offset):
                fixture = EconomyFixture()
                fixture.memory.put(fixture.view + offset, "Q", 0)
                self.assertEqual(fixture.quote()["quote_cache"]["reason"], "province_context_mismatch")
        fixture = EconomyFixture()
        fixture.memory.put(fixture.title + 0x128, "I", 43)
        self.assertEqual(fixture.quote()["quote_cache"]["reason"], "payer_context_mismatch")
        self.assertEqual(fixture.quote(character=43)["quote_cache"]["status"], "cached")

    def test_native_payer_falls_back_to_parent_holder_only_for_barony(self):
        fixture = EconomyFixture()
        fixture.memory.put(fixture.title + 0x128, "I", 0xffffffff)
        self.assertEqual(fixture.quote(character=43)["quote_cache"]["status"], "cached")
        definition = fixture.memory.pointer(fixture.title + 0x48)
        fixture.memory.put(definition + 0x64, "I", 2)
        self.assertEqual(fixture.quote(character=43)["quote_cache"]["reason"], "payer_context_mismatch")

    def test_quote_matches_building_parent_and_slot(self):
        for offset, format, value in ((0, "Q", 0), (8, "Q", 0), (0x10, "i", 2)):
            with self.subTest(offset=offset):
                fixture = EconomyFixture()
                fixture.memory.put(fixture.item + offset, format, value)
                self.assertEqual(fixture.quote()["quote_cache"]["reason"], "quote_not_cached")
        fixture = EconomyFixture()
        self.assertEqual(fixture.quote(index=None)["quote_cache"]["slot_index"], 0)
        duplicate = fixture.item + 0x1000
        fixture.memory.write(duplicate, fixture.memory.read(fixture.item, 0x388))
        fixture.memory.put(duplicate + 0x10, "i", 2)
        fixture.memory.put(fixture.view + 0xe8, "QII", fixture.item_array, 2, 2)
        fixture.memory.put(fixture.item_array + 8, "Q", duplicate)
        self.assertEqual(fixture.quote(index=None)["quote_cache"]["reason"], "ambiguous_cached_slot")
        self.assertEqual(fixture.quote(index=2)["quote_cache"]["slot_index"], 2)

    def test_zero_native_price_is_distinct_from_uninitialized_zero(self):
        fixture = EconomyFixture()
        self.assertEqual(fixture.quote()["cost"]["piety"], 0)
        fixture.cache_value("piety", 0, initialized=False)
        result = fixture.quote()
        self.assertIsNone(result["cost"]["piety"])
        self.assertEqual(result["quote_cache"]["status"], "partially_cached")
        self.assertEqual(result["cost"]["gold_or_treasury"], 123.45678)
        fixture.memory.put(fixture.item + CACHE_FIELDS["piety"] + 0x88, "I", 0)
        self.assertIsNone(fixture.quote()["cost"]["piety"])

    def test_uninitialized_time_and_all_uninitialized_fields(self):
        fixture = EconomyFixture()
        fixture.cache_value("construction_days", 0, initialized=False)
        self.assertIsNone(fixture.quote()["timing"]["construction_days"])
        for name in CACHE_FIELDS:
            fixture.cache_value(name, 0, initialized=False)
        result = fixture.quote()
        self.assertEqual(result["quote_cache"]["status"], "unavailable")
        self.assertEqual(len(result["quote_cache"]["missing_fields"]), 4)
        fixture.cache_value("construction_days", 0)
        self.assertEqual(fixture.quote()["timing"]["construction_days"], 0)

    def test_changed_native_references_results_and_units_are_rejected(self):
        for target in ("scene", "province", "payer", "item", "result", "unit", "vector"):
            with self.subTest(target=target):
                fixture = EconomyFixture()
                addresses = {"scene": fixture.gui_objects[0] + 0x28, "province": fixture.view + 0xd8,
                             "payer": fixture.title + 0x128, "item": fixture.item,
                             "result": fixture.item + 0x90, "unit": fixture.item + 0x50,
                             "vector": fixture.view + 0xe8}
                original = fixture.memory.read
                changed = False

                def read(address, size, **options):
                    nonlocal changed
                    data = original(address, size, **options)
                    if not changed and address == fixture.item + CACHE_FIELDS["construction_days"] + 0x78:
                        fixture.memory.write(addresses[target], b"\xff")
                        changed = True
                    return data

                fixture.memory.read = read
                result = fixture.quote()
                self.assertEqual(result["quote_cache"]["reason"], "native_cache_changed_during_read")
                self.assertTrue(all(value is None for value in result["cost"].values()))

    def test_invalid_vectors_and_strings_have_bounded_unavailable_results(self):
        for capacity, count in ((0, 1), (4097, 4097)):
            fixture = EconomyFixture()
            fixture.memory.put(fixture.view + 0xf0, "II", capacity, count)
            self.assertEqual(fixture.quote()["quote_cache"]["reason"], "invalid_native_building_vector")
        fixture = EconomyFixture()
        fixture.memory.put(fixture.item + 0x60, "QQ", 8193, 8193)
        self.assertEqual(fixture.quote()["quote_cache"]["reason"], "native_cache_unavailable")

    def test_native_unit_strings_support_external_storage_and_unicode(self):
        fixture = EconomyFixture()
        text = "国库资源 @treasury_icon!"
        encoded = text.encode("utf-8")
        address = fixture.item + 0x4000
        fixture.memory.write(address, encoded)
        fixture.memory.write(fixture.item + 0x50, struct.pack("<Q8xQQ", address, len(encoded), len(encoded)))
        result = fixture.quote(index=None)
        self.assertEqual(result["cost_units"]["gold_or_treasury"], text)
        self.assertEqual(result["slot_index"], 0)

    def test_unreadable_native_cache_keeps_definition_prices_available(self):
        fixture = EconomyFixture()
        original = fixture.memory.read

        def read(address, size, **options):
            if address == fixture.base + APPLICATION_RVA:
                raise OSError("Native GUI memory is unavailable")
            return original(address, size, **options)

        fixture.memory.read = read
        result = fixture.quote()
        self.assertEqual(result["quote_cache"]["reason"], "native_cache_unavailable")
        self.assertEqual(result["base_cost"]["gold"], 100)
        self.assertIsNone(result["cost"]["gold_or_treasury"])

    def test_invalid_inputs_are_rejected_before_native_quote_lookup(self):
        fixture = EconomyFixture()
        for changes in ({"building": True}, {"building": "missing"}, {"character": None}, {"index": True}, {"index": 1 << 32}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                fixture.quote(**changes)

    def test_building_definition_and_stored_payment_are_direct_memory_fields(self):
        fixture = EconomyFixture()
        self.assertEqual(fixture.world.building_type(0)["base_cost"], {"gold": 100, "prestige": 20, "piety": 10})
        fixture.memory.put(fixture.construction, "Q", fixture.building)
        fixture.memory.put(fixture.construction + 0x20, "10q", 11111111, 2222222, 3333333, 0, 0, 0, 4444444, 0, 0, 0)
        result = fixture.world.holding_buildings(2482)["construction"]
        self.assertEqual(result["paid_cost"], {"gold": 111.11111, "prestige": 22.22222, "piety": 33.33333, "treasury": 44.44444})
        self.assertEqual(result["paid_cost_basis"], "native_construction_record")
        self.assertEqual(result["base_days_remaining"], 90.25)
        self.assertEqual(result["character_id"], 42)

    def test_changed_construction_payment_is_rejected(self):
        fixture = EconomyFixture()
        fixture.memory.put(fixture.construction, "Q", fixture.building)
        original = fixture.memory.read

        def read(address, size, **options):
            data = original(address, size, **options)
            if address == fixture.construction and size == 0x78:
                fixture.memory.put(fixture.construction + 0x20, "q", 100000)
            return data

        fixture.memory.read = read
        with self.assertRaisesRegex(ValueError, "Construction record changed"):
            fixture.world.holding_buildings(2482)

    def test_public_quote_uses_controlled_character_and_field_budgets(self):
        fixture = EconomyFixture()

        class World:
            def __init__(self, pid=None):
                self.memory = fixture.memory

            def query(self, request):
                return {"data": fixture.world._query(request), "runtime": {"date": DATE, "tick": 1, "world": 100}}

            def close(self):
                pass

        with patch("live_objects.MemoryWorld", World), DirectWorldReader(character_id=42) as reader:
            result = reader.handle({"kind": "construction_quote", "id": 2482, "building": "pastures_01", "index": 0,
                                    "fields": ["cost", "timing.construction_days"], "max_chars": 500})
            self.assertEqual(result["data"]["cost"]["gold_or_treasury"], 123.45678)
            self.assertEqual(result["data"]["timing.construction_days"], 273.12547)
            self.assertNotIn("tick", str(result))
            fixture.install_track()
            upgrade = reader.handle({"kind": "construction_quote", "id": 2482, "building": "pastures_01", "index": 0,
                                     "fields": ["cost", "quote_cache.cache_kind"], "max_chars": 500})
            self.assertEqual(upgrade["data"]["cost"]["gold_or_treasury"], 123.45678)
            self.assertEqual(upgrade["data"]["quote_cache.cache_kind"], "upgrade_track")


if __name__ == "__main__":
    unittest.main()
