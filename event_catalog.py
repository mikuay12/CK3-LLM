"""Installed event text and scripted options for a live pending event."""

from copy import deepcopy
from pathlib import Path
import re

from game_labels import GameLabels
from event_evaluation import render_text
from notification_catalog import TOKEN, definitions


def script_fields(source):
    """Read direct assignments while preserving nested conditions and effects."""
    tokens = [match for match in TOKEN.finditer(source) if not match.group().startswith("#")]
    index = 0
    rows = []
    while index + 2 < len(tokens):
        if tokens[index + 1].group() != "=":
            index += 1
            continue
        first = index
        key = tokens[index].group()
        index += 2
        value_start = index
        if tokens[index].group() == "{":
            depth = 1
            index += 1
            while index < len(tokens) and depth:
                token = tokens[index].group()
                if token == "{":
                    depth += 1
                elif token == "}":
                    depth -= 1
                index += 1
            if depth:
                raise ValueError("Unmatched event script brace")
            value = source[tokens[value_start].end():tokens[index - 1].start()]
            block = True
        else:
            value = tokens[index].group()
            if value.startswith('"'):
                value = value[1:-1].replace('\\"', '"')
            index += 1
            block = False
        rows.append({"key": key, "value": value, "block": block,
                     "script": source[tokens[first].start():tokens[index - 1].end()]})
    return rows


_IGNORED_OPTION_KEYS = frozenset({
    "name", "trigger", "show_as_unavailable", "is_valid", "valid_trigger",
    "ai_chance", "flavor", "trait", "skill", "highlight_portrait",
    "exclusive", "fallback", "clicksound",
})
_DYNAMIC_WEIGHT_KEYS = frozenset({
    "modifier", "opinion_modifier", "compare_modifier", "trigger",
    "compatibility_modifier", "ai_value_modifier",
})
_RESOURCE_LABELS = {
    "add_gold": "金币", "add_prestige": "威望", "add_piety": "虔诚",
    "add_stress": "压力", "add_dread": "恐怖值", "add_tyranny": "暴政",
    "add_legitimacy": "正统性", "add_influence": "影响力",
    "remove_short_term_gold": "消耗金币", "add_short_term_gold": "金币",
}


def _try_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _summarize_effect_fields(fields, labels, *, depth=0):
    """Extract concise Chinese consequence summaries and probability breakdowns."""
    parts = []
    probabilities = []
    for item in fields:
        key = item["key"]
        if key in _IGNORED_OPTION_KEYS:
            continue
        val = item["value"]
        if not item["block"]:
            if key in _RESOURCE_LABELS:
                parts.append(f"{_RESOURCE_LABELS[key]} {val}")
            elif key == "add_trait":
                tname = labels.name("trait_" + val) or val
                parts.append(f"获得特质「{tname}」")
            elif key == "remove_trait":
                tname = labels.name("trait_" + val) or val
                parts.append(f"移除特质「{tname}」")
            elif key == "custom_tooltip":
                txt = labels.name(val) or val
                parts.append(txt)
            elif key == "trigger_event":
                parts.append(f"触发事件 {val}")
            elif key == "show_as_tooltip":
                continue
            else:
                parts.append(f"{key}={val}")
            continue

        # Block effects
        sub = script_fields(val)
        if key == "stress_impact":
            stresses = [f"{s['key']}:{s['value']}" for s in sub if not s["block"]]
            if stresses:
                parts.append("压力影响(" + ", ".join(stresses[:5]) + ")")
        elif key == "add_character_modifier":
            mod = next((s["value"] for s in sub if s["key"] == "modifier" and not s["block"]), None)
            if mod:
                parts.append(f"获得修正「{labels.name(mod) or mod}」")
        elif key == "trigger_event":
            ev = next((s["value"] for s in sub if s["key"] == "id" and not s["block"]), None)
            if ev:
                parts.append(f"触发事件 {ev}")
        elif key == "random_list" and depth < 2:
            branches = []
            has_dynamic = False
            for br in sub:
                w = _try_float(br["key"])
                if w is None or not br["block"]:
                    continue
                br_fields = script_fields(br["value"])
                if any(f["key"] in _DYNAMIC_WEIGHT_KEYS for f in br_fields):
                    has_dynamic = True
                eff_text, _ = _summarize_effect_fields(
                    [f for f in br_fields if f["key"] not in _DYNAMIC_WEIGHT_KEYS and f["key"] not in ("desc", "min", "max")],
                    labels, depth=depth + 1,
                )
                desc_key = next((f["value"] for f in br_fields if f["key"] == "desc" and not f["block"]), None)
                label = (labels.name(desc_key) or desc_key) if desc_key else None
                branches.append({"weight": w, "label": label, "effect": eff_text or "分支效果"})
            total = sum(b["weight"] for b in branches)
            if branches and total > 0:
                for b in branches:
                    pct = round(b["weight"] / total * 100.0, 1)
                    if has_dynamic:
                        probabilities.append({
                            "percent": None,
                            "base_percent": pct,
                            "basis": "native_result_not_located",
                            "label": b["label"],
                            "effect": b["effect"],
                        })
                    else:
                        probabilities.append({
                            "percent": pct,
                            "basis": "script_fixed_weights",
                            "label": b["label"],
                            "effect": b["effect"],
                        })
                parts.append(f"随机分支({len(branches)}项" + ("，含动态修正" if has_dynamic else "") + ")")
        elif key == "random" and depth < 2:
            chance_val = next((_try_float(s["value"]) for s in sub if s["key"] == "chance" and not s["block"]), None)
            has_dynamic = any(s["key"] in _DYNAMIC_WEIGHT_KEYS for s in sub)
            eff_text, _ = _summarize_effect_fields(
                [s for s in sub if s["key"] != "chance" and s["key"] not in _DYNAMIC_WEIGHT_KEYS],
                labels, depth=depth + 1,
            )
            if chance_val is not None:
                if has_dynamic:
                    probabilities.append({"percent": None, "base_percent": round(chance_val, 1),
                                          "basis": "native_result_not_located", "effect": eff_text or "随机触发"})
                else:
                    probabilities.append({"percent": round(chance_val, 1), "basis": "script_fixed_chance",
                                          "effect": eff_text or "随机触发"})
                    if chance_val < 100:
                        probabilities.append({"percent": round(100.0 - chance_val, 1), "basis": "script_fixed_chance",
                                              "effect": "无事发生"})
                parts.append(f"随机触发(基础{chance_val:g}%" + ("，含动态修正" if has_dynamic else "") + ")")
        elif key == "duel" and depth < 2:
            skill = next((s["value"] for s in sub if s["key"] == "skill" and not s["block"]), None)
            val_num = next((s["value"] for s in sub if s["key"] == "value" and not s["block"]), None)
            duel_branches = []
            for br in sub:
                w = _try_float(br["key"])
                if w is not None and br["block"]:
                    br_fields = script_fields(br["value"])
                    eff_text, _ = _summarize_effect_fields(
                        [f for f in br_fields if f["key"] not in _DYNAMIC_WEIGHT_KEYS and f["key"] not in ("desc", "min", "max")],
                        labels, depth=depth + 1,
                    )
                    desc_key = next((f["value"] for f in br_fields if f["key"] == "desc" and not f["block"]), None)
                    duel_branches.append({"weight": w, "label": (labels.name(desc_key) or desc_key) if desc_key else None,
                                          "effect": eff_text or "检定结果"})
            total = sum(b["weight"] for b in duel_branches)
            for b in duel_branches:
                probabilities.append({
                    "percent": None,
                    "base_percent": round(b["weight"] / total * 100.0, 1) if total > 0 else None,
                    "basis": "native_result_not_located",
                    "duel_skill": skill,
                    "label": b["label"],
                    "effect": b["effect"],
                })
            parts.append(f"属性检定(duel:{skill or '?'}" + (f" vs {val_num}" if val_num else "") + ")")
        elif key == "hidden_effect":
            continue
        elif key in ("if", "else_if", "else", "show_as_tooltip") and depth < 1:
            sub_eff, sub_probs = _summarize_effect_fields(
                [s for s in sub if s["key"] != "limit"], labels, depth=depth + 1
            )
            if sub_eff:
                prefix = "条件效果: " if key in ("if", "else_if") else ("否则: " if key == "else" else "")
                parts.append(prefix + sub_eff)
            probabilities.extend(sub_probs)
        else:
            snippet = " ".join(item["script"].split())
            if len(snippet) <= 80:
                parts.append(snippet)
            else:
                parts.append(f"{key}={{...}}")
    summary = "；".join(p for p in parts if p)
    return summary or None, probabilities


class EventCatalog:
    def __init__(self, executable, *, labels=None):
        self.directory = Path(executable).parents[1] / "game"
        self.labels = labels if labels is not None else GameLabels(executable, include_all=True)
        self.index = {}
        self.cached = {}
        self.errors = []
        for path in sorted((self.directory / "events").rglob("*.txt")):
            try:
                rows = definitions(path)
            except (OSError, ValueError) as error:
                self.errors.append(str(error))
                continue
            for row in rows:
                if re.fullmatch(r"[A-Za-z0-9_]+\.[0-9]+", row["key"]):
                    self.index[row["key"]] = path

    def localized(self, field):
        if field is None:
            return None
        if field["block"]:
            variants = []
            for item in script_fields(field["value"]):
                if item["block"] or item["key"] in ("desc", "text", "title", "name"):
                    variants.append({"field": item["key"], **self.localized(item)})
            return {"script": field["value"].strip(), "variants": variants, "resolved": False}
        key = field["value"]
        text = self.labels.name(key)
        return {"key": key, "text": text,
                "resolved": text is not None and "[" not in text and "$" not in text}

    def lookup(self, key):
        if key not in self.cached:
            path = self.index.get(key)
            if path is None:
                raise ValueError("Installed script for this event is unavailable")
            row = next((item for item in definitions(path, include_body=True) if item["key"] == key), None)
            if row is None:
                raise ValueError("The event script definition changed")
            fields = script_fields(row["body"])
            scalar = {field["key"]: field for field in fields if not field["block"]}
            title = next((field for field in fields if field["key"] == "title"), None)
            descriptions = [self.localized(field) for field in fields if field["key"] == "desc"]
            options = []
            for field in fields:
                if field["key"] != "option" or not field["block"]:
                    continue
                members = script_fields(field["value"])
                name = next((item for item in members if item["key"] == "name"), None)
                conditions = [item["script"] for item in members if item["key"] in
                              ("trigger", "show_as_unavailable", "is_valid", "valid_trigger")]
                effect_summary, probabilities = _summarize_effect_fields(members, self.labels)
                options.append({"index": len(options), "name": self.localized(name),
                                "conditional": bool(conditions), "conditions": conditions,
                                "effect_summary": effect_summary, "probabilities": probabilities,
                                "script": field["value"].strip(), "availability": "requires_game_evaluation"})
            self.cached[key] = {"event": key, "event_type": scalar.get("type", {}).get("value"),
                                "title": self.localized(title), "descriptions": descriptions,
                                "options": options,
                                "source": {"kind": "installed_game_scripts", "file": str(path), "line": row["line"]},
                                "text_rendering": "localization_templates",
                                "runtime_conditions": "requires_game_evaluation"}
        return deepcopy(self.cached[key])

    def describe(self, event):
        try:
            result = self.lookup(event["event"])
        except (OSError, ValueError) as error:
            return {"status": "unavailable", "error": str(error)}, []
        context = event.get("scope") or {}

        def render(value):
            if isinstance(value, list):
                return [render(item) for item in value]
            if not isinstance(value, dict):
                return value
            rendered = {key: render(item) for key, item in value.items()}
            if isinstance(value.get("text"), str):
                text, unresolved = render_text(value["text"], context, self.labels)
                rendered.update(text=text, resolved="[" not in text and "]" not in text and "$" not in text)
                if text != value["text"]:
                    rendered.update(template=value["text"], resolved_from="event_scope")
                if unresolved:
                    rendered["unresolved_expressions"] = unresolved
            return rendered

        result = render(result)
        if event.get("title_override"):
            result["title"] = render(self.localized({"value": event["title_override"], "block": False}))
        options = result.pop("options")
        if len(options) != event["option_count"]:
            return {**result, "status": "option_count_mismatch", "script_option_count": len(options)}, []
        for option in options:
            option["condition_results"] = []
            for condition in option["conditions"]:
                fields = script_fields(condition)
                for field in fields:
                    option["condition_results"].append({"field": field["key"], "value": None,
                                                        "basis": "native_result_not_located"})
        return {**result, "status": "matching_option_count",
                "live_option_mapping": "pending_validation"}, options
