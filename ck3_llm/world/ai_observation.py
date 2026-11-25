"""Compact personal observations, deltas and selective CK3 snapshot queries."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

from ck3_llm.world.world_snapshot import WorldSnapshot, format_game_date


DEFAULT_MAX_CHARS = 5000
SKILLS = ("diplomacy", "martial", "stewardship", "intrigue", "learning", "prowess")
QUERY_KINDS = ("character", "title", "province", "path", "events")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _pick(data, names):
    return {name: deepcopy(data[name]) for name in names if name in data}


def _currency(value):
    if isinstance(value, dict):
        value = value.get("value", value.get("currency"))
    return value if type(value) in (int, float) else None


def _field(data, path):
    value = data
    for part in path.split("."):
        if isinstance(value, dict):
            value = value[part]
        elif isinstance(value, list) and part.isdecimal():
            value = value[int(part)]
        else:
            raise KeyError(path)
    return deepcopy(value)


class ObservationInterface:
    def __init__(self, world=None, character_id=None):
        self.world = None
        self.character_id = None
        self.snapshot_tick = None
        self.source_metadata = {}
        self.synchronization = {}
        self._personal_cache = {}
        self._baseline = {}
        self._commands = []
        self._seen_commands = set()
        self._state_change_observed = False
        self._watched_characters = set()
        self._responses = {}
        if world is not None:
            self.set_snapshot(world)
        if character_id is not None:
            self.set_character(character_id)

    def set_snapshot(self, world, snapshot_tick=None, *, source_metadata=None):
        self.world = world
        self.snapshot_tick = snapshot_tick
        self.source_metadata = deepcopy(source_metadata or {})
        self._personal_cache.clear()
        self._state_change_observed = False
        self._watched_characters = {self.character_id} if self.character_id is not None else set()

    def set_character(self, character_id):
        if character_id is not None and (type(character_id) is not int or not 0 <= character_id < 0xFFFFFFFF):
            raise ValueError("Character ID must be a valid unsigned integer reference")
        if character_id == self.character_id:
            return
        self.character_id = character_id
        self._baseline.clear()
        self._commands.clear()
        self._seen_commands.clear()
        self._watched_characters = {character_id} if character_id is not None else set()

    def set_synchronization(self, state):
        self.synchronization = deepcopy(state)

    def _require_world(self):
        if self.world is None:
            raise ValueError("A verified world snapshot is required before querying observations")

    def _personal(self, character_id):
        self._require_world()
        if character_id in self._personal_cache:
            return deepcopy(self._personal_cache[character_id])
        observation = self.world.observation(character_id)
        data = observation["character"]["data"]
        alive = data.get("alive_data", {})
        landed = data.get("landed_data", {})
        family = data.get("family_data", {})
        identity = {"id": character_id, **_pick(data, ("first_name", "nickname_text", "culture", "rite", "dynasty_house"))}
        if type(data.get("birth")) is int:
            identity["age"] = (observation["date_raw"] - data["birth"]) // (365 * 24)
        resources = {name: _currency(alive[name]) for name in ("gold", "prestige", "piety", "influence") if name in alive}
        resources = {name: value for name, value in resources.items() if value is not None}
        resources.update(_pick(alive, ("income", "stress", "health")))
        domain = _pick(landed, ("government", "realm_capital", "domain_limit", "vassals_towards_limit", "vassal_limit", "laws"))
        domain["titles"] = [_pick(title, ("id", "key")) for title in observation["held_titles"]]
        if landed.get("domain"):
            domain["primary_title"] = landed["domain"][0]
        relatives = _pick(family, ("primary_spouse", "child"))
        if landed.get("succession"):
            relatives["heir"] = landed["succession"][0]
        skills = data.get("skill", [])
        personal = {
            "identity": identity, "resources": resources,
            "focus": _pick(alive.get("focus", {}), ("type", "date", "progress")),
            "military": _pick(landed, ("current_strength", "levy", "strength_without_hires")),
            "domain": domain, "family": relatives,
            "skills": dict(zip(SKILLS, skills)) if len(skills) == len(SKILLS) else skills,
            "trait_ids": deepcopy(data.get("traits", [])),
        }
        personal = {name: value for name, value in personal.items() if value != {} and value != []}
        self._personal_cache[character_id] = personal
        return deepcopy(personal)

    def _refresh_watch(self, personal):
        family = personal.get("family", {})
        relatives = family.get("child", [])
        if type(relatives) is int:
            relatives = [relatives]
        self._watched_characters = {self.character_id, *relatives}
        self._watched_characters.update(value for value in (family.get("primary_spouse"), family.get("heir")) if type(value) is int)

    def feed(self, command):
        name = command.get("type", "")
        if self.character_id is None or command.get("channel", "command") != "command":
            return
        if self.world is not None and self._watched_characters == {self.character_id}:
            character = self.world.character(self.character_id)["data"]
            family = _pick(character.get("family_data", {}), ("primary_spouse", "child"))
            successors = character.get("landed_data", {}).get("succession", [])
            if successors:
                family["heir"] = successors[0]
            self._refresh_watch({"family": family})
        fields = command.get("fields") or {}
        interaction = fields.get("character_interaction")
        action = interaction if isinstance(interaction, dict) else fields
        reference_keys = ("character", "actor", "recipient", "secondary_actor", "secondary_recipient")
        if name in ("set_character_focus", "add_perk"):
            reference_keys += ("target",)
        references = {action[key] for key in reference_keys if type(action.get(key)) is int}
        if not references.intersection(self._watched_characters):
            return
        header = command.get("default_state") or {}
        visible = _pick(action, (*reference_keys, "character_interaction", "focus_type", "perk", "decision", "option"))
        item = {"type": name, "identity": header.get("identity"), "sender": header.get("sender"), "fields": visible}
        key = _json(item)
        if key in self._seen_commands:
            return
        self._seen_commands.add(key)
        self._commands.append(item)
        self._state_change_observed = True
        return deepcopy(item)

    def _metadata(self):
        self._require_world()
        date = self.world.get("date")
        hotjoin = self.synchronization.get("hotjoin") or {}
        host = hotjoin.get("sender")
        turns = self.synchronization.get("peer_turns", [])
        host_turn = next((peer["turn"] for peer in turns if peer["machineid"] == host), None)
        advanced = self.snapshot_tick is not None and host_turn is not None and host_turn != self.snapshot_tick
        return {
            "source": "received_world_snapshot", "date": format_game_date(date),
            "sha256": self.world.source_sha256, "tick": self.snapshot_tick,
            "latest_host_turn": host_turn,
            "refresh_needed": bool(advanced or self._state_change_observed),
            "continuous_state_updates": "pending",
            **self.source_metadata,
        }

    @staticmethod
    def _check_budget(max_chars, payload):
        if type(max_chars) is not int or max_chars <= 0:
            raise ValueError("max_chars must be a positive integer")
        if len(_json(payload)) > max_chars:
            raise ValueError("max_chars is too small for the observation metadata")

    def observe(self, *, changes_only=True, max_chars=DEFAULT_MAX_CHARS):
        if type(changes_only) is not bool:
            raise ValueError("changes_only must be a boolean")
        if self.character_id is None:
            raise ValueError("Choose an AI character or specify character in the observation request")
        personal = self._personal(self.character_id)
        self._refresh_watch(personal)
        full = not changes_only or not self._baseline
        changed = {key: value for key, value in personal.items() if full or self._baseline.get(key) != value}
        removed = [key for key in self._baseline if key not in personal]
        result = {
            "character_id": self.character_id, "mode": "full" if full else "delta",
            "snapshot": self._metadata(), "personal": {}, "removed": removed,
            "observed_commands": [], "omitted_fields": list(changed),
            "pending_commands": len(self._commands), "should_decide": bool(changed or removed or self._commands),
        }
        self._check_budget(max_chars, result)
        for key, value in changed.items():
            result["personal"][key] = value
            result["omitted_fields"].remove(key)
            if len(_json(result)) > max_chars:
                del result["personal"][key]
                result["omitted_fields"].append(key)
        delivered = 0
        for item in self._commands:
            result["observed_commands"].append(item)
            result["pending_commands"] -= 1
            if len(_json(result)) > max_chars:
                result["observed_commands"].pop()
                result["pending_commands"] += 1
                break
            delivered += 1
        self._baseline.update(deepcopy(result["personal"]))
        for key in removed:
            self._baseline.pop(key, None)
        del self._commands[:delivered]
        return deepcopy(result)

    def query(self, kind, reference=None, *, fields=None, path=None, max_chars=DEFAULT_MAX_CHARS):
        self._require_world()
        if kind == "character":
            raw = self.world.character(reference)["data"]
            values = self._personal(reference) if fields is None else raw
        elif kind == "title":
            raw = self.world.title(reference)["data"]
            values = _pick(raw, ("key", "holder", "de_facto_liege", "de_jure_liege", "capital", "name")) if fields is None else raw
        elif kind == "province":
            raw = self.world.province(reference)["data"]
            values = _pick(raw, ("terrain", "culture", "faith", "control", "fort_level", "holding_type", "county")) if fields is None else raw
        elif kind == "events":
            if type(reference) is not int or not 0 <= reference < 0xFFFFFFFF:
                raise ValueError("Event queries require a valid character ID")
            try:
                events = self.world.get("player_event")
            except KeyError as error:
                if error.args != ("player_event",):
                    raise
                events = []
            if isinstance(events, dict):
                events = [events]
            if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
                raise ValueError("Invalid player event records")
            events = [event for event in events if event.get("character") == reference]
            raw = {"count": len(events), "events": events}
            values = raw if fields is not None else {
                "count": len(events),
                "events": [_pick(event, ("id", "event", "character", "expiration_date", "scope")) for event in events],
            }
        elif kind == "path":
            if not isinstance(path, list) or not path:
                raise ValueError("A world path requires a nonempty array of property names or IDs")
            values = {"value": self.world.get(*path)}
        else:
            raise ValueError("Query kind must be character, title, province, path or events")
        missing = []
        if fields is not None:
            if not isinstance(fields, list) or not all(isinstance(field, str) and field for field in fields):
                raise ValueError("fields must contain nonempty property paths")
            selected = {}
            for field in fields:
                try:
                    selected[field] = _field(values, field)
                except (KeyError, IndexError):
                    missing.append(field)
            values = selected
        result = {"kind": kind, "reference": reference, "snapshot": self._metadata(),
                  "data": {}, "missing_fields": missing, "omitted_fields": list(values)}
        self._check_budget(max_chars, result)
        for key, value in values.items():
            result["data"][key] = deepcopy(value)
            result["omitted_fields"].remove(key)
            if len(_json(result)) > max_chars:
                del result["data"][key]
                result["omitted_fields"].append(key)
        return result

    def handle(self, request):
        if not isinstance(request, dict):
            raise ValueError("Observation request must be a JSON object")
        request_id = request.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or not request_id):
            raise ValueError("request_id must be a nonempty string")
        signature = json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if request_id in self._responses:
            previous_signature, result = self._responses[request_id]
            if signature != previous_signature:
                raise ValueError("request_id was already used for a different observation request")
            return deepcopy(result)
        result = self._handle(request)
        if request_id is not None:
            self._responses[request_id] = (signature, deepcopy(result))
        return result

    def _handle(self, request):
        if request.get("command") == "observation_status":
            return {"world_ready": self.world is not None, "character_id": self.character_id,
                    "snapshot": self._metadata() if self.world is not None else None}
        if request.get("command") == "observe":
            if "character" in request:
                self.set_character(request["character"])
            return self.observe(changes_only=request.get("changes_only", True), max_chars=request.get("max_chars", DEFAULT_MAX_CHARS))
        if request.get("command") == "query":
            return self.query(request.get("kind"), request.get("id"), fields=request.get("fields"),
                              path=request.get("path"), max_chars=request.get("max_chars", DEFAULT_MAX_CHARS))
        raise ValueError("Observation command must be observe, query or observation_status")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--character", type=int)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--request-file", type=Path, help="JSON observe or query request")
    args = parser.parse_args()
    try:
        interface = ObservationInterface(WorldSnapshot.from_file(args.snapshot), args.character)
        request = {"command": "observe", "max_chars": args.max_chars}
        if args.request_file:
            request = json.loads(args.request_file.read_text(encoding="utf-8-sig"))
        print(_json(interface.handle(request)))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(_json({"event": "observation_error", "message": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
