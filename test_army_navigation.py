"""Regress factual map queries and model-directed movement execution."""

from contextlib import redirect_stdout
import io
import json
from copy import deepcopy
import queue
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from army_navigation import InstalledMap, movement_details, shortest_routes
from live_objects import DirectObservationQueue, QUERY_FIELDS, _request
from memory_world import MemoryWorld


def unit(identifier, province, *, path=None, progress=0, speed=1, retreating=False):
    return {"id": identifier, "army_id": identifier + 100, "owner_id": 42 if identifier == 1 else 99,
            "type": "army", "province_id": province, "path": path or [], "movement_progress": progress,
            "cached_movement_speed": speed, "retreating": retreating, "arrival_date": None}


def graph(edges, *, blocked=()):
    nodes = {}
    for first, second, cost, crossing in edges:
        for identifier in (first, second):
            nodes.setdefault(identifier, {"land_passable": identifier not in blocked, "edges": []})
        for origin, target in ((first, second), (second, first)):
            nodes[origin]["edges"].append({"province_id": target, "movement_cost": cost,
                                            "crossing": crossing, "through_province_id": None})
    return nodes


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.nodes = graph([(10, 20, 12, "normal"), (20, 30, 5, "river"),
                            (30, 40, 5, "normal"), (10, 30, 6, "normal")])

    def test_weighted_route_prefers_shorter_cost_over_fewer_provinces(self):
        routes = shortest_routes(self.nodes.__getitem__, 10, [20, 40])
        self.assertEqual(routes[20]["province_ids"], [10, 30, 20])
        self.assertEqual(routes[20]["movement_cost"], 11)


    def test_native_cost_is_exposed_and_unlocated_movement_results_are_unknown(self):
        details = movement_details(unit(1, 20, path=[30], progress=3, speed=2), self.nodes[20]["edges"])
        self.assertEqual(details["leg_movement_cost"], 5)
        self.assertIsNone(details["leg_progress_fraction"])
        self.assertIsNone(details["movement_locked"])
        self.assertIsNone(details["estimated_next_arrival_days"])

    def test_locking_threshold_is_reported_without_inventing_a_lock_result(self):
        details = movement_details(unit(1, 20, path=[30], progress=2.5), self.nodes[20]["edges"])
        self.assertIsNone(details["movement_locked"])
        details = movement_details(unit(1, 20, path=[30], progress=2.50001), self.nodes[20]["edges"])
        self.assertIsNone(details["leg_progress_fraction"])
        self.assertIsNone(details["movement_locked"])
        details = movement_details(unit(1, 20, path=[30], progress=2.50005), self.nodes[20]["edges"])
        self.assertIsNone(details["movement_locked"])
        self.assertEqual(details["movement_lock_threshold"], 0.5)


    def test_prohibited_native_edges_are_excluded(self):
        nodes = graph([(1, 2, 1, "impassable"), (1, 3, 2, "normal"), (3, 2, 2, "normal")])
        self.assertEqual(shortest_routes(nodes.__getitem__, 1, [2])[2]["province_ids"], [1, 3, 2])

    def test_query_entry_validates_navigation_search_limits(self):
        for kind in ("province_map", "route", "unit_movement"):
            self.assertEqual(_request({"kind": kind})[0], "query")
        for value in (True, 0, -1, 50001, "100"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _request({"kind": "route", "origin": 1, "target": 2, "max_nodes": value})

    def test_water_and_impassables_are_excluded_and_straits_are_optional(self):
        nodes = graph([(1, 2, 1, "normal"), (2, 3, 1, "normal"),
                       (1, 3, 8, "strait")], blocked=[2])
        self.assertEqual(shortest_routes(nodes.__getitem__, 1, [3])[3]["movement_cost"], 8)
        self.assertEqual(shortest_routes(nodes.__getitem__, 1, [3], include_straits=False), {})
        with self.assertRaisesRegex(ValueError, "budget"):
            shortest_routes(nodes.__getitem__, 1, [3], max_nodes=1)


    def test_installed_map_uses_expanded_player_locator_and_classifies_wasteland(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "game"
            (root / "map_data").mkdir(parents=True)
            (root / "gfx/map/map_object_data").mkdir(parents=True)
            (root / "map_data/default.map").write_text(
                'definitions="definition.csv"\n# impassable_mountains=LIST { 1 }\n'
                'sea_zones=RANGE { 2 3 }\nimpassable_mountains=list { 4 }\n', encoding="utf-8")
            (root / "map_data/definition.csv").write_text(
                '0;0;0;0;x\n1;1;2;3;One\n2;2;3;4;Sea\n3;3;4;5;Sea2\n4;4;5;6;Mountain\n', encoding="utf-8")
            (root / "gfx/map/map_object_data/player_stack_locators.txt").write_text(
                'instances={ { id=1 position={ 3 0 4 } } { id=4 position={ 0 0 0 } } }', encoding="utf-8")
            data = InstalledMap(root.parent / "binaries" / "ck3.exe")
            self.assertTrue(data.province(1)["land_passable"])
            self.assertFalse(data.province(2)["land_passable"])
            self.assertFalse(data.province(4)["land_passable"])
            self.assertEqual(data.distance(1, 4), 5)
            self.assertIsNone(data.distance(1, 2))

    def test_navigation_reads_native_edge_cost_and_cached_speed(self):
        records = bytearray(48)
        struct.pack_into("<III", records, 0, 2, 43, 0)
        struct.pack_into("<q", records, 16, 750000)
        class Memory:
            def pointer(self, address):
                return {0x1008: 0x2000, 0x2050: 0x3000}[address]

            def u32(self, address):
                return {0x2024: 42, 0x2058: 1, 0x205c: 1}[address]

            def read(self, address, size):
                return bytes(records[:size])

            def fixed(self, address):
                self.address = address
                return 3.6
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = Memory()
        world.province_pointer = lambda value: 0x1000
        self.assertEqual(world.adjacencies(42, include_cost=True)[0]["movement_cost"], 7.5)
        world.unit = lambda value: unit(1, 42)
        world.lookup = lambda kind, value: 0x4000
        self.assertEqual(world.navigation_unit(1)["cached_movement_speed"], 3.6)
        self.assertEqual(world.memory.address, 0x4190)


class InformationBoundaryTests(unittest.TestCase):
    def test_live_route_query_returns_native_edges_without_running_the_reference_search(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.navigation_node = lambda value: {"position": {"x": 10, "z": 20},
                                                "edges": [{"province_id": 30, "movement_cost": 5}]}
        world.map_data = lambda: type("Map", (), {"province": lambda self, value: {"position": {"x": 14, "z": 17}}})()
        with patch("army_navigation.shortest_routes", side_effect=AssertionError("Computed a game route")):
            result = world._query({"kind": "route", "origin": 20, "target": 40})
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["route"])
        self.assertEqual(result["origin_edges"][0]["movement_cost"], 5)

    def test_memory_query_reports_enemy_actual_direction_progress_and_orders(self):
        world = MemoryWorld.__new__(MemoryWorld)
        enemy = unit(2, 20, path=[30, 40], progress=3, speed=2)
        world.navigation_unit = lambda value: deepcopy(enemy)
        world.navigation_node = lambda value: {
            "position": {"x": 10, "z": 20},
            "edges": [{"province_id": 30, "movement_cost": 5}]}
        world.movement_lock_threshold = lambda: 0.5
        world.map_data = lambda: type("Map", (), {"province": lambda self, value: {"position": {"x": 14, "z": 17}}})()
        result = world._query({"kind": "unit_movement", "id": 2})
        self.assertEqual(result["path"], [30, 40])
        self.assertEqual(enemy["path"], [30, 40])
        self.assertEqual(result["movement"]["next_province_id"], 30)
        self.assertEqual(result["movement"]["destination_province_id"], 40)
        self.assertIsNone(result["movement"]["direction"])
        self.assertEqual(result["movement"]["direction_status"], "native_result_not_located")
        self.assertIsNone(result["movement"]["leg_progress_fraction"])
        self.assertIsNone(result["movement"]["estimated_next_arrival_days"])
        self.assertFalse({"decision", "chosen", "estimated_intercept"} & result.keys())
        enemy["path"] = []
        self.assertIsNone(world.unit_movement(2)["movement"]["direction"])

    def test_public_queries_reject_retired_decision_interfaces(self):
        self.assertNotIn("interception", QUERY_FIELDS)
        self.assertFalse(hasattr(MemoryWorld, "interception"))
        self.assertFalse(hasattr(DirectObservationQueue, "_pursuit_plan"))
        for request in ({"kind": "interception", "id": 1, "target": 2},
                        {"command": "pursue_army", "unit": 1, "target": 2}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                _request(request)

    def test_normal_sdk_queries_preserve_model_selected_destination(self):
        from ck3_client import EventWriter, run_connection
        from jomini_codec import JominiCodec

        class World:
            reads = 0

            def __init__(self, pid=None):
                self.memory = type("Memory", (), {"executable": Path("C:/missing/binaries/ck3.exe"),
                                                  "is_running": lambda self: True})()

            def query(self, request):
                runtime = {"date": 365 * 24 * 5030, "world": 100, "world_data": 200, "tick": 10}
                if request["kind"] == "session":
                    data = {**runtime, "players": [{"name": "AI", "character_id": 42, "machineid": 2, "flags": 0x40}]}
                elif request["kind"] == "player_successions":
                    data = {"items": []}
                elif request["kind"] == "unit_movement":
                    World.reads += 1
                    data = unit(2, 20, path=[30, 40 + World.reads])
                else:
                    raise AssertionError(request)
                return {"runtime": runtime, "data": data}

            def close(self):
                pass

        commands = queue.SimpleQueue()
        class Peer:
            def __init__(self):
                self.events = []
                self.sent = []
                self.stage = 0

            def connect(self):
                self.events.append({"event": "connected"})

            def join(self, room_id, metadata):
                self.events.append({"event": "room_joined", "match_id": room_id})

            def poll(self):
                if World.reads >= 1 and self.stage == 0:
                    commands.put({"command": "query", "kind": "unit_movement", "id": 2})
                    self.stage = 1
                elif World.reads >= 2 and self.stage == 1:
                    commands.put({"command": "send_game_command", "type": "move_unit", "header": {"attribs": 14},
                                  "fields": {"valid_for": "player", "unit": 1, "target": 77,
                                             "unit_order_type": "set", "type": "land", "gathering_army": "reset"}})
                    self.stage = 2
                elif World.reads >= 3 and self.stage == 2:
                    commands.put({"command": "stop"})
                    self.stage = 3
                result, self.events = self.events, []
                return result

            def send_envelope(self, room_id, recipient, messages, magic=0xcafe, presences=None):
                command = JominiCodec().decode_pdx_message(messages[0], from_server=False)
                self.sent.append((recipient, command))
                if command.get("type") == "move_unit":
                    commands.put({"command": "query", "kind": "unit_movement", "id": 2})
                return b"sent"

        def notifications(instance, character, machine):
            return {**instance.session, "sampled_at": time.monotonic()}, []

        retired = ("pursue_army", "pursuit_status", "cancel_pursuit", "_pursuit_move")
        for name in retired:
            commands.put({"command": name, "unit": 1, "target": 2, "request_id": name})
        commands.put({"command": "query", "kind": "unit_movement", "id": 2})
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()), \
                patch("live_objects.MemoryWorld", World), \
                patch.object(DirectObservationQueue, "_notifications", notifications):
            directory = Path(folder) / "client"
            with EventWriter(directory, quiet=True, live_memory_pid=1) as writer:
                writer.session_state.connected = True
                writer.session_state.local_machineid = 2
                peer = Peer()
                run_connection(peer, "room", writer, commands=commands, duration=1,
                               handshake={"command": "send_session_message", "recipient": "host",
                                          "type": "ClientRequestConnected", "fields": {}})
            events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(World.reads, 3)
        self.assertEqual({event["request_id"] for event in events if event["event"] == "command_error"}, set(retired))
        gameplay = [(recipient, command) for recipient, command in peer.sent if command["channel"] == "command"]
        self.assertEqual(len(gameplay), 1)
        recipient, command = gameplay[0]
        self.assertEqual(command["type"], "move_unit")
        self.assertEqual(command["fields"]["target"], 77)
        self.assertEqual(command["fields"]["unit"], 1)
        self.assertEqual(command["default_state"]["sender"], 2)
        self.assertEqual(command["default_state"]["attribs"], 14)
        self.assertEqual(recipient, "host")


if __name__ == "__main__":
    unittest.main()
