"""Installed governance, court position, countermeasure, law and men-at-arms catalogs."""

from copy import deepcopy
from pathlib import Path
import re

from event_catalog import script_fields
from game_labels import GameLabels
from memory_world import page
from notification_catalog import definitions


CATALOG_FOLDERS = {
    "council_task_type": ("council_tasks", "council_task_types", "council_task_type"),
    "court_position_type": ("court_positions/types", "court_position_types", "court_position_type"),
    "court_position_task": ("court_positions/tasks", "court_position_tasks", "court_position_task"),
    "scheme_countermeasure": ("schemes/scheme_countermeasures", "scheme_countermeasures", "scheme_countermeasure"),
    "law": ("laws", "laws", "law"),
    "men_at_arms_type": ("men_at_arms_types", "men_at_arms_types", "men_at_arms_type"),
}
SINGULAR_BY_PLURAL = {plural: singular for singular, (_, plural, _) in CATALOG_FOLDERS.items()}
CONDITION_KEYS = {
    "is_shown", "is_shown_character", "is_valid", "is_valid_showing_failures_only",
    "valid_character", "valid_position", "potential_county", "valid_target",
    "can_keep", "can_pass", "should_start_with", "should_show_for_title",
    "can_recruit", "should_show_when_unavailable",
}


class DefinitionCatalog:
    """Read-only index and structured inspector for installed CK3 game definitions."""

    def __init__(self, executable, *, labels=None):
        self.directory = Path(executable).parents[1] / "game/common"
        self.labels = labels if labels is not None else GameLabels(executable, include_all=True)
        self.index = {kind: {} for kind in CATALOG_FOLDERS}
        self.cached = {}
        self.errors = []
        for kind, (folder, _, _) in CATALOG_FOLDERS.items():
            root = self.directory / folder
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.txt")):
                try:
                    rows = definitions(path)
                except (OSError, ValueError) as error:
                    self.errors.append(str(error))
                    continue
                for row in rows:
                    if row["key"].startswith("debug_"):
                        continue
                    self.index[kind][row["key"]] = row

    def _text(self, key):
        text = self.labels.name(key) if key else None
        return {"key": key, "text": text,
                "resolved": text is not None and "[" not in text and "$" not in text}

    @staticmethod
    def _group_of(kind, scalar, fields):
        if kind == "council_task_type":
            return scalar.get("position")
        if kind == "court_position_type":
            return scalar.get("skill")
        if kind == "court_position_task":
            block = next((field["value"] for field in fields if field["key"] == "court_position_types" and field["block"]), "")
            words = re.findall(r"[A-Za-z0-9_]+", block)
            return words[0] if words else None
        if kind == "law":
            return scalar.get("law_group_type")
        if kind == "men_at_arms_type":
            return scalar.get("type")
        return None

    def lookup(self, kind, identifier):
        if kind not in self.index or not isinstance(identifier, str) or identifier not in self.index[kind]:
            raise ValueError("The installed definition is unavailable")
        cache_key = (kind, identifier)
        if cache_key not in self.cached:
            row = self.index[kind][identifier]
            definition = next((item for item in definitions(Path(row["file"]), include_body=True)
                               if item["key"] == identifier), None)
            if definition is None:
                raise ValueError("The installed definition changed")
            fields = script_fields(definition["body"])
            scalar = {item["key"]: item["value"] for item in fields if not item["block"]}
            desc = next((item for item in fields if item["key"] == "desc"), None)
            desc_key = desc["value"] if desc and not desc["block"] else f"{identifier}_desc"
            description = ({"script": desc["value"], "resolved": False}
                           if desc and desc["block"] else self._text(desc_key))
            result = {
                "id": identifier,
                "name": self._text(scalar.get("name", identifier)),
                "description": description,
                "group": self._group_of(kind, scalar, fields),
                "conditions": [item["script"] for item in fields if item["key"] in CONDITION_KEYS],
                "availability": "requires_game_evaluation",
                "source": {"kind": "installed_game_scripts", "file": row["file"], "line": row["line"]},
            }
            if kind == "council_task_type":
                result.update(
                    position=scalar.get("position"),
                    task_type=scalar.get("task_type"),
                    task_progress=scalar.get("task_progress"),
                    default_task=scalar.get("default_task") == "yes",
                    effects=[item["script"] for item in fields if item["key"] in (
                        "effect_desc", "on_start_task", "on_cancel_task",
                        "on_finish_task_county", "on_finish_task_court", "on_monthly")],
                    modifiers=[item["script"] for item in fields if "modifier" in item["key"]],
                    action={"action": "change_council_task", "parameters": {"task_type": identifier}},
                )
            elif kind == "court_position_type":
                result.update(
                    skill=scalar.get("skill"),
                    max_available_positions=scalar.get("max_available_positions"),
                    cost=[item["script"] for item in fields if item["key"] in ("salary", "revoke_cost")],
                    modifiers=[item["script"] for item in fields if "modifier" in item["key"]],
                    action={"action": "give_court_position", "parameters": {"court_position": identifier}},
                )
            elif kind == "court_position_task":
                cp_block = next((item["value"] for item in fields
                                 if item["key"] == "court_position_types" and item["block"]), "")
                result.update(
                    court_position_types=re.findall(r"[A-Za-z0-9_]+", cp_block),
                    cost=[item["script"] for item in fields if item["key"] == "cost"],
                    effects=[item["script"] for item in fields if item["key"] in (
                        "on_start", "on_end", "on_monthly", "on_yearly", "employee_modifier", "employer_modifier")],
                    action={"action": "start_court_position_task", "parameters": {"task_type": identifier}},
                )
            elif kind == "scheme_countermeasure":
                result.update(
                    effects=[item["script"] for item in fields if item["key"] in (
                        "on_activate", "owner_modifier", "parameters")],
                    action={"action": "set_countermeasure", "parameters": {"scheme_countermeasure_type": identifier}},
                )
            elif kind == "law":
                result.update(
                    law_group=scalar.get("law_group_type"),
                    index=scalar.get("index"),
                    flags=[item["value"] for item in fields if item["key"] == "flag" and not item["block"]],
                    cost=[item["script"] for item in fields if item["key"] in ("pass_cost", "revoke_cost")],
                    effects=[item["script"] for item in fields if item["key"] in ("on_pass", "on_revoke", "modifier")],
                    action={"action": "add_law", "parameters": {"law": identifier}},
                )
            elif kind == "men_at_arms_type":
                stats = {key: scalar[key] for key in (
                    "type", "stack", "damage", "toughness", "pursuit", "screen",
                    "siege_value", "siege_tier", "provision_cost") if key in scalar}
                result.update(
                    regiment_category=scalar.get("type"),
                    stats=stats,
                    counters=[item["script"] for item in fields if item["key"] == "counters"],
                    terrain_bonus=[item["script"] for item in fields if item["key"] == "terrain_bonus"],
                    cost=[item["script"] for item in fields if item["key"] in (
                        "buy_cost", "low_maintenance_cost", "high_maintenance_cost")],
                )
            self.cached[cache_key] = result
        return deepcopy(self.cached[cache_key])

    def query(self, request):
        request_kind = request["kind"]
        kind = SINGULAR_BY_PLURAL.get(request_kind, request_kind)
        if request_kind in CATALOG_FOLDERS:
            return self.lookup(kind, request.get("id"))
        search = request.get("search", "").casefold()
        group = request.get("group")
        items = []
        for identifier in self.index[kind]:
            entry = self.lookup(kind, identifier)
            if group is not None and entry["group"] != group:
                continue
            label = entry["name"].get("text") or ""
            if search and search not in f"{identifier} {label} {entry['group'] or ''}".casefold():
                continue
            items.append({
                "id": identifier,
                "name": entry["name"],
                "group": entry["group"],
                "availability": entry["availability"],
                "details_query": {"kind": kind, "id": identifier},
            })
        return page(items, request)
