"""Regress native council/faction/scheme memory reads and new gameplay commands."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import queue
import struct
import tempfile
import unittest
from unittest.mock import patch

from ck3_llm.protocol.jomini_codec import JominiCodec
from ck3_llm.world.live_objects import DirectWorldReader
from ck3_llm.world.memory_world import MemoryWorld
from ck3_llm.model.model_runtime import ModelConfig, ModelRuntime
from tests.test_siege_progress import BufferMemory


class GovernanceFixture:
    def __init__(self):
        self.world = MemoryWorld.__new__(MemoryWorld)
        self.world.base = 0x140000000
        self.memory = self.world.memory = BufferMemory()
        self.world.tables = {}
        self.world.references = set()
        self.world.state = {}
        self.world.title_index = None
        self.world.primary_cache = {}
        self.world.summary_cache = {}
        self.world.province_index = None
        self.world.ruler_cache = {}
        self.world._scope_tokens = None
        self.world._world_identity = None
        self.world._last_clock = None
        self.world._map_data = None
        self.world._navigation_nodes = {}

        # Character database (42 = Richard, 70 = Marshall, 99 = Rival)
        self.memory.put(self.world.base + 0x5C7C4F0, "Q", 0x20000000)
        self.memory.put(0x20000020, "QI", 0x20001000, 0)
        self.memory.put(0x2000002C, "I", 120)
        for cid, ptr, name in ((42, 0x20002000, "Richard"), (70, 0x20002400, "Marshall"), (99, 0x20002800, "Rival")):
            self.memory.put(0x20001000 + cid * 16 + 8, "Q", ptr)
            self.memory.put(ptr, "Q", self.world.base + 0x47748A0)
            self.memory.put(ptr + 0x18, "I", cid)
            self._string(ptr + 0x20, name)

        # Scope types (1=boolean, 2=char, 3=prov, 4=lt)
        types = 0x20003000
        self.memory.put(self.world.base + 0x5506B90, "QII", types, 5, 5)
        self.memory.put(types + 80 + 4, "I", 0x500)
        self.memory.put(types + 160 + 4, "I", 0x2EA6)
        self.memory.put(types + 240 + 4, "I", 0x04FF)
        self.memory.put(types + 320 + 4, "I", 0x2EA7)

        # CActiveCouncilTask (id=11)
        self.memory.put(self.world.base + 0x5D32EB0, "Q", 0x20004000)
        self.memory.put(0x20004020, "QI", 0x20004100, 0)
        self.memory.put(0x2000402C, "I", 32)
        self.ct_ptr = 0x20004500
        self.ct_def = 0x20004800
        self._def_key(self.ct_def, "task_collect_taxes")
        self.memory.put(0x20004100 + 11 * 16 + 8, "Q", self.ct_ptr)
        self.memory.put(self.ct_ptr, "Q", self.world.base + 0x453D1B0)
        self.memory.put(self.ct_ptr + 0x10, "I", 11)
        self.memory.put(self.ct_ptr + 0x18, "Q", self.ct_def)
        self.memory.put(self.ct_ptr + 0x20, "q", 45 * 100000)
        self.memory.put(self.ct_ptr + 0x28, "I", 54296040)
        self.memory.put(self.ct_ptr + 0x30, "II", 70, 0xFFFFFFFF)
        self.memory.put(self.ct_ptr + 0x39, "B", 1)
        self.memory.put(self.ct_ptr + 0x40, "II", 70, 42)
        self.memory.put(self.ct_ptr + 0x48, "HHIQ", 2, 0, 0, 42)

        # CFaction (id=7)
        self.memory.put(self.world.base + 0x5D32EA0, "Q", 0x20005000)
        self.memory.put(0x20005020, "QI", 0x20005100, 0)
        self.memory.put(0x2000502C, "I", 32)
        self.fac_ptr = 0x20005500
        self.fac_def = 0x20005800
        self._def_key(self.fac_def, "liberty_faction")
        self.memory.put(0x20005100 + 7 * 16 + 8, "Q", self.fac_ptr)
        self.memory.put(self.fac_ptr, "Q", self.world.base + 0x4754088)
        self.memory.put(self.fac_ptr + 0x10, "I", 7)
        self.memory.put(self.fac_ptr + 0x20, "Q", self.fac_def)
        self.memory.put(self.fac_ptr + 0x28, "qqqII", 65 * 100000, 120 * 100000, 80 * 100000, 42, 99)
        m_ptr = 0x20005900
        self.memory.put(m_ptr, "QII", self.world.base + 0x4753FC8, 99, 7)
        self.memory.put(m_ptr + 0x20, "QII", self.world.base + 0x4753FC8, 70, 7)
        t_ptr = 0x20005A00
        self.memory.put(t_ptr, "QI", self.world.base + 0x4754050, 305)
        self.memory.put(self.fac_ptr + 0x48, "QII", m_ptr, 2, 2)
        self.memory.put(self.fac_ptr + 0x60, "QII", t_ptr, 1, 1)
        self.memory.put(self.fac_ptr + 0x7C, "I", 54295008)
        self.memory.put(self.fac_ptr + 0x84, "I", 54296760)
        self.memory.put(self.fac_ptr + 0x8C, "I", 0xFFFFFFFF)

        # CActiveScheme (id=3 active, id=4 invalidated)
        self.memory.put(self.world.base + 0x5D34C38, "Q", 0x20006000)
        self.memory.put(0x20006020, "QI", 0x20006100, 0)
        self.memory.put(0x2000602C, "I", 32)
        for sid, status, exposed in ((3, 0, 1), (4, 1, 0)):
            ptr = 0x20006500 + sid * 0x400
            defn = 0x20006300 + sid * 0x80
            self._def_key(defn, "murder" if sid == 3 else "sway")
            self.memory.put(0x20006100 + sid * 16 + 8, "Q", ptr)
            self.memory.put(ptr, "Q", self.world.base + 0x4789C90)
            self.memory.put(ptr + 0x10, "I", sid)
            self.memory.put(ptr + 0x20, "Q", defn)
            self.memory.put(ptr + 0x28, "IIIIII", status, 42, 0, 99, 0, 0xFFFFFFFF)
            self.memory.put(ptr + 0x40, "I", 0xFFFFFFFF)
            self.memory.put(ptr + 0x78, "i", 6)
            self.memory.put(ptr + 0x258, "I", 54295008)
            self.memory.put(ptr + 0x268, "I", 54296040)
            self.memory.put(ptr + 0x27C, "B", exposed)
            self.memory.put(ptr + 0x280, "i", 45)
            self.memory.put(ptr + 0x298, "i", 2)

    def _string(self, address, text):
        raw = text.encode("utf-8")
        if len(raw) > 15:
            buf = address + 0x10000
            self.memory.put(address, "Q", buf)
            self.memory.write(buf, raw)
            cap = len(raw)
        else:
            self.memory.write(address, raw.ljust(16, b"\0"))
            cap = 15
        self.memory.put(address + 0x10, "QQ", len(raw), cap)

    def _def_key(self, address, key):
        self._string(address + 0x18, key)
        self.memory.put(address + 0x38, "I", 0x4744624F)


class GovernanceAndCommandTests(unittest.TestCase):
    def test_memory_world_reads_council_tasks_factions_and_schemes(self):
        fixture = GovernanceFixture()
        ct = fixture.world._query({"kind": "council_tasks", "character": 42})
        self.assertEqual([item["id"] for item in ct["items"]], [11])
        self.assertEqual(ct["items"][0]["type"], "task_collect_taxes")
        self.assertEqual(ct["items"][0]["owner"]["name"], "Marshall")
        self.assertEqual(ct["items"][0]["court_owner"]["name"], "Richard")
        self.assertTrue(ct["items"][0]["frozen"])

        fac = fixture.world._query({"kind": "factions", "character": 70})
        self.assertEqual([item["id"] for item in fac["items"]], [7])
        self.assertEqual(fac["items"][0]["member_ids"], [99, 70])
        self.assertEqual(fac["items"][0]["title_member_ids"], [305])
        self.assertEqual(fac["items"][0]["power"], 120.0)
        self.assertEqual(fixture.world._query({"kind": "factions", "character": 88})["count"], 0)

        active = fixture.world._query({"kind": "schemes", "character": 42})
        self.assertEqual([item["id"] for item in active["items"]], [3])
        self.assertEqual(active["items"][0]["secrecy"], 45)
        self.assertTrue(active["items"][0]["exposed"])
        all_sch = fixture.world._query({"kind": "schemes", "character": 42, "include_invalidated": True})
        self.assertEqual([item["id"] for item in all_sch["items"]], [3, 4])
        self.assertIsNone(all_sch["items"][1]["exposed_date"])

    def test_direct_reader_formats_dates_and_validates_governance_queries(self):
        fixture = GovernanceFixture()

        class World:
            def __init__(self, pid=None):
                self.memory = fixture.memory

            def query(self, request):
                return {"data": fixture.world._query(request), "runtime": {"date": 54296040, "tick": 1, "world": 100}}

            def close(self):
                pass

        with patch("ck3_llm.world.live_objects.MemoryWorld", World), DirectWorldReader(character_id=42) as reader:
            ct = reader.handle({"kind": "council_task", "id": 11, "fields": ["last_councillor_change", "progress"]})
            self.assertEqual(ct["data"], {"last_councillor_change": "1198.3.7", "progress": 45.0})
            fac = reader.handle({"kind": "faction", "id": 7, "fields": ["max_demand_date", "discontent"]})
            self.assertEqual(fac["data"], {"max_demand_date": "1198.4.6", "discontent": 65.0})
            sch = reader.handle({"kind": "scheme", "id": 3, "fields": ["exposed_date", "opportunities"]})
            self.assertEqual(sch["data"], {"exposed_date": "1198.3.7", "opportunities": 2})
            schema = reader.handle({"command": "schema", "kind": "council_task"})
            self.assertIn("progress", schema["data"])
            with self.assertRaisesRegex(ValueError, "include_invalidated must be a boolean"):
                reader.handle({"kind": "schemes", "include_invalidated": "yes"})

    def test_new_command_round_trips_and_council_target_validation(self):
        codec = JominiCodec()
        header = {"identity": 1, "sender": 2, "receiver": 0, "attribs": 14}
        samples = [
            ("change_council_task", {"character": 70, "liege": 42, "active_council_task": 11,
                                     "task_type": "task_increase_control",
                                     "target": {"type": "prov", "identity": 12786}}),
            ("fire_council_position", {"liege": 42, "character": 70, "position": 1}),
            ("swap_council_position", {"liege": 42, "character": 70, "target": 99}),
            ("leave_council_position", {"character": 70}),
            ("end_scheme", {"scheme": 3}),
            ("expose_secret", {"secret": 8, "character": 42}),
            ("set_countermeasure", {"character": 42, "scheme_countermeasure_type": "redouble_guards"}),
            ("toggle_scheme_agents_auto_assign", {"character": 42}),
            ("create_title", {"character": 42, "landed_title": 305}),
            ("destroy_title", {"character": 42, "landed_title": 305, "cost": True}),
            ("usurp_title", {"character": 42, "landed_title": 305}),
            ("set_primary_title", {"character": 42, "landed_title": 305}),
            ("set_realm_capital", {"cooldown": True, "character": 42, "landed_title": 305}),
            ("join_faction", {"faction": 7, "character": 70}),
            ("leave_faction", {"faction": 7, "character": 70}),
            ("destroy_faction", {"faction": 7}),
            ("faction_press_demand", {"faction": 7}),
            ("stop", {"valid_for": "player", "units": [10, 12]}),
            ("merge_units", {"valid_for": "player", "target": 10, "units": [12]}),
            ("split_half_army", {"valid_for": "player", "actor": 42, "army": 5}),
            ("start_assault", {"valid_for": "player", "siege": 2, "character": 42}),
            ("stop_assault", {"valid_for": "player", "siege": 2, "character": 42}),
            ("extend_mercenary", {"character": 42, "mercenary_company": 14, "cost": True, "war": True}),
            ("hire_holy_order", {"character": 42, "holy_order": 3, "cost": True}),
            ("give_court_position", {"employer": 42, "employee": 70, "court_position": "court_physician_court_position"}),
            ("revoke_court_position", {"court_position": 9}),
            ("cancel_travel_plan_command", {"travel_plan": 4}),
            ("dismiss_important_action", {"important_action_item": 18}),
            ("accept_task_contract", {"task_contract": 22}),
        ]
        for name, fields in samples:
            with self.subTest(command=name):
                decoded = codec.decode_command(codec.encode_command(name, fields, header))
                self.assertEqual(decoded["type"], name)
                self.assertEqual(decoded["fields"], fields)

        with self.assertRaisesRegex(ValueError, "Unsupported interaction scope type: army"):
            codec.encode_command("change_council_task", {
                "task_type": "task_collect_taxes",
                "target": {"type": "army", "identity": 1},
            }, header)

    def test_model_and_client_loop_submit_governance_actions(self):
        from ck3_llm.client import EventWriter, run_connection

        config = ModelConfig(endpoint="http://127.0.0.1:1/v1/chat/completions", model="offline")
        with redirect_stdout(io.StringIO()):
            runtime = ModelRuntime(config, model=object())
            try:
                cmd1 = runtime._action({"action": "change_council_task", "request_id": "ct-1",
                                        "parameters": {"liege": 42, "character": 70, "active_council_task": 11,
                                                       "task_type": "task_increase_control",
                                                       "target": {"type": "province", "identity": 12786}}})
                cmd2 = runtime._action({"action": "end_scheme", "request_id": "sch-1", "parameters": {"scheme": 3}})
                cmd3 = runtime._action({"action": "stop", "request_id": "stop-1",
                                        "parameters": {"valid_for": "player", "units": [10]}})
            finally:
                runtime.close()

        class Peer:
            def __init__(self):
                self.events = []
                self.sent = []

            def connect(self):
                self.events.append({"event": "connected"})

            def join(self, room_id, metadata):
                self.events.append({"event": "room_joined", "match_id": room_id})

            def poll(self):
                result, self.events = self.events, []
                return result

            def send_envelope(self, room_id, recipient, messages, magic=0xCAFE, presences=None):
                self.sent.append((recipient, JominiCodec().decode_pdx_message(messages[0], from_server=False)))
                return b"sent"

        commands = queue.SimpleQueue()
        commands.put(cmd1)
        commands.put(dict(cmd1))
        commands.put(cmd2)
        commands.put(cmd3)
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with EventWriter(Path(folder) / "client", quiet=True) as writer:
                writer.session_state.connected = True
                writer.session_state.local_machineid = 2
                writer.observations.set_character(42)
                peer = Peer()
                run_connection(peer, "room", writer, commands=commands, duration=0.05,
                               handshake={"command": "send_session_message", "recipient": "host",
                                          "type": "ClientRequestConnected", "fields": {}})
        gameplay = [command for _, command in peer.sent if command["channel"] == "command"]
        self.assertEqual([command["type"] for command in gameplay], ["change_council_task", "end_scheme", "stop"])
        self.assertEqual(gameplay[0]["fields"]["target"], {"type": "prov", "identity": 12786})
        self.assertEqual(gameplay[1]["fields"], {"scheme": 3})
        self.assertEqual(gameplay[2]["fields"], {"valid_for": "player", "units": [10]})


if __name__ == "__main__":
    unittest.main()
