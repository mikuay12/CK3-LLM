"""Regress direct-memory query semantics and process-boundary handling."""

from copy import deepcopy
import ctypes
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from ck3_llm.engine import process_memory
from ck3_llm.world.live_objects import DirectObservationQueue, DirectWorldReader, GameLabels, _fit_response
from ck3_llm.world.memory_world import MemoryWorld, integer, page
from ck3_llm.engine.process_memory import ProcessMemory


class FakeWorld:
    def __init__(self, pid=None):
        self.memory = type("Memory", (), {"executable": Path("C:/missing/binaries/ck3.exe"),
                                          "is_running": lambda self: True})()
        self.closed = False
        self.requests = []
        self.data = {"id": 42, "identity": {"name": "Test", "birth": 365 * 24 * 5000},
                     "resources": {"gold": 12.5}, "skills": {"diplomacy": 9}}

    def query(self, request):
        self.requests.append(deepcopy(request))
        data = deepcopy(self.data)
        if request["kind"] == "observe":
            data = {"character": data, "events": []}
        if request["kind"] == "vassals":
            data = page([data, {**data, "id": 43}], request)
        return {"data": data, "runtime": {"date": 365 * 24 * 5030, "tick": 100, "world": 1234}}

    def close(self):
        self.closed = True


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch("ck3_llm.world.live_objects.MemoryWorld", FakeWorld)
        self.patch.start()
        self.reader = DirectWorldReader(character_id=42)

    def tearDown(self):
        self.reader.close()
        self.patch.stop()

    def test_query_hides_execution_metadata(self):
        result = self.reader.handle({"command": "query", "kind": "character", "id": 42})
        self.assertEqual(result["data"]["resources"]["gold"], 12.5)
        self.assertEqual(self.reader.runtime["tick"], 100)
        self.assertNotIn("tick", json.dumps(result))
        self.assertNotIn("world", json.dumps(result))

    def test_declaration_preview_uses_controlled_actor_and_native_result(self):
        evaluator = object()
        self.reader.memory.get_shadow_evaluator = lambda: evaluator
        with patch("ck3_llm.gameplay.gameplay_preview.declaration_preview", return_value={"can_send": False, "reason": "Fame too low"}) as preview:
            result = self.reader.handle({"command": "query", "kind": "declaration_preview", "target": 99,
                                         "casus_belli": "claim_cb", "titles": [607], "claimant": 42})
        preview.assert_called_once_with(evaluator, 42, 99, "claim_cb", [607], 42)
        self.assertFalse(result["data"]["can_send"])
        self.assertEqual(result["data"]["reason"], "Fame too low")

    def test_field_selection(self):
        result = self.reader.handle({"kind": "character", "id": 42, "fields": ["resources.gold"]})
        self.assertEqual(result["data"], {"resources.gold": 12.5})

    def test_death_date_is_formatted_for_model(self):
        self.reader.memory.data["identity"]["death_date"] = 365 * 24 * 5030
        result = self.reader.handle({"kind": "character", "id": 42, "fields": ["identity.death_date"]})
        self.assertEqual(result["data"], {"identity.death_date": "30.1.1"})

    def test_global_scan_exposes_game_date_window(self):
        with patch.object(self.reader.memory, "query", return_value={
                "data": page([], {}), "runtime": {"date": 365 * 24 * 5031,
                "date_started": 365 * 24 * 5030, "spans_updates": True, "tick": 100}}):
            result = self.reader.handle({"kind": "realms"})
        self.assertEqual(result["observed_from"], "30.1.1")
        self.assertEqual(result["date"], "31.1.1")
        self.assertNotIn("tick", json.dumps(result))

    def test_missing_field_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.reader.handle({"kind": "character", "id": 42, "fields": ["resources.unverified"]})

    def test_idempotent_request(self):
        request = {"kind": "character", "id": 42, "request_id": "one"}
        first = self.reader.handle(request)
        first["data"]["id"] = 999
        second = self.reader.handle(request)
        self.assertEqual(second["data"]["id"], 42)
        self.assertEqual(len(self.reader.memory.requests), 1)
        with self.assertRaisesRegex(ValueError, "different request"):
            self.reader.handle({**request, "id": 43})

    def test_fresh_request_rereads(self):
        request = {"kind": "character", "id": 42}
        self.reader.handle(request)
        self.reader.handle(request)
        self.assertEqual(len(self.reader.memory.requests), 2)

    def test_delta_acknowledges_delivered_fields(self):
        first = self.reader.handle({"command": "observe"})
        self.assertEqual(first["mode"], "full")
        self.assertEqual(self.reader.handle({"command": "observe"})["data"], {})
        self.reader.memory.data["resources"]["gold"] = 15
        changed = self.reader.handle({"command": "observe"})
        self.assertEqual(changed["data"], {"resources": {"gold": 15}})

    def test_selected_observation(self):
        result = self.reader.handle({"command": "observe", "fields": ["resources.gold", "events"]})
        self.assertEqual(result["data"], {"resources.gold": 12.5, "events": []})

    def test_invalid_requests(self):
        for request in ([], {"command": "refresh_world"}, {"kind": "unknown"},
                        {"kind": "character", "fields": []}, {"kind": "character", "fields": [3]},
                        {"kind": "characters", "alive": 1}, {"kind": "realms", "limit": 0},
                        {"kind": "titles", "holder": True}, {"kind": "titles", "tier": "invalid"},
                        {"kind": "titles", "search": []}, {"kind": "character", "max_chars": True}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.reader.handle(request)

    def test_schema_explains_game_fields(self):
        result = self.reader.handle({"command": "schema", "kind": "character"})
        self.assertIn("封臣", result["data"]["domain"])
        self.assertNotIn("tick", json.dumps(result))

    def test_neighbors_default_to_controlled_character(self):
        self.reader.memory.data = page([], {})
        self.reader.handle({"kind": "neighbors"})
        self.assertEqual(self.reader.memory.requests[-1]["id"], 42)
        with self.assertRaises(ValueError):
            self.reader.handle({"kind": "neighbors", "include_straits": 1})

    def test_close_is_idempotent(self):
        memory = self.reader.memory
        self.reader.close()
        self.reader.close()
        self.assertTrue(memory.closed)
        with self.assertRaisesRegex(ValueError, "closed"):
            self.reader.handle({"kind": "character", "id": 42})

    def test_small_status_budget(self):
        with self.assertRaises(ValueError):
            self.reader.handle({"command": "observation_status", "max_chars": 1})

    def test_background_queue_finishes_and_closes(self):
        queue = DirectObservationQueue(123)
        queue.submit({"command": "observe", "request_id": "background"}, 42)
        result = queue.finish()
        self.assertEqual(result[0]["request_id"], "background")
        self.assertEqual(result[0]["result"]["character_id"], 42)
        self.assertIsNone(queue.reader.memory)


class BudgetTests(unittest.TestCase):
    def test_page_budget_retains_continuation(self):
        rows = [{"id": number, "name": "x" * 80} for number in range(10)]
        result = _fit_response({"kind": "vassals", "data": page(rows, {})}, 400)
        self.assertGreater(len(result["data"]["items"]), 0)
        self.assertLess(len(result["data"]["items"]), 10)
        self.assertEqual(result["data"]["next_offset"], len(result["data"]["items"]))
        self.assertLessEqual(len(json.dumps(result, separators=(",", ":"))), 400)

    def test_empty_page(self):
        result = _fit_response({"kind": "events", "data": page([], {})}, 200)
        self.assertEqual(result["data"]["items"], [])
        self.assertIsNone(result["data"]["next_offset"])

    def test_one_item_too_large(self):
        with self.assertRaisesRegex(ValueError, "fewer fields"):
            _fit_response({"kind": "events", "data": page([{"value": "x" * 1000}], {})}, 200)

    def test_omissions_are_explicit(self):
        result = _fit_response({"kind": "character", "data": {"id": 42, "detail": "x" * 1000}}, 150)
        self.assertEqual(result["data"], {"id": 42})
        self.assertEqual(result["omitted_fields"], ["detail"])

    def test_integer_rejects_boolean(self):
        with self.assertRaises(ValueError):
            integer(True, "reference")


class MemoryTests(unittest.TestCase):
    def test_global_enumeration_can_span_updates_but_point_queries_retry(self):
        world = MemoryWorld.__new__(MemoryWorld)
        class Memory:
            pid = 123
            bytes_read = read_calls = 0

            def reset(self):
                pass
        world.memory = Memory()
        world.tables = {}
        world.references = set()
        world.primary_cache = {}
        world.summary_cache = {}
        world.ruler_cache = {}
        world.players = lambda: []
        world._query = lambda request: page([], request)
        ticks = iter(range(100))
        world.clock = lambda: {"world": 1000, "date": next(ticks), "tick": 1}
        result = world.query({"kind": "realms"})
        self.assertTrue(result["runtime"]["spans_updates"])
        self.assertEqual(result["runtime"]["date_started"], 0)
        self.assertEqual(result["runtime"]["date"], 2)
        with self.assertRaisesRegex(ValueError, "kept changing"):
            world.query({"kind": "character", "id": 42})
        world.clock = lambda: {"world": next(ticks), "date": 1, "tick": 1}
        with self.assertRaisesRegex(ValueError, "kept changing"):
            world.query({"kind": "realms"})

    def test_external_reads_reuse_only_request_pages(self):
        source = bytes(range(256)) * 2
        class API:
            calls = 0

            def ReadProcessMemory(self, handle, address, buffer, size, read):
                self.calls += 1
                ctypes.memmove(buffer, source[address - 0x1000:address - 0x1000 + size], size)
                ctypes.cast(read, ctypes.POINTER(ctypes.c_size_t))[0] = size
                return True
        memory = ProcessMemory.__new__(ProcessMemory)
        memory.api = API()
        memory.handle = 1
        memory.cache = {}
        memory.bytes_read = memory.read_calls = 0
        self.assertEqual(memory.u32(0x1004), 0x07060504)
        self.assertEqual(memory.u32(0x1008), 0x0b0a0908)
        self.assertEqual(memory.api.calls, 1)
        memory.reset()
        memory.u32(0x1004)
        self.assertEqual(memory.api.calls, 2)

    def test_recycled_reference_is_rejected(self):
        class Memory:
            def pointer(self, address):
                return {0x5c7c4f0: 1000, 1032: 2000, 2024: 3000, 3000: 0x47748a0}[address]

            def u32(self, address):
                return {1044: 100, 3024: 0x1000001}[address]
        world = MemoryWorld.__new__(MemoryWorld)
        world.base = 0
        world.memory = Memory()
        world.tables = {}
        world.references = set()
        with self.assertRaisesRegex(ValueError, "expired"):
            world.lookup("character", 1)

    def test_budgeted_cli_uses_read_handle(self):
        source = Path(process_memory.__file__).read_text(encoding="utf-8")
        self.assertIn("OpenProcess(0x410", source)
        for name in ("WriteProcessMemory", "CreateRemoteThread", "VirtualAllocEx"):
            self.assertNotIn(name, source)

    def test_army_uses_deployed_regiments(self):
        class Memory:
            def u32(self, address):
                return {0x1120: 44, 0x1124: 55}[address]

            def fixed(self, address):
                return 0

            def u8(self, address):
                return 0
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = Memory()
        world.lookup = lambda kind, value: 0x1000
        world.vector = lambda address: [101]
        world.army_regiment = lambda value: {"id": value, "current": 120, "maximum": 200}
        world.regiment = lambda value: self.fail("Army IDs reference deployed regiments")
        world.unit = lambda value: {"id": value}
        result = world.army(7)
        self.assertIsNone(result["current"])
        self.assertEqual(result["strength_status"], "native_result_not_located")
        self.assertEqual(result["regiments"][0]["current"], 120)
        self.assertEqual(result["regiments"][0]["id"], 101)

    def test_client_direct_source_uses_background_queue(self):
        from ck3_llm.client import EventWriter
        with tempfile.TemporaryDirectory() as directory, patch("ck3_llm.world.live_objects.DirectObservationQueue") as queue:
            queue.return_value.finish.return_value = []
            with EventWriter(Path(directory) / "client", live_memory_pid=123) as writer:
                self.assertIs(writer.live_observations, queue.return_value)
                self.assertIsNone(writer._world.database_path)
            queue.assert_called_once_with(123)

    def test_capital_reference_is_a_title(self):
        class Memory:
            def u32(self, address):
                return 9503 if address == 0x10e4 else 0xffffffff
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = Memory()
        world.title_pointer = lambda value: 0x1000
        world.title_summary = lambda value: {"id": 9455, "tier": "kingdom", "holder_id": None}
        world.character_name = lambda value: None
        world.vector = lambda address, width=4: []
        result = world.title("k_zanj")
        self.assertEqual(result["capital_title_id"], 9503)
        self.assertNotIn("capital_province_id", result)

    def test_dead_character_has_no_ai_controller(self):
        class Memory:
            def pointer(self, address):
                return 0
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = Memory()
        world.lookup = lambda kind, value: 0x1000
        world.controller = lambda value: self.fail("Dead characters have no active controller")
        self.assertIsNone(world.character(42, {"controller"})["controller"])

    def test_birth_and_death_dates_and_cached_age_are_native(self):
        year = 365 * 24
        class Memory:
            def pointer(self, address):
                return 0x2000 if address == 0x11d0 else 0

            def u32(self, address):
                return {0x1060: 5000 * year, 0x2004: 5030 * year}.get(address, 0xffffffff)

            def i16(self, address):
                return 30 if address == 0x1068 else 0

            def string(self, address):
                return "Deceased"

            def u8(self, address):
                return 0
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = Memory()
        world.lookup = lambda kind, value: 0x1000
        world.culture = world.character_faith = lambda value: None
        world.state = {"date": 5040 * year}
        result = world.character(42, {"identity"})["identity"]
        self.assertEqual(result["age"], 30)
        self.assertEqual(result["age_status"], "native_memory")
        self.assertEqual(result["death_date"], 5030 * year)
        world.state["date"] += 10 * year
        self.assertEqual(world.character(42, {"identity"})["identity"]["age"], 30)

    def test_landed_units_are_four_byte_references(self):
        class Memory:
            def pointer(self, address):
                return 0x2000 if address == 0x11c0 else 0

            def i32(self, address):
                return 0

            def fixed(self, address):
                return 0
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = Memory()
        world.lookup = lambda kind, value: 0x1000
        calls = []
        def vector(address, width=4):
            calls.append((address, width))
            return [0x3d0001d0] if address == 0x2278 else []
        world.vector = vector
        result = world.character(42, {"military"})
        self.assertEqual(result["military"]["unit_ids"], [0x3d0001d0])
        self.assertIn((0x2278, 4), calls)

    def test_path_uses_graph_node_province_ids(self):
        class Memory:
            def pointer(self, address):
                return 0

            def u32(self, address):
                return {0x3000: 56, 0x1174: 42, 0x1178: 7, 0x1180: 365 * 24 * 5935}.get(address, 0)

            def fixed(self, address):
                return 0
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = Memory()
        world.lookup = lambda kind, value: 0x1000
        world.vector = lambda address, width=4: [0x3000]
        self.assertEqual(world.unit(8)["path"], [56])

    def test_active_combat_uses_serializable_subobject_offsets(self):
        class Memory:
            def pointer(self, address):
                return {0x16b8: 0x3000, 0x1020: 0x4756988, 0x1368: 0x4756988}.get(address, 0)

            def u32(self, address):
                return {0x3010: 2491, 0x16b0: 1, 0x16b4: 44, 0x16c4: 1113,
                        0x1090: 42, 0x1094: 43, 0x13d8: 44, 0x13dc: 45,
                        0x5124: 8, 0x6124: 9}.get(address, 0xffffffff)

            def fixed(self, address):
                return {0x16c8: -20, 0x10c8: 1534, 0x1410: 702}.get(address, 100)

            def u8(self, address):
                return 0
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = Memory()
        world.base = 0
        world.lookup = lambda kind, value: {("combat", 7): 0x1000, ("army", 10): 0x5000,
                                            ("army", 11): 0x6000}[kind, value]
        world.vector = lambda address: {0x1030: [10], 0x1378: [11]}[address]
        world.unit = lambda value: {"owner_id": {8: 42, 9: 44}[value]}
        result = world.combat(7)
        self.assertEqual(result["province_id"], 2491)
        self.assertEqual(result["phase"], "main")
        self.assertEqual(result["phase_days"], 44)
        self.assertEqual(result["attacker_advantage"], -20)
        self.assertEqual(result["attacker"]["owner_ids"], [42])
        self.assertEqual(result["attacker"]["commander_id"], 43)
        self.assertEqual(result["attacker"]["initial_soldiers"], 1534)
        self.assertEqual(result["defender"]["initial_soldiers"], 702)

    def test_active_combat_filter_accepts_leader_and_army_owner(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.rows = lambda kind: [(7, 1000), (8, 2000)]
        world.combat = lambda value: {"id": value, "attacker": {"leader_id": 42, "owner_ids": [43]},
                                      "defender": {"leader_id": 44, "owner_ids": [45]}}
        for character in (42, 43, 44, 45):
            self.assertEqual(world._query({"kind": "combats", "character": character})["count"], 2)
        self.assertEqual(world._query({"kind": "combats", "character": 46})["count"], 0)

    def test_faith_and_rite_have_distinct_query_namespaces(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.faith = lambda value: {"kind": "faith", "id": value}
        world.rite = lambda value: {"kind": "rite", "id": value}
        self.assertEqual(world._query({"kind": "faith", "id": 50}), {"kind": "faith", "id": 50})
        self.assertEqual(world._query({"kind": "rite", "id": 16}), {"kind": "rite", "id": 16})

    def test_title_key_miss_refreshes_metadata_index(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.title_index = (None, [], {"old_title": 7})
        def refresh():
            if world.title_index is None:
                world.title_index = (None, [], {"new_title": 8})
            return world.title_index[1]
        world.title_rows = refresh
        world.lookup = lambda kind, value: 0x1000 if value == 8 else self.fail("Expected current title ID")
        world.memory = type("Memory", (), {"pointer": lambda self, address: 0x2000})()
        world.key = lambda address: "new_title"
        self.assertEqual(world.title_pointer("new_title"), 0x1000)

    def test_dynamic_title_tier_comes_from_native_definition(self):
        class Memory:
            def pointer(self, address):
                return 0x2000

            def u32(self, address):
                return {0x1010: 20186, 0x2064: 5, 0x1128: 42}.get(address, 0xffffffff)
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = Memory()
        world.summary_cache = {}
        world.key = lambda address: "x_script_4045"
        self.assertEqual(world.title_summary(0x1000)["tier"], "empire")

    def test_realm_titles_follow_nested_current_contracts(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.top_ruler = lambda value: 42
        world.lookup = lambda kind, value: value * 0x1000
        world.title_summary = lambda pointer: {"tier": "county" if pointer == 0x7000 else "barony"}
        world.memory = type("Memory", (), {"pointer": lambda self, address: address - 0x1c0 + 0x500})()
        vectors = {
            0x2a6e0: [7, 8, 0x1000001], 0x2a718: [101, 102],
            0x2b6e0: [9], 0x2b718: [103, 104],
            0x2c6e0: [10, 11], 0x2c718: [105],
            0x7110: [12, 13],
        }
        world.vector = lambda address: vectors[address]
        contracts = {
            101: {"liege_id": 42, "vassal_id": 43},
            102: {"liege_id": 99, "vassal_id": 42},
            103: {"liege_id": 43, "vassal_id": 44},
            104: {"liege_id": 43, "vassal_id": 44},
            105: {"liege_id": 44, "vassal_id": 42},
        }
        world.contract = contracts.__getitem__
        world.title_rows = lambda: self.fail("A specific realm query must stay inside its domains")
        self.assertEqual(world.realm_title_rows(42), [(value, None) for value in [0x1000001, 7, 8, 9, 10, 11, 12, 13]])
        self.assertEqual(world.realm_title_rows(43), [])

    def test_global_realms_skip_unheld_and_subordinate_titles(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.title_rows = lambda: [(1, "k_test"), (2, "k_unheld"), (3, "k_vassal")]
        world.database = lambda kind: (100, 4, 16, 0)
        class Memory:
            def pointer(self, address):
                return {124: 1000, 140: 2000, 156: 3000}[address]

            def read(self, address, size, *, fresh=False):
                liege, holder = {1232: (0xffffffff, 42), 2232: (0xffffffff, 0xffffffff), 3232: (1, 43)}[address]
                result = bytearray(size)
                struct.pack_into("<I", result, 0, liege)
                struct.pack_into("<I", result, 0x40, holder)
                return bytes(result)
        world.memory = Memory()
        def lookup(kind, value):
            self.assertIn((kind, value), (("title", 1), ("character", 42)))
            return 1000 if kind == "title" else 4000
        world.lookup = lookup
        title = {"id": 1, "key": "k_test", "tier": "kingdom", "holder_id": 42,
                 "de_facto_liege_id": None, "de_jure_liege_id": None}
        world.title_summary = lambda pointer: title
        world.primary_title = lambda pointer: title
        world.liege = lambda pointer: None
        world.character_name = lambda value: {"id": value, "name": "Ruler"}
        world.controller = lambda value: {"type": "game_ai"}
        result = world._query({"kind": "realms"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["holder_id"], 42)


class NeighborTests(unittest.TestCase):
    def test_adjacency_records_and_optional_straits(self):
        records = b"".join(struct.pack("<12I", crossing, neighbor, through, *([0] * 9))
                           for crossing, neighbor, through in ((0, 43, 0), (1, 44, 99), (2, 45, 0), (3, 46, 98)))
        class Memory:
            def pointer(self, address):
                return {0x1008: 0x2000, 0x2050: 0x3000}[address]

            def u32(self, address):
                return {0x2024: 42, 0x2058: 4, 0x205c: 4}[address]

            def read(self, address, size):
                return records[:size]
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = Memory()
        world.province_pointer = lambda value: 0x1000
        self.assertEqual([item["province_id"] for item in world.adjacencies(42)], [43, 45, 46])
        crossing = world.adjacencies(42, True)[1]
        self.assertEqual(crossing, {"province_id": 44, "crossing": "strait", "through_province_id": 99})

    def test_unbuilt_barony_inherits_county_realm(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.lookup = lambda kind, value: value
        world.title_summary = lambda value: {"id": value, "holder_id": 42, "de_facto_liege_id": None}
        world.realm_summary = lambda value: {"ruler": {"id": value}}
        title = {"id": 1, "holder_id": None, "de_facto_liege_id": 2}
        self.assertEqual(world.title_realm(title), {"ruler": {"id": 42}})
        world.title_summary = lambda value: title
        with self.assertRaisesRegex(ValueError, "Cyclic"):
            world.title_realm(title)

    def test_borders_exclude_internal_land_and_water_and_group_vassals(self):
        world = MemoryWorld.__new__(MemoryWorld)
        titles = {
            1: {"id": 1, "tier": "barony", "holder_id": 42, "de_facto_liege_id": None},
            2: {"id": 2, "tier": "barony", "holder_id": 42, "de_facto_liege_id": None},
            3: {"id": 3, "tier": "barony", "holder_id": 8, "de_facto_liege_id": None},
            4: {"id": 4, "tier": "barony", "holder_id": None, "de_facto_liege_id": 2},
        }
        world.lookup = lambda kind, value: value
        world.title_summary = titles.__getitem__
        world.realm_summary = lambda value: {"ruler": {"id": 42 if value == 42 else 7}}
        world.realm_title_rows = lambda value: [(1, None), (2, None)]
        world.title_province = lambda value: {1: 10, 2: 11}[value]
        world.province_pointer = lambda value: value * 0x1000
        world.memory = type("Memory", (), {"u32": lambda self, address:
            {0xc738: 3, 0xd738: 0xffffffff, 0xe738: 4}[address]})()
        world.adjacencies = lambda value, include_straits: [
            {"province_id": neighbor, "crossing": "land", "through_province_id": None}
            for neighbor in ([11, 12, 13, 14] if value == 10 else [12])]
        result = world._query({"kind": "neighbors", "id": 42})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["ruler"]["id"], 7)
        self.assertEqual(result["items"][0]["border_count"], 2)
        world.top_ruler = lambda value: 7
        result = world._query({"kind": "borders", "id": 42, "other": 8, "limit": 1})
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["next_offset"], 1)


class LabelTests(unittest.TestCase):
    def test_static_names_and_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "binaries" / "ck3.exe"
            locale = Path(directory) / "game" / "localization" / "simp_chinese"
            locale.mkdir(parents=True)
            (locale / "titles_l_simp_chinese.yml").write_text('l_simp_chinese:\n k_zanj: "赞吉"\n alias: "$k_zanj$"\n', encoding="utf-8")
            labels = GameLabels(executable)
            self.assertEqual(labels.apply({"key": "k_zanj"})["name"], "赞吉")
            self.assertEqual(labels.name("alias"), "赞吉")
            self.assertEqual(labels.apply({"key": "k_zanj", "name": "自定义"})["name"], "自定义")


if __name__ == "__main__":
    unittest.main()
