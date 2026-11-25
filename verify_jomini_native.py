"""Compare Python command bytes with CK3's actual serializer in a CPU emulator.

Uses Unicorn only for offline verification. CK3's code executes in emulated
memory with a small output-stream stub. The game process is never started.
This checks serialization bytes, independently of the Python encoder. It does
not check whether a real multiplayer session accepts or executes a command.
"""

import argparse
import hashlib
import json
from pathlib import Path
import random
import struct
import sys

sys.path.insert(0, str(Path(__file__).parent / ".deps"))
from unicorn import Uc, UC_ARCH_X86, UC_MODE_64, UC_HOOK_CODE
from unicorn.x86_const import (
    UC_X86_REG_RAX, UC_X86_REG_RCX, UC_X86_REG_RDX, UC_X86_REG_R8,
    UC_X86_REG_R9, UC_X86_REG_RIP, UC_X86_REG_RSP,
    UC_X86_REG_GS_BASE,
)

from extract_jomini_tokens import SUPPORTED_EXE_SHA256
from inspect_network import PortableExecutable
from jomini_codec import COMMAND_SCHEMAS, JominiCodec
from nakama_native import DEFAULT_GAME_DIR


class NativeSerializerOracle:
    def __init__(self, executable):
        pe = PortableExecutable(Path(executable))
        if hashlib.sha256(pe.data).hexdigest() != SUPPORTED_EXE_SHA256:
            raise ValueError("The serializer offsets require the verified executable hash")
        self.base = pe.image_base
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        for section in pe.sections:
            size = max(section["virtual_size"], section["raw_size"])
            self.cpu.mem_map(self.base + section["rva"], (size + 4095) & ~4095)
            start = section["raw_offset"]
            self.cpu.mem_write(self.base + section["rva"], pe.data[start:start + section["raw_size"]])
        self.scratch = 0x20000000
        self.stack = 0x30000000
        self.cpu.mem_map(self.scratch, 0x10000)
        self.cpu.mem_map(self.stack, 0x10000)
        # Database-reference writers read a thread-local full-object flag.
        tls = 0x40000000
        self.cpu.mem_map(tls, 0x10000)
        self.cpu.reg_write(UC_X86_REG_GS_BASE, tls)
        self.cpu.mem_write(tls + 0x58, struct.pack("<Q", tls + 0x100))
        self.cpu.mem_write(tls + 0x100, struct.pack("<Q", tls + 0x200))
        self.stop = self.scratch + 0xF000
        self.bulk_stub = self.scratch + 0xE000
        self.byte_stub = self.scratch + 0xE010
        self.cpu.mem_write(self.bulk_stub, b"\xc3")
        self.cpu.mem_write(self.byte_stub, b"\xc3")
        self.writer = self.scratch
        stream = self.scratch + 0x100
        stream_vtable = self.scratch + 0x200
        self.cpu.mem_write(stream, struct.pack("<Q", stream_vtable))
        self.cpu.mem_write(stream_vtable + 0x50, struct.pack("<Q", self.bulk_stub))
        self.cpu.mem_write(stream_vtable + 0x48, struct.pack("<Q", self.byte_stub))
        # CWriter::WriteToken checks this table's maximum built-in token ID.
        pool = self.scratch + 0x400
        self.cpu.mem_write(self.base + 0x5CD3B08, struct.pack("<Q", pool))
        self.cpu.mem_write(pool + 0x60, struct.pack("<I", 0xFFFF))
        self.cpu.hook_add(UC_HOOK_CODE, self._write_stream,
                          begin=self.bulk_stub, end=self.byte_stub)
        self.malloc_stub = self.base + 0x4233514
        self.pdx_alloc_stub = self.base + 0x859470
        self.free_stub = self.base + 0x42338C4
        self.pdx_free_stub = self.base + 0x40FF040
        self.cpu.mem_write(self.malloc_stub, b"\xc3")
        self.cpu.mem_write(self.pdx_alloc_stub, b"\xc3")
        self.cpu.mem_write(self.free_stub, b"\xc3")
        self.cpu.mem_write(self.pdx_free_stub, b"\xc3")
        self.cpu.hook_add(UC_HOOK_CODE, self._alloc_stub,
                          begin=self.malloc_stub, end=self.malloc_stub)
        self.cpu.hook_add(UC_HOOK_CODE, self._alloc_stub,
                          begin=self.pdx_alloc_stub, end=self.pdx_alloc_stub)
        self.output = bytearray()

    def _alloc_stub(self, cpu, address, size, user_data):
        requested = cpu.reg_read(UC_X86_REG_RCX)
        cpu.reg_write(UC_X86_REG_RAX, self._allocate(bytes(max(1, min(requested, 1 << 16)))))

    def _write_stream(self, cpu, address, size, user_data):
        if address == self.bulk_stub:
            pointer = cpu.reg_read(UC_X86_REG_RDX)
            length = cpu.reg_read(UC_X86_REG_R8)
            if length > 1 << 20:
                raise ValueError("Unexpected native stream write length")
            self.output.extend(cpu.mem_read(pointer, length))
        elif address == self.byte_stub:
            self.output.append(cpu.reg_read(UC_X86_REG_RDX) & 255)

    def _call(self, rva, rcx=0, rdx=0, r8=0, r9=0):
        stack_pointer = self.stack + 0xF008
        self.cpu.mem_write(stack_pointer, struct.pack("<Q", self.stop))
        for register, value in (
            (UC_X86_REG_RSP, stack_pointer), (UC_X86_REG_RCX, rcx),
            (UC_X86_REG_RDX, rdx), (UC_X86_REG_R8, r8), (UC_X86_REG_R9, r9),
        ):
            self.cpu.reg_write(register, value)
        self.cpu.emu_start(self.base + rva, self.stop, timeout=2_000_000, count=100_000)
        if self.cpu.reg_read(UC_X86_REG_RIP) != self.stop:
            raise RuntimeError("Native serializer did not return within the execution bound")

    def _allocate(self, data):
        address = self.allocation
        self.allocation += (len(data) + 15) & ~15
        if self.allocation > self.scratch + 0xD000:
            raise ValueError("Native verification fixture exceeds the scratch buffer")
        self.cpu.mem_write(address, data)
        return address

    def _string(self, address, value):
        raw = value.encode("utf-8")
        if len(raw) <= 15:
            self.cpu.mem_write(address, raw.ljust(16, b"\0"))
            capacity = 15
        else:
            self.cpu.mem_write(address, struct.pack("<Q", self._allocate(raw + b"\0")))
            capacity = len(raw)
        self.cpu.mem_write(address + 0x10, struct.pack("<QQ", len(raw), capacity))

    def _database_reference(self, value, size=0x80):
        # Recovered virtual-base layout, using the compiled name writer.
        reference = self._allocate(bytes(size))
        vbtable = self._allocate(struct.pack("<ii", 0, 0x38))
        vtable = self._allocate(struct.pack("<QQ", 0, self.base + 0x38451D0))
        self.cpu.mem_write(reference + 8, struct.pack("<Q", vbtable))
        self.cpu.mem_write(reference + 0x40, struct.pack("<Q", vtable))
        self._string(reference + 0x10, value)
        return reference

    def _decision_parameters(self, parameters):
        scope = self._allocate(bytes(0x110))
        targets = parameters.get("event_targets", {})
        entries = self._allocate(bytes(24 * len(targets)))
        names = self._allocate(bytes(32 * len(targets)))
        for index, (name, value) in enumerate(targets.items()):
            self._string(names + 32 * index, name)
            self.cpu.mem_write(entries + 24 * index, struct.pack(
                "<IIHHIQ", index, 0, 1, 0, 0, value.get("identity", 0),
            ))
        self.cpu.mem_write(scope + 0x28, struct.pack("<QII", entries, len(targets), len(targets)))
        # Populate the compiled name pool and scope-type registry for fixtures.
        pool = self.base + 0x5DD6570
        self.cpu.mem_write(pool, bytes(0x50))
        self.cpu.mem_write(pool + 0x30, struct.pack("<QII", names, len(targets), len(targets)))
        types = self._allocate(bytes(2 * 80))
        self.cpu.mem_write(types + 80 + 4, struct.pack("<I", 0x500))
        self.cpu.mem_write(self.base + 0x5506B90, struct.pack("<QII", types, 2, 2))
        return scope

    def _interaction_scope_value(self, value):
        kinds = {"boolean": 1, "char": 2, "prov": 3, "province": 3, "lt": 4, "landed_title": 4, "artifact": 5, "war": 6}
        return struct.pack("<HHIQ", kinds[value["type"]] if value else 0, 0, 0, value.get("identity", 0))

    def _interaction(self, address, data):
        from jomini_codec import CULTURE_TRAIT_PILLAR_TOKENS, SPECIAL_INTERACTION_SPECS, supported_interaction_specs
        special_indices = {
            "remove_scheme_interaction": 0, "liege_modify_vassal_contract": 1,
            "end_war_attacker_victory_interaction": 2, "end_war_white_peace_interaction": 3,
            "end_war_attacker_defeat_interaction": 4, "break_betrothal_interaction": 5,
            "arrange_marriage_interaction": 6, "grant_titles_interaction": 7,
            "grant_governorship_interaction": 8, "revoke_title_interaction": 9,
            "ransom_interaction": 10, "release_from_prison_interaction": 11,
            "execute_prisoner_interaction": 12, "declare_war_interaction": 13,
            "offer_peace_interaction": 14, "invite_to_scheme_interaction": 15,
            "call_ally_interaction": 16, "interfere_in_war_interaction": 17,
            "grant_vassal_interaction": 18, "retract_vassal_interaction": 19,
            "create_claimant_faction_against_interaction": 20,
            "create_ceremonial_claimant_faction_against_interaction": 21,
            "request_conversion_interaction": 22, "rite_conversion": 23,
            "reform_faith": 24, "request_culture_conversion_interaction": 25,
            "legitimize_bastard": 26, "invite_to_council_interaction": 27,
            "force_vote_in_succession_election": 28, "force_join_faction": 29,
            "feudalize_holding": 30, "tribalize_holding": 31,
            "force_onto_council_interaction": 32, "request_council_position": 33,
            "diarch_revoke_title": 34, "vassal_modify_vassal_contract": 35,
            "request_court_position": 36, "grant_court_position": 37,
            "migration": 38, "request_great_project_contribution": 39,
        }
        name = data["character_interaction"]
        specs = supported_interaction_specs()
        special_name = "declare_war_interaction" if name == "declare_war_interaction" else specs.get(name, {}).get("special_interaction")
        definition = self._database_reference(name, size=0x2700)
        kind = special_indices.get(special_name, 40)
        self.cpu.mem_write(definition + 0x26f8, bytes([kind]))
        self.cpu.mem_write(address, struct.pack("<Q", definition))
        names = list(dict.fromkeys(key for field in ("scope", "original_scope")
                                   for key in data[field].get("event_targets", {})))
        name_array = self._allocate(bytes(len(names) * 32))
        for index, key in enumerate(names):
            self._string(name_array + index * 32, key)
        pool = self.base + 0x5dd6570
        self.cpu.mem_write(pool, bytes(0x50))
        self.cpu.mem_write(pool + 0x30, struct.pack("<QII", name_array, len(names), len(names)))
        self._scope_types()
        for field, offset in (("scope", 8), ("original_scope", 0x170)):
            scope = data[field]
            target = address + offset
            self.cpu.mem_write(target, self._interaction_scope_value(scope.get("root", {})))
            self.cpu.mem_write(target + 0x10, struct.pack("<i", scope.get("seed", -1)))
            values = scope.get("event_targets", {})
            entries = self._allocate(bytes(len(values) * 24))
            for index, (key, value) in enumerate(values.items()):
                self.cpu.mem_write(entries + index * 24, struct.pack("<II", names.index(key), 0)
                                   + self._interaction_scope_value(value))
            self.cpu.mem_write(target + 0x18, struct.pack("<QII", entries, len(values), len(values)))
        for key, offset in (("actor", 0x2d8), ("recipient", 0x2dc), ("secondary_actor", 0x2e0),
                            ("secondary_recipient", 0x2e4), ("intermediary", 0x2e8), ("puppet_or_actor", 0x2ec)):
            self.cpu.mem_write(address + offset, struct.pack("<I", data.get(key, 0xffffffff)))
        self.cpu.mem_write(address + 0x2f0, self._interaction_scope_value(data.get("target", {})))
        options = bytes(data.get("options", []))
        self.cpu.mem_write(address + 0x300, struct.pack("<QII", self._allocate(options), len(options), len(options)))
        empty_vt = {
            2: 0x46d4448, 3: 0x46d4368, 4: 0x46d43d8, 6: 0x44c9bf8, 28: 0x46d4528,
        }
        if kind in empty_vt:
            obj = self._allocate(struct.pack("<Q", self.base + empty_vt[kind]))
            self.cpu.mem_write(address + 0x330, struct.pack("<Q", obj))
            return
        if kind in (7, 8):
            payload = data[special_name]
            titles = payload.get("titles", [])
            t_ptr = self._allocate(b"".join(struct.pack("<I", item) for item in titles))
            obj = self._allocate(bytes(0x30))
            self.cpu.mem_write(obj, struct.pack("<QQII", self.base + 0x4535080, t_ptr, len(titles), len(titles)))
            if payload.get("government"):
                self.cpu.mem_write(obj + 0x20, struct.pack("<Q", self._named_reference(payload["government"])))
            self.cpu.mem_write(address + 0x330, struct.pack("<Q", obj))
            return
        if kind in (9, 34, 20, 21):
            payload = data[special_name]
            vt = 0x4535000 if kind in (9, 34) else 0x45610E8
            obj = self._allocate(struct.pack("<QI", self.base + vt, payload.get("title", 0xffffffff)))
            self.cpu.mem_write(address + 0x330, struct.pack("<Q", obj))
            return
        if kind in (27, 32, 33):
            payload = data[special_name]
            vt = 0x453D900 if kind == 27 else 0x453D970
            obj = self._allocate(struct.pack("<QI", self.base + vt, payload.get("active_council_task", 0xffffffff)))
            self.cpu.mem_write(address + 0x330, struct.pack("<Q", obj))
            return
        if kind in (36, 37):
            payload = data[special_name]
            vt = 0x459C3B8 if kind == 36 else 0x459C428
            ref = self._named_reference(payload["court_position"]) if payload.get("court_position") else 0
            obj = self._allocate(struct.pack("<QQ", self.base + vt, ref))
            self.cpu.mem_write(address + 0x330, struct.pack("<Q", obj))
            return
        if kind in (1, 35):
            payload = data[special_name]
            vt = 0x453AE30 if kind == 1 else 0x453AF20
            types = payload.get("types", [])
            levels = payload.get("level", [])
            t_refs = self._allocate(b"".join(struct.pack("<Q", self._database_reference(item)) for item in types))
            l_bytes = self._allocate(bytes(levels))
            obj = self._allocate(bytes(0x90))
            self.cpu.mem_write(obj, struct.pack("<QQII", self.base + vt, t_refs, len(types), len(types)))
            self.cpu.mem_write(obj + 0x70, struct.pack("<QII", l_bytes, len(levels), len(levels)))
            self.cpu.mem_write(address + 0x330, struct.pack("<Q", obj))
            return
        if kind == 38:
            payload = data[special_name]
            defenders = payload.get("defenders", [])
            d_ptr = self._allocate(b"".join(struct.pack("<I", item) for item in defenders))
            obj = self._allocate(bytes(0x30))
            self.cpu.mem_write(obj, struct.pack("<QIII", self.base + 0x4544318, payload["target"], payload["domain"], payload["recipient"]))
            self.cpu.mem_write(obj + 0x18, struct.pack("<QII", d_ptr, len(defenders), len(defenders)))
            self.cpu.mem_write(address + 0x330, struct.pack("<Q", obj))
            return
        if kind == 25:
            traits = data[special_name].get("traits", {})
            traditions = traits.get("traditions", [])
            tr_refs = self._allocate(b"".join(struct.pack("<Q", self._database_reference(item)) for item in traditions))
            pillar_ptrs = []
            for idx, (pkey, _) in enumerate(CULTURE_TRAIT_PILLAR_TOKENS):
                if pkey in traits:
                    pref = self._database_reference(traits[pkey], size=0x1540)
                    self.cpu.mem_write(pref + 0x1520, bytes([idx]))
                    pillar_ptrs.append(pref)
            p_refs = self._allocate(b"".join(struct.pack("<Q", p) for p in pillar_ptrs))
            obj = self._allocate(bytes(0xC0))
            self.cpu.mem_write(obj, struct.pack("<QQ", self.base + 0x4757A08, self.base + 0x4754AF0))
            self.cpu.mem_write(obj + 8 + 0x58, struct.pack("<QII", tr_refs, len(traditions), len(traditions)))
            self.cpu.mem_write(obj + 8 + 0x70, struct.pack("<QII", p_refs, len(pillar_ptrs), len(pillar_ptrs)))
            self.cpu.mem_write(address + 0x330, struct.pack("<Q", obj))
            return
        if kind != 13:
            self.cpu.mem_write(address + 0x330, bytes(8))
            return
        declaration = data["declare_war_interaction"]
        war = self._allocate(bytes(0x30))
        war_vtable = self.base + 0x4540228
        casus = self._allocate(bytes(0x40))
        self._string(casus + 0x18, declaration["casus_belli"])
        self.cpu.mem_write(casus + 0x38, struct.pack("<I", 0x4744624f))
        self.cpu.mem_write(war, struct.pack("<QQ", war_vtable, casus))
        titles = declaration.get("titles", [])
        title_array = self._allocate(b"".join(struct.pack("<I", title) for title in titles))
        self.cpu.mem_write(war + 0x10, struct.pack("<QII", title_array, len(titles), len(titles)))
        self.cpu.mem_write(war + 0x28, struct.pack("<I??", declaration.get("claimant", 0xffffffff),
                                               declaration.get("uses_hook", False), declaration.get("has_hook", False)))
        self.cpu.mem_write(address + 0x330, struct.pack("<Q", war))

    def _named_reference(self, name):
        item = self._allocate(bytes(0x40))
        self._string(item + 0x18, name)
        self.cpu.mem_write(item + 0x38, struct.pack("<I", 0x4744624F))
        return item

    def _string_pool(self, names):
        array = self._allocate(bytes(len(names) * 32))
        for index, key in enumerate(names):
            self._string(array + index * 32, key)
        pool = self.base + 0x5DD6570
        self.cpu.mem_write(pool, bytes(0x50))
        self.cpu.mem_write(pool + 0x30, struct.pack("<QII", array, len(names), len(names)))

    def _scope_types(self):
        types = self._allocate(bytes(7 * 80))
        self.cpu.mem_write(types + 80 + 4, struct.pack("<I", 0x500))
        self.cpu.mem_write(types + 160 + 4, struct.pack("<I", 0x2EA6))
        self.cpu.mem_write(types + 240 + 4, struct.pack("<I", 0x04FF))
        self.cpu.mem_write(types + 320 + 4, struct.pack("<I", 0x2EA7))
        self.cpu.mem_write(types + 400 + 4, struct.pack("<I", 0x3594))
        self.cpu.mem_write(types + 480 + 4, struct.pack("<I", 0x2B3F))
        self.cpu.mem_write(self.base + 0x5506B90, struct.pack("<QII", types, 7, 7))

    def _bucket_array(self, address, buckets, fmt):
        records = self._allocate(bytes(24 * len(buckets)))
        for index, group in enumerate(buckets):
            raw = b"".join(struct.pack(fmt, value) for value in group)
            ptr = self._allocate(raw) if raw else 0
            self.cpu.mem_write(records + index * 24, struct.pack("<QII", ptr, len(group), len(group)))
        self.cpu.mem_write(address, struct.pack("<QII", records, len(buckets), len(buckets)))

    def _activity_intent(self, address, intent):
        kind = intent.get("type")
        self.cpu.mem_write(address, struct.pack("<Q", self._named_reference(kind) if kind else 0))
        self.cpu.mem_write(address + 8, struct.pack("<I", intent.get("target", 0xFFFFFFFF)))

    def _travel_plan(self, address, plan, pool_names):
        from jomini_codec import TRAVEL_ON_ARRIVAL_DESTINATIONS

        self.cpu.mem_write(address, bytes(0x358))
        self.cpu.mem_write(address, struct.pack("<II", plan.get("owner", 0xFFFFFFFF), plan.get("travel_leader", 0xFFFFFFFF)))
        companions = plan.get("companions", [])
        if companions:
            comp_ptr = self._allocate(bytes(0x20 * len(companions)))
            sources = {"none": 0, "travel_leader": 1, "travel_option": 2, "activity": 3, "activity_option": 4}
            for index, item in enumerate(companions):
                entry = comp_ptr + index * 0x20
                self.cpu.mem_write(entry, struct.pack("<IiIiI", item["character"], item["date"],
                                                      0, item["location"], sources[item.get("source", "none")]))
                if item.get("travel_option"):
                    self.cpu.mem_write(entry + 0x18, struct.pack("<Q", self._named_reference(item["travel_option"])))
            self.cpu.mem_write(address + 8, struct.pack("<QII", comp_ptr, len(companions), len(companions)))
        self.cpu.mem_write(address + 0xE4, struct.pack("<II", plan.get("activity", 0xFFFFFFFF),
                                                       plan.get("task_contract", 0xFFFFFFFF)))
        options = plan.get("options", [])
        if options:
            opt_ptr = self._allocate(b"".join(struct.pack("<Q", self._database_reference(opt)) for opt in options))
            self.cpu.mem_write(address + 0xA8, struct.pack("<QII", opt_ptr, len(options), len(options)))
        self.cpu.mem_write(address + 0xD8, struct.pack("<Ii", plan["departure_location"], plan["departure_date"]))
        destinations = plan.get("destinations", [])
        if destinations:
            dst_ptr = self._allocate(b"".join(struct.pack("<i", value) for value in destinations))
            self.cpu.mem_write(address + 0x20, struct.pack("<QII", dst_ptr, len(destinations), len(destinations)))
        self._bucket_array(address + 0x50, plan.get("waypoints", []), "<i")
        modifiers = plan.get("modifier", [])
        if modifiers:
            mod_ptr = self._allocate(bytes(0x48 * len(modifiers)))
            for index, item in enumerate(modifiers):
                entry = mod_ptr + index * 0x48
                self.cpu.mem_write(entry, struct.pack("<Qi", self._database_reference(item["modifier"]), item["expiration_date"]))
                self.cpu.mem_write(entry + 0x30, struct.pack("<q", round(item.get("multiplier", 1.0) * 100000)))
            self.cpu.mem_write(address + 0xC0, struct.pack("<QII", mod_ptr, len(modifiers), len(modifiers)))
        for field, offset in (("on_start", 0xF0), ("on_travel_planner_cancel", 0x140), ("on_arrival", 0x190)):
            trigger = plan.get(field, {})
            self._string(address + offset, trigger.get("event", ""))
            self._string(address + offset + 0x20, trigger.get("on_action", ""))
        scope = plan.get("scope", {})
        target = address + 0x1E0
        self.cpu.mem_write(target, self._interaction_scope_value(scope.get("root", {})))
        self.cpu.mem_write(target + 0x10, struct.pack("<i", scope.get("seed", -1)))
        event_targets = scope.get("event_targets", {})
        if event_targets:
            entries = self._allocate(bytes(len(event_targets) * 24))
            for index, (key, value) in enumerate(event_targets.items()):
                self.cpu.mem_write(entries + index * 24, struct.pack("<II", pool_names.index(key), 0)
                                   + self._interaction_scope_value(value))
            self.cpu.mem_write(target + 0x18, struct.pack("<QII", entries, len(event_targets), len(event_targets)))
        arrival_modes = ("all", "first", "last", "all_but_last", "invalid")
        self.cpu.mem_write(address + 0x348, struct.pack("<Ii???",
            arrival_modes.index(plan.get("on_arrival_destinations", "all_but_last")),
            plan.get("max_path_duration", -1),
            plan.get("travel_with_domicile", False),
            plan.get("migration", False),
            plan.get("can_cancel_planning", True),
        ))
        self.cpu.mem_write(address + 0x354, struct.pack("<i", plan.get("path_duration", -1)))

    def _activity_plan(self, address, plan):
        self.cpu.mem_write(address, bytes(0x550))
        pool_names = []
        for cat, choice in plan.get("options", {}).items():
            for name in (cat, choice):
                if name not in pool_names:
                    pool_names.append(name)
        for phase in plan.get("phases", []):
            if phase["phase"] not in pool_names:
                pool_names.append(phase["phase"])
        for slot in plan.get("special_guests", {}):
            if slot not in pool_names:
                pool_names.append(slot)
        for name in plan.get("variables", {}):
            if name not in pool_names:
                pool_names.append(name)
        travel = plan.get("travel_plan")
        if travel:
            for name in travel.get("scope", {}).get("event_targets", {}):
                if name not in pool_names:
                    pool_names.append(name)
        self._scope_types()
        self._string_pool(pool_names)
        if plan.get("type"):
            self.cpu.mem_write(address, struct.pack("<Q", self._named_reference(plan["type"])))
        self.cpu.mem_write(address + 8, struct.pack("<I", plan.get("host", 0xFFFFFFFF)))
        self._activity_intent(address + 0x10, plan.get("intent", {}))
        self.cpu.mem_write(address + 0x20, struct.pack("<iIIii",
            plan["start_date"], 0, plan.get("guest_delay_days", 0), plan.get("invite_duration", 0), 0))
        for field, offset in (("tenets", 0x68), ("doctrines", 0x80)):
            values = plan.get(field, [])
            if values:
                ptr = self._allocate(b"".join(struct.pack("<Q", self._database_reference(item)) for item in values))
                self.cpu.mem_write(address + offset, struct.pack("<QII", ptr, len(values), len(values)))
        options = list(plan.get("options", {}).items())
        if options:
            opt_ptr = self._allocate(bytes(16 * len(options)))
            for index, (category, choice) in enumerate(options):
                cat_obj = self._allocate(bytes(0x10))
                opt_obj = self._allocate(bytes(0x10))
                self.cpu.mem_write(cat_obj + 8, struct.pack("<I", pool_names.index(category)))
                self.cpu.mem_write(opt_obj + 8, struct.pack("<I", pool_names.index(choice)))
                self.cpu.mem_write(opt_ptr + index * 16, struct.pack("<QQ", cat_obj, opt_obj))
            self.cpu.mem_write(address + 0x98, struct.pack("<QII", opt_ptr, len(options), len(options)))
        phases = plan.get("phases", [])
        if phases:
            phase_ptr = self._allocate(bytes(0x38 * len(phases)))
            for index, item in enumerate(phases):
                entry = phase_ptr + index * 0x38
                phase_type = self._allocate(bytes(0x10))
                self.cpu.mem_write(phase_type + 8, struct.pack("<I", pool_names.index(item["phase"])))
                self.cpu.mem_write(entry, struct.pack("<Qi", phase_type, item["province"]))
                subsets = item.get("guest_subsets", [])
                if subsets:
                    buckets = self._allocate(bytes(0x40 * (len(subsets) + 1)))
                    for s_idx, sub in enumerate(subsets):
                        slot = buckets + s_idx * 0x40
                        self.cpu.mem_write(slot + 4, b"\x01")
                        self._string(slot + 8, sub["key"])
                        guests = sub.get("guests", [])
                        g_ptr = self._allocate(b"".join(struct.pack("<I", g) for g in guests)) if guests else 0
                        self.cpu.mem_write(slot + 0x28, struct.pack("<QII", g_ptr, len(guests), len(guests)))
                    self.cpu.mem_write(buckets + len(subsets) * 0x40 + 4, b"\x01")
                    self.cpu.mem_write(entry + 0x18, struct.pack("<QIIB", buckets, len(subsets), len(subsets) - 1, 0))
                else:
                    buckets = self._allocate(bytes(0x80))
                    self.cpu.mem_write(buckets + 0x40 + 4, b"\x01")
                    self.cpu.mem_write(entry + 0x18, struct.pack("<QIIB", buckets, 0, 0, 0))
            self.cpu.mem_write(address + 0xB0, struct.pack("<QII", phase_ptr, len(phases), len(phases)))
        self._bucket_array(address + 0xC8, plan.get("invitations", []), "<I")
        guests = list(plan.get("special_guests", {}).items())
        if guests:
            sg_ptr = self._allocate(bytes(16 * len(guests)))
            for index, (slot, char_id) in enumerate(guests):
                slot_obj = self._allocate(bytes(0x10))
                self.cpu.mem_write(slot_obj + 8, struct.pack("<I", pool_names.index(slot)))
                self.cpu.mem_write(sg_ptr + index * 16, struct.pack("<QI", slot_obj, char_id))
            self.cpu.mem_write(address + 0x1B0, struct.pack("<QII", sg_ptr, len(guests), len(guests)))
        if travel is not None:
            self._travel_plan(address + 0x1C8, travel, pool_names)
            self.cpu.mem_write(address + 0x520, b"\x01")
        variables = list(plan.get("variables", {}).items())
        if variables:
            var_ptr = self._allocate(bytes(0x20 * (len(variables) + 1)))
            for index, (name, value) in enumerate(variables):
                slot = var_ptr + index * 0x20
                self.cpu.mem_write(slot + 4, b"\x01")
                self.cpu.mem_write(slot + 8, struct.pack("<I", pool_names.index(name)))
                self.cpu.mem_write(slot + 0x10, self._interaction_scope_value(value))
            self.cpu.mem_write(var_ptr + len(variables) * 0x20 + 4, b"\x01")
            self.cpu.mem_write(address + 0x530, struct.pack("<QIIB", var_ptr, len(variables), len(variables) - 1, 0))

    def encode(self, name, fields, header):
        schema = COMMAND_SCHEMAS[name]
        self.allocation = self.scratch + 0x2000
        command = self.scratch + 0x1000
        large_commands = ("send_character_interaction", "join_activity_command", "start_activity_command")
        self.cpu.mem_write(command, bytes(0x600 if name in large_commands else 0x180))
        self.cpu.mem_write(command, struct.pack("<Q", self.base + schema["vtable_rva"]))
        self.cpu.mem_write(command + 8, bytes([header.get("attribs", 8)]))
        self.cpu.mem_write(command + 0xC, struct.pack("<iiI", header["sender"], header["receiver"], header["identity"]))
        if name == "set_ready_command":
            self.cpu.mem_write(command + 0x18, struct.pack("<i?", fields["machineid"], fields["status"]))
        elif name == "set_character_controller":
            self.cpu.mem_write(command + 0x18, struct.pack(
                "<Ii?", fields["playable"], fields["machineid"], fields.get("leave_ai_on", False),
            ))
        elif name == "close_succession":
            self.cpu.mem_write(command + 0x20, struct.pack(
                "<Ii", fields.get("character", 0xFFFFFFFF), fields["machineid"],
            ))
        elif name == "sync_players_command":
            for field, offset in (("character", 0x18), ("machineid", 0x30), ("players", 0x48), ("flags", 0x60)):
                values = fields.get(field, [])
                if field == "players":
                    pointer = self._allocate(bytes(32 * len(values)))
                    for index, value in enumerate(values):
                        self._string(pointer + 32 * index, value)
                else:
                    raw = bytes(values) if field == "flags" else b"".join(struct.pack("<I", value) for value in values)
                    pointer = self._allocate(raw)
                self.cpu.mem_write(command + offset, struct.pack("<QII", pointer, len(values), len(values)))
        elif name == "add_player":
            self._string(command + 0x18, fields["name"])
            self.cpu.mem_write(command + 0x38, struct.pack("<i?", fields["machineid"], fields["is_host"]))
        elif name == "set_player_here_command":
            self.cpu.mem_write(command + 0x18, struct.pack("<i", fields["machineid"]))
        elif name == "set_observer_command":
            self.cpu.mem_write(command + 0x18, struct.pack("<i?", fields["machineid"], fields["flag"]))
        elif name == "add_perk":
            perk = self._allocate(bytes(0x40))
            self._string(perk + 0x18, fields["perk"])
            self.cpu.mem_write(perk + 0x38, struct.pack("<I", 0x4744624F))
            self.cpu.mem_write(command + 0x20, struct.pack("<I", fields["target"]))
            self.cpu.mem_write(command + 0x28, struct.pack("<Q", perk))
        elif name == "set_character_focus":
            self.cpu.mem_write(command + 0x20, struct.pack("<I", fields["target"]))
            self.cpu.mem_write(command + 0x28, struct.pack("<Q", self._database_reference(fields["focus_type"])))
            self.cpu.mem_write(command + 0x30, struct.pack("<I", fields["actor"]))
        elif name == "execute_decision":
            self.cpu.mem_write(command + 0x20, struct.pack(
                "<II", fields["character"], fields.get("puppeteer", 0xFFFFFFFF),
            ))
            self.cpu.mem_write(command + 0x28, struct.pack("<Q", self._database_reference(fields["decision"])))
            if "parameters" in fields:
                self.cpu.mem_write(command + 0x30, struct.pack("<Q", self._decision_parameters(fields["parameters"])))
        elif name == "select_event_option":
            self.cpu.mem_write(command + 0x20, struct.pack("<ii", fields["data"], fields["option"]))
        elif name == "reply_character_interaction":
            value = schema["enums"]["accept"][fields["accept"]]
            self.cpu.mem_write(command + 0x20, struct.pack("<Ii", fields["character_interaction"], value))
        elif name in ("join_activity_command", "decline_activity_invite_command"):
            self.cpu.mem_write(command + 0x20, struct.pack("<II", fields.get("activity", 0xffffffff),
                                                        fields.get("character", 0xffffffff)))
            if name == "join_activity_command":
                self.cpu.mem_write(command + 0x28, bytes([0 if fields.get("travel", "yes") == "yes" else 1]))
                self.cpu.mem_write(command + 0x390, bytes([fields.get("check_start_date", True)]))
                if "travel_plan_draft_created" in fields:
                    self._scope_types()
                    names = list(fields["travel_plan_draft_created"].get("scope", {}).get("event_targets", {}))
                    self._string_pool(names)
                    self._travel_plan(command + 0x30, fields["travel_plan_draft_created"], names)
                    self.cpu.mem_write(command + 0x388, b"\x01")
        elif name == "start_activity_command":
            self._activity_plan(command + 0x20, fields["data"])
        elif name == "set_activity_intent_command":
            self.cpu.mem_write(command + 0x20, struct.pack("<I", fields.get("character", 0xFFFFFFFF)))
            self._activity_intent(command + 0x28, fields["intent"])
        elif name == "send_character_interaction":
            self._interaction(command + 0x20, fields["character_interaction"])
            self.cpu.mem_write(command + 0x358, struct.pack("<qq", round(fields["ai_intermediary_accept"] * 100000),
                                                         round(fields["ai_accept"] * 100000)))
        elif name == "raise_troops":
            self.cpu.mem_write(command + 0x20, struct.pack("<I", fields["character"]))
            points = fields.get("rally_points", [])
            raw = b"".join(struct.pack("<ii", item["province"], item["color"]) for item in points)
            self.cpu.mem_write(command + 0x28, struct.pack("<QII", self._allocate(raw), len(points), len(points)))
            army_type = ("raid", "regular", "men_at_arms", "barter", "none").index(fields["raise_army_type"])
            self.cpu.mem_write(command + 0x40, struct.pack("<ii?", fields.get("rally_point", -1),
                                                        army_type, fields.get("supply_limit", False)))
        elif name == "move_unit":
            self.cpu.mem_write(command + 0x20, struct.pack("<IIiiii",
                ("none", "player", "ai").index(fields["valid_for"]), fields["unit"], fields["target"],
                ("set", "append", "invalid").index(fields["unit_order_type"]),
                ("land", "water", "any").index(fields["type"]),
                ("reset", "default").index(fields["gathering_army"])))
        elif name == "select_commander":
            self.cpu.mem_write(command + 0x20, struct.pack("<III",
                ("none", "player", "ai").index(fields["valid_for"]), fields["character"], fields["army"]))
        elif name in ("disband_army", "stop_gathering_army", "detach_commander"):
            self.cpu.mem_write(command + 0x20, struct.pack("<II",
                ("none", "player", "ai").index(fields["valid_for"]), fields["army"]))
        elif name == "hire_mercenary":
            self.cpu.mem_write(command + 0x20, struct.pack("<IIi", fields["character"],
                                                        fields["mercenary_company"], fields.get("flags", 3)))
        elif name == "extend_mercenary":
            flags = 1 if (not fields.get("cost") and not fields.get("war")) else (
                (1 if fields.get("cost") else 0) | (2 if fields.get("war") else 0)
            )
            self.cpu.mem_write(command + 0x20, struct.pack("<III", fields["character"],
                                                        fields["mercenary_company"], flags))
        elif name == "hire_holy_order":
            flags = 3 if (not fields.get("cost") and not fields.get("war")) else (
                (1 if fields.get("cost") else 0) | (2 if fields.get("war") else 0)
            )
            self.cpu.mem_write(command + 0x20, struct.pack("<III", fields["character"],
                                                        fields["holy_order"], flags))
        elif name == "stop":
            units = fields.get("units", [])
            raw = b"".join(struct.pack("<I", item) for item in units)
            self.cpu.mem_write(command + 0x20, struct.pack("<I", ("none", "player", "ai").index(fields["valid_for"])))
            self.cpu.mem_write(command + 0x28, struct.pack("<QII", self._allocate(raw), len(units), len(units)))
        elif name == "merge_units":
            units = fields.get("units", [])
            raw = b"".join(struct.pack("<I", item) for item in units)
            self.cpu.mem_write(command + 0x20, struct.pack("<II", ("none", "player", "ai").index(fields["valid_for"]),
                                                        fields["target"]))
            self.cpu.mem_write(command + 0x28, struct.pack("<QII", self._allocate(raw), len(units), len(units)))
        elif name == "split_half_army":
            self.cpu.mem_write(command + 0x20, struct.pack("<III", ("none", "player", "ai").index(fields["valid_for"]),
                                                        fields["actor"], fields["army"]))
        elif name in ("start_assault", "stop_assault"):
            self.cpu.mem_write(command + 0x20, struct.pack("<III", ("none", "player", "ai").index(fields["valid_for"]),
                                                        fields["character"], fields["siege"]))
        elif name == "change_council_task":
            self._scope_types()
            self.cpu.mem_write(command + 0x20, struct.pack("<I", fields.get("active_council_task", 0xFFFFFFFF)))
            self.cpu.mem_write(command + 0x28, struct.pack("<Q", self._database_reference(fields["task_type"])))
            self.cpu.mem_write(command + 0x30, struct.pack("<II", fields.get("character", 0xFFFFFFFF),
                                                        fields.get("liege", 0xFFFFFFFF)))
            self.cpu.mem_write(command + 0x38, self._interaction_scope_value(fields.get("target", {})))
        elif name == "fire_council_position":
            self.cpu.mem_write(command + 0x20, struct.pack("<III", fields["liege"], fields["character"], fields["position"]))
        elif name == "swap_council_position":
            self.cpu.mem_write(command + 0x20, struct.pack("<III", fields["liege"], fields["character"], fields["target"]))
        elif name in ("leave_council_position", "end_scheme", "toggle_scheme_agents_auto_assign",
                      "revoke_court_position", "cancel_travel_plan_command", "dismiss_important_action",
                      "accept_task_contract", "destroy_faction", "faction_press_demand"):
            key = schema["fields"][0][0]
            self.cpu.mem_write(command + 0x20, struct.pack("<I", fields.get(key, 0xFFFFFFFF)))
        elif name == "expose_secret":
            self.cpu.mem_write(command + 0x20, struct.pack("<II", fields["secret"], fields["character"]))
        elif name == "set_countermeasure":
            self.cpu.mem_write(command + 0x20, struct.pack("<I", fields.get("character", 0xFFFFFFFF)))
            self.cpu.mem_write(command + 0x28, struct.pack("<Q", self._named_reference(fields["scheme_countermeasure_type"])))
        elif name in ("create_title", "usurp_title", "set_primary_title"):
            self.cpu.mem_write(command + 0x20, struct.pack("<II", fields["character"], fields["landed_title"]))
        elif name == "destroy_title":
            self.cpu.mem_write(command + 0x20, struct.pack("<?", fields["cost"]))
            self.cpu.mem_write(command + 0x24, struct.pack("<II", fields["character"], fields["landed_title"]))
        elif name == "set_realm_capital":
            self.cpu.mem_write(command + 0x20, struct.pack("<?", fields["cooldown"]))
            self.cpu.mem_write(command + 0x24, struct.pack("<II", fields["character"], fields["landed_title"]))
        elif name in ("join_faction", "leave_faction"):
            self.cpu.mem_write(command + 0x20, struct.pack("<II", fields["faction"], fields["character"]))
        elif name == "give_court_position":
            self.cpu.mem_write(command + 0x20, struct.pack("<II", fields.get("employer", 0xFFFFFFFF),
                                                        fields.get("employee", 0xFFFFFFFF)))
            self.cpu.mem_write(command + 0x28, struct.pack("<Q", self._named_reference(fields["court_position"])))
        elif name == "add_law":
            self.cpu.mem_write(command + 0x20, struct.pack("<I", fields["character"]))
            self.cpu.mem_write(command + 0x28, struct.pack("<Q", self._database_reference(fields["law"])))
        elif name == "add_title_law":
            self.cpu.mem_write(command + 0x20, struct.pack("<IIi", fields["title"], fields["character"], fields["law"]))
        elif name == "clear_title_laws":
            self.cpu.mem_write(command + 0x20, struct.pack("<II", fields["title"], fields["character"]))
        elif name == "nominate_successor_for_title":
            self.cpu.mem_write(command + 0x20, struct.pack("<III?", fields["elector"], fields["title"],
                                                           fields["candidate"], fields.get("force", False)))
        elif name == "designate_diarch":
            self.cpu.mem_write(command + 0x20, struct.pack("<II", fields["liege"], fields["designated_diarch"]))
        elif name == "set_diarchy_mandate":
            self.cpu.mem_write(command + 0x20, struct.pack("<I", fields["liege"]))
            self.cpu.mem_write(command + 0x28, struct.pack("<Q", self._named_reference(fields["mandate"])))
        elif name == "create_maa_regiment":
            cost_byte = {"no": 0, "yes": 1}.get(fields.get("cost", "yes"), 2)
            self.cpu.mem_write(command + 0x20, struct.pack("<IIIii?", ("none", "player", "ai").index(fields["valid_for"]),
                                                           fields.get("title", 0xFFFFFFFF), fields["character"],
                                                           fields["regiment"], fields["count"], bool(cost_byte)))
        elif name == "add_maa_to_regiment":
            cost_byte = {"no": 0, "yes": 1}.get(fields.get("cost", "yes"), 2)
            self.cpu.mem_write(command + 0x20, struct.pack("<IIi?", ("none", "player", "ai").index(fields["valid_for"]),
                                                           fields["regiment"], fields["count"], bool(cost_byte)))
        elif name == "destroy_maa_regiment":
            self.cpu.mem_write(command + 0x20, struct.pack("<II", ("none", "player", "ai").index(fields["valid_for"]),
                                                           fields["regiment"]))
        elif name == "set_maa_reinforce":
            self.cpu.mem_write(command + 0x20, struct.pack("<II?", ("none", "player", "ai").index(fields["valid_for"]),
                                                           fields["character"], fields["reinforce"]))
        elif name == "reorganize_army":
            self.cpu.mem_write(command + 0x20, struct.pack("<III", ("none", "player", "ai").index(fields["valid_for"]),
                                                           fields["army"], fields.get("merge_army", 0xFFFFFFFF)))
            regs = fields.get("regiments", [])
            raw = b"".join(struct.pack("<I", item) for item in regs)
            self.cpu.mem_write(command + 0x30, struct.pack("<QII", self._allocate(raw), len(regs), len(regs)))
            self.cpu.mem_write(command + 0x48, struct.pack("<ii", fields["select_unit"], fields["disband_army"]))
        elif name == "split_army_for_siege":
            self.cpu.mem_write(command + 0x20, struct.pack("<IIIi", ("none", "player", "ai").index(fields["valid_for"]),
                                                           fields["actor"], fields["army"], fields["soldiers_to_stay"]))
        elif name == "attach_to_unit":
            self.cpu.mem_write(command + 0x20, struct.pack("<III", ("none", "player", "ai").index(fields["valid_for"]),
                                                           fields.get("unit", 0xFFFFFFFF), fields.get("to", 0xFFFFFFFF)))
        elif name == "detatch_from_unit":
            self.cpu.mem_write(command + 0x20, struct.pack("<II", ("none", "player", "ai").index(fields["valid_for"]),
                                                           fields.get("unit", 0xFFFFFFFF)))
        elif name in ("toggle_raiding", "raid"):
            self.cpu.mem_write(command + 0x20, struct.pack("<III", ("none", "player", "ai").index(fields["valid_for"]),
                                                           fields["character"], fields["army"]))
        elif name == "knight_status":
            status_idx = {"force": 0, "default": 1, "not": 2, "none": 3}[fields["knight_status"]]
            self.cpu.mem_write(command + 0x20, struct.pack("<II", fields["character"], status_idx))
        elif name in ("add_rally_point", "remove_rally_point"):
            self.cpu.mem_write(command + 0x20, struct.pack("<Ii", fields["character"], fields["target"]))
        elif name == "move_rally_point":
            self.cpu.mem_write(command + 0x20, struct.pack("<Iii", fields["character"], fields["rally_point"], fields["target"]))
        elif name == "mass_prisoner_action":
            prisoners = fields.get("prisoners", [])
            raw = b"".join(struct.pack("<I", item) for item in prisoners)
            self.cpu.mem_write(command + 0x20, struct.pack("<QII", self._allocate(raw), len(prisoners), len(prisoners)))
            idx = {"ransom_interaction": 10, "release_from_prison_interaction": 11, "execute_prisoner_interaction": 12}[fields["interaction"]]
            self.cpu.mem_write(command + 0x38, struct.pack("<IB", fields.get("actor", 0xFFFFFFFF), idx))
        elif name == "equip_artifact":
            self.cpu.mem_write(command + 0x20, struct.pack("<III", fields["character"], fields["artifact"], fields["replace"]))
        elif name == "unequip_artifact":
            self.cpu.mem_write(command + 0x20, struct.pack("<II", fields["character"], fields["artifact"]))
        elif name == "start_court_position_task":
            self.cpu.mem_write(command + 0x20, struct.pack("<I", fields.get("court_position", 0xFFFFFFFF)))
            self.cpu.mem_write(command + 0x28, struct.pack("<Q", self._named_reference(fields["task_type"])))
        elif name == "stop_court_position_task":
            self.cpu.mem_write(command + 0x20, struct.pack("<I", fields.get("court_position", 0xFFFFFFFF)))
        elif name == "select_culture_fascination":
            self.cpu.mem_write(command + 0x20, struct.pack("<IIi", fields.get("player", 0xFFFFFFFF),
                                                           fields.get("culture", 0xFFFFFFFF), fields["culture_innovation"]))
        elif name == "add_dynasty_perk":
            self.cpu.mem_write(command + 0x20, struct.pack("<I", fields["target"]))
            self.cpu.mem_write(command + 0x28, struct.pack("<Q", self._named_reference(fields["perk"])))
            if "trait" in fields:
                self.cpu.mem_write(command + 0x30, struct.pack("<Q", self._named_reference(fields["trait"])))
        elif name == "create_faction":
            self.cpu.mem_write(command + 0x20, struct.pack("<iII", fields["faction_type"], fields["character"], fields["target"]))
        elif name == "add_construction":
            self.cpu.mem_write(command + 0x20, struct.pack("<Iiii", fields["character"], fields["province"],
                                                        fields["index"], fields["type"]))
        elif name == "cancel_construction":
            self.cpu.mem_write(command + 0x20, struct.pack("<Ii", fields["character"], fields["province"]))
        elif name == "pause_game_command":
            self.cpu.mem_write(command + 0x20, struct.pack("<i?", fields["player"], fields["value"]))
        elif name == "set_speed":
            self.cpu.mem_write(command + 0x20, struct.pack("<i", fields["speed"]))
        elif name == "client_ping":
            self.cpu.mem_write(command + 0x18, struct.pack("<II", fields["turn"], fields["machineid"]))
        elif name.startswith("filetransfer_"):
            self._string(command + 0x18, fields["filename"])
            self.cpu.mem_write(command + 0x38, struct.pack("<Ii", fields["size"], fields["checksum"]))
            if name == "filetransfer_send_chunk_metas_command":
                for field, offset, tag in (("checksums", 0x40, "i"), ("sizes", 0x58, "I")):
                    values = fields.get(field, [])
                    raw = b"".join(struct.pack("<" + tag, value) for value in values)
                    self.cpu.mem_write(command + offset, struct.pack("<QII", self._allocate(raw), len(values), len(values)))
                self.cpu.mem_write(command + 0x70, struct.pack("<i", fields["machineid"]))
            if name == "filetransfer_chunk_recieved_command":
                self.cpu.mem_write(command + 0x40, struct.pack("<iiII", fields["machineid"], fields["chunk_id"],
                                                            fields["chunks_completed"], fields["chunks_total"]))
            if name == "filetransfer_fast_cmd_handshake":
                self.cpu.mem_write(command + 0x40, struct.pack("<I", fields["filetransfer_fast_fld_chunk_size"]))
            if "data" in fields:
                blob = bytes.fromhex(fields["data"]["hex"])
                self.cpu.mem_write(command + 0x40, struct.pack("<QII", self._allocate(blob), len(blob), len(blob)))
            if name == "filetransfer_request_chunk_command":
                self.cpu.mem_write(command + 0x40, struct.pack("<i", fields["chunk_id"]))
            elif "chunk_id" in fields and name != "filetransfer_chunk_recieved_command":
                self.cpu.mem_write(command + 0x58, struct.pack("<i", fields["chunk_id"]))
        else:
            raise ValueError("Native command fixture is not implemented")
        self.cpu.mem_write(self.writer, bytes(0x40))
        self.cpu.mem_write(self.writer + 8, struct.pack("<Q", self.scratch + 0x100))
        self.cpu.mem_write(self.writer + 0x14, b"\x01")
        self.output.clear()
        # Reproduce the dispatcher's type token + '=' followed by virtual Write.
        self._call(0x3F9B9D0, self.writer, schema["token"])
        self._call(0x3F9B9D0, self.writer, 1)
        self._call(0x3F90DC0, command, self.writer)
        return bytes(self.output)


def _special_interaction_sample(index):
    variants = [
        ("gift_artifact_interaction", {"target": {"type": "artifact", "identity": 300 + index}}, {}),
        ("diarch_legal_meddling_interaction", {"target": {"type": "lt", "identity": 300 + index}}, {}),
        ("send_poem_interaction", {"options": [True, False]}, {}),
        ("ransom_interaction", {"secondary_recipient": 120, "options": [False, False, True, False, False, False, False, False, False]}, {}),
        ("release_from_prison_interaction", {"options": [True, False, True]}, {}),
        ("execute_prisoner_interaction", {"options": [True, False]}, {}),
        ("grant_vassal_interaction", {"secondary_actor": 77}, {}),
        ("retract_vassal_interaction", {"secondary_recipient": 88, "options": [True]}, {}),
        ("feudalize_holding_interaction", {"target": {"type": "lt", "identity": 500 + index}}, {}),
        ("end_war_attacker_victory_interaction", {}, {"end_war_attacker_victory_interaction": {}}),
        ("end_war_attacker_white_peace_interaction", {}, {"end_war_white_peace_interaction": {}}),
        ("end_war_attacker_defeat_interaction", {}, {"end_war_attacker_defeat_interaction": {}}),
        ("force_vote_in_succession_election_interaction", {}, {"force_vote_in_succession_election": {}}),
        ("grant_titles_interaction", {"target": {"type": "lt", "identity": 401}},
         {"grant_titles_interaction": {"titles": [401, 402], "government": "feudal_government"}}),
        ("grant_governorship_interaction", {"target": {"type": "lt", "identity": 403}},
         {"grant_governorship_interaction": {"titles": [403]}}),
        ("revoke_title_interaction", {"target": {"type": "lt", "identity": 405}},
         {"revoke_title_interaction": {"title": 405}}),
        ("diarch_revoke_title_interaction", {"target": {"type": "lt", "identity": 406}},
         {"diarch_revoke_title": {"title": 406}}),
        ("create_claimant_faction_against_interaction", {},
         {"create_claimant_faction_against_interaction": {"title": 407}}),
        ("invite_to_council_position_interaction", {}, {"invite_to_council_interaction": {"active_council_task": 55}}),
        ("force_onto_council", {}, {"force_onto_council_interaction": {"active_council_task": 56}}),
        ("grant_court_position", {}, {"grant_court_position": {"court_position": "court_physician_court_position"}}),
        ("request_court_position", {}, {"request_court_position": {"court_position": "antiquarian_court_position"}}),
        ("liege_modify_vassal_contract_interaction", {},
         {"liege_modify_vassal_contract": {"types": ["feudal_government_taxes", "feudal_government_levies"], "level": [2, 1]}}),
        ("vassal_modify_vassal_contract_interaction", {},
         {"vassal_modify_vassal_contract": {"types": ["special_contract"], "level": [1]}}),
        ("request_culture_conversion_interaction", {},
         {"request_culture_conversion_interaction": {"traits": {
             "traditions": ["tradition_only_the_strong"], "ethos": "ethos_bellicose", "language": "language_norse"}}}),
    ]
    name, extra_top, special_block = variants[index % len(variants)]
    targets = {
        "actor": {"type": "char", "identity": 42},
        "recipient": {"type": "char", "identity": 99},
        "is_puppet_action": {"type": "boolean"},
    }
    if "target" in extra_top:
        targets["target"] = extra_top["target"]
    return {
        "character_interaction": {
            "character_interaction": name,
            "actor": 42,
            "recipient": 99,
            "puppet_or_actor": 42,
            "scope": {"root": {}, "seed": -1, "event_targets": targets},
            "original_scope": {"root": {}, "seed": -1},
            **extra_top,
            **special_block,
        },
        "ai_intermediary_accept": 100.0,
        "ai_accept": 25.0,
    }


def verify(game_dir=DEFAULT_GAME_DIR, cases=128):
    oracle = NativeSerializerOracle(Path(game_dir) / "binaries" / "ck3.exe")
    codec = JominiCodec()
    generator = random.Random(0xCAFE)
    comparisons = 0
    for index in range(cases):
        header = {
            "identity": generator.randrange(1 << 32),
            "sender": generator.randrange(-(1 << 31), 1 << 31),
            "receiver": generator.randrange(-(1 << 31), 1 << 31),
            "attribs": generator.randrange(256),
        }
        machine = generator.randrange(-(1 << 31), 1 << 31)
        commands = [
            ("sync_players_command", {
                "machineid": [generator.randrange(1 << 32) for _ in range(index % 6)],
                "players": ["local" if n & 1 else "synthetic-long-player-name" for n in range(index % 6)],
                "character": [generator.randrange(1 << 32) for _ in range(index % 6)],
                "flags": [generator.randrange(256) for _ in range(index % 6)],
            }),
            ("add_player", {"name": "local" if index & 1 else "synthetic-long-player-name",
                            "machineid": machine, "is_host": bool(index & 1)}),
            ("set_player_here_command", {"machineid": machine}),
            ("set_observer_command", {"machineid": machine, "flag": bool(index & 1)}),
            ("add_perk", {"target": generator.randrange(1 << 32),
                          "perk": "test_perk" if index & 1 else "synthetic_long_lifestyle_perk"}),
            ("set_character_focus", {
                "target": generator.randrange(1 << 32), "actor": generator.randrange(1 << 32),
                "focus_type": "" if index % 3 == 0 else ("test_focus" if index & 1 else "martial_strategy_focus"),
            }),
            ("execute_decision", {
                "character": generator.randrange(1 << 32),
                "puppeteer": generator.randrange(1 << 32) if index & 1 else 0xFFFFFFFF,
                "decision": "" if index % 3 == 0 else ("test_decision" if index & 1 else "hold_mystical_communion_decision"),
                **({"parameters": {} if index % 5 == 0 else {"event_targets": {
                    "test_flag": {"type": "boolean", "identity": 1},
                    "long_synthetic_parameter_flag": {"type": "boolean"},
                    "\u6d4b\u8bd5": {"type": "boolean", "identity": 1},
                }}} if index & 1 else {}),
            }),
            ("select_event_option", {"data": machine, "option": generator.randrange(-(1 << 31), 1 << 31)}),
            ("reply_character_interaction", {"character_interaction": generator.randrange(1 << 32),
                                             "accept": ("accept", "reject", "block", "acknowledge")[index % 4]}),
            ("join_activity_command", {
                "activity": generator.randrange(1 << 32) if index % 4 else 0xFFFFFFFF,
                "character": generator.randrange(1 << 32) if index % 3 else 0xFFFFFFFF,
                "travel": "yes" if index & 1 else "no", "check_start_date": bool(index % 3),
                **({"travel_plan_draft_created": {
                    "owner": 42, **({"travel_leader": 70} if index & 1 else {}),
                    "departure_date": 51307320 + index * 24, "departure_location": 1603,
                    **({"destinations": [1604]} if index & 1 else {}),
                    "waypoints": [[1603, 1604]] if index & 1 else [],
                    "on_arrival": {}, "on_start": {}, "on_travel_planner_cancel": {},
                    **({"on_arrival_destinations": "last"} if index & 1 else {}),
                    "scope": {"root": {"type": "char", "identity": 42}, "seed": index},
                }} if index % 2 == 0 else {}),
            }),
            ("decline_activity_invite_command", {
                "activity": generator.randrange(1 << 32) if index % 4 else 0xffffffff,
                "character": generator.randrange(1 << 32) if index % 3 else 0xffffffff}),
            ("set_activity_intent_command", {
                "character": generator.randrange(1 << 32) if index % 3 else 0xFFFFFFFF,
                "intent": {"type": "recreation_intent" if index & 1 else "murder_intent",
                           **({"target": 99} if index & 1 else {})},
            }),
            ("start_activity_command", {
                "data": {
                    "type": "activity_feast" if index & 1 else "activity_hunt",
                    "host": 42,
                    "intent": {"type": "recreation_intent", **({"target": 99} if index & 1 else {})},
                    "start_date": 51307320 + index * 24,
                    "guest_delay_days": 14 if index & 1 else 0,
                    "invite_duration": 30 if index & 1 else 0,
                    **({"options": {"feast_course": "feast_course_lavish"}} if index & 1 else {}),
                    "phases": [{
                        "phase": "feast_phase_meal", "province": 1603,
                        **({"guest_subsets": [{"key": "main_guests", "guests": [70, 99]}]} if index & 1 else {}),
                    }],
                    **({"special_guests": {"honorary_guest": 99}} if index & 1 else {}),
                    "invitations": [[70, 99], []] if index & 1 else [],
                    **({"travel_plan": {
                        "owner": 42, "departure_date": 51307320, "departure_location": 1603,
                        "destinations": [1604], "waypoints": [],
                        "companions": [{"character": 70, "date": 51307320, "location": 1603, "source": "travel_leader"}],
                        "modifier": [{"modifier": "travel_safety_modifier", "expiration_date": 51310000, "multiplier": 1.5}],
                        "on_arrival": {"event": "travel.0001"}, "on_start": {}, "on_travel_planner_cancel": {},
                        "scope": {"root": {}, "seed": -1},
                    }} if index % 3 == 0 else {}),
                    **({"variables": {"feast_var": {"type": "boolean", "identity": 1}}} if index % 4 == 0 else {}),
                    **({"tenets": ["tenet_carnal_exaltation"]} if index % 5 == 0 else {}),
                    **({"doctrines": ["doctrine_monogamy"]} if index % 6 == 0 else {}),
                },
            }),
            ("send_character_interaction", {
                "character_interaction": {
                    "character_interaction": "declare_war_interaction", "actor": 42, "recipient": 99,
                    "puppet_or_actor": 42,
                    "scope": {"root": {}, "seed": -1, "event_targets": {
                        "actor": {"type": "char", "identity": 42}, "recipient": {"type": "char", "identity": 99},
                        "long_synthetic_scope_name": {"type": "char", "identity": 0xffffffff},
                        "hook": {"type": "boolean"}, "target": {"type": "boolean", "identity": 1}}},
                    "original_scope": {"root": {}, "seed": -1},
                    "target": {"type": "boolean", "identity": 1},
                    **({"options": [bool(number & 1) for number in range(index % 4)]} if index % 4 else {}),
                    "declare_war_interaction": {
                        "casus_belli": "county_conquest_cb" if index & 1 else "debug_war",
                        **({"titles": [2997 + number for number in range(index % 3)]} if index % 3 else {}),
                        "claimant": 0xffffffff if index & 1 else 12345,
                        "uses_hook": bool(index & 1), "has_hook": bool(index & 1)},
                }, "ai_intermediary_accept": 100.0, "ai_accept": 1.0,
            }),
            ("send_character_interaction", _special_interaction_sample(index)),
            ("raise_troops", {"character": generator.randrange(1 << 32),
                              "rally_points": [{"province": 12786 + number, "color": number} for number in range(index % 4)],
                              "rally_point": 0 if index & 1 else -1,
                              "raise_army_type": ("raid", "regular", "men_at_arms", "barter", "none")[index % 5],
                              "supply_limit": bool(index & 1)}),
            ("move_unit", {"valid_for": ("none", "player", "ai")[index % 3],
                           "unit": generator.randrange(1 << 32), "target": 12786 + index,
                           "unit_order_type": ("set", "append", "invalid")[index % 3],
                           "type": ("land", "water", "any")[index % 3],
                           "gathering_army": "reset" if index & 1 else "default"}),
            ("disband_army", {"valid_for": "player", "army": generator.randrange(1 << 32)}),
            ("stop_gathering_army", {"valid_for": "player", "army": generator.randrange(1 << 32)}),
            ("select_commander", {"valid_for": ("none", "player", "ai")[index % 3],
                                  "character": generator.randrange(1 << 32), "army": generator.randrange(1 << 32)}),
            ("detach_commander", {"valid_for": ("none", "player", "ai")[index % 3],
                                  "army": generator.randrange(1 << 32)}),
            ("hire_mercenary", {"character": generator.randrange(1 << 32),
                                "mercenary_company": generator.randrange(1 << 32), "flags": index % 4}),
            ("extend_mercenary", {"character": generator.randrange(1 << 32),
                                  "mercenary_company": generator.randrange(1 << 32),
                                  "cost": (index % 3) == 2, "war": (index % 3) >= 1}),
            ("hire_holy_order", {"character": generator.randrange(1 << 32),
                                 "holy_order": generator.randrange(1 << 32),
                                 "cost": (index % 3) == 1, "war": (index % 3) == 2}),
            ("stop", {"valid_for": ("none", "player", "ai")[index % 3],
                      "units": [generator.randrange(1 << 32) for _ in range(index % 4)]}),
            ("merge_units", {"valid_for": ("none", "player", "ai")[index % 3],
                             "target": generator.randrange(1 << 32),
                             "units": [generator.randrange(1 << 32) for _ in range(index % 4)]}),
            ("split_half_army", {"valid_for": ("none", "player", "ai")[index % 3],
                                 "actor": generator.randrange(1 << 32), "army": generator.randrange(1 << 32)}),
            ("start_assault", {"valid_for": ("none", "player", "ai")[index % 3],
                               "siege": generator.randrange(1 << 32), "character": generator.randrange(1 << 32)}),
            ("stop_assault", {"valid_for": ("none", "player", "ai")[index % 3],
                              "siege": generator.randrange(1 << 32), "character": generator.randrange(1 << 32)}),
            ("change_council_task", {
                "character": generator.randrange(1 << 32) if index & 1 else 0xFFFFFFFF,
                "liege": generator.randrange(1 << 32) if index % 3 else 0xFFFFFFFF,
                "active_council_task": generator.randrange(1 << 32) if index % 4 else 0xFFFFFFFF,
                "task_type": "task_collect_taxes" if index & 1 else "task_fabricated_claim_on_county",
                "target": {"type": ("char", "prov", "lt")[index % 3],
                           "identity": generator.randrange(1, 5000)} if index % 4 else {},
            }),
            ("fire_council_position", {"liege": generator.randrange(1 << 32),
                                       "character": generator.randrange(1 << 32),
                                       "position": generator.randrange(1 << 32)}),
            ("swap_council_position", {"liege": generator.randrange(1 << 32),
                                       "character": generator.randrange(1 << 32),
                                       "target": generator.randrange(1 << 32)}),
            ("leave_council_position", {"character": generator.randrange(1 << 32)}),
            ("end_scheme", {"scheme": generator.randrange(1 << 32)}),
            ("expose_secret", {"secret": generator.randrange(1 << 32), "character": generator.randrange(1 << 32)}),
            ("set_countermeasure", {
                "character": generator.randrange(1 << 32) if index & 1 else 0xFFFFFFFF,
                "scheme_countermeasure_type": "redouble_guards" if index & 1 else "synthetic_long_countermeasure_name",
            }),
            ("toggle_scheme_agents_auto_assign", {
                "character": generator.randrange(1 << 32) if index % 3 else 0xFFFFFFFF,
            }),
            ("create_title", {"character": generator.randrange(1 << 32), "landed_title": generator.randrange(1 << 32)}),
            ("destroy_title", {"character": generator.randrange(1 << 32), "landed_title": generator.randrange(1 << 32),
                               "cost": bool(index & 1)}),
            ("usurp_title", {"character": generator.randrange(1 << 32), "landed_title": generator.randrange(1 << 32)}),
            ("set_primary_title", {"character": generator.randrange(1 << 32), "landed_title": generator.randrange(1 << 32)}),
            ("set_realm_capital", {"cooldown": bool(index & 1), "character": generator.randrange(1 << 32),
                                   "landed_title": generator.randrange(1 << 32)}),
            ("join_faction", {"faction": generator.randrange(1 << 32), "character": generator.randrange(1 << 32)}),
            ("leave_faction", {"faction": generator.randrange(1 << 32), "character": generator.randrange(1 << 32)}),
            ("destroy_faction", {"faction": generator.randrange(1 << 32)}),
            ("faction_press_demand", {"faction": generator.randrange(1 << 32)}),
            ("give_court_position", {
                "employer": generator.randrange(1 << 32) if index & 1 else 0xFFFFFFFF,
                "employee": generator.randrange(1 << 32) if index % 3 else 0xFFFFFFFF,
                "court_position": "court_physician_court_position" if index & 1 else "bodyguard_court_position",
            }),
            ("revoke_court_position", {"court_position": generator.randrange(1 << 32) if index % 3 else 0xFFFFFFFF}),
            ("cancel_travel_plan_command", {"travel_plan": generator.randrange(1 << 32) if index % 3 else 0xFFFFFFFF}),
            ("dismiss_important_action", {"important_action_item": generator.randrange(1 << 32) if index % 3 else 0xFFFFFFFF}),
            ("accept_task_contract", {"task_contract": generator.randrange(1 << 32) if index % 3 else 0xFFFFFFFF}),
            ("add_law", {"character": generator.randrange(1 << 32), "law": "crown_authority_2" if index & 1 else "feudal_elective_succession_law"}),
            ("add_title_law", {"title": generator.randrange(1 << 32), "character": generator.randrange(1 << 32), "law": index % 10}),
            ("clear_title_laws", {"title": generator.randrange(1 << 32), "character": generator.randrange(1 << 32)}),
            ("nominate_successor_for_title", {
                "elector": generator.randrange(1 << 32), "title": generator.randrange(1 << 32),
                "candidate": generator.randrange(1 << 32), "force": bool(index & 1),
            }),
            ("designate_diarch", {"liege": generator.randrange(1 << 32), "designated_diarch": generator.randrange(1 << 32)}),
            ("set_diarchy_mandate", {"liege": generator.randrange(1 << 32), "mandate": "fill_coffers" if index & 1 else "expand_realm"}),
            ("create_maa_regiment", {
                "valid_for": ("none", "player", "ai")[index % 3],
                "title": generator.randrange(1 << 32) if index & 1 else 0xFFFFFFFF,
                "character": generator.randrange(1 << 32), "regiment": index % 20, "count": 1 + (index % 5),
                "cost": "no" if index & 1 else "yes",
            }),
            ("add_maa_to_regiment", {
                "valid_for": ("none", "player", "ai")[index % 3],
                "regiment": generator.randrange(1 << 32), "count": 1 if index & 1 else -1,
                "cost": "no" if index % 3 == 0 else "yes",
            }),
            ("destroy_maa_regiment", {"valid_for": ("none", "player", "ai")[index % 3], "regiment": generator.randrange(1 << 32)}),
            ("set_maa_reinforce", {"valid_for": ("none", "player", "ai")[index % 3],
                                   "character": generator.randrange(1 << 32), "reinforce": bool(index & 1)}),
            ("reorganize_army", {
                "valid_for": ("none", "player", "ai")[index % 3],
                "army": generator.randrange(1 << 32),
                "merge_army": generator.randrange(1 << 32) if index & 1 else 0xFFFFFFFF,
                "select_unit": index % 3, "disband_army": 1 if index % 4 == 0 else 0,
                "regiments": [generator.randrange(1 << 32) for _ in range(index % 4)],
            }),
            ("split_army_for_siege", {"valid_for": ("none", "player", "ai")[index % 3],
                                      "actor": generator.randrange(1 << 32), "army": generator.randrange(1 << 32),
                                      "soldiers_to_stay": 200 + index * 50}),
            ("attach_to_unit", {"valid_for": ("none", "player", "ai")[index % 3],
                                "unit": generator.randrange(1 << 32) if index & 1 else 0xFFFFFFFF,
                                "to": generator.randrange(1 << 32) if index % 3 else 0xFFFFFFFF}),
            ("detatch_from_unit", {"valid_for": ("none", "player", "ai")[index % 3],
                                   "unit": generator.randrange(1 << 32) if index & 1 else 0xFFFFFFFF}),
            ("toggle_raiding", {"valid_for": ("none", "player", "ai")[index % 3],
                                "character": generator.randrange(1 << 32), "army": generator.randrange(1 << 32)}),
            ("raid", {"valid_for": ("none", "player", "ai")[index % 3],
                      "character": generator.randrange(1 << 32), "army": generator.randrange(1 << 32)}),
            ("knight_status", {"character": generator.randrange(1 << 32),
                               "knight_status": ("force", "default", "not", "none")[index % 4]}),
            ("add_rally_point", {"character": generator.randrange(1 << 32), "target": 1200 + index}),
            ("move_rally_point", {"character": generator.randrange(1 << 32), "rally_point": index % 3, "target": 1300 + index}),
            ("remove_rally_point", {"character": generator.randrange(1 << 32), "target": 1400 + index}),
            ("mass_prisoner_action", {
                "interaction": ("ransom_interaction", "release_from_prison_interaction", "execute_prisoner_interaction")[index % 3],
                "actor": generator.randrange(1 << 32) if index & 1 else 0xFFFFFFFF,
                "prisoners": [generator.randrange(1 << 32) for _ in range(index % 4)],
            }),
            ("equip_artifact", {"character": generator.randrange(1 << 32), "artifact": generator.randrange(1 << 32),
                                "replace": generator.randrange(1 << 32) if index & 1 else 0xFFFFFFFF}),
            ("unequip_artifact", {"character": generator.randrange(1 << 32), "artifact": generator.randrange(1 << 32)}),
            ("start_court_position_task", {
                "court_position": generator.randrange(1 << 32) if index & 1 else 0xFFFFFFFF,
                "task_type": "court_physician_prevent_disease_task" if index & 1 else "antiquarian_search_task",
            }),
            ("stop_court_position_task", {"court_position": generator.randrange(1 << 32) if index % 3 else 0xFFFFFFFF}),
            ("select_culture_fascination", {
                "player": generator.randrange(1 << 32) if index & 1 else 0xFFFFFFFF,
                "culture": generator.randrange(1 << 32) if index % 3 else 0xFFFFFFFF,
                "culture_innovation": index % 15,
            }),
            ("add_dynasty_perk", {
                "target": generator.randrange(1 << 32),
                "perk": "blood_legacy_1" if index & 1 else "kin_legacy_2",
                **({"trait": "giant" if index % 4 == 0 else "fecund"} if index % 2 == 0 else {}),
            }),
            ("create_faction", {"faction_type": index % 6, "character": generator.randrange(1 << 32), "target": generator.randrange(1 << 32)}),
            ("add_construction", {"character": generator.randrange(1 << 32), "province": 2482 + index,
                                  "index": index % 5 - 1, "type": index % 4 - 1}),
            ("cancel_construction", {"character": generator.randrange(1 << 32), "province": 2482 + index}),
            ("pause_game_command", {"player": machine, "value": bool(index & 1)}),
            ("close_succession", {"machineid": machine,
                                  **({"character": generator.randrange(1 << 32)} if index % 2 else {})}),
            ("set_speed", {"speed": machine}),
            ("client_ping", {"turn": generator.randrange(1 << 32), "machineid": generator.randrange(1 << 32)}),
            ("set_ready_command", {"machineid": machine, "status": bool(index & 1)}),
            ("set_character_controller", {
                "machineid": machine, "playable": generator.randrange(1 << 32),
                "leave_ai_on": bool(index & 1),
            }),
        ]
        transfer = {
            "filename": "world.ck3" if index & 1 else "synthetic-long-transfer-name.ck3",
            "size": generator.randrange(1 << 32),
            "checksum": generator.randrange(-(1 << 31), 1 << 31),
        }
        for name in (
            "filetransfer_init_command", "filetransfer_fast_cmd_request_handshake", "filetransfer_fast_cmd_initialize",
            "filetransfer_request_chunk_metas_command",
        ):
            commands.append((name, dict(transfer)))
        commands.append(("filetransfer_fast_cmd_handshake", {
            **transfer, "filetransfer_fast_fld_chunk_size": generator.randrange(1 << 32),
        }))
        blob = {"hex": generator.randbytes(index % 257).hex()}
        commands.append(("filetransfer_fast_cmd_chunk", {
            **transfer, "chunk_id": generator.randrange(-(1 << 31), 1 << 31), "data": blob,
        }))
        commands.append(("filetransfer_fast_cmd_chunkstatus", {**transfer, "data": blob}))
        commands.extend([
            ("filetransfer_send_chunk_metas_command", {
                **transfer, "checksums": [generator.randrange(-(1 << 31), 1 << 31) for _ in range(index % 6)],
                "sizes": [generator.randrange(1 << 32) for _ in range(index % 6)], "machineid": machine,
            }),
            ("filetransfer_request_chunk_command", {**transfer, "chunk_id": machine}),
            ("filetransfer_send_chunk_command", {**transfer, "chunk_id": machine, "data": blob}),
            ("filetransfer_chunk_recieved_command", {
                **transfer, "machineid": machine, "chunk_id": machine,
                "chunks_completed": generator.randrange(1 << 32), "chunks_total": generator.randrange(1 << 32),
            }),
        ])
        for name, fields in commands:
            expected = oracle.encode(name, fields, header)
            encoded = codec.encode_command(name, fields, header)
            if encoded != expected:
                raise AssertionError(f"Python/native serialization mismatch for {name}")
            decoded = codec.decode_command(expected)
            assert decoded["type"] == name
            assert decoded["default_state"] == header
            expected_fields = dict(fields)
            for field, _, _, default in COMMAND_SCHEMAS[name]["fields"]:
                if default is not None and expected_fields.get(field) == default:
                    expected_fields.pop(field, None)
            assert decoded["fields"] == expected_fields
            comparisons += 1
    return {"native_byte_comparisons": comparisons, "result": "passed", "live_ck3_execution": "pending"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, default=DEFAULT_GAME_DIR)
    parser.add_argument("--cases", type=int, default=128)
    args = parser.parse_args()
    if args.cases <= 0:
        parser.error("cases must be positive")
    print(json.dumps(verify(args.game_dir, args.cases)))


if __name__ == "__main__":
    main()
