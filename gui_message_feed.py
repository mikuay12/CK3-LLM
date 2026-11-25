"""Read the native GUI's rendered message cache through a read-only handle."""

import struct

from building_economy import APPLICATION_RVA, HOLDING_VIEW_CHAIN
from world_snapshot import format_game_date


def message_feed(world, request):
    from memory_world import page

    if request.get("character") is not None:
        raise ValueError("GUI message recipient ownership is unavailable. Use player_messages for a character-scoped queue")
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
        if length > capacity or length > 65536 or (capacity < 16 and length > 15):
            raise ValueError("Invalid native GUI message string")
        data = header[:length] if capacity < 16 else read(struct.unpack_from("<Q", header)[0], length)
        return data.decode("utf-8")

    def key(pointer):
        if not pointer:
            return None
        tag = number(pointer + 0x38, "I")
        if tag == 0x4e756c6c:
            return None
        if tag != 0x4744624f:
            raise ValueError("Invalid native GUI message definition")
        return string(pointer + 0x18)

    metadata = {"source": "native_gui_message_feed", "scope": "local_game_gui",
                "recipient_character_id": None, "recipient_status": "native_result_not_located"}
    # Both readers follow the same application, active scene, graphics idler
    # and interface handler. The feed is a separate child of that handler.
    address = world.base + APPLICATION_RVA
    for vtable, next_offset in HOLDING_VIEW_CHAIN[:-1]:
        interface = number(address)
        if not interface or number(interface) != world.base + vtable:
            return {**metadata, **page([], request), "status": "unavailable", "reason": "interface_unavailable"}
        address = interface + next_offset
    handler = number(interface + 0x478)
    if not handler or number(handler) != world.base + 0x44fb590:
        return {**metadata, **page([], request), "status": "unavailable", "reason": "message_feed_unavailable"}
    array, capacity, count = struct.unpack("<QII", read(handler + 0xe8, 16))
    if count > capacity or count > 10000 or (count and not array):
        raise ValueError("Invalid native GUI message vector")
    result = page(range(count), request)
    items = []
    for index in result["items"]:
        pointer = number(array + index * 8)
        if not pointer or number(pointer) != handler:
            raise ValueError("Native GUI message changed its parent")
        item = {"index": index, "type": key(number(pointer + 8)),
                "filter": key(number(pointer + 0x10)),
                "title": string(pointer + 0x11d0), "description": string(pointer + 0x11f0),
                "effects_text": string(pointer + 0x1210),
                "created_date": format_game_date(number(pointer + 0x1178, "I")),
                "arrival_date": format_game_date(number(pointer + 0x1180, "I"))}
        items.append(item)
    if any(world.memory.read(address, len(data), fresh=True) != data for address, data in samples):
        raise ValueError("Native GUI message cache changed during reading")
    return {**metadata, **result, "items": items, "status": "cached", "freshness": "untracked"}
