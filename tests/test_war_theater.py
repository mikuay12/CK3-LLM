"""Offline tests for terrain, native supply limit, practical route, and war_theater."""

import json
from pathlib import Path
import struct
import unittest

from ck3_llm.world.army_navigation import InstalledMap
from ck3_llm.world.live_objects import QUERY_FIELDS, _request
from ck3_llm.world.memory_world import MemoryWorld
from ck3_llm.engine.shadow_evaluator import BufferMemory, ShadowEvaluator
from ck3_llm.world.war_theater import war_theater


DEFAULT_EXE = Path(r"C:\Games\Crusader Kings III\binaries\ck3.exe")


class FakeWarWorld:
    """Minimal in-memory world stub for testing war_theater end-to-end."""

    def __init__(self):
        # 1: Anjou (friendly cap, plains, fort 2)
        # 2: Maine (friendly frontline, hills, fort 1, besieged by enemy)
        # 3: Chartres (enemy war_target, frontline, farmlands, fort 2)
        # 4: Paris (enemy cap, farmlands, fort 4)
        # 5: Orleans (enemy interior, forest, fort 1)
        self._provs = {
            1: {"id": 1, "title": {"id": 101, "key": "b_angers", "name": "安茹", "holder_id": 10, "tier": "barony", "province_id": 1},
                "fort_level": 2, "terrain": "plains", "supply_limit": 3500, "occupying_character_id": None, "pos": {"x": 100.0, "z": 100.0}},
            2: {"id": 2, "title": {"id": 102, "key": "b_le_mans", "name": "曼恩", "holder_id": 10, "tier": "barony", "province_id": 2},
                "fort_level": 1, "terrain": "hills", "supply_limit": 2800, "occupying_character_id": None, "pos": {"x": 115.0, "z": 110.0}},
            3: {"id": 3, "title": {"id": 103, "key": "b_chartres", "name": "沙特尔", "holder_id": 20, "tier": "barony", "province_id": 3},
                "fort_level": 2, "terrain": "farmlands", "supply_limit": 4200, "occupying_character_id": None, "pos": {"x": 130.0, "z": 120.0}},
            4: {"id": 4, "title": {"id": 104, "key": "b_paris", "name": "巴黎", "holder_id": 20, "tier": "barony", "province_id": 4},
                "fort_level": 4, "terrain": "farmlands", "supply_limit": 5000, "occupying_character_id": None, "pos": {"x": 150.0, "z": 135.0}},
            5: {"id": 5, "title": {"id": 105, "key": "b_orleans", "name": "奥尔良", "holder_id": 20, "tier": "barony", "province_id": 5},
                "fort_level": 1, "terrain": "forest", "supply_limit": 3000, "occupying_character_id": None, "pos": {"x": 135.0, "z": 95.0}},
        }
        self._edges = {
            1: [{"province_id": 2, "crossing": "normal", "movement_cost": 10.0, "through_province_id": None}],
            2: [
                {"province_id": 1, "crossing": "normal", "movement_cost": 10.0, "through_province_id": None},
                {"province_id": 3, "crossing": "river", "movement_cost": 12.0, "through_province_id": None},
            ],
            3: [
                {"province_id": 2, "crossing": "river", "movement_cost": 12.0, "through_province_id": None},
                {"province_id": 4, "crossing": "normal", "movement_cost": 10.0, "through_province_id": None},
                {"province_id": 5, "crossing": "normal", "movement_cost": 11.0, "through_province_id": None},
            ],
            4: [{"province_id": 3, "crossing": "normal", "movement_cost": 10.0, "through_province_id": None}],
            5: [{"province_id": 3, "crossing": "normal", "movement_cost": 11.0, "through_province_id": None}],
        }
        self._titles = {
            101: {"id": 101, "key": "b_angers", "name": "安茹", "holder_id": 10, "tier": "county", "province_id": 1, "de_jure_vassal_ids": []},
            102: {"id": 102, "key": "b_le_mans", "name": "曼恩", "holder_id": 10, "tier": "county", "province_id": 2, "de_jure_vassal_ids": []},
            103: {"id": 103, "key": "b_chartres", "name": "沙特尔", "holder_id": 20, "tier": "county", "province_id": 3, "de_jure_vassal_ids": []},
            104: {"id": 104, "key": "b_paris", "name": "巴黎", "holder_id": 20, "tier": "county", "province_id": 4, "de_jure_vassal_ids": []},
            105: {"id": 105, "key": "b_orleans", "name": "奥尔良", "holder_id": 20, "tier": "county", "province_id": 5, "de_jure_vassal_ids": []},
            200: {"id": 200, "key": "d_chartres", "name": "沙特尔公国", "holder_id": 20, "tier": "duchy", "province_id": None, "de_jure_vassal_ids": [103]},
        }

    def war(self, war_id):
        return {
            "id": war_id,
            "casus_belli": "county_conquest_cb",
            "target_title_ids": [200],
            "resolving": False,
            "attacker": {
                "participants": [{"character": {"id": 10, "name": "安茹伯爵"}, "joined": 0, "casualties": 0}],
                "ticking_war_score": 0.0,
                "controls_all_targets": False,
            },
            "defender": {
                "participants": [{"character": {"id": 20, "name": "法兰西国王"}, "joined": 0, "casualties": 0}],
                "ticking_war_score": 0.0,
                "controls_all_targets": True,
            },
        }

    def character(self, char_id, groups=None):
        if char_id == 10:
            return {"id": 10, "domain": {"capital_title_id": 101}, "military": {"unit_ids": [1001]}}
        return {"id": 20, "domain": {"capital_title_id": 104}, "military": {"unit_ids": [2001]}}

    def unit_movement(self, unit_id):
        if unit_id == 1001:
            return {
                "id": 1001, "army_id": 3001, "owner_id": 10, "province_id": 1,
                "cached_movement_speed": 2.0,
                "movement": {"destination_province_id": 2, "estimated_next_arrival_days": 5,
                             "movement_locked": False, "direction": {"cardinal": "NE"}},
            }
        return {
            "id": 2001, "army_id": 3002, "owner_id": 20, "province_id": 4,
            "cached_movement_speed": 2.0,
            "movement": {"destination_province_id": 3, "estimated_next_arrival_days": 4,
                         "movement_locked": True, "direction": {"cardinal": "SW"}},
        }

    def army(self, army_id):
        return {"id": army_id, "current": 4500 if army_id == 3001 else 6000, "supply": 95.0}

    def province(self, pid, character_id=None):
        return dict(self._provs[pid])

    def title(self, tid):
        return dict(self._titles[tid])

    def title_summary(self, ptr):
        return dict(self._titles[ptr])

    def top_ruler(self, char_id):
        return char_id

    def rows(self, kind):
        if kind == "title":
            return [(tid, tid) for tid in (101, 102, 103, 104, 105)]
        if kind == "siege":
            return [(501, 501)]
        if kind == "combat":
            return []
        return []

    def siege(self, sid):
        return {
            "id": sid, "province_id": 2,
            "attacker": {"id": 20}, "defender": {"id": 10},
            "progress": {"percent": 40.0},
            "projection": {"estimated_days_remaining": 30},
        }

    def adjacencies(self, pid, include_straits=True):
        return list(self._edges[pid])

    def map_data(self):
        provs = self._provs
        class _Map:
            def province(self, pid):
                p = provs[pid]
                return {"province_id": pid, "map_name": p["title"]["name"], "terrain": p["terrain"],
                        "land_passable": True, "position": p["pos"]}
        return _Map()

    def navigation_node(self, pid):
        p = self._provs[pid]
        return {"province_id": pid, "land_passable": True, "position": p["pos"], "edges": list(self._edges[pid])}


class WarTheaterAndTerrainTests(unittest.TestCase):
    def test_installed_map_loads_province_terrain(self):
        if not DEFAULT_EXE.is_file():
            self.skipTest("Installed CK3 executable is unavailable")
        imap = InstalledMap(DEFAULT_EXE)
        self.assertEqual(imap.province(1)["terrain"], "mountains")
        self.assertEqual(imap.province(3)["terrain"], "plains")
        self.assertIsNotNone(imap.province(1)["map_name"])

    def test_shadow_evaluator_computes_native_province_supply_limit(self):
        if not DEFAULT_EXE.is_file():
            self.skipTest("Installed CK3 executable is unavailable")
        from tests.test_siege_progress import SiegeFixture

        base = 0x140000000
        mem = BufferMemory(base)
        fx = SiegeFixture(base, 0x2000_0000)
        fx.memory.put(base + 0x5C7DCE4, "i", 2000)
        fx.memory.put(base + 0x5C7DCE8, "i", 150)
        fx.memory.put(base + 0x5C7DCE0, "i", 1000)
        end_node = 0x2000_F000
        buckets = 0x2000_F100
        fx.memory.put(end_node, "QQ", end_node, end_node)
        fx.memory.put(buckets, "QQ", end_node, end_node)
        fx.memory.put(base + 0x5DD63B0, "Q", end_node)
        fx.memory.put(base + 0x5DD63B0 + 0x18, "Q", buckets)
        terrain = 0x2000_E000
        tmod = 0x2000_E400
        fx.memory.put(terrain + 0xB8, "Q", tmod)
        fx.memory.put(fx.province + 0x20, "Q", terrain)
        for char_ptr in (fx.character, fx.beneficiary, 0x2000_A400):
            fx.memory.put(char_ptr + 0x1C, "I", 0x43686172)
            fx.memory.put(char_ptr + 0x1C0, "Q", fx.landed)
        fx.memory.put(fx.landed + 0x28, "Q", fx.character)
        fx.memory.put(fx.landed + 0x1C0, "Q", fx.landed)
        for addr, raw in fx.memory.writes:
            mem.add_region(addr, raw)

        evaluator = ShadowEvaluator(DEFAULT_EXE, memory_source=mem)
        res = evaluator.evaluate_province_supply_limit(fx.province, fx.character)
        self.assertEqual(res["supply_limit"], 2000)
        self.assertEqual(res["source"], "native_shadow_unicorn")

    def test_route_query_computes_hops_crossings_direction_and_eta_when_enabled(self):
        fake = FakeWarWorld()
        world = MemoryWorld.__new__(MemoryWorld)
        world.shadow_evaluator = None
        world.navigation_node = fake.navigation_node
        world.map_data = fake.map_data

        res = world.map_route({"origin": 1, "target": 4, "compute": True, "speed": 2.0})
        self.assertEqual(res["status"], "graph_shortest_path")
        self.assertTrue(res["reachable"])
        self.assertEqual(res["hops"], 3)
        self.assertEqual(res["route"], [1, 2, 3, 4])
        self.assertAlmostEqual(res["total_movement_cost"], 32.0, places=2)
        self.assertEqual(res["crossings"], {"river": 1})
        self.assertEqual(res["direction"]["cardinal"], "NE")
        self.assertEqual(res["estimated_days"], 16)

    def test_war_theater_returns_compact_strategic_overview(self):
        world = FakeWarWorld()
        brief = war_theater(world, 77, character_id=10, max_targets=5, max_armies=5)

        self.assertEqual(brief["side"], "attacker")
        self.assertEqual(brief["realm_scale"]["friendly"], {"counties": 2, "fortified": 2, "occupied": 0})
        self.assertEqual(brief["realm_scale"]["enemy"], {"counties": 3, "fortified": 3, "occupied": 0})
        self.assertEqual(brief["capitals"]["friendly"]["province_id"], 1)
        self.assertEqual(brief["capitals"]["enemy"]["province_id"], 4)
        self.assertEqual(brief["capitals"]["enemy"]["hops_from_opposing_main"], 3)
        self.assertEqual([t["province_id"] for t in brief["war_targets"]], [3])
        # Priority order: war_target (3) -> enemy_capital (4) -> interior (5)
        self.assertEqual([t["province_id"] for t in brief["priority_targets"]], [3, 4, 5])
        self.assertIn("war_target", brief["priority_targets"][0]["tags"])
        self.assertIn("frontline", brief["priority_targets"][0]["tags"])
        self.assertEqual(brief["friendly_threats"][0]["province_id"], 2)
        self.assertTrue(brief["friendly_threats"][0]["besieged"])
        self.assertEqual(brief["distances"]["to_enemy_main"]["hops"], 3)
        self.assertEqual(brief["distances"]["to_enemy_main"]["crossings"], {"river": 1})
        self.assertEqual(brief["distances"]["to_enemy_main"]["estimated_days"], 16)
        self.assertEqual(brief["armies"]["friendly"][0]["terrain"], "plains")
        self.assertEqual(brief["armies"]["friendly"][0]["supply_limit"], 3500)
        self.assertLess(len(json.dumps(brief, ensure_ascii=False)), 4000)

    def test_live_objects_validates_war_theater_and_route_params(self):
        self.assertIn("war_theater", QUERY_FIELDS)
        self.assertEqual(_request({"kind": "war_theater", "id": 1, "max_targets": 6, "max_armies": 4})[0], "query")
        self.assertEqual(_request({"kind": "route", "origin": 1, "target": 2, "compute": True})[0], "query")
        with self.assertRaises(ValueError):
            _request({"kind": "war_theater", "id": 1, "max_targets": 0})
        with self.assertRaises(ValueError):
            _request({"kind": "route", "origin": 1, "target": 2, "compute": "yes"})

    def test_memory_world_refreshes_shadow_vm_only_when_clock_advances(self):
        refreshes = []
        tick = [10]

        class StubVM:
            def refresh_dynamic_state(self):
                refreshes.append(tick[0])

        class StubEval:
            vm = StubVM()

        class StubMem:
            pid = 1
            bytes_read = 0
            read_calls = 0
            def reset(self):
                pass

        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = StubMem()
        world.tables = {}
        world.references = set()
        world.primary_cache = {}
        world.summary_cache = {}
        world.ruler_cache = {}
        world.shadow_evaluator = StubEval()
        world.clock = lambda: {"world": 1, "world_data": 2, "date": 100, "tick": tick[0]}
        world.players = lambda: []
        world._query = lambda req: {"ok": True}

        world.query({"kind": "session"})
        world.query({"kind": "session"})
        self.assertEqual(refreshes, [10])
        tick[0] = 11
        world.query({"kind": "session"})
        self.assertEqual(refreshes, [10, 11])


if __name__ == "__main__":
    unittest.main()
