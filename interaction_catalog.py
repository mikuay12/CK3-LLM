"""Installed option flags and command construction for supported interactions."""

from copy import deepcopy
from pathlib import Path

from event_catalog import script_fields
from game_labels import GameLabels
from jomini_codec import (
    PLAIN_SPECIAL_INTERACTIONS,
    SIMPLE_INTERACTIONS,
    SPECIAL_INTERACTION_SPECS,
    simple_interaction_fields,
)
from memory_world import page
from notification_catalog import definitions


INTERACTION_FILES = {
    "arrange_marriage_interaction": "00_marriage_interactions.txt",
    "gift_interaction": "00_gift.txt",
    "offer_vassalization_interaction": "00_character_interactions.txt",
    "invite_to_court_interaction": "00_courtier_and_guest_interactions.txt",
}


class InteractionCatalog:
    def __init__(self, executable, *, labels=None):
        self.executable = Path(executable)
        self.directory = self.executable.parents[1] / "game/common/character_interactions"
        self.labels = labels
        self.index = {}
        self.cached = {}
        self.errors = []
        if self.directory.is_dir():
            for path in sorted(self.directory.glob("*.txt")):
                stem = path.stem.lower()
                if "debug" in stem or "test" in stem:
                    continue
                try:
                    rows = definitions(path, include_body=True)
                except (OSError, ValueError) as error:
                    self.errors.append(str(error))
                    continue
                for row in rows:
                    name = row["key"]
                    fields = script_fields(row["body"])
                    fmap = {field["key"]: field["value"] for field in fields if not field["block"]}
                    category = fmap.get("category")
                    if category and "debug" in str(category).lower():
                        continue
                    special = fmap.get("special_interaction")
                    if special == "declare_war_interaction":
                        continue
                    if special is not None and special not in SPECIAL_INTERACTION_SPECS and special not in PLAIN_SPECIAL_INTERACTIONS:
                        continue
                    target_type = fmap.get("target_type")
                    if target_type not in (None, "title", "artifact", "court_position_type"):
                        continue
                    self.index[name] = {
                        "id": name,
                        "file": str(path),
                        "line": row["line"],
                        "category": category,
                        "target_type": target_type,
                        "special_interaction": special,
                    }
            ordered = {name: self.index[name] for name in SIMPLE_INTERACTIONS if name in self.index}
            for name, meta in self.index.items():
                ordered.setdefault(name, meta)
            self.index = ordered

    def lookup(self, name):
        if not isinstance(name, str) or name not in self.index:
            raise ValueError("Unsupported character interaction")
        if name not in self.cached:
            meta = self.index[name]
            path = Path(meta["file"])
            definition = next((row for row in definitions(path, include_body=True) if row["key"] == name), None)
            if definition is None:
                raise ValueError("The installed interaction definition is unavailable")
            fields = script_fields(definition["body"])
            special = next((field["value"] for field in fields if field["key"] == "special_interaction" and not field["block"]), None)
            if special == "declare_war_interaction" or (
                special is not None and special not in SPECIAL_INTERACTION_SPECS and special not in PLAIN_SPECIAL_INTERACTIONS
            ):
                raise ValueError("The installed interaction uses an unsupported native payload")
            target_type = next((field["value"] for field in fields if field["key"] == "target_type" and not field["block"]), None)
            if target_type not in (None, "title", "artifact", "court_position_type"):
                raise ValueError("The installed interaction uses an unsupported target type")
            desc = next((field["value"] for field in fields if field["key"] == "desc" and not field["block"]), None)
            options = []
            for field in fields:
                if field["key"] == "send_option" and field["block"]:
                    members = script_fields(field["value"])
                    flag = next((item["value"] for item in members if item["key"] == "flag" and not item["block"]), None)
                    if flag is None or flag in {option["flag"] for option in options}:
                        raise ValueError("The interaction has missing or duplicate option flags")
                    label = next((item["value"] for item in members if item["key"] == "localization" and not item["block"]), flag)
                    options.append({"index": len(options), "flag": flag, "localization_key": label,
                                    "script": field["value"].strip()})
            special_parameters = (
                [field for field, _, _, _ in SPECIAL_INTERACTION_SPECS[special]["fields"]]
                if special in SPECIAL_INTERACTION_SPECS else []
            )
            self.cached[name] = {
                "id": name,
                "category": meta["category"],
                "target_type": target_type,
                "special_interaction": special,
                "special_parameters": special_parameters,
                "description_key": desc or f"{name}_desc",
                "send_options": options,
                "conditions": [field["script"] for field in fields if field["key"] in
                               ("is_shown", "is_valid", "is_available", "can_send", "ai_accept", "cost")],
                "source": {"file": str(path), "line": definition["line"]},
                "availability": "requires_game_evaluation",
                "live_validation": "pending",
            }
        return deepcopy(self.cached[name])

    def describe(self, name):
        result = self.lookup(name)
        if self.labels is None:
            self.labels = GameLabels(self.executable, include_all=True)
        result["label"] = self.labels.name(name)
        result["description"] = self.labels.name(result.pop("description_key"))
        for option in result["send_options"]:
            option["label"] = self.labels.name(option["localization_key"])
        return result

    def query(self, request):
        if request["kind"] == "interaction":
            result = self.describe(request.get("id"))
            fields = request.get("fields") or []
            include_scripts = any(path.startswith("send_options.") and path.endswith(".script") for path in fields)
            for option in result["send_options"]:
                if not include_scripts:
                    option.pop("script")
                option["details_query"] = {"kind": "interaction", "id": result["id"],
                                           "fields": [f"send_options.{option['index']}.script"]}
            return result
        if self.labels is None:
            self.labels = GameLabels(self.executable, include_all=True)
        search = request.get("search", "").casefold()
        group = request.get("group")
        items = []
        for name, meta in self.index.items():
            if group is not None and meta["category"] != group:
                continue
            label = self.labels.name(name)
            if search and search not in f"{name} {label or ''} {meta['category'] or ''}".casefold():
                continue
            items.append({
                "id": name,
                "label": label,
                "category": meta["category"],
                "target_type": meta["target_type"],
                "details_query": {"kind": "interaction", "id": name},
            })
        return page(items, request)

    def build(self, actor, recipient, interaction, *, option_flags=None, target=None, special_fields=None, **parameters):
        definition = self.lookup(interaction)
        option_flags = {} if option_flags is None else option_flags
        names = [option["flag"] for option in definition["send_options"]]
        if not isinstance(option_flags, dict) or set(option_flags) - set(names):
            raise ValueError("Unknown interaction option flags. Query interaction.send_options for supported flags")
        flags = {name: option_flags.get(name, False) for name in names}
        return simple_interaction_fields(
            actor, recipient, interaction, flags=flags, target=target, special_fields=special_fields, **parameters
        )
