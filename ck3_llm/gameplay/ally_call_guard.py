"""Apply explicitly configured standing decisions to player interactions and armies."""


class AllyCallGuard:
    interaction_type = "call_ally_interaction"
    request_prefix = "ally-call"

    def __init__(self, actors=(), *, machines=(), interval=0.5, retry_seconds=5.0):
        self.actors = frozenset(actors)
        self.machines = frozenset(machines)
        self.interval = interval
        self.retry_seconds = retry_seconds
        self.next_poll = 0.0
        self.attempts = {}
        self.character = None

    def poll(self, world, character, now):
        if not (self.actors or self.machines) or character is None or now < self.next_poll:
            return []
        self.next_poll = now + self.interval
        if self.character != character:
            self.attempts.clear()
            self.character = character
        items = []
        offset = 0
        while offset is not None:
            result = world.query({"kind": "incoming_interactions", "character": character,
                                  "offset": offset, "limit": 100})["data"]
            items.extend(result["items"])
            offset = result["next_offset"]
        actors = set(self.actors)
        if self.machines:
            players = world.query({"kind": "session"})["data"]["players"]
            actors.update(player["character_id"] for player in players
                          if player["machineid"] in self.machines and player["character_id"] is not None)
        calls = [item for item in items
                 if (item.get("actor") or {}).get("id") in actors
                 and (item.get("interaction") or {}).get("key") == self.interaction_type
                 and item.get("role") == "recipient"]
        present = {item["id"] for item in calls}
        self.attempts = {key: value for key, value in self.attempts.items() if key in present}
        commands = []
        for item in calls:
            count, last = self.attempts.get(item["id"], (0, float("-inf")))
            if count >= 3 or now - last < self.retry_seconds:
                continue
            count += 1
            self.attempts[item["id"]] = (count, now)
            commands.append({"command": "send_game_command", "type": "reply_character_interaction",
                             "request_id": f"{self.request_prefix}-{character}-{item['id']}-{count}",
                             "header": {"attribs": 14},
                             "fields": {"character_interaction": item["id"], "accept": "accept"}})
        return commands


class MarriageProposalGuard(AllyCallGuard):
    """Accept marriage proposals only from explicitly configured human players."""

    interaction_type = "arrange_marriage_interaction"
    request_prefix = "marriage-proposal"

    def __init__(self, *, machines=(), interval=0.5, retry_seconds=5.0):
        super().__init__(machines=machines, interval=interval, retry_seconds=retry_seconds)


class PeaceArmyGuard:
    """Disband after the last war ends, following an explicit standing decision."""

    def __init__(self, *, interval=1.0):
        self.interval = interval
        self.next_poll = 0.0
        self.character = None
        self.war_seen = False
        self.attempts = {}

    def poll(self, world, character, now):
        if character is None or now < self.next_poll:
            return []
        self.next_poll = now + self.interval
        if self.character != character:
            self.character = character
            self.war_seen = False
            self.attempts.clear()
        military = world.query({"kind": "character", "id": character, "fields": ["military"]})["data"]["military"] or {}
        if military.get("war_ids"):
            self.war_seen = True
            self.attempts.clear()
            return []
        if not self.war_seen:
            return []
        commands = []
        for unit in military.get("unit_ids", []):
            army = world.query({"kind": "unit", "id": unit})["data"].get("army_id")
            if army is None:
                continue
            count, last = self.attempts.get(army, (0, float("-inf")))
            if count >= 3 or now - last < 5:
                continue
            count += 1
            self.attempts[army] = (count, now)
            commands.append({"command": "send_game_command", "type": "disband_army",
                             "request_id": f"peace-disband-{character}-{army}-{count}",
                             "header": {"attribs": 14}, "fields": {"valid_for": "player", "army": army}})
        if not military.get("unit_ids"):
            self.war_seen = False
            self.attempts.clear()
        return commands
