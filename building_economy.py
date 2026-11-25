"""Read native construction prices and GUI quotation caches."""

import struct


BASE_COST_FIELDS = {"gold": 0x6f518, "prestige": 0x6f520, "piety": 0x6f528}
RESOURCE_INDICES = {"gold": 0, "prestige": 1, "piety": 2, "treasury": 6}
CACHE_FIELDS = {"gold_or_treasury": 0x18, "prestige": 0xf8, "piety": 0x1d8, "construction_days": 0x2b8}
TRACK_CACHE_FIELDS = {"gold_or_treasury": 0x478, "prestige": 0x638, "piety": 0x718, "construction_days": 0x7f8}

# CApplication's constructor publishes this singleton. Each following link has
# a verified concrete vtable, including the in-game scene before its GUI idler.
APPLICATION_RVA = 0x5ccd808
HOLDING_VIEW_CHAIN = ((0x44abf28, 0x28), (0x44e6408, 0x10), (0x44cc6f0, 0x88),
                      (0x44cab38, 0xd0), (0x4553820, None))


def _native_quote(world, province, building, character_id, index):
    """Sample the game's existing quote without evaluating its GUI getters."""
    samples = []

    def read(address, size):
        data = world.memory.read(address, size, fresh=True)
        samples.append((address, data))
        return data

    def number(address, format="Q"):
        return struct.unpack("<" + format, read(address, struct.calcsize("<" + format)))[0]

    def string(address):
        header = read(address, 32)
        length, capacity = struct.unpack_from("<QQ", header, 16)
        if length > capacity or length > 8192 or (capacity < 16 and length > 15):
            raise ValueError("Invalid native cost unit string")
        data = header[:length] if capacity < 16 else read(struct.unpack_from("<Q", header)[0], length)
        return data.decode("utf-8", errors="replace")

    def cached_value(item, offset, *, duration=False):
        result, multiplier, kind, flags, state = struct.unpack("<qqIHB", read(item + offset + 0x78, 23))
        # Native constructors mark unevaluated breakdowns with kind=1 and
        # state bit 0x04. Evaluated zero-price results clear both markers.
        initialized = kind == 0 and not state & 4
        if duration:
            initialized = initialized and bool(state & 0x20)
        return result / 100000 if initialized else None

    unavailable = {"status": "unavailable", "source": "native_gui_cache", "freshness": "untracked"}
    try:
        address = world.base + APPLICATION_RVA
        for vtable, next_offset in HOLDING_VIEW_CHAIN:
            view = number(address)
            if not view or number(view) != world.base + vtable:
                return {**unavailable, "reason": "holding_view_unavailable"}
            if next_offset is not None:
                address = view + next_offset
        if number(view + 0xd8) != province or number(view + 0x1c0) != province:
            return {**unavailable, "reason": "province_context_mismatch"}

        # SGUIPotentialBuildingItem obtains its payer from this province's
        # ordinary title holder, with the parent-title fallback for a barony.
        title = world.lookup("title", number(province + 0x738, "I"))
        payer = number(title + 0x128, "I")
        if payer == 0xffffffff:
            definition = number(title + 0x48)
            if definition and number(definition + 0x64, "I") == 1:
                parent = world.lookup("title", number(title + 0xe8, "I"))
                payer = number(parent + 0x128, "I")
        if payer != character_id:
            return {**unavailable, "reason": "payer_context_mismatch"}

        header = read(view + 0xe8, 16)
        array, capacity, count = struct.unpack("<QII", header)
        if count > capacity or count > 4096 or (count and not array):
            return {**unavailable, "reason": "invalid_native_building_vector"}
        entries = struct.unpack(f"<{count}Q", read(array, count * 8)) if count else ()
        matches = []
        for item in entries:
            if not item:
                continue
            parent, candidate, slot = struct.unpack("<QQi", read(item, 20))
            if parent == view and candidate == building and (index is None or index == slot):
                matches.append((item, slot))
        if len(matches) > 1:
            return {**unavailable, "reason": "ambiguous_cached_slot"}
        cache_fields = CACHE_FIELDS
        cache_kind = "candidate_list"
        if matches:
            item, slot = matches[0]
        else:
            # HoldingView.GetTrack returns the embedded SGUITrackItem.
            item = view + 0x1c8
            parent, current, next_building, selected, slot = struct.unpack("<QQQQi", read(item + 0x450, 36))
            if parent != view or next_building != building or (index is not None and index != slot):
                return {**unavailable, "reason": "quote_not_cached"}
            cache_kind = "upgrade_track"
            # Selected-level previews and the next upgrade share breakdowns.
            # Their values have a unique target in the default upgrade view.
            if selected not in (current, next_building):
                return {**unavailable, "cache_kind": cache_kind, "reason": "ambiguous_track_quote_target"}
            cache_fields = dict(TRACK_CACHE_FIELDS)
            gold = cached_value(item, 0x478)
            treasury = cached_value(item, 0x558)
            if gold is not None and treasury is not None:
                return {**unavailable, "cache_kind": cache_kind, "reason": "ambiguous_cached_currency"}
            if treasury is not None:
                cache_fields["gold_or_treasury"] = 0x558
        values = {}
        units = {}
        for name, offset in cache_fields.items():
            values[name] = cached_value(item, offset, duration=name == "construction_days")
            if name != "construction_days":
                units[name] = string(item + offset + 0x38)
        if any(world.memory.read(address, len(data), fresh=True) != data for address, data in samples):
            return {**unavailable, "reason": "native_cache_changed_during_read"}
        present = sum(value is not None for value in values.values())
        return {"status": "cached" if present == len(values) else "partially_cached" if present else "unavailable",
                "source": "native_gui_cache", "freshness": "untracked", "slot_index": slot, "cache_kind": cache_kind,
                "values": values, "cost_units": units,
                "missing_fields": [name for name, value in values.items() if value is None]}
    except (OSError, ValueError) as error:
        return {**unavailable, "reason": "native_cache_unavailable", "error": str(error)}


def construction_quote(world, province_id, building_id, character_id, index=None):
    from memory_world import integer
    integer(character_id, "construction character ID")
    if index is not None and (type(index) is not int or not -0x80000000 <= index <= 0x7fffffff):
        raise ValueError("Construction slot index requires a signed 32-bit integer")
    province = world.province_pointer(province_id)
    type_id, building = world.building_pointer(building_id)
    world.lookup("character", character_id)
    cache = _native_quote(world, province, building, character_id, index)
    values = cache.pop("values", {})
    units = cache.pop("cost_units", {})
    duration = values.get("construction_days")
    timing_source = "native_gui_cache"
    timing_status = "cached" if duration is not None else "unavailable"
    availability = {"status": "requires_game_evaluation"}
    evaluator = world.get_shadow_evaluator() if hasattr(world, "get_shadow_evaluator") else None
    if evaluator is not None:
        try:
            shadow = evaluator.evaluate_construction_quote(
                character_id,
                province_id,
                province,
                building,
                slot_index=cache.get("slot_index", index),
                refresh=True,
            )
            for name in ("gold_or_treasury", "prestige", "piety"):
                if values.get(name) is None:
                    values[name] = shadow[name]
            if duration is None:
                duration = shadow["construction_days"]
                timing_source = shadow["source"]
                timing_status = shadow["source"]
            if cache.get("status") == "unavailable":
                cache = {**cache, "status": shadow["source"], "source": shadow["source"]}
            availability = {
                "can_construct": shadow["can_construct"],
                "can_construct_ignoring_cost": shadow["can_construct_ignoring_cost"],
                "status": shadow["source"],
            }
        except (OSError, ValueError, RuntimeError):
            pass
    return {"province_id": province_id, "character_id": character_id, "slot_index": cache.get("slot_index", index),
            "building_type": {"id": type_id, **world.building_definition(building)},
            "base_cost": {name: world.memory.fixed(building + offset) for name, offset in BASE_COST_FIELDS.items()},
            "cost": {name: values.get(name) for name in CACHE_FIELDS if name != "construction_days"},
            "cost_units": units, "quote_cache": cache,
            "timing": {"construction_days": duration, "source": timing_source,
                       "status": timing_status, "freshness": "untracked" if timing_source == "native_gui_cache" else "live"},
            "availability": availability,
            "live_validation": "native_shadow_unicorn" if timing_source == "native_shadow_unicorn"
                               else ("pending" if cache.get("cache_kind") == "upgrade_track" else "standard_candidate_cache_verified")}
