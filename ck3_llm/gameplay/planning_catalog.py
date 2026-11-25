"""Installed decisions and activity choices for model-directed planning."""

from copy import deepcopy
from pathlib import Path

from ck3_llm.gameplay.event_catalog import script_fields
from ck3_llm.gameplay.game_labels import GameLabels
from ck3_llm.world.memory_world import integer, page
from ck3_llm.gameplay.notification_catalog import definitions


FOLDERS = {"decision": "decisions", "activity_type": "activities/activity_types"}
CONDITIONS = {"is_shown", "is_valid", "is_valid_showing_failures_only", "is_available", "can_execute",
              "can_start", "can_start_showing_failures_only", "can_select", "is_location_valid"}


class PlanningCatalog:
    def __init__(self, executable, *, labels=None):
        self.directory = Path(executable).parents[1] / "game/common"
        self.labels = labels if labels is not None else GameLabels(executable, include_all=True)
        self.index = {kind: {} for kind in FOLDERS}
        self.cached = {}
        self.errors = []
        for kind, folder in FOLDERS.items():
            for path in sorted((self.directory / folder).rglob("*.txt")):
                try:
                    rows = definitions(path)
                except (OSError, ValueError) as error:
                    self.errors.append(str(error))
                    continue
                for row in rows:
                    self.index[kind][row["key"]] = row

    def _text(self, key):
        text = self.labels.name(key)
        return {"key": key, "text": text,
                "resolved": text is not None and "[" not in text and "$" not in text}

    def _choice(self, field, query):
        members = script_fields(field["value"]) if field["block"] else []
        return {"id": field["key"], "name": self._text(field["key"]), "available": None,
                "availability": "native_result_not_located",
                "conditions": [item["script"] for item in members if item["key"] in CONDITIONS],
                "script": field["value"], "details_query": query}

    def lookup(self, kind, identifier):
        if kind not in self.index or not isinstance(identifier, str) or identifier not in self.index[kind]:
            raise ValueError("The installed planning definition is unavailable")
        cache_key = kind, identifier
        if cache_key not in self.cached:
            row = self.index[kind][identifier]
            definition = next((item for item in definitions(Path(row["file"]), include_body=True)
                               if item["key"] == identifier), None)
            if definition is None:
                raise ValueError("The installed planning definition changed")
            fields = script_fields(definition["body"])
            scalar = {item["key"]: item["value"] for item in fields if not item["block"]}
            description = next((item for item in fields if item["key"] == "desc"), None)
            result = {"id": identifier, "name": self._text(scalar.get("name", identifier)),
                      "description": self._text(description["value"] if description and not description["block"]
                                                else identifier + "_desc"),
                      "conditions": [item["script"] for item in fields if item["key"] in CONDITIONS],
                      "shown": None, "available": None, "availability": "native_result_not_located",
                      "cost": {"gold": None, "prestige": None, "piety": None, "treasury": None,
                               "status": "native_result_not_located",
                               "script": [item["script"] for item in fields if item["key"] in ("cost", "ui_predicted_cost")]},
                      "cooldown": {"remaining_days": None, "status": "native_result_not_located",
                                   "script": [item["script"] for item in fields if item["key"] == "cooldown"]},
                      "source": {"kind": "installed_game_scripts", "file": row["file"], "line": row["line"]}}
            if description and description["block"]:
                result["description"] = {"script": description["value"], "resolved": False}
            if kind == "decision":
                result.update(
                    effects=[item["script"] for item in fields if item["key"] == "effect"],
                    confirmation=self._text(scalar.get("confirm_text", identifier + "_confirm")),
                    selection_tooltip=self._text(scalar.get("selection_tooltip", identifier + "_tooltip")),
                    widget=scalar.get("widget"),
                    parameter_schema_status="native_result_not_located",
                    action={"action": "execute_decision", "parameters": {"decision": identifier}},
                )
            else:
                phases, options = [], []
                for field in fields:
                    if field["key"] == "phases" and field["block"]:
                        for phase in script_fields(field["value"]):
                            if not phase["block"]:
                                continue
                            query = {"kind": kind, "id": identifier, "fields": [f"phases.{len(phases)}"]}
                            phases.append(self._choice(phase, query))
                    if field["key"] == "options" and field["block"]:
                        for category in script_fields(field["value"]):
                            if not category["block"]:
                                continue
                            choices = []
                            for choice in script_fields(category["value"]):
                                if choice["block"]:
                                    query = {"kind": kind, "id": identifier,
                                             "fields": [f"options.{len(options)}.choices.{len(choices)}"]}
                                    choices.append(self._choice(choice, query))
                            options.append({"id": category["key"], "name": self._text(category["key"]), "choices": choices})
                result.update(phases=phases, options=options,
                              host_intents=[item["script"] for item in fields if item["key"] == "host_intents"],
                              guest_intents=[item["script"] for item in fields if item["key"] == "guest_intents"],
                              special_guests=[item["script"] for item in fields if item["key"] == "special_guests"],
                              hosting_status="requires_game_validation",
                              action={"action": "start_activity_command", "parameters": {"type": identifier}})
            self.cached[cache_key] = result
        return deepcopy(self.cached[cache_key])

    def query(self, request):
        kind = {"decisions": "decision", "activity_types": "activity_type"}.get(request["kind"], request["kind"])
        character = request.get("character")
        if character is not None:
            integer(character, "character ID")
        if request["kind"] in ("decisions", "activity_types"):
            search = request.get("search", "").casefold()
            rows = []
            for identifier in sorted(self.index[kind]):
                name = self._text(identifier)
                if search not in (identifier + " " + (name["text"] or "")).casefold():
                    continue
                query = {"kind": kind, "id": identifier}
                if character is not None:
                    query["character"] = character
                rows.append({"id": identifier, "name": name, "shown": None, "available": None,
                             "availability": "native_result_not_located", "details_query": query})
            return {**page(rows, request), "character_id": character, "source": "installed_game_scripts",
                    "definition_errors": list(self.errors)}
        result = self.lookup(kind, request.get("id"))
        result["character_id"] = character
        if kind == "decision" and character is not None:
            result["action"]["parameters"]["character"] = character
        elif kind == "activity_type" and character is not None:
            result["action"]["parameters"]["host"] = character
        selected_fields = request.get("fields") or []
        include_choice_details = any(field.startswith(("phases.", "options.")) for field in selected_fields)
        if kind == "activity_type" and not include_choice_details:
            for choice in result["phases"] + [choice for category in result["options"] for choice in category["choices"]]:
                choice.pop("script")
                choice.pop("conditions")
        return result
