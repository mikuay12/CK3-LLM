"""Read the character's native activity invitations and explicit reply operations."""

from pathlib import Path
import re
import struct


_LOC_LINE_RE = re.compile(r'\s+([^\s:#]+):\s*(?:\d+\s*)?"(.*)"\s*(?:#.*)?$')
_CK3_MARKUP_RE = re.compile(r"\[[^\]]*\]|\$[^$]*\$|@[A-Za-z0-9_]+!|#(?:[A-Za-z0-9_;:]+|!)")


def _clean_compact_text(text, limit):
    if not isinstance(text, str):
        return None
    cleaned = _CK3_MARKUP_RE.sub("", text).replace("\\n", " ").replace("\n", " ")
    cleaned = " ".join(cleaned.split()).strip(" -|·:")
    if not cleaned:
        return None
    if len(cleaned) > limit:
        return cleaned[: max(1, limit - 1)].rstrip() + "…"
    return cleaned


def _activity_labels(world):
    cached = getattr(world, "_activity_labels", None)
    if cached is not None:
        return cached
    labels = {}
    executable = getattr(getattr(world, "memory", None), "executable", None)
    if executable:
        loc_dir = Path(executable).parents[1] / "game" / "localization" / "simp_chinese"
        if loc_dir.is_dir():
            paths = list(loc_dir.rglob("*activit*_l_simp_chinese.yml"))
            activities_dir = loc_dir / "activities"
            if activities_dir.is_dir():
                paths.extend(activities_dir.rglob("*.yml"))
            for path in sorted(set(paths)):
                try:
                    lines = path.read_text(encoding="utf-8-sig").splitlines()
                except OSError:
                    continue
                for line in lines:
                    match = _LOC_LINE_RE.match(line)
                    if match:
                        labels[match[1]] = match[2].replace('\\"', '"')
    world._activity_labels = labels
    return labels


def _compact_activity_display(world, key, host, phases, phase_index, attendees, invited):
    labels = _activity_labels(world)
    raw_label = labels.get(key)
    label = _clean_compact_text(raw_label, 40) if raw_label else None
    if not label:
        return {"title": None, "summary": None, "description": None, "status": "native_result_not_located"}
    host_name = None
    if isinstance(host, dict):
        host_name = _clean_compact_text(host.get("name"), 24)
    title = _clean_compact_text(f"{label}（{host_name}）" if host_name else label, 60)
    phase_label = None
    if 0 <= phase_index < len(phases):
        phase_key = phases[phase_index].get("key")
        phase_label = _clean_compact_text(labels.get(phase_key) or phase_key, 24)
    parts = [f"{'受邀·' if invited else ''}{label}"]
    if host_name:
        parts.append(f"主办:{host_name}")
    if phase_label:
        parts.append(f"阶段:{phase_label}")
    parts.append(f"参与:{len(attendees)}人")
    summary = _clean_compact_text(" | ".join(parts), 120)
    return {"title": title, "summary": summary, "description": None, "status": "compact_localized_summary"}


def _activity(world, entity_id, character, *, details, invited=True):
    from memory_world import reference

    memory = world.memory
    pointer = world.lookup("activity", entity_id)
    # CActivity's serializable base starts at +0x10. These offsets use the
    # complete object, as do Activity.IsInvited and the command executor.
    header = memory.read(pointer + 0x3a0, 0x82, fresh=True)
    definition, host = struct.unpack_from("<QI", header)
    key = world.key(definition)
    if key is None:
        raise ValueError("The activity definition is unavailable")
    phase_index = struct.unpack_from("<i", header, 0x40)[0]
    query = {"kind": "activity_invitation" if invited else "activity", "id": entity_id}
    join_parameters = {"activity": entity_id, "travel": "yes"}
    if character is not None:
        query["character"] = character
        join_parameters["character"] = character
    result = {"id": entity_id, "character_id": character,
              "activity": {"key": key}, "host": world.character_name(reference(host)),
              "active": bool(header[0x80]), "completed": bool(header[0x81]),
              "current_phase": phase_index,
              "active_start_date": struct.unpack_from("<I", header, 0x54)[0],
              "details_query": query}
    if invited:
        result["invitation_state"] = "invited"
    if details:
        phases_pointer, capacity, count = struct.unpack_from("<QII", header, 0x28)
        if count > capacity or count * 0x38 > 1048576 or (count and not phases_pointer):
            raise ValueError("Invalid activity phase vector")
        phases_data = memory.read(phases_pointer, count * 0x38, fresh=True) if count else b""
        phases = []
        for index in range(count):
            phase, province = struct.unpack_from("<QI", phases_data, index * 0x38)
            phases.append({"index": index, "key": world.key(phase), "province_id": reference(province)})
        attendees = world.vector(pointer + 0x528, fresh=True)
        join_eval = None
        evaluator = world.get_shadow_evaluator() if hasattr(world, "get_shadow_evaluator") else None
        if evaluator is not None and character is not None:
            try:
                join_eval = evaluator.evaluate_activity_join(pointer, character, refresh=True)
            except (OSError, ValueError, RuntimeError):
                join_eval = None
        result.update(
            creation_date=struct.unpack_from("<I", header, 0x4c)[0],
            phases=phases,
            attending_character_ids=attendees,
            display=_compact_activity_display(
                world, key, result["host"], phases, phase_index, attendees, invited
            ),
            response_options=[
                {"id": "join", "label": "参加",
                 "available": join_eval["can_join"] if join_eval is not None else None,
                 "availability": join_eval["source"] if join_eval is not None else "requires_game_evaluation",
                 "action": {"action": "join_activity_command",
                            "parameters": join_parameters}},
            ],
            live_validation="native_shadow_unicorn" if join_eval is not None else "pending",
        )
        if invited:
            result["response_options"].append(
                {"id": "decline", "label": "拒绝",
                 "available": (not bool(header[0x81])) if join_eval is not None else None,
                 "availability": "native_memory" if join_eval is not None else "requires_game_evaluation",
                 "action": {"action": "decline_activity_invite_command",
                            "parameters": {"activity": entity_id, "character": character}}})
        if count and memory.read(phases_pointer, len(phases_data), fresh=True) != phases_data:
            raise ValueError("Activity phases changed during reading")
        if world.vector(pointer + 0x528, fresh=True) != attendees:
            raise ValueError("Activity attendees changed during reading")
    if memory.read(pointer + 0x3a0, len(header), fresh=True) != header:
        raise ValueError("The activity changed during reading")
    return result


def activities(world, request, character=None):
    from memory_world import page

    _activity_labels(world)
    if request["kind"] == "activity":
        return _activity(world, request.get("id"), character, details=True, invited=False)
    completed = request.get("completed", False)
    if type(completed) is not bool:
        raise ValueError("completed must be a boolean")
    identifiers = [entity_id for entity_id, pointer in world.rows("activity")
                   if bool(world.memory.read(pointer + 0x421, 1, fresh=True)[0]) == completed]
    result = page(identifiers, request)
    result["items"] = [_activity(world, entity_id, character, details=False, invited=False)
                       for entity_id in result["items"]]
    if any(item["completed"] != completed for item in result["items"]):
        raise ValueError("Activity completion changed during reading")
    return {**result, "source": "native_activity_database", "join_eligibility": "native_result_not_located"}


def activity_invitations(world, request, character):
    from memory_world import page

    _activity_labels(world)
    memory = world.memory
    owner = world.lookup("character", character)
    alive = memory.pointer(owner + 0x1b0, fresh=True)
    identifiers = world.vector(alive + 0x550, fresh=True) if alive else []
    if request["kind"] == "activity_invitation":
        entity_id = request.get("id")
        if type(entity_id) is not int or entity_id not in identifiers:
            raise ValueError("This activity invitation is unavailable for the character")
        result = _activity(world, entity_id, character, details=True)
    else:
        result = page(identifiers, request)
        result["items"] = [_activity(world, entity_id, character, details=False) for entity_id in result["items"]]
    if (memory.pointer(owner + 0x1b0, fresh=True) != alive
            or (alive and world.vector(alive + 0x550, fresh=True) != identifiers)):
        raise ValueError("Activity invitations changed during reading")
    return result
