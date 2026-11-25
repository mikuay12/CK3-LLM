"""Offline tests for the zero-injection Copy-on-Write ShadowEvaluator."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import queue
import shutil
import struct
import tempfile
import unittest

from ck3_llm.client import EventWriter, run_connection
from ck3_llm.protocol.jomini_codec import JominiCodec
from ck3_llm.protocol.nakama_native import DEFAULT_GAME_DIR
from ck3_llm.protocol.pdx_nakama_codec import encode_payload
from ck3_llm.engine.shadow_evaluator import (
    BufferMemory,
    DB_REGISTRY_LOCK_RVA,
    FIXED_POINT_SCALE,
    INTERACTION_TYPE_INFO_RVA,
    ShadowEvaluator,
)


# Global DB registry RVAs read by DB_LOOKUP_RVA (0x3f93e90).
DB_BUCKETS_PTR_RVA = 0x5CC5A70
DB_BUCKETS_MASK_RVA = 0x5CC5A7C
DB_BUCKETS_COUNT_FLAG_RVA = 0x5CC5A80


def _build_synthetic_ck3_memory(
    base,
    interaction_defs,
    *,
    registry_locked=False,
    node_code=None,
):
    """Populate a read-only BufferMemory fixture mimicking CK3's runtime globals."""
    mem = BufferMemory(base)
    scratch = base + 0x9000_0000
    cursor = scratch

    def alloc_blob(data):
        nonlocal cursor
        addr = cursor
        mem.add_region(addr, data)
        cursor += ((len(data) + 63) & ~63)
        return addr

    # Tiny x86-64 CDatabase::Find stub:
    # r8 points to const char** interaction_name; compares first char or matches entry table.
    # For clean offline flexibility, we place a small dispatch table in memory:
    # Stub compares requested C string against up to N entries at [stub_data]:
    # Actually simplest native x86-64 stub for CDatabase::Find(rcx=this, rdx=type_info, r8=name_ptr_ptr):
    # We read `[this + 0x10]` = entry_ptr (returns first matching node in a simple linked list:
    # struct Node { const char* name; uint64 def_ptr; Node* next; }!).
    # Machine code loop over Node*:
    #   mov r10, [r8]          ; target cstr
    #   mov rax, [rcx + 0x10]  ; head node
    # loop:
    #   test rax, rax
    #   jz done
    #   mov r9, [rax]          ; node->name
    #   xor r11d, r11d
    # cmp_loop:
    #   mov dl, [r9 + r11]
    #   cmp dl, [r10 + r11]
    #   jne next_node
    #   test dl, dl
    #   jz found
    #   inc r11
    #   jmp cmp_loop
    # found:
    #   mov rax, [rax + 8]     ; node->def_ptr
    #   ret
    # next_node:
    #   mov rax, [rax + 16]    ; node->next
    #   jmp loop
    # done:
    #   ret
    db_find_stub = bytes([
        0x4D, 0x8B, 0x10,              # +0:  mov r10, qword ptr [r8]
        0x48, 0x8B, 0x41, 0x10,        # +3:  mov rax, qword ptr [rcx + 0x10]
        # loop (+7):
        0x48, 0x85, 0xC0,              # +7:  test rax, rax
        0x74, 0x24,                    # +10: jz done (+12 + 36 = +48)
        0x4C, 0x8B, 0x08,              # +12: mov r9, qword ptr [rax]
        0x45, 0x31, 0xDB,              # +15: xor r11d, r11d
        # cmp_loop (+18):
        0x43, 0x8A, 0x14, 0x19,        # +18: mov dl, byte ptr [r9 + r11]
        0x43, 0x3A, 0x14, 0x1A,        # +22: cmp dl, byte ptr [r10 + r11]
        0x75, 0x0E,                    # +26: jne next_node (+28 + 14 = +42)
        0x84, 0xD2,                    # +28: test dl, dl
        0x74, 0x05,                    # +30: jz found (+32 + 5 = +37)
        0x49, 0xFF, 0xC3,              # +32: inc r11
        0xEB, 0xED,                    # +35: jmp cmp_loop (+37 - 19 = +18)
        # found (+37):
        0x48, 0x8B, 0x40, 0x08,        # +37: mov rax, qword ptr [rax + 8]
        0xC3,                          # +41: ret
        # next_node (+42):
        0x48, 0x8B, 0x40, 0x10,        # +42: mov rax, qword ptr [rax + 0x10]
        0xEB, 0xD7,                    # +46: jmp loop (+48 - 41 = +7)
        # done (+48):
        0xC3,                          # +48: ret
    ])
    find_code_addr = alloc_blob(db_find_stub)
    vtable_addr = alloc_blob(struct.pack("<4Q", 0, 0, find_code_addr, 0))

    # Build CCharacterInteractionType mocks and linked list nodes.
    head_ptr = 0
    for name, spec in interaction_defs.items():
        def_buf = bytearray(0x2800)
        # CAIWillDoData at +0x18c8 (intermediary) and +0x1918 (recipient):
        # +0x28: int64 base_value, +0x30: Node** array, +0x3c: int32 count
        inter_val = int(round(spec.get("intermediary_base", 0.0) * FIXED_POINT_SCALE))
        recip_val = int(round(spec.get("recipient_base", 0.0) * FIXED_POINT_SCALE))
        struct.pack_into("<q", def_buf, 0x18C8 + 0x28, inter_val)
        struct.pack_into("<q", def_buf, 0x1918 + 0x28, recip_val)

        modifiers = spec.get("recipient_modifiers", ())
        if modifiers:
            node_ptrs = []
            for mod_delta in modifiers:
                # Native modifier node vtable[6] (+0x30): adds int64 delta stored at [rcx+8] to *rdx.
                # add rax, ... / mov rax, [rcx+8]; add [rdx], rax; ret
                code = node_code or bytes([
                    0x48, 0x8B, 0x41, 0x08,  # mov rax, [rcx + 8]
                    0x48, 0x01, 0x02,        # add [rdx], rax
                    0xC3,                    # ret
                ])
                mod_fn = alloc_blob(code)
                mod_vt = alloc_blob(struct.pack("<8Q", 0, 0, 0, 0, 0, 0, mod_fn, 0))
                mod_node = alloc_blob(struct.pack("<Qq", mod_vt, int(round(mod_delta * FIXED_POINT_SCALE))))
                node_ptrs.append(mod_node)
            arr_addr = alloc_blob(struct.pack(f"<{len(node_ptrs)}Q", *node_ptrs))
            struct.pack_into("<Q", def_buf, 0x1918 + 0x30, arr_addr)
            struct.pack_into("<i", def_buf, 0x1918 + 0x3C, len(node_ptrs))

        def_addr = alloc_blob(bytes(def_buf))
        name_addr = alloc_blob(name.encode("utf-8") + b"\x00")
        head_ptr = alloc_blob(struct.pack("<QQQ", name_addr, def_addr, head_ptr))

    db_obj_addr = alloc_blob(struct.pack("<QQQ", vtable_addr, 0, head_ptr))

    # Populate 2 buckets (mask=0, count_flag=0 -> sentinel at bucket[1], entry at bucket[0]).
    bucket0 = bytearray(0x50)
    bucket0[4] = 1
    struct.pack_into("<Q", bucket0, 0x08, base + INTERACTION_TYPE_INFO_RVA)
    struct.pack_into("<Q", bucket0, 0x48, db_obj_addr)
    buckets_addr = alloc_blob(bytes(bucket0) + b"\x00" * 0x50)

    mem.add_region(base + DB_BUCKETS_PTR_RVA, struct.pack("<Q", buckets_addr))
    mem.add_region(base + DB_BUCKETS_MASK_RVA, struct.pack("<I", 0))
    mem.add_region(base + DB_BUCKETS_COUNT_FLAG_RVA, b"\x00")
    if registry_locked:
        mem.add_region(base + DB_REGISTRY_LOCK_RVA, struct.pack("<I", 1))
    return mem, def_addr


class ShadowEvaluatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.executable = DEFAULT_GAME_DIR / "binaries/ck3.exe"
        if not cls.executable.exists():
            raise unittest.SkipTest("ck3.exe is not installed locally")

    def test_cow_isolation_and_native_acceptance_with_modifiers_and_locked_registry(self):
        base = 0x7FF6_1000_0000  # Deliberately relocated ASLR base != 0x140000000
        defs = {
            "gift_interaction": {"recipient_base": -25.0, "recipient_modifiers": (15.5, 4.0)},
            "arrange_marriage_interaction": {"recipient_base": -40.0, "intermediary_base": 12.25},
        }
        mem, _ = _build_synthetic_ck3_memory(base, defs, registry_locked=True)
        # Snapshot BufferMemory backing before VM execution.
        before_lock = mem.read(base + DB_REGISTRY_LOCK_RVA, 4)
        self.assertEqual(before_lock, b"\x01\x00\x00\x00")

        evaluator = ShadowEvaluator(self.executable, memory_source=mem)
        # Recipient != Actor runs CAIWillDoData::Calc including modifier loop;
        # intermediary == -1 short-circuits to 100.0 in native CalcAIIntermediaryAccept.
        res1 = evaluator.evaluate_interaction_acceptance(42, 99, "gift_interaction")
        self.assertAlmostEqual(res1["ai_accept"], -5.5, places=4)
        self.assertAlmostEqual(res1["ai_intermediary_accept"], 100.0, places=4)
        self.assertEqual(res1["source"], "native_shadow_unicorn")

        # Self-interaction short-circuits recipient acceptance to 100.0 natively.
        res_self = evaluator.evaluate_interaction_acceptance(42, 42, "gift_interaction")
        self.assertAlmostEqual(res_self["ai_accept"], 100.0, places=4)

        # Intermediary present uses +0x18c8 CAIWillDoData natively.
        res_inter = evaluator.evaluate_interaction_acceptance(
            42, 99, "arrange_marriage_interaction", intermediary=77
        )
        self.assertAlmostEqual(res_inter["ai_accept"], -40.0, places=4)
        self.assertAlmostEqual(res_inter["ai_intermediary_accept"], 12.25, places=4)

        # Backing memory remained untouched (zero writes to process memory).
        self.assertEqual(mem.read(base + DB_REGISTRY_LOCK_RVA, 4), b"\x01\x00\x00\x00")

    def test_refresh_dynamic_state_reflects_updated_memory(self):
        base = 0x140000000
        defs = {"gift_interaction": {"recipient_base": 10.0}}
        mem, def_addr = _build_synthetic_ck3_memory(base, defs)
        evaluator = ShadowEvaluator(self.executable, memory_source=mem)
        self.assertAlmostEqual(evaluator.evaluate_interaction_acceptance(42, 99, "gift_interaction")["ai_accept"], 10.0)

        # Update live backing memory and verify refresh picks up new value.
        mem.add_region(def_addr + 0x1918 + 0x28, struct.pack("<q", int(-60 * FIXED_POINT_SCALE)))
        updated = evaluator.evaluate_interaction_acceptance(42, 99, "gift_interaction", refresh=True)
        self.assertAlmostEqual(updated["ai_accept"], -60.0)

    def test_unknown_interaction_raises_cleanly(self):
        mem, _ = _build_synthetic_ck3_memory(0x140000000, {"gift_interaction": {"recipient_base": 5.0}})
        evaluator = ShadowEvaluator(self.executable, memory_source=mem)
        with self.assertRaisesRegex(ValueError, "nonexistent_interaction"):
            evaluator.evaluate_interaction_acceptance(42, 99, "nonexistent_interaction")

    def test_war_target_uses_loaded_native_scope_type(self):
        base = 0x7FF6_1000_0000
        mem, _ = _build_synthetic_ck3_memory(base, {"call_ally_interaction": {"recipient_base": 15.0}})
        table = base + 0xA000_0000
        types = bytearray(17 * 0x50)
        struct.pack_into("<I", types, 16 * 0x50 + 4, 0x2B3F)
        mem.add_region(table, bytes(types))
        mem.add_region(base + 0x5506B90, struct.pack("<QII", table, 17, 17))
        evaluator = ShadowEvaluator(self.executable, memory_source=mem)
        self.assertEqual(evaluator._scope_type_id("war"), 16)

    def test_ck3_client_uses_shadow_evaluator_when_ai_accept_omitted(self):
        root = Path(tempfile.mkdtemp(prefix="ck3_shadow_client_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        mem, _ = _build_synthetic_ck3_memory(
            0x7FF6_2000_0000,
            {"arrange_marriage_interaction": {"recipient_base": -17.5, "intermediary_base": 30.0}},
        )
        evaluator = ShadowEvaluator(self.executable, memory_source=mem)

        commands = queue.SimpleQueue()
        # Note: NO ai_accept supplied in request (model-facing form).
        commands.put({
            "command": "send_interaction",
            "interaction": "arrange_marriage_interaction",
            "recipient": 99,
            "secondary_actor": 43,
            "secondary_recipient": 100,
            "option_flags": {"matrilineal": True},
            "request_id": "shadow-1",
        })

        class Peer:
            def __init__(self):
                self.events = []
                self.sent = []

            def connect(self):
                self.events.append({"event": "connected"})

            def join(self, room_id, metadata):
                self.events.append({"event": "room_joined", "match_id": room_id})

            def poll(self):
                if self.sent:
                    commands.put({"command": "stop"})
                result, self.events = self.events, []
                return result

            def send_envelope(self, room_id, recipient, messages, magic=0xCAFE, presences=None):
                command = JominiCodec().decode_pdx_message(messages[0], from_server=False)
                if command["channel"] == "command":
                    self.sent.append(command)
                    self.events.append({
                        "event": "match_data",
                        "opcode": 255,
                        "payload": encode_payload("observer", messages, len(self.sent), "host"),
                        "presence": {"user_id": "host"},
                    })
                return b"sent"

        with redirect_stdout(io.StringIO()), EventWriter(root / "capture", quiet=True) as writer:
            writer.session_state.connected = True
            writer.session_state.local_machineid = 2
            writer.observations.set_character(42)
            writer.shadow_evaluator = evaluator
            peer = Peer()
            run_connection(
                peer,
                "room",
                writer,
                commands=commands,
                duration=1,
                handshake={
                    "command": "send_session_message",
                    "recipient": "host",
                    "type": "ClientRequestConnected",
                    "fields": {},
                },
            )
            self.assertEqual(writer.actions.status("shadow-1")["status"], "host_echoed")

        self.assertEqual(len(peer.sent), 1)
        fields = peer.sent[0]["fields"]
        self.assertAlmostEqual(fields["ai_accept"], -17.5, places=4)
        self.assertAlmostEqual(fields["ai_intermediary_accept"], 100.0, places=4)
        self.assertEqual(fields["character_interaction"]["actor"], 42)
        self.assertEqual(fields["character_interaction"]["recipient"], 99)

    def test_ck3_client_errors_when_ai_accept_omitted_without_memory(self):
        root = Path(tempfile.mkdtemp(prefix="ck3_shadow_nomem_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        commands = queue.SimpleQueue()
        commands.put({
            "command": "send_interaction",
            "interaction": "gift_interaction",
            "recipient": 99,
            "request_id": "shadow-missing",
        })
        commands.put({"command": "stop"})

        class Peer:
            def __init__(self):
                self.events = []

            def connect(self):
                self.events.append({"event": "connected"})

            def join(self, room_id, metadata):
                self.events.append({"event": "room_joined", "match_id": room_id})

            def poll(self):
                result, self.events = self.events, []
                return result

            def send_envelope(self, *args, **kwargs):
                return b"sent"

        with redirect_stdout(io.StringIO()), EventWriter(root / "capture", quiet=True) as writer:
            writer.session_state.connected = True
            writer.session_state.local_machineid = 2
            writer.observations.set_character(42)
            run_connection(
                Peer(),
                "room",
                writer,
                commands=commands,
                duration=1,
                handshake={
                    "command": "send_session_message",
                    "recipient": "host",
                    "type": "ClientRequestConnected",
                    "fields": {},
                },
            )

        events = [json.loads(line) for line in (root / "capture/events.jsonl").read_text(encoding="utf-8").splitlines()]
        errors = [event for event in events if event.get("event") == "command_error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("ShadowEvaluator", errors[0]["message"])

    def test_native_world_field_evaluators_run_ck3_getters_in_shadow_vm(self):
        from tests.test_construction_timing import ConstructionTimingFixture, DATE
        from tests.test_siege_progress import SiegeFixture

        base = 0x140000000
        scratch = 0x2000_0000
        mem = BufferMemory(base)

        # 1. Siege progress (0x2526600 & 0x25252a0)
        siege_fx = SiegeFixture(base, scratch)
        siege_fx.memory.put(siege_fx.regiment + 0x18, "II", 500, 350)
        siege_fx.memory.put(siege_fx.regiment + 0x128, "I", 500)
        siege_fx.memory.put(siege_fx.province + 0x730, "i", 300)
        siege_fx.memory.put(siege_fx.province + 0x850, "i", 10)
        siege_fx.memory.put(siege_fx.siege + 0x3d0, "q", 5_000_000)
        for addr, raw in siege_fx.memory.writes:
            mem.add_region(addr, raw)

        # 2. Construction timing (0x24763a0 & 0x24761c0)
        c_scratch = 0x2100_0000
        const_fx = ConstructionTimingFixture(base, c_scratch)
        const_fx.memory.put(const_fx.construction + 0x18, "q", 3_650_000)  # 36.5 base days
        const_fx.memory.put(const_fx.construction + 0x0c, "I", DATE - 20 * 24)
        for addr, raw in const_fx.memory.writes:
            mem.add_region(addr, raw)

        # 3. Unit movement (0x24b2c00, 0x24b3680, lock threshold, 0x24b520a..0x24b533e)
        u_base = 0x2200_0000
        unit, loc, defn, nodes, path_arr, edge = (
            u_base + 0x1000, u_base + 0x2000, u_base + 0x3000,
            u_base + 0x4000, u_base + 0x5000, u_base + 0x6000,
        )
        u_buf = bytearray(0x200)
        struct.pack_into("<Q", u_buf, 0x20, loc)
        struct.pack_into("<QII", u_buf, 0x38, path_arr, 1, 1)
        struct.pack_into("<q", u_buf, 0x168, 750_000)  # 7.5 progress out of 10.0 cost
        struct.pack_into("<q", u_buf, 0x190, 250_000)  # 2.5 speed -> 1 day left
        mem.add_region(unit, bytes(u_buf))
        mem.add_region(loc, struct.pack("<QQ", 0, defn))
        d_buf = bytearray(0xC0)
        struct.pack_into("<QII", d_buf, 0x50, edge, 1, 1)
        struct.pack_into("<Q", d_buf, 0xB0, nodes)
        mem.add_region(defn, bytes(d_buf))
        n_buf = bytearray(0x40)
        struct.pack_into("<I", n_buf, 0, 42)
        n_buf[0x0B] = 1
        struct.pack_into("<I", n_buf, 0x20, 43)
        n_buf[0x2B] = 1
        mem.add_region(nodes, bytes(n_buf))
        mem.add_region(path_arr, struct.pack("<Q", nodes + 0x20))
        e_buf = bytearray(0x30)
        struct.pack_into("<I", e_buf, 4, 43)
        struct.pack_into("<q", e_buf, 0x10, 1_000_000)  # 10.0 cost
        mem.add_region(edge, bytes(e_buf))
        mem.add_region(base + 0x5C7EB08, struct.pack("<q", 50_000))  # 0.5 lock threshold

        # 4. Regiment current/max strength (0xcce339 + 0x128)
        reg_ptr = 0x2300_1000
        reg_buf = bytearray(0x140)
        struct.pack_into("<I", reg_buf, 0x128, 1000)
        struct.pack_into("<II", reg_buf, 0x18, 400, 250)  # chunk 0: 150 missing
        struct.pack_into("<II", reg_buf, 0x18 + 0x24, 600, 600)  # chunk 1: full
        mem.add_region(reg_ptr, bytes(reg_buf))

        # 5. Siege daily projection (0x2527a50) defines & dummy unit fallback
        dummy_unit = 0x2000_D000
        unit_fb = bytearray(0x180)
        struct.pack_into("<I", unit_fb, 0x174, 0xFFFFFFFF)
        mem.add_region(dummy_unit, bytes(unit_fb))
        mem.add_region(base + 0x5D33388, struct.pack("<Q", dummy_unit))
        mem.add_region(base + 0x5C7E6A0, struct.pack("<q", 100_000))  # 1.0 base daily progress
        mem.add_region(base + 0x5C7E6C8, struct.pack("<q", 100_000))

        # 6. Combat result casualties (0x2677801..0x2677812 across 2 regiments)
        side_ptr = 0x2400_1000
        regs_ptr = 0x2400_2000
        side_buf = bytearray(0x50)
        struct.pack_into("<QII", side_buf, 0x38, regs_ptr, 2, 2)
        mem.add_region(side_ptr, bytes(side_buf))
        regs_buf = bytearray(0x50 * 2)
        struct.pack_into("<qqq", regs_buf, 0x28, 100 * 100_000, 25 * 100_000, 5 * 100_000)
        struct.pack_into("<qqq", regs_buf, 0x50 + 0x28, 40 * 100_000, 10 * 100_000, 0)
        mem.add_region(regs_ptr, bytes(regs_buf))

        evaluator = ShadowEvaluator(self.executable, memory_source=mem)

        siege_res = evaluator.evaluate_siege_progress(siege_fx.siege)
        self.assertGreater(siege_res["points_required"], 0)
        self.assertGreater(siege_res["percent"], 0)
        self.assertEqual(siege_res["source"], "native_shadow_unicorn")

        proj_res = evaluator.evaluate_siege_projection(siege_fx.siege, current_date=DATE)
        self.assertEqual(proj_res["status"], "native_shadow_unicorn")
        self.assertAlmostEqual(proj_res["points_per_day"], 1.0, places=4)
        self.assertGreater(proj_res["estimated_days_remaining"], 0)
        self.assertEqual(
            proj_res["estimated_completion_date"],
            DATE + proj_res["estimated_days_remaining"] * 24,
        )

        const_res = evaluator.evaluate_construction_timing(const_fx.construction, const_fx.province, current_date=DATE)
        self.assertGreater(const_res["end_date"], DATE)
        self.assertGreater(const_res["days_remaining"], 0)
        self.assertGreaterEqual(const_res["progress_percent"], 0.0)
        self.assertLessEqual(const_res["progress_percent"], 100.0)

        move_res = evaluator.evaluate_unit_movement(unit)
        self.assertAlmostEqual(move_res["leg_movement_cost"], 10.0, places=4)
        self.assertAlmostEqual(move_res["leg_progress_fraction"], 0.75, places=4)
        self.assertTrue(move_res["movement_locked"])
        self.assertEqual(move_res["estimated_next_arrival_days"], 1)

        reg_res = evaluator.evaluate_regiment_strength(reg_ptr)
        self.assertEqual(reg_res["current"], 850)
        self.assertEqual(reg_res["maximum"], 1000)

        cas_res = evaluator.evaluate_combat_casualties(side_ptr)
        self.assertAlmostEqual(cas_res["casualties"], 180.0, places=4)
        self.assertEqual(cas_res["regiment_count"], 2)
        self.assertEqual(cas_res["source"], "native_shadow_unicorn")

    def test_unit_movement_direction_from_native_map_coordinates(self):
        from ck3_llm.world.memory_world import MemoryWorld

        class StubMap:
            def province(self, pid):
                coords = {1: {"x": 100.0, "z": 100.0}, 2: {"x": 120.0, "z": 120.0}}
                return {"province_id": pid, "position": coords[pid]}

        class StubEvaluator:
            def evaluate_unit_movement(self, pointer):
                return {
                    "leg_movement_cost": 10.0,
                    "leg_progress_fraction": 0.6,
                    "movement_locked": True,
                    "estimated_next_arrival_days": 2,
                    "source": "native_shadow_unicorn",
                }

        world = MemoryWorld.__new__(MemoryWorld)
        world.shadow_evaluator = StubEvaluator()
        world._map_data = StubMap()
        world.navigation_unit = lambda eid: {
            "id": eid,
            "province_id": 1,
            "path": [2],
            "movement_progress": 6.0,
            "cached_movement_speed": 2.0,
            "retreating": False,
        }
        world.navigation_node = lambda pid: {
            "province_id": pid,
            "position": {"x": 100.0, "z": 100.0},
            "edges": [{"province_id": 2, "crossing": "land", "movement_cost": 10.0}],
        }
        world.movement_lock_threshold = lambda: 0.5
        world.lookup = lambda kind, eid: 0x2000_1000

        res = world.unit_movement(7)
        self.assertEqual(res["movement"]["direction_status"], "native_map_coordinates")
        self.assertEqual(res["movement"]["direction"]["to_province_id"], 2)
        self.assertEqual(res["movement"]["direction"]["cardinal"], "NE")
        self.assertEqual(res["movement"]["direction"]["dx"], 20.0)
        self.assertEqual(res["movement"]["direction"]["dz"], 20.0)

    def test_activity_display_is_compact_and_markup_free(self):
        from ck3_llm.world.activity_invitations import _clean_compact_text, _compact_activity_display

        self.assertEqual(
            _clean_compact_text("#high [ACTIVITY.GetHost.GetName|U]的@gold_icon!宴会#!  \n  $extra$", 60),
            "的宴会",
        )
        self.assertLessEqual(len(_clean_compact_text("A" * 200, 60)), 60)

        class StubWorld:
            class StubMem:
                executable = self.executable
            memory = StubMem()

        display = _compact_activity_display(
            StubWorld(),
            "activity_feast",
            {"id": 70, "name": "菲利普"},
            [{"index": 0, "key": "feast_phase_meal", "province_id": 1}],
            0,
            [70, 99, 101],
            True,
        )
        self.assertEqual(display["status"], "compact_localized_summary")
        self.assertIsNone(display["description"])
        self.assertIn("宴会", display["title"])
        self.assertLessEqual(len(display["title"]), 60)
        self.assertIn("受邀·宴会", display["summary"])
        self.assertIn("参与:3人", display["summary"])
        self.assertLessEqual(len(display["summary"]), 120)


if __name__ == "__main__":
    unittest.main()
