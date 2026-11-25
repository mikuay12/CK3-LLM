"""Read native pending NPC/player requests and expose model-selected replies."""

import struct

from jomini_codec import INTERACTION_REPLIES


REPLY_LABELS = {"accept": "接受", "reject": "拒绝", "block": "阻止", "acknowledge": "确认"}


def _native_display(world, entity_id, intermediary_status):
    from building_economy import APPLICATION_RVA, HOLDING_VIEW_CHAIN

    memory = world.memory
    unavailable = {"title": None, "description": None, "status": "native_result_not_located"}
    samples = []

    def read(address, size):
        data = memory.read(address, size, fresh=True)
        samples.append((address, data))
        return data

    def pointer(address):
        return struct.unpack("<Q", read(address, 8))[0]

    def string(address):
        header = read(address, 32)
        length, capacity = struct.unpack_from("<QQ", header, 16)
        if length > capacity or length > 65536 or (capacity < 16 and length > 15):
            raise ValueError("Invalid native interaction text cache")
        data = header[:length] if capacity < 16 else read(struct.unpack_from("<Q", header)[0], length)
        return data.decode("utf-8")

    address = world.base + APPLICATION_RVA
    for vtable, next_offset in HOLDING_VIEW_CHAIN[:-1]:
        interface = pointer(address)
        if not interface or pointer(interface) != world.base + vtable:
            return unavailable
        address = interface + next_offset
    window = pointer(interface + 0xf8)
    if not window or pointer(window) != world.base + 0x455ce98:
        return unavailable
    array, capacity, count = struct.unpack("<QII", read(window + 0x3f8, 16))
    if count > capacity or count * 0xc8 > 1048576 or (count and not array):
        raise ValueError("Invalid native interaction text vector")
    entries = read(array, count * 0xc8) if count else b""
    for index in range(count):
        item = array + index * 0xc8
        if struct.unpack_from("<I", entries, index * 0xc8)[0] != entity_id:
            continue
        # GetNotificationText selects the opening and body for the recipient's
        # current reply stage. GetSigned and GetSignature use the last pair.
        strings = item + 8
        offset = 0 if intermediary_status == 1 else 0x40
        result = {"title": None, "opening": string(strings + offset),
                  "description": string(strings + offset + 0x20),
                  "signed": string(strings + 0x80), "signature": string(strings + 0xa0),
                  "status": "partially_cached", "source": "native_interaction_text_cache",
                  "title_status": "native_result_not_located", "freshness": "untracked"}
        if any(memory.read(address, len(data), fresh=True) != data for address, data in samples):
            raise ValueError("Native interaction text cache changed during reading")
        return result
    return unavailable


def _recipient_role(character, recipient, intermediary, status):
    if intermediary == character and status == 1:
        return "intermediary"
    if recipient == character:
        return "recipient"
    return None


def incoming_interaction(world, entity_id, character, *, details=True):
    from memory_world import reference

    memory = world.memory
    pointer = world.lookup("pending_interaction", entity_id)
    ownership = memory.read(pointer + 0x2f0, 0x18, fresh=True)
    stage = memory.read(pointer + 0x5c0, 7, fresh=True)
    actor, recipient, secondary_actor, secondary_recipient, intermediary, _ = struct.unpack("<6I", ownership)
    status, ai_intermediary_accept, ai_accept, is_notification = struct.unpack("<I3B", stage)
    role = _recipient_role(character, recipient, intermediary, status)
    if role is None:
        raise ValueError("This pending interaction belongs to another recipient")
    definition = memory.pointer(pointer + 0x18)
    key = world.key(definition)
    if key is None:
        raise ValueError("The pending interaction definition is unavailable")
    result = {"id": entity_id, "character_id": character, "role": role,
              "interaction": {"key": key}, "actor": world.character_name(reference(actor)),
              "recipient": world.character_name(reference(recipient)),
              "days_in_queue": memory.i32(pointer + 0x5b8),
              "is_notification": bool(is_notification),
              "details_query": {"kind": "incoming_interaction", "id": entity_id, "character": character}}
    if details:
        try:
            display = _native_display(world, entity_id, status)
        except (OSError, ValueError) as error:
            display = {"title": None, "description": None, "status": "unavailable", "reason": str(error)}
        reply_eval = None
        evaluator = world.get_shadow_evaluator() if hasattr(world, "get_shadow_evaluator") else None
        if evaluator is not None:
            try:
                reply_eval = evaluator.evaluate_pending_interaction_replies(pointer, refresh=True)
            except (OSError, ValueError, RuntimeError):
                reply_eval = None
        result.update(
            secondary_actor=world.character_name(reference(secondary_actor)),
            secondary_recipient=world.character_name(reference(secondary_recipient)),
            intermediary=world.character_name(reference(intermediary)),
            intermediary_status={0: "none", 1: "pending", 2: "accept", 3: "decline"}.get(status, status),
            ai_reply_delay=memory.i32(pointer + 0x5bc),
            ai_intermediary_accepts=bool(ai_intermediary_accept),
            ai_accepts=bool(ai_accept),
            selected_options=[bool(value) for value in world.vector(pointer + 0x318, 1)],
            scope=world.scope_context(pointer + 0x20),
            original_scope=world.scope_context(pointer + 0x188),
            display=display,
            response_options=[{"id": name, "label": REPLY_LABELS[name],
                               "available": reply_eval[name] if reply_eval is not None else None,
                               "availability": reply_eval["source"] if reply_eval is not None else "requires_game_evaluation",
                               "action": {"action": "reply_character_interaction",
                                          "parameters": {"character_interaction": entity_id, "accept": name}}}
                              for name in INTERACTION_REPLIES],
            live_validation="native_shadow_unicorn" if reply_eval is not None else "pending",
        )
    if (memory.read(pointer + 0x2f0, len(ownership), fresh=True) != ownership
            or memory.read(pointer + 0x5c0, len(stage), fresh=True) != stage):
        raise ValueError("The pending interaction recipient or reply stage changed during reading")
    return result


def incoming_interactions(world, request, character):
    from memory_world import page

    memory = world.memory
    identifiers = [entity_id for entity_id, pointer in world.rows("pending_interaction")
                   if _recipient_role(character, memory.u32(pointer + 0x2f4, fresh=True),
                                      memory.u32(pointer + 0x300, fresh=True),
                                      memory.u32(pointer + 0x5c0, fresh=True)) is not None]
    result = page(identifiers, request)
    result["items"] = [incoming_interaction(world, entity_id, character, details=False)
                       for entity_id in result["items"]]
    return result
