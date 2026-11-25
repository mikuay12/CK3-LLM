"""Acknowledge this client's completed inheritance through the native protocol."""


class SuccessionGuard:
    def __init__(self, *, interval=0.5, retry_seconds=2.0):
        self.interval = interval
        self.retry_seconds = retry_seconds
        self.next_poll = 0.0
        self.context = None
        self.attempts = {}
        self.pending = False

    def poll(self, world, character, machineid, now):
        if (world is None or type(machineid) is not int
                or machineid < 0 or now < self.next_poll):
            return []
        self.next_poll = now + self.interval
        if self.context != (character, machineid):
            self.context = (character, machineid)
            self.attempts.clear()
            self.pending = False
        result = world.query({"kind": "player_successions", "machineid": machineid,
                              "limit": 100})["data"]
        # Never select a destiny character or close a host/other player's window.
        owned = [item for item in result["items"]
                 if item.get("machineid") == machineid and item.get("has_open_succession") is True]
        self.pending = bool(owned)
        pending = [item for item in owned if character is not None and item.get("character_id") == character]
        if not pending:
            self.attempts.clear()
            return []
        alive = world.query({"kind": "character", "id": character,
                             "fields": ["identity"]})["data"]["identity"]["alive"]
        if not alive:
            return []
        count, last = self.attempts.get(character, (0, float("-inf")))
        if count >= 3 or now - last < self.retry_seconds:
            return []
        count += 1
        self.attempts[character] = (count, now)
        return [{"command": "send_game_command", "type": "close_succession",
                 "request_id": f"succession-close-{machineid}-{character}-{count}",
                 "header": {"attribs": 14},
                 "fields": {"character": character, "machineid": machineid}}]
