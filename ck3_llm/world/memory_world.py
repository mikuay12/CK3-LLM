"""Version-bound semantic views over CK3's live process objects."""

import hashlib
import json
import math
from pathlib import Path
import struct
import time

from ck3_llm.protocol.jomini_codec import DEFAULT_TOKENS
from ck3_llm.engine.process_memory import ProcessMemory


DATABASES = {
    "character": (0x5c7c4f0, 0x18, 0x47748a0),
    "title": (0x5d32b08, 0x10, 0x4723198),
    "culture": (0x5d33300, 0x10, 0x474c960),
    "rite": (0x5d33308, 8, 0x4748f20),
    "faith": (0x5d33310, 8, 0x4740c00),
    "contract": (0x5d33b88, 8, 0x4746150),
    "war": (0x5d32e68, 8, 0x4743d48),
    "army": (0x5d32e58, 0x10, 0x4746d80),
    "regiment": (0x5d33b68, 0x10, 0x4755708),
    "unit": (0x5d33390, 0x10, 0x4744430),
    "army_regiment": (0x5d34328, 0x10, 0x4755918),
    "combat_result": (0x5d34fb8, 8, 0x4757750),
    "combat": (0x5d32e80, 8, 0x474def8),
    "siege": (0x5d33c88, 8, 0x474a5b8),
    "mercenary_company": (0x5d32f20, 0x10, 0x47699c8),
    "important_action_item": (0x5d34ee0, 0x10, 0x4756ff0),
    "pending_interaction": (0x5d33c78, 0x10, 0x4769590),
    "activity": (0x5d32c90, 8, 0x473ea08),
    "council_task": (0x5d32eb0, 0x10, 0x453d1b0),
    "faction": (0x5d32ea0, 0x10, 0x4754088),
    "scheme": (0x5d34c38, 0x10, 0x4789c90),
    "court_position": (0x5d32d20, 8, 0x473f400),
    "holy_order": (0x5d32f18, 0x10, 0x4754d28),
    "secret": (0x5d34c28, 0x10, 0x473f5b0),
    "task_contract": (0x5d34c30, 8, 0x4757f98),
}
SKILLS = ("diplomacy", "martial", "stewardship", "intrigue", "learning", "prowess")
TIERS = {"h": "hegemony", "e": "empire", "k": "kingdom", "d": "duchy", "c": "county", "b": "barony"}
NATIVE_TIERS = {1: "barony", 2: "county", 3: "duchy", 4: "kingdom", 5: "empire", 6: "hegemony"}
CROSSINGS = {0: "normal", 1: "strait", 2: "river", 3: "large_river", 4: "impassable"}
SIEGE_EFFECTS = ("breach", "starvation", "disease", "desertion", "stalemate")


def reference(value):
    return None if value == 0xffffffff else value


def integer(value, label, maximum=0xfffffffe):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"Invalid {label}")
    return value


def page(rows, request):
    offset = integer(request.get("offset", 0), "offset")
    limit = integer(request.get("limit", 20), "limit", 100)
    if limit < 1:
        raise ValueError("limit must be between 1 and 100")
    end = min(offset + limit, len(rows))
    return {"count": len(rows), "offset": offset, "next_offset": end if end < len(rows) else None,
            "items": rows[offset:end]}


class MemoryWorld:
    def __init__(self, pid=None, *, shadow_evaluator=None):
        self.memory = ProcessMemory(pid)
        self.base = self.memory.base
        self.tables = {}
        self.references = set()
        self.state = {}
        self.title_index = None
        self.primary_cache = {}
        self.summary_cache = {}
        self.province_index = None
        self.ruler_cache = {}
        self._scope_tokens = None
        self._world_identity = None
        self._last_clock = None
        self._map_data = None
        self._navigation_nodes = {}
        self.shadow_evaluator = shadow_evaluator
        self._shadow_auto_tried = shadow_evaluator is not None

    def get_shadow_evaluator(self):
        evaluator = getattr(self, "shadow_evaluator", None)
        if evaluator is not None:
            return evaluator
        if getattr(self, "_shadow_auto_tried", True):
            return None
        self._shadow_auto_tried = True
        executable = getattr(self.memory, "executable", None)
        if executable is not None and Path(executable).is_file():
            try:
                from ck3_llm.engine.shadow_evaluator import ShadowEvaluator
                self.shadow_evaluator = ShadowEvaluator(executable, memory_source=self.memory)
                state = getattr(self, "state", None)
                if isinstance(state, dict) and "tick" in state:
                    self._shadow_clock = {k: state.get(k) for k in ("world", "world_data", "date", "tick")}
            except (OSError, ValueError, RuntimeError):
                self.shadow_evaluator = None
        return self.shadow_evaluator

    def close(self):
        self.memory.close()

    def clock(self):
        memory = self.memory
        world = memory.pointer(self.base + 0x5c7dbe8, fresh=True)
        synchronization = memory.pointer(self.base + 0x5c7f410, fresh=True)
        if not world or not synchronization:
            raise ValueError("An active game is required")
        world_data = memory.pointer(world + 0xa0, fresh=True)
        if not world_data:
            raise ValueError("An active game is required")
        return {"world": world, "world_data": world_data,
                "date": memory.u32(world + 8, fresh=True),
                "tick": memory.u32(synchronization + 0x68, fresh=True)}

    def world_data(self):
        return self.memory.pointer(self.state["world"] + 0xa0)

    def players(self):
        memory = self.memory
        manager = memory.pointer(self.base + 0x5ccd808)
        if not manager:
            raise ValueError("The player manager is unavailable")
        count = memory.u32(manager + 0x1e4)
        if count > 64:
            raise ValueError("Invalid player count")
        array = memory.pointer(manager + 0x1d8)
        result = []
        for index in range(count):
            pointer = memory.pointer(array + index * 8)
            result.append({"name": memory.string(pointer + 8),
                           "character_id": reference(memory.u32(pointer + 0x58)),
                           "machineid": memory.u32(pointer + 0x70),
                           "reported_turn": memory.u32(pointer + 0x74),
                           "flags": memory.u8(pointer + 0x48)})
        return result

    def player_successions(self):
        """Read CPlayerInGame's persisted succession state, including offline players."""
        memory = self.memory
        result = []
        address = self.world_data() + 0x22340
        entries = self.vector(address, 8, fresh=True)
        for pointer in entries:
            raw = memory.read(pointer + 0xB0, 0x21A, fresh=True)
            if struct.unpack_from("<I", raw, 0x2C)[0] != 0x506C496E:
                raise ValueError("Unexpected player-in-game identity")
            result.append({
                "name": memory.string(pointer + 0xB8),
                "character_id": reference(struct.unpack_from("<I", raw)[0]),
                "machineid": struct.unpack_from("<i", raw, 0x28)[0],
                "has_open_succession": bool(raw[0x1B0]),
                "has_succession_data": bool(raw[0x218]),
            })
            if memory.read(pointer + 0xB0, 0x21A, fresh=True) != raw:
                raise ValueError("Player succession changed during reading")
        if self.vector(address, 8, fresh=True) != entries:
            raise ValueError("Player-in-game list changed during reading")
        return result

    def database(self, kind):
        if kind not in self.tables:
            global_rva, id_offset, vtable = DATABASES[kind]
            pointer = self.memory.pointer(self.base + global_rva)
            if not pointer:
                raise ValueError(f"{kind} objects are unavailable")
            length = self.memory.u32(pointer + 0x2c)
            slots = self.memory.pointer(pointer + 0x20)
            if length > 0x1000000 or (length and not slots):
                raise ValueError(f"Invalid {kind} object table")
            self.tables[kind] = (slots, length, id_offset, self.base + vtable)
        return self.tables[kind]

    def lookup(self, kind, entity_id):
        integer(entity_id, f"{kind} ID")
        slots, length, id_offset, vtable = self.database(kind)
        index = entity_id & 0xffffff
        if index >= length:
            raise ValueError(f"{kind} was not found")
        pointer = self.memory.pointer(slots + index * 16 + 8)
        if not pointer or self.memory.u32(pointer + id_offset) != entity_id or self.memory.pointer(pointer) != vtable:
            raise ValueError(f"{kind} was not found or its reference expired")
        self.references.add((kind, entity_id, pointer))
        return pointer

    def rows(self, kind):
        slots, length, id_offset, vtable = self.database(kind)
        result = []
        for index in range(length):
            pointer = self.memory.pointer(slots + index * 16 + 8)
            if not pointer:
                continue
            if self.memory.pointer(pointer) != vtable:
                raise ValueError(f"Unexpected {kind} object type")
            entity_id = self.memory.u32(pointer + id_offset)
            if entity_id & 0xffffff != index:
                raise ValueError(f"Invalid {kind} object identity")
            result.append((entity_id, pointer))
        return result

    def vector(self, address, width=4, *, fresh=False):
        memory = self.memory
        options = {"fresh": True} if fresh else {}
        count = memory.u32(address + 12, **options)
        capacity = memory.u32(address + 8, **options)
        pointer = memory.pointer(address, **options)
        if count > capacity or count * width > 1048576 or (count and not pointer):
            raise ValueError("Invalid object vector")
        data = memory.read(pointer, count * width, **options) if count else b""
        code = {1: "B", 4: "I", 8: "Q"}[width]
        return list(struct.unpack(f"<{count}{code}", data))

    def key(self, pointer):
        if not pointer or self.memory.u32(pointer + 0x38) != 0x4744624f:
            return None
        return self.memory.string(pointer + 0x18)

    def controller(self, entity_id):
        player = next((item for item in self.state["players"] if item["character_id"] == entity_id), None)
        return {"type": "player", "name": player["name"]} if player else {"type": "game_ai"}

    def character_name(self, entity_id):
        if entity_id is None:
            return None
        pointer = self.lookup("character", entity_id)
        return {"id": entity_id, "name": self.memory.string(pointer + 0x20)}

    def culture(self, entity_id):
        if entity_id == 0xffffffff:
            return None
        pointer = self.lookup("culture", entity_id)
        return {"id": entity_id, "name": self.memory.string(pointer + 0x30),
                "key": self.key(self.memory.pointer(pointer + 0x28))}

    def rite(self, entity_id):
        if entity_id == 0xffffffff:
            return None
        pointer = self.lookup("rite", entity_id)
        return {"id": entity_id, "key": self.key(self.memory.pointer(pointer + 0x4b0)) or self.memory.string(pointer + 0x518),
                "name": self.memory.string(pointer + 0x540),
                "faith_id": reference(self.memory.u32(pointer + 0x4b8)),
                "religious_head_id": reference(self.memory.u32(pointer + 0x4c0))}

    def faith(self, entity_id):
        if entity_id == 0xffffffff:
            return None
        memory = self.memory
        pointer = self.lookup("faith", entity_id)
        return {"id": entity_id, "key": memory.string(pointer + 0xe0),
                "name": memory.string(pointer + 0x108), "description": memory.string(pointer + 0x1c8),
                "rite_ids": self.vector(pointer + 0x20)}

    def character_faith(self, rite_id):
        rite = self.rite(rite_id)
        if rite is None:
            return None
        if rite["faith_id"] is None:
            return {"id": None, "key": None, "name": None, "rite": rite}
        faith = self.faith(rite["faith_id"])
        return {"id": faith["id"], "key": faith["key"], "name": faith["name"], "rite": rite}

    def traits(self, pointer):
        memory = self.memory
        database = memory.pointer(self.base + 0x5c7c4b0)
        count = memory.u32(database + 0x5c)
        entries = memory.pointer(database + 0x50)
        result = []
        for entity_id in self.vector(pointer + 0xf8):
            if entity_id >= count:
                raise ValueError("Trait ID is out of range")
            result.append({"id": entity_id, "key": self.key(memory.pointer(entries + entity_id * 8))})
        return result

    def title_pointer(self, value):
        if type(value) is int:
            return self.lookup("title", value)
        if not isinstance(value, str) or not value:
            raise ValueError("A title ID or script key is required")
        had_index = self.title_index is not None
        self.title_rows()
        entity_id = self.title_index[2].get(value)
        if entity_id is None and had_index:
            self.title_index = None
            self.title_rows()
            entity_id = self.title_index[2].get(value)
        if entity_id is not None:
            try:
                pointer = self.lookup("title", entity_id)
                if self.key(self.memory.pointer(pointer + 0x48)) == value:
                    return pointer
            except ValueError:
                self.title_index = None
                raise
            self.title_index = None
        raise ValueError("Title key was not found")

    def title_rows(self):
        database = self.memory.pointer(self.base + DATABASES["title"][0])
        signature = (self.world_data(), database, self.memory.pointer(database + 0x20),
                     self.memory.u32(database + 0x2c), self.memory.u32(database + 0x3c))
        if self.title_index is None or self.title_index[0] != signature:
            rows = [(entity_id, self.key(self.memory.pointer(pointer + 0x48))) for entity_id, pointer in self.rows("title")]
            self.title_index = (signature, rows, {key: entity_id for entity_id, key in rows if key is not None})
        return self.title_index[1]

    def title_province(self, title_id):
        memory = self.memory
        manager = self.world_data() + 0x118
        array = memory.pointer(manager + 0x28)
        count = memory.u32(manager + 0x34)
        signature = (self.world_data(), array, count)
        mapped = self.province_index[1].get(title_id) if self.province_index is not None else None
        valid = False
        if mapped is not None and self.province_index[0] == signature:
            pointer = memory.pointer(array + mapped * 8)
            valid = memory.u32(pointer + 0x738) == title_id
        if not valid:
            mapping = {}
            for province_id in range(1, count):
                pointer = memory.pointer(array + province_id * 8)
                title = reference(memory.u32(pointer + 0x738))
                if title is not None:
                    mapping[title] = province_id
            self.province_index = (signature, mapping)
        return self.province_index[1].get(title_id)

    def title_summary(self, pointer):
        if pointer in self.summary_cache:
            return self.summary_cache[pointer]
        memory = self.memory
        definition = memory.pointer(pointer + 0x48)
        key = self.key(definition)
        result = {"id": memory.u32(pointer + 0x10), "key": key,
                "tier": NATIVE_TIERS.get(memory.u32(definition + 0x64), "special"),
                "holder_id": reference(memory.u32(pointer + 0x128)),
                "de_facto_liege_id": reference(memory.u32(pointer + 0xe8)),
                "de_jure_liege_id": reference(memory.u32(pointer + 0x108))}
        self.summary_cache[pointer] = result
        return result

    def title(self, value, *, province_id=None):
        pointer = self.title_pointer(value)
        result = self.title_summary(pointer)
        result.update(holder=self.character_name(result["holder_id"]),
                      controller=self.controller(result["holder_id"]) if result["holder_id"] is not None else None,
                      capital_title_id=reference(self.memory.u32(pointer + 0xe4)),
                      de_jure_vassal_ids=self.vector(pointer + 0x110),
                      heir_ids=self.vector(pointer + 0x150),
                      laws=[self.key(value) for value in self.vector(pointer + 0x228, 8)],
                      lessee_id=reference(self.memory.u32(pointer + 0x12c)))
        if result["tier"] == "barony":
            result["province_id"] = province_id if province_id is not None else self.title_province(result["id"])
        return result

    def primary_title(self, pointer):
        if pointer in self.primary_cache:
            return self.primary_cache[pointer]
        landed = self.memory.pointer(pointer + 0x1c0)
        domain = self.vector(landed + 0x1e0) if landed else []
        result = self.title_summary(self.lookup("title", domain[0])) if domain else None
        self.primary_cache[pointer] = result
        return result

    def liege(self, pointer):
        primary = self.primary_title(pointer)
        if primary is None:
            return None
        entity_id = primary["de_facto_liege_id"]
        visited = {primary["id"]}
        character_id = self.memory.u32(pointer + 0x18)
        while entity_id is not None:
            if entity_id in visited:
                raise ValueError("Cyclic title hierarchy")
            visited.add(entity_id)
            title = self.title_summary(self.lookup("title", entity_id))
            if title["holder_id"] is not None and title["holder_id"] != character_id:
                return title["holder_id"]
            entity_id = title["de_facto_liege_id"]
        return None

    def top_ruler(self, entity_id):
        visited = []
        current = entity_id
        while current not in self.ruler_cache:
            if current in visited:
                raise ValueError("Cyclic character liege hierarchy")
            visited.append(current)
            liege = self.liege(self.lookup("character", current))
            if liege is None:
                break
            current = liege
        ruler = self.ruler_cache.get(current, current)
        self.ruler_cache.update((value, ruler) for value in visited)
        return ruler

    def realm_title_rows(self, ruler):
        """Follow current vassal contracts and domains within one sovereign realm."""
        if self.top_ruler(ruler) != ruler:
            return []
        pending = [ruler]
        visited = set()
        titles = set()
        while pending:
            character = pending.pop()
            if character in visited:
                continue
            visited.add(character)
            pointer = self.lookup("character", character)
            landed = self.memory.pointer(pointer + 0x1c0)
            if not landed:
                continue
            domain = self.vector(landed + 0x1e0)
            titles.update(domain)
            for title_id in domain:
                title_pointer = self.lookup("title", title_id)
                if self.title_summary(title_pointer)["tier"] == "county":
                    titles.update(self.vector(title_pointer + 0x110))
            for contract_id in self.vector(landed + 0x218):
                contract = self.contract(contract_id)
                if contract["liege_id"] == character and contract["vassal_id"] is not None:
                    pending.append(contract["vassal_id"])
        return [(entity_id, None) for entity_id in sorted(titles, key=lambda value: value & 0xffffff)]

    def realm_summary(self, character_id):
        if character_id is None:
            return None
        ruler = self.top_ruler(character_id)
        primary = self.primary_title(self.lookup("character", ruler))
        if primary is None:
            return None
        return {"ruler": self.character_name(ruler), "primary_title": primary,
                "controller": self.controller(ruler)}

    def title_realm(self, title):
        visited = set()
        while title["holder_id"] is None:
            liege = title["de_facto_liege_id"]
            if liege is None:
                return None
            if liege in visited:
                raise ValueError("Cyclic title hierarchy")
            visited.add(liege)
            title = self.title_summary(self.lookup("title", liege))
        return self.realm_summary(title["holder_id"])

    def realm_borders(self, value, include_straits=False):
        character = self.title_summary(self.title_pointer(value))["holder_id"] if isinstance(value, str) else value
        origin = self.realm_summary(character)
        if origin is None:
            raise ValueError("A landed character or held title is required for realm borders")
        ruler = origin["ruler"]["id"]
        provinces = {}
        for entity_id, key in self.realm_title_rows(ruler):
            title = self.title_summary(self.lookup("title", entity_id))
            if title["tier"] != "barony":
                continue
            realm = self.title_realm(title)
            if realm is None or realm["ruler"]["id"] != ruler:
                continue
            province_id = self.title_province(entity_id)
            if province_id is not None:
                provinces[province_id] = title
        borders = []
        for province_id, title in sorted(provinces.items()):
            for adjacency in self.adjacencies(province_id, include_straits):
                if adjacency["crossing"] == "impassable":
                    continue
                neighbor = adjacency["province_id"]
                if neighbor in provinces:
                    continue
                pointer = self.province_pointer(neighbor)
                title_id = reference(self.memory.u32(pointer + 0x738))
                if title_id is None:
                    continue
                other_title = self.title_summary(self.lookup("title", title_id))
                realm = self.title_realm(other_title)
                if realm is None or realm["ruler"]["id"] == ruler:
                    continue
                borders.append({"province_id": province_id, "title": title,
                                "neighbor_province_id": neighbor, "neighbor_title": other_title,
                                "realm": realm, "crossing": "land" if adjacency["crossing"] == "normal" else adjacency["crossing"],
                                "through_province_id": adjacency["through_province_id"]})
        return origin, borders

    def contract(self, entity_id):
        memory = self.memory
        pointer = self.lookup("contract", entity_id)
        vassal = memory.pointer(pointer + 0x20)
        liege = memory.pointer(pointer + 0x28)
        return {"id": entity_id, "vassal_id": memory.u32(vassal + 0x18) if vassal else None,
                "liege_id": memory.u32(liege + 0x18) if liege else None,
                "type": self.key(memory.pointer(pointer + 0xc8))}

    def character(self, entity_id, groups=None):
        memory = self.memory
        pointer = self.lookup("character", entity_id)
        alive = memory.pointer(pointer + 0x1b0)
        landed = memory.pointer(pointer + 0x1c0)
        family = memory.pointer(pointer + 0x1a8)
        court = memory.pointer(pointer + 0x1b8)
        result = {"id": entity_id}
        wanted = set(groups) if groups is not None else {
            "identity", "controller", "skills", "base_skills", "traits", "resources", "focus", "family", "court", "domain", "military"}
        if "identity" in wanted:
            birth = memory.u32(pointer + 0x60)
            dead = memory.pointer(pointer + 0x1d0) if not alive else 0
            death_date = memory.u32(dead + 4) if dead else None
            if hasattr(memory, "i16"):
                age, age_status = memory.i16(pointer + 0x68), "native_memory"
            elif hasattr(memory, "read"):
                age, age_status = struct.unpack("<h", memory.read(pointer + 0x68, 2))[0], "native_memory"
            else:
                age, age_status = None, "native_result_not_located"
            result["identity"] = {"name": memory.string(pointer + 0x20), "regnal_name": memory.string(pointer + 0x40),
                                  "birth": birth, "death_date": death_date,
                                  "age": age, "age_status": age_status,
                                  "female": bool(memory.u8(pointer + 0x1a1)), "alive": bool(alive),
                                  "culture": self.culture(memory.u32(pointer + 0xb0)),
                                  "faith": self.character_faith(memory.u32(pointer + 0xb4)),
                                  "dynasty_house_id": reference(memory.u32(pointer + 0x158))}
        if "controller" in wanted:
            result["controller"] = self.controller(entity_id) if alive else None
        for name, offset in (("skills", 0xd8), ("base_skills", 0xc0)):
            if name in wanted:
                result[name] = {key: memory.i32(pointer + offset + index * 4) for index, key in enumerate(SKILLS)}
        if "traits" in wanted:
            result["traits"] = self.traits(pointer)
        if "resources" in wanted:
            values = {"gold": 0x100, "piety": 0x110, "prestige": 0x130, "influence": 0x150,
                      "piety_accumulated": 0x118, "prestige_accumulated": 0x138,
                      "income": 0x2b0, "health": 0x2e8, "fertility": 0x2d8}
            result["resources"] = {key: memory.fixed(alive + offset) for key, offset in values.items()} if alive else None
            if alive:
                result["resources"]["stress"] = memory.i32(alive + 0x2f8)
        if "focus" in wanted:
            focus = memory.pointer(alive + 0x280) if alive else 0
            result["focus"] = {"key": self.key(memory.pointer(focus + 8))} if focus else None
        if "family" in wanted:
            fields = {"father_id": 0, "mother_id": 4, "spouse_id": 0x14, "betrothed_id": 0x10}
            result["family"] = {key: reference(memory.u32(family + offset)) for key, offset in fields.items()} if family else None
            if family:
                result["family"].update(child_ids=self.vector(family + 0x38), spouse_ids=self.vector(family + 0x20))
        if "court" in wanted:
            fields = {"employer_id": 0xc8, "host_id": 0xcc, "army_id": 0xf4}
            result["court"] = {key: reference(memory.u32(court + offset)) for key, offset in fields.items()} if court else None
            if court:
                result["court"]["knight"] = bool(memory.u8(court + 0x16c))
        if "domain" in wanted:
            result["domain"] = None
            if landed:
                result["domain"] = {"government": self.key(memory.pointer(landed + 0x3f8)),
                                    "primary_title": self.primary_title(pointer), "liege_id": self.liege(pointer),
                                    "title_ids": self.vector(landed + 0x1e0),
                                    "capital_title_id": reference(memory.u32(landed + 0x1f8)),
                                    "domain_limit": memory.i32(landed + 0x3d8), "vassal_limit": memory.i32(landed + 0x3dc),
                                    "vassals_towards_limit": memory.i32(landed + 0x3e0),
                                    "contract_ids": self.vector(landed + 0x218), "succession_ids": self.vector(landed + 0x3a0)}
                result["domain"]["laws"] = [self.key(value) for value in self.vector(landed + 0x200, 8)]
        if "military" in wanted:
            fields = {"current_strength": 0x2f0, "strength": 0x2f4, "levy": 0x2f8,
                      "strength_for_liege": 0x2fc, "strength_without_hires": 0x300}
            result["military"] = {key: memory.i32(landed + offset) for key, offset in fields.items()} if landed else None
            if landed:
                result["military"].update(war_ids=self.vector(landed + 0x318),
                                          unit_ids=self.vector(landed + 0x278),
                                          dread=memory.fixed(landed + 0x350), tyranny=memory.fixed(landed + 0x358))
        return result

    def war_side(self, pointer):
        memory = self.memory
        participants = []
        for member in self.vector(pointer + 8, 8):
            entity_id = reference(memory.u32(member + 8))
            participants.append({"character": self.character_name(entity_id),
                                 "joined": memory.u32(member + 0xc), "casualties": memory.u32(member + 0x20)})
        return {"participants": participants, "ticking_war_score": memory.fixed(pointer + 0x20),
                "controls_all_targets": bool(memory.u8(pointer + 0x58))}

    def war(self, entity_id):
        memory = self.memory
        pointer = self.lookup("war", entity_id)
        casus = pointer + 0xf8
        return {"id": entity_id, "attacker": self.war_side(pointer + 0x20),
                "defender": self.war_side(pointer + 0x80), "start_date": memory.u32(pointer + 0xe0),
                "casus_belli": self.key(memory.pointer(casus + 8)),
                "target_title_ids": self.vector(casus + 0x178),
                "claimant_id": reference(memory.u32(casus + 0x198)),
                "resolving": bool(memory.u8(pointer + 0x358))}

    def regiment(self, entity_id):
        memory = self.memory
        pointer = self.lookup("regiment", entity_id)
        chunks = [(memory.u32(pointer + 0x18 + index * 0x24), memory.u32(pointer + 0x1c + index * 0x24))
                  for index in range(7)]
        current = maximum = None
        strength_status = "native_result_not_located"
        evaluator = self.get_shadow_evaluator()
        if evaluator is not None:
            try:
                strength = evaluator.evaluate_regiment_strength(pointer)
                current, maximum = strength["current"], strength["maximum"]
                strength_status = strength["source"]
            except (OSError, ValueError, RuntimeError):
                pass
        return {"id": entity_id, "type": self.key(memory.pointer(pointer + 0x118)),
                "current": current, "maximum": maximum, "strength_status": strength_status,
                "strength_chunks": [{"maximum": maximum, "current": current} for maximum, current in chunks],
                "owner_id": reference(memory.u32(pointer + 0x12c)),
                "owning_title_id": reference(memory.u32(pointer + 0x130)),
                "controlling_title_id": reference(memory.u32(pointer + 0x134)),
                "war_id": reference(memory.u32(pointer + 0x13c))}

    def combat_result_side(self, pointer):
        memory = self.memory
        if memory.pointer(pointer) != self.base + 0x4757718:
            raise ValueError("Unexpected combat side result")
        initial = memory.fixed(pointer + 0x10)
        surviving = memory.fixed(pointer + 0x18)
        casualties = None
        casualties_status = "native_result_not_located"
        evaluator = self.get_shadow_evaluator()
        if evaluator is not None:
            try:
                native = evaluator.evaluate_combat_casualties(pointer)
                casualties = native["casualties"]
                casualties_status = native["source"]
            except (OSError, ValueError, RuntimeError):
                pass
        return {"main_participant_id": reference(memory.u32(pointer + 0xc)),
                "commander_id": reference(memory.u32(pointer + 8)),
                "initial_soldiers": initial, "surviving_soldiers": surviving,
                "casualties": casualties, "casualties_status": casualties_status,
                "participant_ids": self.vector(pointer + 0x20)}

    def combat_result(self, entity_id, character=None):
        memory = self.memory
        pointer = self.lookup("combat_result", entity_id)
        province = memory.pointer(pointer + 0x18)
        winning_side = {0: "attacker", 1: "defender"}.get(memory.u32(pointer + 0x20))
        result = {"id": entity_id, "province_id": memory.u32(province + 0x10) if province else None,
                  "winning_side": winning_side, "start_date": memory.u32(pointer + 0x2c),
                  "end_date": memory.u32(pointer + 0x34), "wiped": bool(memory.u8(pointer + 0x28)),
                  "attacker": self.combat_result_side(pointer + 0xe8),
                  "defender": self.combat_result_side(pointer + 0x138),
                  "interested_character_ids": self.vector(pointer + 0xb8)}
        if character is not None:
            side = next((name for name in ("attacker", "defender")
                         if character == result[name]["main_participant_id"] or character in result[name]["participant_ids"]), None)
            result["outcome"] = ("victory" if side == winning_side else "defeat") if side and winning_side else None
        return result

    def unit(self, entity_id):
        memory = self.memory
        pointer = self.lookup("unit", entity_id)
        location = memory.pointer(pointer + 0x20)
        # Path entries reference graph nodes. Their first DWORD is the province ID.
        path = [memory.u32(value) for value in self.vector(pointer + 0x38, 8)]
        return {"id": entity_id, "type": {0: "army", 1: "fleet"}.get(memory.u32(pointer + 0x18), "unknown"),
                "owner_id": reference(memory.u32(pointer + 0x174)),
                "province_id": memory.u32(location + 0x10) if location else None,
                "army_id": reference(memory.u32(pointer + 0x178)),
                "path": path,
                "movement_progress": memory.fixed(pointer + 0x168),
                "retreating": memory.u32(pointer + 0x170) != 0,
                "arrival_date": memory.u32(pointer + 0x180) if path else None}

    def navigation_unit(self, entity_id):
        unit = self.unit(entity_id)
        pointer = self.lookup("unit", entity_id)
        # CUnit's tooltip divides edge cost and progress by this cached speed.
        speed = self.memory.fixed(pointer + 0x190)
        unit["cached_movement_speed"] = speed if speed > 0 else None
        return unit

    def map_data(self):
        if getattr(self, "_map_data", None) is None:
            from ck3_llm.world.army_navigation import InstalledMap
            self._map_data = InstalledMap(self.memory.executable)
        return self._map_data

    def navigation_node(self, province_id):
        if province_id not in self._navigation_nodes:
            data = self.map_data().province(province_id)
            definition = self.memory.pointer(self.province_pointer(province_id) + 8)
            if not definition or self.memory.u32(definition + 0x24) != province_id:
                raise ValueError("Province map definition changed")
            self._navigation_nodes[province_id] = {
                **data, "passability_source": "installed_map_definition"}
        return {**self._navigation_nodes[province_id],
                "edges": self.adjacencies(province_id, True, include_cost=True)}

    def unit_movement(self, entity_id):
        from ck3_llm.world.army_navigation import movement_details
        unit = self.navigation_unit(entity_id)
        node = self.navigation_node(unit["province_id"])
        movement = movement_details(unit, node["edges"], lock_threshold=self.movement_lock_threshold())
        movement["direction"] = None
        movement["direction_status"] = "native_result_not_located"
        evaluator = self.get_shadow_evaluator()
        if evaluator is not None:
            try:
                pointer = self.lookup("unit", entity_id)
                native = evaluator.evaluate_unit_movement(pointer)
                movement["leg_movement_cost"] = native["leg_movement_cost"]
                movement["leg_progress_fraction"] = native["leg_progress_fraction"]
                movement["movement_locked"] = native["movement_locked"]
                movement["estimated_next_arrival_days"] = native["estimated_next_arrival_days"]
                movement["derived_fields_status"] = native["source"]
            except (OSError, ValueError, RuntimeError):
                pass
            if unit["path"] and node.get("position") is not None:
                try:
                    next_province = unit["path"][0]
                    target_pos = self.map_data().province(next_province).get("position")
                    if target_pos is not None:
                        dx = target_pos["x"] - node["position"]["x"]
                        dz = target_pos["z"] - node["position"]["z"]
                        angle = math.degrees(math.atan2(dz, dx))
                        cardinal = ("E", "NE", "N", "NW", "W", "SW", "S", "SE")[int((angle + 360 + 22.5) // 45) % 8]
                        movement["direction"] = {
                            "to_province_id": next_province,
                            "dx": round(dx, 2),
                            "dz": round(dz, 2),
                            "cardinal": cardinal,
                        }
                        movement["direction_status"] = "native_map_coordinates"
                except (OSError, ValueError):
                    pass
        return {**unit, "map": {key: value for key, value in node.items() if key != "edges"},
                "movement": movement}

    def movement_lock_threshold(self):
        threshold = self.memory.fixed(self.base + 0x5c7eb08)
        if not 0 <= threshold <= 1:
            raise ValueError("Invalid native movement lock threshold")
        return threshold

    def map_route(self, request):
        from ck3_llm.world.army_navigation import compass_direction, shortest_routes

        origin = integer(request.get("origin"), "origin province ID")
        target = integer(request.get("target"), "target province ID")
        origin_data = self.navigation_node(origin)
        target_data = self.map_data().province(target)
        origin_pos = origin_data.get("position")
        target_pos = target_data.get("position")
        evaluator = self.get_shadow_evaluator()
        if evaluator is None and not request.get("compute"):
            return {"origin_province_id": origin, "target_province_id": target,
                    "status": "unavailable", "reason": "native_result_not_located", "reachable": None,
                    "straight_line_map_distance": None, "distance_unit": "map_units", "route": None,
                    "origin_position": origin_pos, "target_position": target_pos,
                    "origin_edges": origin_data["edges"], "route_basis": "native_memory_fields",
                    "native_route_source": "unit_movement.path", "timing": "unavailable"}
        include_straits = bool(request.get("include_straits", True))
        max_nodes = int(request.get("max_nodes", 20000))
        routes = shortest_routes(self.navigation_node, origin, [target],
                                 include_straits=include_straits, max_nodes=max_nodes)
        distance = (round(math.hypot(target_pos["x"] - origin_pos["x"], target_pos["z"] - origin_pos["z"]), 2)
                    if origin_pos and target_pos else None)
        direction = compass_direction(origin_pos, target_pos)
        found = routes.get(target)
        if not found:
            return {"origin_province_id": origin, "target_province_id": target,
                    "status": "graph_shortest_path", "reachable": False, "hops": None,
                    "total_movement_cost": None, "straight_line_map_distance": distance,
                    "distance_unit": "map_units", "direction": direction,
                    "crossings": {}, "route": None, "estimated_days": None,
                    "origin_position": origin_pos, "target_position": target_pos,
                    "route_basis": "native_adjacency_graph", "timing": "unavailable"}
        crossings = {}
        for seg in found["segments"]:
            kind = seg.get("crossing")
            if kind and kind != "normal":
                crossings[kind] = crossings.get(kind, 0) + 1
        speed = request.get("speed")
        if speed is None and request.get("unit_id") is not None:
            try:
                speed = self.navigation_unit(request["unit_id"]).get("cached_movement_speed")
            except (OSError, ValueError):
                speed = None
        estimated_days = (math.ceil(found["movement_cost"] / speed)
                          if isinstance(speed, (int, float)) and speed > 0 else None)
        return {"origin_province_id": origin, "target_province_id": target,
                "status": "graph_shortest_path", "reachable": True,
                "hops": len(found["province_ids"]) - 1,
                "total_movement_cost": round(found["movement_cost"], 2),
                "straight_line_map_distance": distance, "distance_unit": "map_units",
                "direction": direction, "crossings": crossings,
                "route": found["province_ids"], "estimated_days": estimated_days,
                "origin_position": origin_pos, "target_position": target_pos,
                "route_basis": "native_adjacency_graph",
                "timing": "cached_speed_estimate" if estimated_days is not None else "unavailable"}

    def combat_side(self, pointer):
        memory = self.memory
        if memory.pointer(pointer) != self.base + 0x4756988:
            raise ValueError("Unexpected combat side")
        army_ids = self.vector(pointer + 0x10)
        owners = []
        for army_id in army_ids:
            army = self.lookup("army", army_id)
            unit_id = reference(memory.u32(army + 0x124))
            if unit_id is not None:
                owner = self.unit(unit_id)["owner_id"]
                if owner is not None and owner not in owners:
                    owners.append(owner)
        return {"army_ids": army_ids, "owner_ids": owners,
                "leader_id": reference(memory.u32(pointer + 0x70)),
                "commander_id": reference(memory.u32(pointer + 0x74)),
                "fighting_soldiers": memory.fixed(pointer + 0x98),
                "initial_soldiers": memory.fixed(pointer + 0xa8),
                "initial_levies": memory.fixed(pointer + 0xb0),
                "retreat_disabled": bool(memory.u8(pointer + 0xc0)),
                "early_retreat_allowed": bool(memory.u8(pointer + 0xc1))}

    def siege_context(self, entity_id, *, army_owners=None):
        memory = self.memory
        pointer = self.lookup("siege", entity_id)
        province_pointer = memory.pointer(pointer + 0x200)
        if not province_pointer:
            raise ValueError("The siege province is unavailable")
        title_id = reference(memory.u32(province_pointer + 0x738))
        defender_id = (reference(memory.u32(self.lookup("title", title_id) + 0x128))
                       if title_id is not None else None)
        army_id = reference(memory.u32(pointer + 0x208))
        attacker_id = None
        if army_owners is not None:
            attacker_id = army_owners.get(army_id)
        elif army_id is not None:
            army = self.lookup("army", army_id)
            unit_id = reference(memory.u32(army + 0x124))
            if unit_id is not None:
                unit = self.lookup("unit", unit_id)
                attacker_id = reference(memory.u32(unit + 0x174))
        return {"id": entity_id, "province_id": memory.u32(province_pointer + 0x10),
                "owner_army_id": army_id, "attacker": {"id": attacker_id} if attacker_id is not None else None,
                "defender": {"id": defender_id} if defender_id is not None else None}

    def siege(self, entity_id):
        memory = self.memory
        context = self.siege_context(entity_id)
        pointer = self.lookup("siege", entity_id)
        province_pointer = memory.pointer(pointer + 0x200)
        province = self.province(context["province_id"])
        title = province["title"]
        result = {**context, "title": title,
                  "attacker": self.character_name((context["attacker"] or {}).get("id")),
                  "defender": self.character_name((context["defender"] or {}).get("id")), "defending_realm": province["realm"],
                  "progress_points": memory.fixed(pointer + 0x3d0),
                  "progress": self.siege_progress(pointer, province_pointer),
                  "phase_days_remaining": memory.i32(pointer + 0x43c),
                  "start_date": memory.u32(pointer + 0x444),
                  "effects": {key: memory.u32(pointer + 0x3d8 + index * 4) for index, key in enumerate(SIEGE_EFFECTS)}}
        evaluator = self.get_shadow_evaluator()
        if evaluator is not None:
            try:
                commander_id = 0xFFFFFFFF
                if context.get("owner_army_id") is not None:
                    army_ptr = self.lookup("army", context["owner_army_id"])
                    commander_id = memory.u32(army_ptr + 0x120)
                result["projection"] = evaluator.evaluate_siege_projection(
                    pointer,
                    commander_id=commander_id,
                    current_date=self.state.get("date"),
                )
            except (OSError, ValueError, RuntimeError):
                pass
        return result

    def fixed_raw(self, address):
        return struct.unpack("<q", self.memory.read(address, 8))[0]

    def modifier_raw(self, pointer, modifier):
        """Read the native aggregate modifier table used by unfiltered lookups."""
        count = self.memory.i32(pointer + 0x74)
        if not 0 <= count <= 65535:
            raise ValueError("Invalid aggregate modifier count")
        keys = self.memory.pointer(pointer + 0x68)
        values = self.memory.pointer(pointer + 0xd0)
        if count and (not keys or not values):
            raise ValueError("Aggregate modifier data is unavailable")
        identifiers = struct.unpack(f"<{count}H", self.memory.read(keys, count * 2)) if count else ()
        if modifier not in identifiers:
            return 0
        return self.fixed_raw(values + identifiers.index(modifier) * 8)

    def siege_progress(self, pointer, province):
        memory = self.memory
        garrison_id = reference(memory.u32(province + 0x70c))
        current = 0
        garrison_capacity = None
        if garrison_id is not None:
            garrison = self.lookup("regiment", garrison_id)
            current = memory.i32(garrison + 0x1c)
            if memory.u32(garrison + 0x30) == 3 and current == 0:
                current = memory.i32(garrison + 0x18)
            evaluator = self.get_shadow_evaluator()
            if evaluator is not None:
                garrison_capacity = memory.u32(garrison + 0x128)
        else:
            evaluator = self.get_shadow_evaluator()
        if evaluator is not None:
            try:
                native = evaluator.evaluate_siege_progress(pointer)
                return {"status": native["source"], "percent": native["percent"],
                        "points_required": native["points_required"], "garrison_current": current,
                        "garrison_base": memory.i32(province + 0x730), "garrison_capacity": garrison_capacity,
                        "basis": native["source"]}
            except (OSError, ValueError, RuntimeError):
                pass
        return {"status": "unavailable", "reason": "native_result_not_located",
                "percent": None, "points_required": None, "garrison_current": current,
                "garrison_base": memory.i32(province + 0x730), "garrison_capacity": None,
                "basis": "native_memory_fields"}

    def siege_side(self, siege, character_id):
        if (siege["attacker"] or {}).get("id") == character_id:
            return "attacker"
        defender = (siege["defender"] or {}).get("id")
        visited = set()
        while defender is not None:
            if defender in visited:
                raise ValueError("Cyclic siege defender liege hierarchy")
            if defender == character_id:
                return "defender"
            visited.add(defender)
            defender = self.liege(self.lookup("character", defender))
        return None

    def combat(self, entity_id):
        memory = self.memory
        pointer = self.lookup("combat", entity_id)
        location = memory.pointer(pointer + 0x6b8)
        return {"id": entity_id, "province_id": memory.u32(location + 0x10) if location else None,
                "phase": {0: "maneuver", 1: "main", 2: "pursuit"}.get(memory.u32(pointer + 0x6b0), "unknown"),
                "phase_days": memory.u32(pointer + 0x6b4),
                "attacker_advantage": memory.fixed(pointer + 0x6c8),
                "combat_width": memory.u32(pointer + 0x6c4),
                "attacker": self.combat_side(pointer + 0x20),
                "defender": self.combat_side(pointer + 0x368)}

    def scope_value(self, pointer):
        memory = self.memory
        index, auxiliary, unused, value = struct.unpack("<HHIQ", memory.read(pointer, 16))
        if index == 0:
            return None
        registry = self.base + 0x5506b90
        count = memory.u32(registry + 12)
        if count > 4096 or index >= count:
            raise ValueError("Invalid notification scope type")
        if self._scope_tokens is None:
            self._scope_tokens = json.loads(DEFAULT_TOKENS.read_text(encoding="utf-8"))["tokens"]
        token = memory.u32(memory.pointer(registry) + index * 80 + 4)
        kind = self._scope_tokens.get(hex(token), "unknown")
        result = {"type": kind}
        if kind == "boolean":
            result["value"] = bool(value)
        elif kind in ("char", "lt", "prov", "culture", "faith", "rite", "war", "army", "artifact") and value <= 0xffffffff:
            result["id"] = reference(value)
            if kind == "char" and result["id"] is not None:
                result.update(self.character_name(value))
            if kind == "lt" and result["id"] is not None:
                result["title"] = self.title_summary(self.lookup("title", value))
        elif kind in ("value", "fixed_point", "number"):
            result["value"] = struct.unpack("<q", struct.pack("<Q", value))[0] / 100000
        else:
            result["resolved"] = False
        return result

    def scope_targets(self, pointer):
        memory = self.memory
        count = memory.u32(pointer + 12)
        capacity = memory.u32(pointer + 8)
        entries = memory.pointer(pointer)
        if count > capacity or count > 1024 or (count and not entries):
            raise ValueError("Invalid notification event targets")
        if not count:
            return {}
        names = self.base + 0x5dd6570
        name_count = memory.u32(names + 0x3c)
        name_array = memory.pointer(names + 0x30)
        if not name_array:
            raise ValueError("Notification target names are unavailable")
        targets = {}
        for index in range(count):
            entry = entries + index * 24
            name_id = memory.u32(entry)
            if name_id >= name_count:
                raise ValueError("Notification target name expired")
            name = memory.string(name_array + name_id * 32)
            targets[name] = self.scope_value(entry + 8)
        return targets

    def scope_context(self, pointer):
        return {"root": self.scope_value(pointer), "targets": self.scope_targets(pointer + 0x18)}

    def important_action(self, entity_id):
        memory = self.memory
        pointer = self.lookup("important_action_item", entity_id)
        return {"id": entity_id, "type": self.key(memory.pointer(pointer + 0x18)),
                "character_id": reference(memory.u32(pointer + 0x24)),
                "visible": bool(memory.u8(pointer + 0x190)),
                "scope": self.scope_context(pointer + 0x28)}

    def player_messages(self, character_id):
        memory = self.memory
        self.lookup("character", character_id)
        manager = self.world_data() + 0x222e8
        messages = []
        occurrences = {}
        for player in self.vector(manager + 0x58, 8):
            if memory.pointer(player) != self.base + 0x473cdd8:
                raise ValueError("Unexpected player notification owner")
            if memory.u32(player + 0xb0) != character_id:
                continue
            count, capacity = memory.u32(player + 0x254), memory.u32(player + 0x250)
            if count > capacity or count > 10000:
                raise ValueError("Invalid retained player message count")
            first = memory.pointer(player + 0x248)
            if count and not first:
                raise ValueError("Retained player messages are unavailable")
            for index in range(count):
                pointer = first + index * 0x11a0
                message = {"type": self.key(memory.pointer(pointer)), "character_id": character_id,
                           "filter": self.key(memory.pointer(pointer + 8)),
                           "left": self.scope_value(pointer + 0x18), "right": self.scope_value(pointer + 0x28),
                           "targets": self.scope_targets(pointer + 0x38),
                           "title": memory.string(pointer + 0x1128), "description": memory.string(pointer + 0x1148),
                           "created_date": memory.u32(pointer + 0x1170), "arrival_date": memory.u32(pointer + 0x1178),
                           "source": "native_player_message_queue"}
                signature = json.dumps(message, sort_keys=True, ensure_ascii=False).encode("utf-8")
                fingerprint = hashlib.sha256(signature).hexdigest()[:24]
                occurrences[fingerprint] = occurrences.get(fingerprint, 0) + 1
                message["id"] = f"{fingerprint}:{occurrences[fingerprint]}"
                messages.append(message)
        return messages

    def mercenary_company(self, entity_id):
        memory = self.memory
        pointer = self.lookup("mercenary_company", entity_id)
        regiments = [self.regiment(value) for value in self.vector(pointer + 0x30)]
        county_id = reference(memory.u32(pointer + 0x24))
        county = self.title_summary(self.lookup("title", county_id)) if county_id is not None else None
        current = maximum = None
        strength_status = "native_result_not_located"
        evaluator = self.get_shadow_evaluator()
        if evaluator is not None:
            try:
                strength = evaluator.evaluate_mercenary_strength(pointer)
                current, maximum = strength["current"], strength["maximum"]
                strength_status = strength["source"]
            except (OSError, ValueError, RuntimeError):
                pass
        return {"id": entity_id, "culture_id": reference(memory.u32(pointer + 0x20)),
                "home_county": county, "home_realm": self.title_realm(county) if county else None,
                "title_id": reference(memory.u32(pointer + 0x2c)),
                "hired_by_id": reference(memory.u32(pointer + 0x48)),
                "hired_until": memory.u32(pointer + 0x4c) if memory.u32(pointer + 0x48) != 0xffffffff else None,
                "current": current, "maximum": maximum, "strength_status": strength_status,
                "regiments": regiments}

    def army_regiment(self, entity_id):
        memory = self.memory
        pointer = self.lookup("army_regiment", entity_id)
        return {"id": entity_id, "type": self.key(memory.pointer(pointer + 0x18)),
                "current": memory.u32(pointer + 0x38), "maximum": memory.u32(pointer + 0x3c),
                "army_id": reference(memory.u32(pointer + 0x140)),
                "vassal_id": reference(memory.u32(pointer + 0x144)),
                "knight_id": reference(memory.u32(pointer + 0x148))}

    def army(self, entity_id):
        memory = self.memory
        pointer = self.lookup("army", entity_id)
        unit_id = reference(memory.u32(pointer + 0x124))
        regiments = [self.army_regiment(value) for value in self.vector(pointer + 0x38)]
        current = maximum = None
        strength_status = "native_result_not_located"
        if self.get_shadow_evaluator() is not None:
            current = sum(item["current"] for item in regiments if item.get("current") is not None)
            maximum = sum(item["maximum"] for item in regiments if item.get("maximum") is not None)
            strength_status = "native_army_regiment_sum"
        return {"id": entity_id, "commander_id": reference(memory.u32(pointer + 0x120)),
                "unit": self.unit(unit_id) if unit_id is not None else None,
                "current": current, "maximum": maximum, "strength_status": strength_status,
                "regiments": regiments,
                "supply": memory.fixed(pointer + 0x180), "raiding": bool(memory.u8(pointer + 0x1d4)),
                "raid_gold": memory.fixed(pointer + 0x1e0)}

    def council_task(self, entity_id):
        memory = self.memory
        pointer = self.lookup("council_task", entity_id)
        owner_id = reference(memory.u32(pointer + 0x40))
        court_owner_id = reference(memory.u32(pointer + 0x44))
        last_appointed_id = reference(memory.u32(pointer + 0x30))
        next_forced_id = reference(memory.u32(pointer + 0x34))
        return {"id": entity_id, "type": self.key(memory.pointer(pointer + 0x18)),
                "owner_id": owner_id, "owner": self.character_name(owner_id),
                "court_owner_id": court_owner_id, "court_owner": self.character_name(court_owner_id),
                "target": self.scope_value(pointer + 0x48),
                "progress": memory.fixed(pointer + 0x20),
                "last_councillor_change": memory.u32(pointer + 0x28),
                "last_appointed_councillor_id": last_appointed_id,
                "next_forced_councillor_id": next_forced_id,
                "frozen": bool(memory.u8(pointer + 0x39))}

    def _struct_vector_ids(self, address, stride, id_offset=8):
        memory = self.memory
        count = memory.u32(address + 12)
        capacity = memory.u32(address + 8)
        pointer = memory.pointer(address)
        if count > capacity or count * stride > 1048576 or (count and not pointer):
            raise ValueError("Invalid object vector")
        raw = memory.read(pointer, count * stride) if count else b""
        return [struct.unpack_from("<I", raw, index * stride + id_offset)[0] for index in range(count)]

    def faction(self, entity_id):
        memory = self.memory
        pointer = self.lookup("faction", entity_id)
        target_id = reference(memory.u32(pointer + 0x40))
        leader_id = reference(memory.u32(pointer + 0x44))
        return {"id": entity_id, "type": self.key(memory.pointer(pointer + 0x20)),
                "target_id": target_id, "target": self.character_name(target_id),
                "leader_id": leader_id, "leader": self.character_name(leader_id),
                "discontent": memory.fixed(pointer + 0x28),
                "power": memory.fixed(pointer + 0x30),
                "power_threshold": memory.fixed(pointer + 0x38),
                "member_ids": self._struct_vector_ids(pointer + 0x48, 0x20, 8),
                "title_member_ids": self._struct_vector_ids(pointer + 0x60, 0x18, 8),
                "created_date": memory.u32(pointer + 0x7c),
                "max_demand_date": memory.u32(pointer + 0x84),
                "war_id": reference(memory.u32(pointer + 0x8c))}

    def scheme(self, entity_id):
        memory = self.memory
        pointer = self.lookup("scheme", entity_id)
        status = {0: "continue", 1: "invalidated"}.get(memory.u32(pointer + 0x28), "invalid")
        owner_id = reference(memory.u32(pointer + 0x2c))
        target_type = {0: "character", 1: "title", 2: "culture", 3: "faith", 4: "nothing"}.get(
            memory.u32(pointer + 0x30), "unknown")
        target_id = reference(memory.u32(pointer + 0x34))
        exposed = bool(memory.u8(pointer + 0x27c))
        return {"id": entity_id, "type": self.key(memory.pointer(pointer + 0x20)),
                "status": status, "owner_id": owner_id, "owner": self.character_name(owner_id),
                "target_type": target_type, "target_id": target_id,
                "contract_id": reference(memory.u32(pointer + 0x3c)),
                "artifact_id": reference(memory.u32(pointer + 0x40)),
                "progress": memory.i32(pointer + 0x78),
                "secrecy": memory.i32(pointer + 0x280),
                "opportunities": memory.i32(pointer + 0x298),
                "start_date": memory.u32(pointer + 0x258),
                "exposed": exposed,
                "exposed_date": memory.u32(pointer + 0x268) if exposed else None}

    def court_position(self, entity_id):
        memory = self.memory
        pointer = self.lookup("court_position", entity_id)
        employer_id = reference(memory.u32(pointer + 0x120))
        employee_id = reference(memory.u32(pointer + 0x124))
        return {"id": entity_id,
                "type": self.key(memory.pointer(pointer + 0x110)),
                "task_type": self.key(memory.pointer(pointer + 0x118)),
                "employer_id": employer_id, "employer": self.character_name(employer_id),
                "employee_id": employee_id, "employee": self.character_name(employee_id),
                "hire_date": memory.u32(pointer + 0x128)}

    def holy_order(self, entity_id):
        memory = self.memory
        pointer = self.lookup("holy_order", entity_id)
        founder_id = reference(memory.u32(pointer + 0x40))
        hired_by_id = reference(memory.u32(pointer + 0x80))
        return {"id": entity_id,
                "type": self.key(memory.pointer(pointer + 0x30)),
                "tenet": self.key(memory.pointer(pointer + 0x38)),
                "faith_id": reference(memory.u32(pointer + 0x20)),
                "rite_id": reference(memory.u32(pointer + 0x24)),
                "title_id": reference(memory.u32(pointer + 0x28)),
                "founder_id": founder_id, "founder": self.character_name(founder_id),
                "hired_by_id": hired_by_id, "hired_by": self.character_name(hired_by_id),
                "worldliness": memory.fixed(pointer + 0x48),
                "leased_title_ids": self.vector(pointer + 0x50),
                "regiment_ids": self.vector(pointer + 0x68),
                "army_regiment_ids": self.vector(pointer + 0x88)}

    def secret(self, entity_id):
        memory = self.memory
        pointer = self.lookup("secret", entity_id)
        owner_id = reference(memory.u32(pointer + 0x40))
        return {"id": entity_id,
                "type": self.key(memory.pointer(pointer + 0x20)),
                "relation_type": self.key(memory.pointer(pointer + 0x28)),
                "target": self.scope_value(pointer + 0x30),
                "owner_id": owner_id, "owner": self.character_name(owner_id),
                "exposed": bool(memory.u8(pointer + 0xa8)),
                "delete": bool(memory.u8(pointer + 0xa9)),
                "participant_ids": self.vector(pointer + 0x60),
                "spent_by_ids": self.vector(pointer + 0x78),
                "cannot_expose_ids": self.vector(pointer + 0x90)}

    def task_contract(self, entity_id):
        memory = self.memory
        pointer = self.lookup("task_contract", entity_id)
        location = memory.pointer(pointer + 0x28)
        destination = memory.pointer(pointer + 0x30)
        employer_id = reference(memory.u32(pointer + 0x38))
        owner_id = reference(memory.u32(pointer + 0x50))
        acceptance_raw = memory.u32(pointer + 0x78)
        completion_raw = memory.u32(pointer + 0x80)
        statuses = {0: "not_available", 1: "available", 2: "accepted", 3: "completed", 4: "invalidated"}
        return {"id": entity_id,
                "type": self.key(memory.pointer(pointer + 0x40)),
                "status": statuses.get(memory.u32(pointer + 0x54), "not_available"),
                "tier": memory.u32(pointer + 0x4c),
                "name": memory.string(pointer + 0x58),
                "employer_id": employer_id, "employer": self.character_name(employer_id),
                "owner_id": owner_id, "owner": self.character_name(owner_id),
                "target_id": reference(memory.u32(pointer + 0x48)),
                "location_province_id": reference(memory.u32(location + 0x10)) if location else None,
                "destination_province_id": (reference(memory.u32(destination + 0x10))
                                            if destination and memory.u32(destination + 0x85c) == 0x50726f76 else None),
                "acceptance_date": acceptance_raw if acceptance_raw >= 5002 * 8760 else None,
                "completion_date": completion_raw if completion_raw >= 5002 * 8760 else None,
                "completed_contract_reward_desc": memory.string(pointer + 0x88)}

    def relations(self, entity_id, other=None):
        self.lookup("character", entity_id)
        memory = self.memory
        manager = self.world_data() + 0xa510
        result = []
        for pointer in self.vector(manager + 0x20, 8):
            first, second = memory.u32(pointer + 8), memory.u32(pointer + 0xc)
            if entity_id not in (first, second) or (other is not None and other not in (first, second)):
                continue
            related = second if first == entity_id else first
            result.append({"character": self.character_name(related), "war_id": reference(memory.u32(pointer + 0x20)),
                           "alliance_count": memory.u32(pointer + 0x94),
                           "broken_alliance": bool(memory.u8(pointer + 0x1a9))})
        return result

    def opinions(self, target_id, owner_id=None):
        memory = self.memory
        target = self.lookup("character", target_id)
        alive = memory.pointer(target + 0x1b0)
        if not alive:
            return []
        result = []
        for pointer in self.vector(alive + 0x38, 8):
            if memory.u8(pointer + 0x4a):
                continue
            owner = memory.pointer(pointer + 0x38)
            owner_reference = memory.u32(owner + 0x18)
            if owner_id is not None and owner_reference != owner_id:
                continue
            if memory.pointer(pointer + 0x40) != target:
                raise ValueError("Opinion target reference changed")
            modifiers = []
            for modifier in self.vector(pointer + 8, 8):
                modifiers.append({"key": self.key(memory.pointer(modifier + 8)),
                                  "value": memory.i32(modifier + 0x24),
                                  "start_date": memory.u32(modifier + 0x10),
                                  "expiration_date": memory.u32(modifier + 0x18)})
            count = memory.u32(pointer + 0x2c)
            capacity = memory.u32(pointer + 0x28)
            if count > capacity or count > 10000:
                raise ValueError("Invalid scripted relationship array")
            records = memory.pointer(pointer + 0x20)
            scripted = [self.key(memory.pointer(records + index * 40)) for index in range(count)]
            result.append({"owner": self.character_name(owner_reference), "target_id": target_id,
                           "modifiers": modifiers, "scripted_relations": scripted})
        return result

    def province_pointer(self, entity_id):
        memory = self.memory
        integer(entity_id, "province ID")
        manager = self.world_data() + 0x118
        if memory.pointer(manager) != self.base + 0x4787058:
            raise ValueError("Unexpected province manager")
        count = memory.u32(manager + 0x34)
        if entity_id == 0 or entity_id >= count:
            raise ValueError("Province was not found")
        pointer = memory.pointer(memory.pointer(manager + 0x28) + entity_id * 8)
        if not pointer or memory.u32(pointer + 0x10) != entity_id or memory.pointer(pointer) != self.base + 0x4742e90:
            raise ValueError("Province reference expired")
        return pointer

    def adjacencies(self, entity_id, include_straits=False, *, include_cost=False):
        memory = self.memory
        definition = memory.pointer(self.province_pointer(entity_id) + 8)
        if not definition or memory.u32(definition + 0x24) != entity_id:
            raise ValueError("Province map definition changed")
        count = memory.u32(definition + 0x5c)
        capacity = memory.u32(definition + 0x58)
        pointer = memory.pointer(definition + 0x50)
        if count > capacity or count > 1024 or (count and not pointer):
            raise ValueError("Invalid province adjacency array")
        records = memory.read(pointer, count * 48) if count else b""
        result = []
        for index in range(count):
            crossing, neighbor, through = struct.unpack_from("<III", records, index * 48)
            if crossing not in CROSSINGS:
                raise ValueError("Unknown province crossing type")
            if crossing == 1 and not include_straits:
                continue
            result.append({"province_id": neighbor, "crossing": CROSSINGS[crossing],
                           "through_province_id": through or None})
            if include_cost:
                result[-1]["movement_cost"] = struct.unpack_from("<q", records, index * 48 + 0x10)[0] / 100000
        return result

    def province(self, entity_id, character_id=None):
        memory = self.memory
        pointer = self.province_pointer(entity_id)
        title_id = reference(memory.u32(pointer + 0x738))
        title = self.title(title_id, province_id=entity_id) if title_id is not None else None
        occupier = reference(memory.u32(pointer + 0x73c))
        holding = pointer + 0x620
        county = memory.pointer(pointer + 0x848)
        county_data = None
        if county and memory.pointer(county) == self.base + 0x4746bc0:
            county_data = {"development": memory.u32(county + 0x38c),
                           "development_progress": memory.fixed(county + 0x390),
                           "control": memory.fixed(county + 0x398),
                           "culture": self.culture(memory.u32(county + 0x388)),
                           "faith": self.character_faith(memory.u32(county + 0x384))}
        terrain = None
        try:
            terrain = self.map_data().province(entity_id).get("terrain")
        except (OSError, ValueError, AttributeError):
            terrain = None
        supply_limit = None
        supply_limit_status = "native_result_not_located"
        evaluator = self.get_shadow_evaluator()
        if evaluator is not None:
            ref_char = character_id if character_id is not None else (title.get("holder_id") if title else None)
            if ref_char is not None:
                try:
                    char_ptr = self.lookup("character", ref_char)
                    native_supply = evaluator.evaluate_province_supply_limit(pointer, char_ptr)
                    supply_limit = native_supply["supply_limit"]
                    supply_limit_status = native_supply["source"]
                except (OSError, ValueError, RuntimeError):
                    pass
        return {"id": entity_id, "title": title,
                "realm": self.title_realm(title) if title is not None else None,
                "county": county_data, "terrain": terrain,
                "supply_limit": supply_limit, "supply_limit_status": supply_limit_status,
                "occupying_character_id": occupier, "occupying_realm": self.realm_summary(occupier),
                "holding_type": self.key(memory.pointer(holding)), "fort_level": memory.i32(pointer + 0x850),
                "monthly_income": memory.fixed(holding + 0xf8),
                "levy_id": reference(memory.u32(holding + 0xe8)), "garrison_id": reference(memory.u32(holding + 0xec))}

    def building_pointers(self):
        database = self.memory.pointer(self.base + 0x5c7c4c8)
        if not database:
            raise ValueError("The building type database is unavailable")
        return self.vector(database + 0x50, 8)

    def building_definition(self, pointer):
        key = self.key(pointer)
        if key is None:
            raise ValueError("The building type definition is unavailable")
        return {"key": key, "base_construction_days": self.memory.i32(pointer + 0x58),
                "availability": "requires_game_evaluation"}

    def building_pointer(self, entity_id):
        pointers = self.building_pointers()
        if isinstance(entity_id, str):
            index = next((index for index, pointer in enumerate(pointers) if self.key(pointer) == entity_id), None)
            if index is None:
                raise ValueError("The building type was not found")
        else:
            index = integer(entity_id, "building type ID")
        if index >= len(pointers):
            raise ValueError("The building type ID was not found")
        if not pointers[index]:
            raise ValueError("The building type definition is unavailable")
        return index, pointers[index]

    def building_type(self, entity_id):
        from ck3_llm.world.building_economy import BASE_COST_FIELDS
        index, pointer = self.building_pointer(entity_id)
        return {"id": index, **self.building_definition(pointer),
                "base_cost": {name: self.memory.fixed(pointer + offset) for name, offset in BASE_COST_FIELDS.items()},
                "previous_building_key": self.key(self.memory.pointer(pointer + 0x6f500))}

    def building_types(self, request):
        rows = [(index, pointer) for index, pointer in enumerate(self.building_pointers()) if pointer]
        if request.get("search"):
            search = request["search"].casefold()
            keys = request.get("search_keys", set())
            rows = [(index, pointer) for index, pointer in rows
                    if (key := self.key(pointer)) is not None and (search in key.casefold() or key in keys)]
        result = page(rows, request)
        result["items"] = [{"id": index, **self.building_definition(pointer),
                            "details_query": {"kind": "building_type", "id": index}} for index, pointer in result["items"]]
        return result

    def holding_buildings(self, entity_id):
        memory = self.memory
        holding = self.province_pointer(entity_id) + 0x620
        count = memory.u32(holding + 0x1c)
        capacity = memory.u32(holding + 0x18)
        array = memory.pointer(holding + 0x10)
        if count > capacity or count * 16 > 1048576 or (count and not array):
            raise ValueError("Invalid holding building slots")
        slots = []
        for index in range(count):
            slot = array + index * 16
            slots.append({"index": index, "building_key": self.key(memory.pointer(slot)),
                          "disabled": bool(memory.u8(slot + 8)), "ruined": bool(memory.u8(slot + 9))})
        construction = None
        record = memory.read(holding + 0x68, 0x78, fresh=True)
        building = struct.unpack_from("<Q", record)[0]
        if building:
            from ck3_llm.world.building_economy import RESOURCE_INDICES
            building, slot_index, start_date = struct.unpack_from("<QiI", record)
            resources = struct.unpack_from("<10q", record, 0x20)
            construction = {"building_key": self.key(building), "slot_index": slot_index,
                            "start_date": start_date,
                            "character_id": reference(struct.unpack_from("<I", record, 0x70)[0]),
                            "paid_cost": {name: resources[index] / 100000 for name, index in RESOURCE_INDICES.items()},
                            "paid_cost_basis": "native_construction_record",
                            "base_days_remaining": struct.unpack_from("<q", record, 0x18)[0] / 100000}
            try:
                construction["timing"] = self.construction_timing(holding - 0x620)
            except (OSError, ValueError) as error:
                construction["timing"] = {"status": "unavailable", "error": str(error)}
        if memory.read(holding + 0x68, len(record), fresh=True) != record:
            raise ValueError("Construction record changed during the memory query")
        return {"province_id": entity_id, "holding_type": self.key(memory.pointer(holding)), "slot_count": count,
                "slots": slots, "special_building_key": self.key(memory.pointer(holding + 0x28)),
                "duchy_building_key": self.key(memory.pointer(holding + 0x38)),
                "great_building_key": self.key(memory.pointer(holding + 0x48)), "construction": construction,
                "live_validation": "pending"}

    def construction_timing(self, province):
        """Read stored construction time and evaluate native end/progress via ShadowEvaluator when attached."""
        memory = self.memory
        construction = province + 0x688
        building = memory.pointer(construction)
        if not building:
            raise ValueError("The construction is unavailable")
        remaining_base = self.fixed_raw(construction + 0x18)
        evaluator = self.get_shadow_evaluator()
        if evaluator is not None:
            try:
                current_date = self.state.get("date")
                native = evaluator.evaluate_construction_timing(construction, province, current_date=current_date)
                return {"status": native["source"],
                        "base_days_remaining": remaining_base / 100000, "time_multiplier": None,
                        "days_remaining": native["days_remaining"], "end_date": native["end_date"],
                        "progress_percent": native["progress_percent"], "basis": native["source"]}
            except (OSError, ValueError, RuntimeError):
                pass
        return {"status": "unavailable", "reason": "native_result_not_located",
                "base_days_remaining": remaining_base / 100000, "time_multiplier": None,
                "days_remaining": None, "end_date": None, "progress_percent": None,
                "basis": "native_construction_record"}

    def events(self, entity_id=None):
        if entity_id is not None:
            self.lookup("character", entity_id)
        memory = self.memory
        result = []
        queue = self.world_data() + 0x34480 + 0x1f18
        pointers = self.vector(queue, 8)
        for pointer in pointers:
            character = memory.u32(pointer + 0x1b8, fresh=True)
            if entity_id is None or entity_id == character:
                identifier = memory.u32(pointer + 0x1bc, fresh=True)
                definition = memory.pointer(pointer + 0x1b0, fresh=True)
                result.append({"id": identifier, "character_id": character,
                               "event": memory.string(definition + 0x10),
                               "expiration_date": memory.u32(pointer + 0x1a8),
                               "details_query": {"kind": "event", "id": identifier,
                                                 "character": character}})
                if (memory.u32(pointer + 0x1b8, fresh=True) != character
                        or memory.u32(pointer + 0x1bc, fresh=True) != identifier
                        or memory.pointer(pointer + 0x1b0, fresh=True) != definition):
                    raise ValueError("The pending event changed during reading")
        if self.vector(queue, 8, fresh=True) != pointers:
            raise ValueError("The pending event queue changed during reading")
        return result

    def event(self, entity_id, character_id=None):
        integer(entity_id, "pending event ID")
        memory = self.memory
        for pointer in self.vector(self.world_data() + 0x34480 + 0x1f18, 8):
            if memory.u32(pointer + 0x1bc, fresh=True) != entity_id:
                continue
            character = memory.u32(pointer + 0x1b8, fresh=True)
            if character_id is not None and character != character_id:
                raise ValueError("The pending event belongs to another character")
            definition = memory.pointer(pointer + 0x1b0, fresh=True)
            if not definition:
                raise ValueError("The pending event definition is unavailable")
            options = self.vector(definition + 0x1a0, 8)
            result = {"id": entity_id, "character_id": character,
                      "event": memory.string(definition + 0x10),
                      "expiration_date": memory.u32(pointer + 0x1a8),
                      "scope": self.scope_context(pointer),
                      "title_override": memory.string(pointer + 0x188) or None,
                      "primary": bool(memory.u8(pointer + 0x1c0)),
                      "death": bool(memory.u8(pointer + 0x1c1)),
                      "option_count": len(options),
                      "options_query": {"kind": "event_options", "id": entity_id, "character": character}}
            evaluator = self.get_shadow_evaluator()
            if evaluator is not None:
                try:
                    result["native_options"] = evaluator.evaluate_event_options(pointer, refresh=True)
                except (OSError, ValueError, RuntimeError):
                    pass
            if (memory.u32(pointer + 0x1bc, fresh=True) != entity_id
                    or memory.u32(pointer + 0x1b8, fresh=True) != character
                    or memory.pointer(pointer + 0x1b0, fresh=True) != definition
                    or pointer not in self.vector(self.world_data() + 0x34480 + 0x1f18, 8, fresh=True)):
                raise ValueError("The pending event changed during reading")
            return result
        raise ValueError("The pending event was not found or has already ended")

    def _query(self, request):
        kind = request["kind"]
        groups = {name.split(".")[0] for name in request["fields"]} if "fields" in request else None
        if kind == "session":
            return dict(self.state)
        if kind == "player_successions":
            items = self.player_successions()
            if "machineid" in request:
                machine = request["machineid"]
                if type(machine) is not int or not -1 <= machine < 1 << 31:
                    raise ValueError("Invalid player succession machine ID")
                items = [item for item in items if item["machineid"] == machine]
            return page(items, request)
        if kind == "character":
            return self.character(request.get("id"), groups)
        if kind == "observe":
            return {"character": self.character(request["id"], groups), "events": self.events(request["id"])}
        if kind == "realm":
            ruler = self.title(request["id"])["holder_id"] if isinstance(request.get("id"), str) else request.get("id")
            return {"ruler": self.character(ruler), "primary_title": self.primary_title(self.lookup("character", ruler))}
        if kind == "title":
            return self.title(request.get("id"))
        if kind == "province":
            return self.province(request.get("id"), character_id=request.get("character"))
        if kind == "war_theater":
            from ck3_llm.world.war_theater import war_theater
            return war_theater(
                self,
                request.get("id"),
                request.get("character"),
                max_targets=request.get("max_targets", 8),
                max_armies=request.get("max_armies", 8),
                max_nodes=request.get("max_nodes", 4000),
            )
        if kind == "holding_buildings":
            return self.holding_buildings(request.get("id"))
        if kind == "building_type":
            return self.building_type(request.get("id"))
        if kind == "building_types":
            return self.building_types(request)
        if kind == "construction_quote":
            from ck3_llm.world.building_economy import construction_quote
            return construction_quote(self, request.get("id"), request.get("building"),
                                      request.get("character"), request.get("index"))
        if kind == "province_map":
            return self.map_data().province(request.get("id"))
        if kind == "route":
            return self.map_route(request)
        if kind == "unit_movement":
            return self.unit_movement(request.get("id"))
        if kind == "adjacencies":
            result = page(self.adjacencies(request.get("id"), request.get("include_straits", False)), request)
            for item in result["items"]:
                pointer = self.province_pointer(item["province_id"])
                title_id = reference(self.memory.u32(pointer + 0x738))
                title = self.title_summary(self.lookup("title", title_id)) if title_id is not None else None
                item.update(title=title, realm=self.title_realm(title) if title is not None else None)
            return result
        if kind in ("neighbors", "borders"):
            origin, borders = self.realm_borders(request.get("id"), request.get("include_straits", False))
            if "other" in request:
                other = self.top_ruler(request["other"])
                borders = [item for item in borders if item["realm"]["ruler"]["id"] == other]
            items = borders
            if kind == "neighbors":
                realms = {}
                for border in borders:
                    ruler = border["realm"]["ruler"]["id"]
                    if ruler not in realms:
                        realms[ruler] = {**border["realm"], "border_count": 0,
                                         "example_border": {key: border[key] for key in
                                                            ("province_id", "neighbor_province_id", "crossing")}}
                    realms[ruler]["border_count"] += 1
                items = list(realms.values())
            return {**page(items, request), "origin": origin}
        if kind == "culture":
            return self.culture(request.get("id"))
        if kind == "faith":
            return self.faith(request.get("id"))
        if kind == "rite":
            return self.rite(request.get("id"))
        if kind == "contract":
            return self.contract(request.get("id"))
        if kind in ("war", "army", "regiment", "army_regiment", "unit", "combat", "siege",
                    "mercenary_company", "council_task", "faction", "scheme",
                    "court_position", "holy_order", "secret", "task_contract"):
            return getattr(self, kind)(request.get("id"))
        if kind == "court_positions":
            character = request.get("character")
            if character is not None:
                integer(character, "court position character ID")
            items = []
            for entity_id, pointer in self.rows("court_position"):
                if character is not None:
                    employer_id = reference(self.memory.u32(pointer + 0x120))
                    employee_id = reference(self.memory.u32(pointer + 0x124))
                    if character not in (employer_id, employee_id):
                        continue
                item = self.court_position(entity_id)
                items.append(item)
            return page(items, request)
        if kind == "holy_orders":
            character = request.get("character")
            if character is not None:
                integer(character, "holy order character ID")
            faith = request.get("faith")
            if faith is not None:
                integer(faith, "holy order faith ID")
            items = []
            for entity_id, _ in self.rows("holy_order"):
                item = self.holy_order(entity_id)
                if character is not None and character not in (item["founder_id"], item["hired_by_id"]):
                    continue
                if faith is not None and item["faith_id"] != faith:
                    continue
                items.append(item)
            return page(items, request)
        if kind == "secrets":
            character = request.get("character")
            if character is not None:
                integer(character, "secret character ID")
            include_invalidated = request.get("include_invalidated", False)
            items = []
            for entity_id, _ in self.rows("secret"):
                item = self.secret(entity_id)
                if not include_invalidated and item["delete"]:
                    continue
                target = item["target"] or {}
                if character is not None and character not in (item["owner_id"], *item["participant_ids"]) and not (
                    target.get("type") == "char" and target.get("id") == character
                ):
                    continue
                items.append(item)
            return page(items, request)
        if kind == "task_contracts":
            character = request.get("character")
            if character is not None:
                integer(character, "task contract character ID")
            items = []
            for entity_id, _ in self.rows("task_contract"):
                item = self.task_contract(entity_id)
                if character is not None and character not in (item["employer_id"], item["owner_id"], item["target_id"]):
                    continue
                items.append(item)
            return page(items, request)
        if kind == "council_tasks":
            character = request.get("character")
            if character is not None:
                integer(character, "council character ID")
            items = []
            for entity_id, _ in self.rows("council_task"):
                item = self.council_task(entity_id)
                if character is not None and character not in (item["court_owner_id"], item["owner_id"]):
                    continue
                items.append(item)
            return page(items, request)
        if kind == "factions":
            character = request.get("character")
            if character is not None:
                integer(character, "faction character ID")
            items = []
            for entity_id, _ in self.rows("faction"):
                item = self.faction(entity_id)
                if character is not None and character not in (item["target_id"], item["leader_id"], *item["member_ids"]):
                    continue
                items.append(item)
            return page(items, request)
        if kind == "schemes":
            character = request.get("character")
            if character is not None:
                integer(character, "scheme character ID")
            include_invalidated = request.get("include_invalidated", False)
            items = []
            for entity_id, _ in self.rows("scheme"):
                item = self.scheme(entity_id)
                if not include_invalidated and item["status"] != "continue":
                    continue
                if character is not None and character != item["owner_id"] and not (
                    item["target_type"] == "character" and character == item["target_id"]
                ):
                    continue
                items.append(item)
            return page(items, request)
        if kind == "sieges":
            items = []
            army_owners = None
            if "character" in request:
                character_id = integer(request["character"], "siege character ID")
                character = self.lookup("character", character_id)
                landed = self.memory.pointer(character + 0x1c0)
                unit_ids = self.vector(landed + 0x278) if landed else []
                army_owners = {}
                for unit_id in unit_ids:
                    unit = self.lookup("unit", unit_id)
                    army_id = reference(self.memory.u32(unit + 0x178))
                    if army_id is not None and self.memory.u32(unit + 0x174) == character_id:
                        army_owners[army_id] = character_id
            for entity_id, pointer in self.rows("siege"):
                side = None
                if "character" in request:
                    side = self.siege_side(self.siege_context(entity_id, army_owners=army_owners), request["character"])
                    if side is None:
                        continue
                item = self.siege(entity_id)
                if side is not None:
                    item["side"] = side
                items.append(item)
            return page(items, request)
        if kind == "mercenary_companies":
            realm = request.get("realm")
            if isinstance(realm, str):
                realm = self.title(realm)["holder_id"]
            identifiers = []
            for entity_id, pointer in self.rows("mercenary_company"):
                if realm is not None:
                    county_id = reference(self.memory.u32(pointer + 0x24))
                    county = self.title_summary(self.lookup("title", county_id)) if county_id is not None else None
                    home = self.title_realm(county) if county else None
                    if home is None or home["ruler"]["id"] != realm:
                        continue
                identifiers.append(entity_id)
            result = page(identifiers, request)
            result["items"] = [self.mercenary_company(value) for value in result["items"]]
            return result
        if kind == "combats":
            items = [self.combat(entity_id) for entity_id, pointer in self.rows("combat")]
            if "character" in request:
                character = request["character"]
                items = [item for item in items if any(
                    character == item[side]["leader_id"] or character in item[side]["owner_ids"]
                    for side in ("attacker", "defender"))]
            return page(items, request)
        if kind == "combat_result":
            return self.combat_result(request.get("id"), request.get("character"))
        if kind == "combat_results":
            identifiers = []
            character = request.get("character")
            for entity_id, pointer in self.rows("combat_result"):
                if character is None or character in self.vector(pointer + 0xb8):
                    identifiers.append((self.memory.u32(pointer + 0x34), entity_id))
            identifiers.sort(reverse=True)
            result = page([entity_id for date, entity_id in identifiers], request)
            result["items"] = [self.combat_result(entity_id, character) for entity_id in result["items"]]
            return result
        if kind == "relations":
            return page(self.relations(request.get("id"), request.get("other")), request)
        if kind == "opinions":
            return page(self.opinions(request.get("id"), request.get("owner")), request)
        if kind == "important_action":
            return self.important_action(request.get("id"))
        if kind == "alerts":
            character = integer(request.get("character"), "notification recipient")
            identifiers = []
            for entity_id in self.vector(self.world_data() + 0x36510):
                pointer = self.lookup("important_action_item", entity_id)
                if self.memory.u32(pointer + 0x24) == character and self.memory.u8(pointer + 0x190):
                    identifiers.append(entity_id)
            result = page(identifiers, request)
            result["items"] = [self.important_action(value) for value in result["items"]]
            return result
        if kind == "player_messages":
            return page(self.player_messages(integer(request.get("character"), "notification recipient")), request)
        if kind in ("incoming_interactions", "incoming_interaction"):
            from ck3_llm.world.pending_interactions import incoming_interaction, incoming_interactions
            character = integer(request.get("character"), "interaction recipient")
            self.lookup("character", character)
            if kind == "incoming_interaction":
                return incoming_interaction(self, request.get("id"), character)
            return incoming_interactions(self, request, character)
        if kind in ("activity_invitations", "activity_invitation"):
            from ck3_llm.world.activity_invitations import activity_invitations

            character = integer(request.get("character"), "character ID")
            return activity_invitations(self, request, character)
        if kind in ("activities", "activity"):
            from ck3_llm.world.activity_invitations import activities

            character = request.get("character")
            if character is not None:
                integer(character, "character ID")
                self.lookup("character", character)
            return activities(self, request, character)
        if kind == "message_feed":
            from ck3_llm.world.gui_message_feed import message_feed
            return message_feed(self, request)
        if kind in ("wars", "armies"):
            entity_kind = {"wars": "war", "armies": "army"}[kind]
            if kind == "wars" and "character" in request:
                landed = self.memory.pointer(self.lookup("character", request["character"]) + 0x1c0)
                identifiers = self.vector(landed + 0x318) if landed else []
            elif kind == "armies" and "character" in request:
                owner = request["character"]
                self.lookup("character", owner)
                identifiers = []
                for value, pointer in self.rows("army"):
                    unit_id = reference(self.memory.u32(pointer + 0x124))
                    if unit_id is not None and self.unit(unit_id).get("owner_id") == owner:
                        identifiers.append(value)
            else:
                identifiers = [value for value, pointer in self.rows(entity_kind)]
            result = page(identifiers, request)
            result["items"] = [getattr(self, entity_kind)(value) for value in result["items"]]
            if kind == "armies":
                for item in result["items"]:
                    item["regiment_ids"] = [member["id"] for member in item.pop("regiments")]
            return result
        if kind == "events":
            return page(self.events(request.get("id", request.get("character"))), request)
        if kind in ("event", "event_options"):
            return self.event(request.get("id"), request.get("character"))
        if kind == "players":
            return [{"name": item["name"], "character": self.character_name(item["character_id"])} for item in self.state["players"]]
        if kind == "vassals":
            pointer = self.lookup("character", request.get("id"))
            landed = self.memory.pointer(pointer + 0x1c0)
            contracts = [self.contract(value) for value in self.vector(landed + 0x218)] if landed else []
            result = page([item for item in contracts if item["liege_id"] == request["id"]], request)
            result["items"] = [{**self.character(item["vassal_id"], groups), "contract": item} for item in result["items"]]
            return result
        if kind == "characters":
            rows = []
            for entity_id, pointer in self.rows("character"):
                if "alive" in request and bool(self.memory.pointer(pointer + 0x1b0)) != request["alive"]:
                    continue
                if "landed" in request and bool(self.memory.pointer(pointer + 0x1c0)) != request["landed"]:
                    continue
                if "search" in request and request["search"].casefold() not in self.memory.string(pointer + 0x20).casefold():
                    continue
                rows.append(entity_id)
            result = page(rows, request)
            result["items"] = [self.character(value, groups) for value in result["items"]]
            return result
        if kind in ("titles", "realms"):
            realm = request.get("realm")
            if isinstance(realm, str):
                realm = self.title(realm)["holder_id"]
            if realm is not None:
                self.lookup("character", realm)
            if "holder" in request and request["holder"] is not None:
                landed = self.memory.pointer(self.lookup("character", request["holder"]) + 0x1c0)
                rows = [(value, None) for value in self.vector(landed + 0x1e0)] if landed else []
            elif realm is not None:
                rows = self.realm_title_rows(realm)
            else:
                rows = self.title_rows()
            summaries = []
            seen = set()
            for entity_id, key in rows:
                if key and key[0] in TIERS and "tier" in request and TIERS[key[0]] != request["tier"]:
                    continue
                search_keys = request.get("search_keys", ())
                if key is not None and "search" in request and request["search"].casefold() not in key.casefold() and key not in search_keys:
                    continue
                if kind == "realms":
                    slots, length, id_offset, vtable = self.database("title")
                    pointer = self.memory.pointer(slots + (entity_id & 0xffffff) * 16 + 8)
                    if not pointer:
                        continue
                    ownership = self.memory.read(pointer + 0xe8, 0x44, fresh=True)
                    de_facto_liege = struct.unpack_from("<I", ownership)[0]
                    holder = struct.unpack_from("<I", ownership, 0x40)[0]
                    if de_facto_liege != 0xffffffff or holder == 0xffffffff:
                        continue
                title = self.title_summary(self.lookup("title", entity_id))
                if "holder" in request and title["holder_id"] != request["holder"]:
                    continue
                if "tier" in request and title["tier"] != request["tier"]:
                    continue
                if "liege" in request and title["de_facto_liege_id"] != request["liege"]:
                    continue
                if "de_jure_liege" in request and title["de_jure_liege_id"] != request["de_jure_liege"]:
                    continue
                if realm is not None:
                    owner = self.title_realm(title)
                    if owner is None or owner["ruler"]["id"] != realm:
                        continue
                if kind == "realms":
                    ruler = title["holder_id"]
                    if ruler is None or ruler in seen:
                        continue
                    seen.add(ruler)
                    character = self.lookup("character", ruler)
                    primary = self.primary_title(character)
                    if primary is None or self.liege(character) is not None:
                        continue
                    title = primary
                    if "tier" in request and title["tier"] != request["tier"]:
                        continue
                if "search" in request and request["search"].casefold() not in (title["key"] or "").casefold() and title["key"] not in search_keys:
                    continue
                summaries.append(title)
            result = page(summaries, request)
            result["items"] = [{**item, "holder": self.character_name(item["holder_id"]),
                                "controller": self.controller(item["holder_id"]) if item["holder_id"] is not None else None}
                               for item in result["items"]]
            return result
        raise ValueError("Unsupported direct-memory query kind")

    def _robin_hood_date_map(self, table_address):
        memory = self.memory
        header = memory.read(table_address + 0x08, 17, fresh=True)
        buckets, count, capacity, probe_limit = struct.unpack("<QIIB", header)
        if count == 0:
            if memory.read(table_address + 0x08, 17, fresh=True) != header:
                raise ValueError("Native cooldown table changed during reading")
            return {}
        total_slots = capacity + probe_limit + 1
        if capacity > 4096 or count > total_slots or total_slots > 4096 or not buckets:
            raise ValueError("Invalid native cooldown table")
        raw = memory.read(buckets, total_slots * 24, fresh=True)
        entries = {}
        for slot in range(total_slots):
            offset = slot * 24
            if raw[offset + 4] == 0:
                continue
            definition, expiration_date = struct.unpack_from("<QI", raw, offset + 8)
            key = self.key(definition)
            if key is not None:
                entries[key] = expiration_date
        if (memory.read(table_address + 0x08, 17, fresh=True) != header
                or memory.read(buckets, len(raw), fresh=True) != raw):
            raise ValueError("Native cooldown table changed during reading")
        return entries

    def planning_cooldown(self, kind, identifier, character_id):
        """Read native decision (landed +0x430) or activity_type (alive +0x500) cooldowns."""
        if kind not in ("decision", "activity_type"):
            raise ValueError("Cooldown lookup supports decision and activity_type")
        integer(character_id, "character ID")
        last_error = None
        for attempt in range(3):
            if hasattr(self.memory, "reset"):
                self.memory.reset()
            self.references.clear()
            before = self.clock()
            if self._world_identity != (before["world"], before["world_data"]):
                self.tables.clear()
                self.title_index = None
                self.primary_cache.clear()
                self.summary_cache.clear()
                self.province_index = None
                self.ruler_cache.clear()
                self._world_identity = (before["world"], before["world_data"])
            self.state = dict(before)
            try:
                pointer = self.lookup("character", character_id)
                alive = self.memory.pointer(pointer + 0x1b0, fresh=True)
                landed = self.memory.pointer(pointer + 0x1c0, fresh=True)
                if not alive:
                    result = {"remaining_days": None, "status": "unavailable", "reason": "character_dead"}
                elif kind == "decision" and not landed:
                    result = {"remaining_days": None, "status": "unavailable", "reason": "character_not_landed"}
                else:
                    table = (landed + 0x430) if kind == "decision" else (alive + 0x500)
                    entries = self._robin_hood_date_map(table)
                    current_date = before["date"]
                    expiration_date = entries.get(identifier)
                    remaining_hours = max(0, expiration_date - current_date) if expiration_date is not None else 0
                    remaining_days = remaining_hours // 24 if remaining_hours % 24 == 0 else remaining_hours / 24
                    result = {
                        "remaining_days": remaining_days,
                        "remaining_hours": remaining_hours,
                        "on_cooldown": remaining_hours > 0,
                        "expiration_date": expiration_date,
                        "current_date": current_date,
                        "status": "native_memory",
                    }
                after = self.clock()
                if (before["world"], before.get("world_data")) != (after["world"], after.get("world_data")) or before != after:
                    raise ValueError("World advanced during the cooldown query")
                slots, _, id_offset, _ = self.database("character")
                if (self.memory.pointer(slots + (character_id & 0xffffff) * 16 + 8, fresh=True) != pointer
                        or self.memory.u32(pointer + id_offset, fresh=True) != character_id
                        or self.memory.pointer(pointer + 0x1b0, fresh=True) != alive
                        or self.memory.pointer(pointer + 0x1c0, fresh=True) != landed):
                    raise ValueError("Character reference changed during the cooldown query")
                return result
            except (OSError, ValueError) as error:
                last_error = error
                if attempt == 2 and before == self.clock():
                    raise
        raise ValueError("The world kept changing during the cooldown query") from last_error

    def evaluate_decision(self, identifier, character_id):
        """Evaluate native CDecision::IsShown / IsValid / Cost via zero-injection ShadowEvaluator."""
        integer(character_id, "character ID")
        evaluator = self.get_shadow_evaluator()
        if evaluator is None:
            return None
        if hasattr(self.memory, "reset"):
            self.memory.reset()
        pointer = self.lookup("character", character_id)
        try:
            return evaluator.evaluate_decision(character_id, pointer, identifier, refresh=True)
        except (OSError, ValueError, RuntimeError):
            return None

    def evaluate_activity_type(self, identifier, character_id):
        """Evaluate native CActivityType::IsShown / CanStart via zero-injection ShadowEvaluator."""
        integer(character_id, "character ID")
        evaluator = self.get_shadow_evaluator()
        if evaluator is None:
            return None
        if hasattr(self.memory, "reset"):
            self.memory.reset()
        pointer = self.lookup("character", character_id)
        try:
            return evaluator.evaluate_activity_type(character_id, pointer, identifier, refresh=True)
        except (OSError, ValueError, RuntimeError):
            return None

    def men_at_arms_type_indices(self):
        """Read native CMenAtArmsType key -> integer index mapping from 0x5c7c4e0."""
        cached = getattr(self, "_maa_type_indices", None)
        if cached is not None:
            return cached
        try:
            db = self.memory.pointer(self.base + 0x5c7c4e0)
            count = self.memory.u32(db + 0x5c)
            vec = self.memory.pointer(db + 0x50)
            if not db or not vec or count <= 0 or count > 2048:
                return {}
            result = {}
            for idx in range(count):
                entry = self.memory.pointer(vec + idx * 8)
                if entry:
                    key = self.memory.string(entry + 0x18)
                    if key:
                        result[key] = idx
            self._maa_type_indices = result
            return result
        except (OSError, ValueError):
            return {}

    def query(self, request):
        started = time.perf_counter()
        if request["kind"] in ("province_map", "route", "unit_movement", "war_theater"):
            # File parsing precedes the live consistency window.
            self.map_data()
        last_error = None
        live_scan = (
            request["kind"] in (
                "neighbors", "borders", "combat_results", "war_theater", "sieges", "armies",
                "council_tasks", "court_positions", "factions", "schemes", "secrets", "task_contracts",
            )
            or (request["kind"] in ("characters", "titles", "realms")
                and request.get("realm") is None and request.get("holder") is None)
        )
        max_attempts = 5 if live_scan else 3
        for attempt in range(max_attempts):
            self.memory.reset()
            self.tables.clear()
            self.references.clear()
            self.primary_cache.clear()
            self.summary_cache.clear()
            self.ruler_cache.clear()
            before = self.clock()
            identity = (before["world"], before.get("world_data"))
            previous_clock = getattr(self, "_last_clock", None) or {}
            if identity != getattr(self, "_world_identity", None) or before["tick"] < previous_clock.get("tick", 0):
                self.title_index = self.province_index = None
                self._navigation_nodes = {}
                self._world_identity = identity
            evaluator = getattr(self, "shadow_evaluator", None)
            if evaluator is not None and (attempt > 0 or before != getattr(self, "_shadow_clock", None)):
                vm = getattr(evaluator, "vm", None)
                if vm is not None and hasattr(vm, "refresh_dynamic_state"):
                    vm.refresh_dynamic_state()
                self._shadow_clock = dict(before)
            self.state = {**before, "players": self.players()}
            try:
                data = self._query(request)
                after = self.clock()
                if (before["world"], before.get("world_data")) != (after["world"], after.get("world_data")) or (before != after and not live_scan):
                    raise ValueError("World advanced during the memory query")
                for kind, entity_id, pointer in self.references:
                    slots, length, id_offset, vtable = self.database(kind)
                    if self.memory.pointer(slots + (entity_id & 0xffffff) * 16 + 8, fresh=True) != pointer or self.memory.u32(pointer + id_offset, fresh=True) != entity_id:
                        raise ValueError("Object reference changed during the memory query")
                after = self.clock()
                if (before["world"], before.get("world_data")) != (after["world"], after.get("world_data")) or (before != after and not live_scan):
                    raise ValueError("World advanced while validating object references")
                if request["kind"] in ("siege", "sieges"):
                    sieges = data["items"] if request["kind"] == "sieges" else [data]
                    for siege in sieges:
                        siege.setdefault(
                            "projection",
                            {"status": "unavailable", "reason": "native_result_not_located",
                             "points_per_day": None, "sample_game_days": None,
                             "estimated_days_remaining": None, "estimated_completion_date": None},
                        )
                self._last_clock = dict(after)
                return {"data": data, "runtime": {**after, "date_started": before["date"],
                        "spans_updates": before != after, "pid": self.memory.pid, "attempts": attempt + 1,
                        "read_ms": round((time.perf_counter() - started) * 1000, 3),
                        "bytes_read": self.memory.bytes_read, "read_calls": self.memory.read_calls}}
            except (OSError, ValueError) as error:
                last_error = error
                # Game objects can change before the completed tick is
                # published. Retry with fresh pages within the existing bound.
                if attempt == max_attempts - 1 and before == self.clock():
                    raise
        raise ValueError("The world kept changing during the query. Request a smaller page or fewer fields") from last_error
