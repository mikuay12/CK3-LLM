"""Tests for Track 1 realm objects, Track 2 expanded interactions, and Track 3 catalogs & commands."""

import json
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from ck3_llm.gameplay.definition_catalog import DefinitionCatalog
from ck3_llm.gameplay.game_actions import ACTION_LABELS, action
from ck3_llm.gameplay.interaction_catalog import InteractionCatalog
from ck3_llm.protocol.jomini_codec import JominiCodec, simple_interaction_fields, supported_simple_interactions
from ck3_llm.world.live_objects import DirectWorldReader, QUERY_FIELDS
from ck3_llm.world.memory_world import MemoryWorld
from tests.test_siege_progress import BufferMemory


CK3_EXE = Path(r"C:\Games\Crusader Kings III\binaries\ck3.exe")


class RealmObjectsFixture:
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
        self.next_string = 0x21000000

        # Character database (42 = Richard, 70 = Physician, 99 = Employer)
        self.memory.put(self.world.base + 0x5C7C4F0, "Q", 0x20000000)
        self.memory.put(0x20000020, "QI", 0x20001000, 0)
        self.memory.put(0x2000002C, "I", 120)
        for cid, ptr, name in ((42, 0x20002000, "Richard"), (70, 0x20002400, "Physician"), (99, 0x20002800, "Baron")):
            self.memory.put(0x20001000 + cid * 16 + 8, "Q", ptr)
            self.memory.put(ptr, "Q", self.world.base + 0x47748A0)
            self.memory.put(ptr + 0x18, "I", cid)
            self._string(ptr + 0x20, name)

        # Scope types (2=char, 5=artifact)
        types = 0x20003000
        self.memory.put(self.world.base + 0x5506B90, "QII", types, 6, 6)
        self.memory.put(types + 80 + 4, "I", 0x500)
        self.memory.put(types + 160 + 4, "I", 0x2EA6)
        self.memory.put(types + 240 + 4, "I", 0x04FF)
        self.memory.put(types + 320 + 4, "I", 0x2EA7)
        self.memory.put(types + 400 + 4, "I", 0x3594)

        # CCourtPosition (id=5)
        self._make_db(0x5D32D20, 0x20004000, 0x20004100)
        cp_ptr = 0x20004500
        cp_def = 0x20004800
        cp_task = 0x20004900
        self._def_key(cp_def, "court_physician_court_position")
        self._def_key(cp_task, "court_physician_prevent_disease_task")
        self.memory.put(0x20004100 + 5 * 16 + 8, "Q", cp_ptr)
        self.memory.put(cp_ptr, "QII", self.world.base + 0x473F400, 5, 0x436F506F)
        self.memory.put(cp_ptr + 0x10, "Q", self.world.base + 0x473F3C8)
        self.memory.put(cp_ptr + 0x110, "QQIII", cp_def, cp_task, 42, 70, 54296040)

        # CHolyOrder (id=8)
        self._make_db(0x5D32F18, 0x20005000, 0x20005100)
        ho_ptr = 0x20005500
        ho_type = 0x20005800
        ho_tenet = 0x20005900
        self._def_key(ho_type, "holy_order_templars")
        self._def_key(ho_tenet, "tenet_armed_pilgrimages")
        self.memory.put(0x20005100 + 8 * 16 + 8, "Q", ho_ptr)
        self.memory.put(ho_ptr, "Q", self.world.base + 0x4754D28)
        self.memory.put(ho_ptr + 0x10, "I", 8)
        self.memory.put(ho_ptr + 0x20, "III", 12, 3, 505)
        self.memory.put(ho_ptr + 0x30, "QQIIq", ho_type, ho_tenet, 42, 0, 15 * 100000)
        self.memory.put(0x20005A00, "II", 601, 602)
        self.memory.put(ho_ptr + 0x50, "QII", 0x20005A00, 2, 2)
        self.memory.put(0x20005A20, "I", 701)
        self.memory.put(ho_ptr + 0x68, "QII", 0x20005A20, 1, 1)
        self.memory.put(ho_ptr + 0x80, "I", 99)
        self.memory.put(0x20005A40, "I", 801)
        self.memory.put(ho_ptr + 0x88, "QII", 0x20005A40, 1, 1)

        # CActiveSecret (id=14 active, id=15 marked delete)
        self._make_db(0x5D34C28, 0x20006000, 0x20006100)
        sec_type = 0x20006800
        sec_rel = 0x20006900
        self._def_key(sec_type, "secret_murder")
        self._def_key(sec_rel, "secret_relation_none")
        for sid, delete in ((14, 0), (15, 1)):
            sptr = 0x20006500 + (sid - 14) * 0x100
            self.memory.put(0x20006100 + sid * 16 + 8, "Q", sptr)
            self.memory.put(sptr, "QQI", self.world.base + 0x473F5B0, self.world.base + 0x473F5E8, sid)
            self.memory.put(sptr + 0x20, "QQ", sec_type, sec_rel)
            self.memory.put(sptr + 0x30, "HHIQ", 2, 0, 0, 70)
            self.memory.put(sptr + 0x40, "I", 42)
            self.memory.put(0x20006A00, "II", 42, 99)
            self.memory.put(sptr + 0x60, "QII", 0x20006A00, 2, 2)
            self.memory.put(sptr + 0x78, "QII", 0, 0, 0)
            self.memory.put(0x20006A20, "I", 99)
            self.memory.put(sptr + 0x90, "QII", 0x20006A20, 1, 1)
            self.memory.put(sptr + 0xA8, "BB", 1 if sid == 14 else 0, delete)

        # CTaskContract (id=19 accepted with dest, id=20 available without dest)
        self._make_db(0x5D34C30, 0x20007000, 0x20007100)
        tc_def = 0x20007800
        self._def_key(tc_def, "laamp_treasure_contract")
        loc_ptr = 0x20007900
        self.memory.put(loc_ptr + 0x10, "I", 1234)
        dest_ptr = 0x20008000
        self.memory.put(dest_ptr + 0x10, "I", 1456)
        self.memory.put(dest_ptr + 0x85C, "I", 0x50726F76)
        null_dest = 0x20009000
        self.memory.put(null_dest + 0x85C, "I", 0)
        for tid, status, dptr, acc, owner in ((19, 2, dest_ptr, 54296040, 42), (20, 1, null_dest, 0x29C77F8, 0xFFFFFFFF)):
            tptr = 0x20007500 + (tid - 19) * 0x140
            self.memory.put(0x20007100 + tid * 16 + 8, "Q", tptr)
            self.memory.put(tptr, "QIIQ", self.world.base + 0x4757F98, tid, 0x5461436F, self.world.base + 0x4757FA8)
            self.memory.put(tptr + 0x28, "QQIIQI", loc_ptr, dptr, 99, 0, tc_def, 70)
            self.memory.put(tptr + 0x4C, "III", 2, owner, status)
            self._string(tptr + 0x58, f"Contract {tid}")
            self.memory.put(tptr + 0x78, "IiIi", acc, -1, 0x29C77F8, -1)
            self._string(tptr + 0x88, "Gold reward")

    def _make_db(self, global_rva, db_addr, slots_addr, length=32):
        self.memory.put(self.world.base + global_rva, "Q", db_addr)
        self.memory.put(db_addr + 0x20, "QI", slots_addr, 0)
        self.memory.put(db_addr + 0x2C, "I", length)

    def _string(self, address, text):
        raw = text.encode("utf-8")
        if len(raw) <= 15:
            self.memory.write(address, raw.ljust(16, b"\0"))
            self.memory.put(address + 0x10, "QQ", len(raw), 15)
        else:
            buf = self.next_string
            self.next_string += (len(raw) + 32) & ~15
            self.memory.write(buf, raw + b"\0")
            self.memory.put(address, "Q", buf)
            self.memory.put(address + 0x10, "QQ", len(raw), len(raw))

    def _def_key(self, address, key):
        self._string(address + 0x18, key)
        self.memory.put(address + 0x38, "I", 0x4744624F)


class RealmObjectsTests(unittest.TestCase):
    def test_direct_world_reader_supports_four_new_object_kinds(self):
        fixture = RealmObjectsFixture()
        world = fixture.world
        world.clock = lambda: {"world": 1, "world_data": 2, "date": 54296500, "tick": 100}
        world.players = lambda: []
        world.query = lambda request: {
            "data": MemoryWorld._query(world, request),
            "runtime": {**world.clock(), "date_started": 54296500, "spans_updates": False},
        }
        labels = type("Labels", (), {"values": {}, "apply": staticmethod(lambda value: value)})()
        reader = DirectWorldReader.__new__(DirectWorldReader)
        reader.memory = world
        reader.labels = labels
        reader._lock = __import__("threading").RLock()
        reader.character_id = 42
        reader.runtime = {}
        reader._baseline = {}
        reader._responses = {}
        reader._catalog = reader._event_catalog = reader._interaction_catalog = None
        reader._planning_catalog = reader._definition_catalog = reader._notifications = None

        schema = reader.handle({"command": "schema", "max_chars": 50000})["data"]
        for key in ("court_position", "court_positions", "holy_order", "holy_orders",
                    "secret", "secrets", "task_contract", "task_contracts"):
            self.assertIn(key, schema)
            self.assertIn(key, QUERY_FIELDS)

        cp = reader.handle({"kind": "court_positions", "character": 70})["data"]
        self.assertEqual(cp["count"], 1)
        self.assertEqual(cp["items"][0]["type"], "court_physician_court_position")
        self.assertEqual(cp["items"][0]["hire_date"], "1198.3.7")

        ho = reader.handle({"kind": "holy_orders", "faith": 12})["data"]
        self.assertEqual(ho["count"], 1)
        self.assertEqual(ho["items"][0]["type"], "holy_order_templars")
        self.assertEqual(ho["items"][0]["worldliness"], 15.0)
        self.assertEqual(ho["items"][0]["leased_title_ids"], [601, 602])

        sec = reader.handle({"kind": "secrets", "character": 99})["data"]
        self.assertEqual(sec["count"], 1)
        self.assertEqual(sec["items"][0]["id"], 14)
        sec_all = reader.handle({"kind": "secrets", "include_invalidated": True})["data"]
        self.assertEqual(sec_all["count"], 2)

        tc = reader.handle({"kind": "task_contracts", "character": 42})["data"]
        self.assertEqual(tc["count"], 1)
        self.assertEqual(tc["items"][0]["status"], "accepted")
        self.assertEqual(tc["items"][0]["destination_province_id"], 1456)
        self.assertEqual(tc["items"][0]["acceptance_date"], "1198.3.7")
        self.assertIsNone(tc["items"][0]["completion_date"])


@unittest.skipUnless(CK3_EXE.exists(), "Installed CK3 files are required for catalog tests")
class ExpandedInteractionsAndCatalogsTests(unittest.TestCase):
    def test_interaction_catalog_covers_installed_simple_and_target_interactions(self):
        catalog = InteractionCatalog(CK3_EXE)
        all_rows = catalog.query({"kind": "interactions", "limit": 100})
        self.assertGreaterEqual(all_rows["count"], 460)
        self.assertIn("send_poem_interaction", supported_simple_interactions())
        self.assertIn("gift_artifact_interaction", supported_simple_interactions())
        self.assertIn("diarch_legal_meddling_interaction", supported_simple_interactions())
        self.assertIn("invite_to_council_position_interaction", supported_simple_interactions())
        self.assertIn("liege_modify_vassal_contract_interaction", supported_simple_interactions())
        self.assertNotIn("declare_war_interaction", supported_simple_interactions())

        filtered = catalog.query({"kind": "interactions", "group": "interaction_category_friendly", "search": "poem"})
        self.assertTrue(any(item["id"] == "send_poem_interaction" for item in filtered["items"]))

        codec = JominiCodec()
        header = {"identity": 1, "sender": 0, "receiver": 0, "attribs": 14}

        poem_fields = catalog.build(42, 99, "send_poem_interaction", ai_accept=25.0)
        poem_roundtrip = codec.decode_command(codec.encode_command("send_character_interaction", poem_fields, header))["fields"]
        self.assertEqual(poem_roundtrip["character_interaction"]["character_interaction"], "send_poem_interaction")

        art_fields = catalog.build(42, 99, "gift_artifact_interaction", ai_accept=10.0,
                                   target={"type": "artifact", "identity": 321})
        art_roundtrip = codec.decode_command(codec.encode_command("send_character_interaction", art_fields, header))["fields"]
        self.assertEqual(art_roundtrip["character_interaction"]["target"]["identity"], 321)

        with self.assertRaises(ValueError):
            simple_interaction_fields(42, 99, "gift_artifact_interaction", ai_accept=10.0)
        self.assertIn("target", action("send_interaction")["parameters"])

    def test_definition_catalog_loads_all_six_static_kinds_and_wires_reader(self):
        catalog = DefinitionCatalog(CK3_EXE)
        for plural, singular, sample_id in (
            ("council_task_types", "council_task_type", "task_collect_taxes"),
            ("court_position_types", "court_position_type", "court_physician_court_position"),
            ("court_position_tasks", "court_position_task", "antiquarian_court_position_task_improve_artifacts"),
            ("scheme_countermeasures", "scheme_countermeasure", "bounties_for_whispers_t1"),
            ("laws", "law", "crown_authority_1"),
            ("men_at_arms_types", "men_at_arms_type", "light_footmen"),
        ):
            listing = catalog.query({"kind": plural, "limit": 5})
            self.assertGreater(listing["count"], 10)
            detail = catalog.query({"kind": singular, "id": sample_id})
            self.assertEqual(detail["id"], sample_id)
            self.assertEqual(detail["availability"], "requires_game_evaluation")

        class StubMemory:
            memory = type("Mem", (), {"executable": CK3_EXE})()
            def query(self, request):
                raise AssertionError("Static catalogs should not query live game memory")
            def close(self):
                pass

        with patch("ck3_llm.world.live_objects.MemoryWorld", lambda pid=None: StubMemory()), DirectWorldReader() as reader:
            res = reader.handle({"kind": "laws", "group": "crown_authority", "limit": 10})["data"]
            self.assertGreaterEqual(res["count"], 4)
            self.assertIn("crown_authority_2", [item["id"] for item in res["items"]])
            maa = reader.handle({"kind": "men_at_arms_type", "id": "light_footmen"})["data"]
            self.assertEqual(maa["regiment_category"], "skirmishers")


class TwentyEightNewCommandsTests(unittest.TestCase):
    def test_all_twenty_eight_new_commands_round_trip_and_expose_action_schemas(self):
        codec = JominiCodec()
        header = {"identity": 10, "sender": 0, "receiver": 0, "attribs": 14}
        samples = [
            ("add_law", {"character": 42, "law": "crown_authority_2"}),
            ("add_title_law", {"title": 300, "character": 42, "law": 2}),
            ("clear_title_laws", {"title": 300, "character": 42}),
            ("nominate_successor_for_title", {"elector": 42, "title": 300, "candidate": 70, "force": True}),
            ("designate_diarch", {"liege": 42, "designated_diarch": 70}),
            ("set_diarchy_mandate", {"liege": 42, "mandate": "fill_coffers"}),
            ("create_maa_regiment", {"valid_for": "player", "title": 300, "character": 42, "regiment": 2, "count": 3, "cost": "no"}),
            ("add_maa_to_regiment", {"valid_for": "player", "regiment": 55, "count": 1, "cost": "yes"}),
            ("destroy_maa_regiment", {"valid_for": "player", "regiment": 55}),
            ("set_maa_reinforce", {"valid_for": "player", "character": 42, "reinforce": True}),
            ("reorganize_army", {"valid_for": "player", "army": 11, "merge_army": 12, "select_unit": 0, "disband_army": 0, "regiments": [101, 102]}),
            ("split_army_for_siege", {"valid_for": "player", "actor": 42, "army": 11, "soldiers_to_stay": 300}),
            ("attach_to_unit", {"valid_for": "player", "unit": 8, "to": 9}),
            ("detatch_from_unit", {"valid_for": "player", "unit": 8}),
            ("toggle_raiding", {"valid_for": "player", "character": 42, "army": 11}),
            ("raid", {"valid_for": "player", "character": 42, "army": 11}),
            ("knight_status", {"character": 70, "knight_status": "force"}),
            ("add_rally_point", {"character": 42, "target": 1278}),
            ("move_rally_point", {"character": 42, "rally_point": 0, "target": 1280}),
            ("remove_rally_point", {"character": 42, "target": 1280}),
            ("mass_prisoner_action", {"interaction": "execute_prisoner_interaction", "actor": 42, "prisoners": [70, 99]}),
            ("equip_artifact", {"character": 42, "artifact": 501, "replace": 500}),
            ("unequip_artifact", {"character": 42, "artifact": 501}),
            ("start_court_position_task", {"court_position": 5, "task_type": "court_physician_prevent_disease_task"}),
            ("stop_court_position_task", {"court_position": 5}),
            ("select_culture_fascination", {"player": 42, "culture": 3, "culture_innovation": 7}),
            ("add_dynasty_perk", {"target": 15, "perk": "blood_legacy_1", "trait": "giant"}),
            ("create_faction", {"faction_type": 2, "character": 42, "target": 99}),
        ]
        self.assertEqual(len(samples), 28)
        for command_name, fields in samples:
            with self.subTest(command=command_name):
                self.assertIn(command_name, ACTION_LABELS)
                schema = action(command_name)
                self.assertEqual(schema["command_type"], command_name)
                encoded = codec.encode_command(command_name, fields, header)
                decoded = codec.decode_command(encoded)
                self.assertEqual(decoded["type"], command_name)
                if command_name == "add_maa_to_regiment":
                    self.assertNotIn("cost", decoded["fields"])
                else:
                    for key, value in fields.items():
                        self.assertEqual(decoded["fields"][key], value)


if __name__ == "__main__":
    unittest.main()
