"""Installed CK3 notification types and human-readable delivery metadata."""

from copy import deepcopy
from pathlib import Path
import re

from ck3_llm.gameplay.game_labels import GameLabels


TOKEN = re.compile(r'#[^\n]*|"(?:\\.|[^"\\])*"|[{}=]|[^\s{}=#"]+')
SCALAR_FIELDS = {
    "display", "style", "message_filter_type", "title", "desc", "tooltip", "icon", "flag",
    "combine_into_one", "group", "always_show", "auto_pause", "sort_order", "type",
    "is_dangerous", "priority",
}


def definitions(path, *, include_body=False):
    source = path.read_text(encoding="utf-8-sig")
    tokens = [match for match in TOKEN.finditer(source) if not match.group().startswith("#")]
    closing = {}
    stack = []
    for index, token in enumerate(tokens):
        if token.group() == "{":
            stack.append(index)
        elif token.group() == "}":
            if not stack:
                raise ValueError(f"Unmatched closing brace in {path}")
            closing[stack.pop()] = index
    if stack:
        raise ValueError(f"Unmatched opening brace in {path}")

    index = 0
    rows = []
    while index + 2 < len(tokens):
        key = tokens[index].group()
        if tokens[index + 1].group() != "=" or tokens[index + 2].group() != "{":
            index += 1
            continue
        start = index + 2
        end = closing[start]
        fields = {}
        blocks = []
        field_index = start + 1
        while field_index + 2 < end:
            field = tokens[field_index].group()
            if tokens[field_index + 1].group() != "=":
                field_index += 1
                continue
            value = tokens[field_index + 2].group()
            if value == "{":
                blocks.append(field)
                field_index = closing[field_index + 2] + 1
                continue
            if field in SCALAR_FIELDS:
                if value.startswith('"'):
                    value = value[1:-1]
                if field == "flag":
                    fields.setdefault(field, []).append(value)
                else:
                    fields[field] = value
            field_index += 3
        if not key.startswith("@"):
            rows.append({"key": key, "file": str(path),
                         "line": source.count("\n", 0, tokens[index].start()) + 1,
                         "properties": fields, "blocks": blocks})
            if include_body:
                rows[-1]["body"] = source[tokens[start].end():tokens[end].start()]
        index = end + 1
    return rows


MILITARY_TYPES = {
    "battle_started": "交战开始", "battle_phase_changed": "战斗阶段变化", "battle_result": "战斗结果",
    "retreat_started": "开始撤退", "retreat_ended": "撤退结束", "movement_started": "开始行军",
    "movement_stopped": "行军结束", "war_started": "加入战争", "war_ended": "战争结束",
    "siege_started": "围城开始", "siege_breach": "围城产生城墙缺口", "siege_ended": "围城结束或中断",
}
STATE_TYPES = {
    "character_died": "控制角色去世", "titles_changed": "直属头衔变化",
    "liege_changed": "领主变化", "succession_changed": "继承顺序变化",
    "family_changed": "配偶、婚约或子女变化", "traits_changed": "特质变化",
    "faith_changed": "信仰变化", "culture_changed": "文化变化",
    "debt_entered": "进入负债", "debt_cleared": "清偿负债",
    "pending_event": "待处理事件", "pending_interaction": "待回复的人物请求", "activity_invitation": "活动邀请", "laws_changed": "法律变化",
    "primary_title_changed": "主头衔变化", "capital_changed": "首都变化", "government_changed": "政体变化",
    "stress_level_changed": "压力等级变化",
}
EXTRA_GROUPS = {"military": "军事状态", "important_actions": "局势提示与警报", "suggestions": "主动建议",
                "personal_state": "角色与领地变化", "pending_events": "待处理事件", "pending_interactions": "待回复的人物请求",
                "activity_invitations": "活动邀请"}


def plain_text(value):
    value = re.sub(r"@[A-Za-z0-9_]+!", "", value)
    value = re.sub(r"#(?:[A-Za-z0-9_=:.]+|!)\s*", "", value)
    return value.strip()


_CONCEPT_PATTERN = re.compile(r"\[([a-z0-9_]+)\|[A-Za-z_]+\]")
_ICON_FUNC_PATTERN = re.compile(r"\[Get[A-Za-z0-9_]*Icon[^\]]*\]")
_KEY_HINTS = (
    ("house_bloc", "家族集团"), ("confederation", "邦联"), ("legitimacy", "正统性"),
    ("governor", "总督"), ("diarch", "摄政"), ("mandate", "摄政指令"),
    ("tournament", "比武大会"), ("chariot_race", "战车赛"), ("great_project", "大型工程"),
    ("natural_disaster", "自然灾害"), ("epidemic", "瘟疫"), ("holy_order", "军事修会"),
    ("vassal_contract", "封臣契约"), ("tributary", "朝贡"), ("accolade", "勋号"),
    ("legend", "传奇"), ("artifact", "宝物"), ("barter", "以物易物"),
    ("scheme", "计谋"), ("murder", "暗杀"), ("abduct", "绑架"), ("sway", "拉拢"),
    ("fabricate_hook", "伪造牵制"), ("siphon", "挪用国库"), ("study", "研学"),
    ("mentoring", "指导"), ("countermeasure", "反制措施"), ("secret", "秘密"),
    ("war", "战争"), ("ally", "盟友"), ("enemy", "敌方"), ("troops", "军队"),
    ("faction", "派系"), ("title", "头衔"), ("vassal", "封臣"), ("liege", "领主"),
    ("activity", "活动"), ("travel", "旅行"), ("wedding", "婚礼"), ("feast", "宴会"),
    ("hunt", "狩猎"), ("pilgrimage", "朝圣"), ("culture", "文化"), ("faith", "信仰"),
    ("religion", "宗教"), ("dynasty", "宗族"), ("family", "亲族"), ("court", "宫廷"),
    ("council", "内阁"), ("councillor", "内阁成员"), ("marshal", "军事总管"),
    ("steward", "财政总管"), ("spymaster", "密谋总管"), ("chancellor", "外交总管"),
    ("chaplain", "宫廷牧师"), ("task", "任务"), ("tax", "税收"), ("domain", "直辖领地"), ("county", "伯爵领"),
    ("nomadic", "游牧"), ("herd", "牧群"), ("admin", "行政"), ("mandala", "曼荼罗"),
    ("cohesion", "凝聚力"), ("leader", "领袖"), ("gain", "获得/提升"), ("loss", "失去/下降"),
    ("compliments", "称赞"), ("supports", "支持"), ("challenges", "质疑"), ("upset", "不满"),
    ("created", "创建"), ("joined", "加入"), ("left", "离开"), ("broken", "破裂"),
    ("disbanded", "解散"), ("destroyed", "摧毁"), ("invalidated", "失效"),
    ("started", "开始"), ("completed", "完成"), ("aborted", "中止"), ("ended", "结束"),
    ("changed", "变更"), ("change", "变化"), ("reward", "奖励"), ("prize", "奖品"),
    ("good", "正面"), ("bad", "负面"), ("neutral", "中性"),
    ("popup", "弹窗"), ("toast", "顶部横幅"), ("custom", "自定义"),
)


def _resolve_readable(text, labels):
    if not text:
        return ""
    text = _ICON_FUNC_PATTERN.sub("", text)
    text = _CONCEPT_PATTERN.sub(
        lambda m: plain_text(labels.name("game_concept_" + m.group(1)) or m.group(1)),
        text,
    )
    return plain_text(text)


def _key_chinese_hints(key):
    lower = key.lower()
    parts = set(lower.split("_"))
    found = []
    for token, zh in _KEY_HINTS:
        matched = (token in lower) if "_" in token else (token in parts)
        if matched and zh not in found:
            found.append(zh)
        if len(found) >= 4:
            break
    return " · ".join(found)


def _dynamic_desc_note(desc_template):
    if not desc_template:
        return ""
    stripped = desc_template.strip()
    if "$DESCRIPTION_AND_EFFECT$" in stripped or ("$EFFECT$" in stripped and "$DESCRIPTION$" in stripped):
        return "正文同时展示具体事件描述文字与数值/效果变动（由游戏脚本动态填入）"
    if "$EFFECT$" in stripped:
        return "正文仅展示具体数值/效果变动（由游戏脚本动态填入）"
    if "$DESCRIPTION$" in stripped:
        return "正文展示具体事件描述文字（由游戏脚本动态填入）"
    if "$" in stripped:
        return "正文内容由游戏脚本在触发时动态生成"
    return ""


class NotificationCatalog:
    def __init__(self, executable):
        self.game_directory = Path(executable).parents[1] / "game"
        self.labels = GameLabels(executable, include_all=True)
        self.items = {}
        self.groups = dict(EXTRA_GROUPS)
        filters = {}
        for folder in ("message_group_types", "message_filter_types", "messages", "important_actions", "suggestions"):
            for path in sorted((self.game_directory / "common" / folder).glob("*.txt")):
                for row in definitions(path):
                    key, properties = row["key"], row["properties"]
                    if folder == "message_group_types":
                        self.groups[key] = plain_text(self.labels.name("message_group_type_" + key) or key)
                        continue
                    if folder == "message_filter_types":
                        filters[key] = properties
                        continue
                    namespace = {"messages": "message", "important_actions": "action", "suggestions": "suggestion"}[folder]
                    filter_key = properties.get("message_filter_type")
                    filter_props = filters.get(filter_key, {})
                    group = filter_props.get("group", "default")
                    label_key = properties.get("title", key)
                    if namespace != "message":
                        group = "important_actions" if namespace == "action" else "suggestions"
                        label_key = key + "_label" if self.labels.name(key + "_label") else key
                    item_id = namespace + "." + key
                    raw_title = self.labels.name(label_key) or key
                    raw_desc = self.labels.name(properties.get("desc", key + "_desc"))
                    filter_label = _resolve_readable(self.labels.name("message_filter_" + filter_key), self.labels) if filter_key else ""
                    filter_desc = _resolve_readable(self.labels.name("message_filter_" + filter_key + "_desc"), self.labels) if filter_key else ""
                    label = _resolve_readable(raw_title, self.labels)
                    human_note = ""
                    if namespace == "message":
                        dyn_title = raw_title.strip().startswith("$") or label.strip().startswith("$")
                        key_hints = _key_chinese_hints(key)
                        if dyn_title:
                            parts = [p for p in (filter_label, key_hints) if p]
                            label = (" / ".join(parts) if parts else key) + "（动态标题）"
                        note_parts = []
                        if filter_label or filter_desc:
                            if filter_label and filter_desc:
                                note_parts.append(f"归类「{filter_label}」：{filter_desc}")
                            elif filter_desc:
                                note_parts.append(filter_desc)
                            else:
                                note_parts.append(f"归类「{filter_label}」")
                        dyn_desc = _dynamic_desc_note(raw_desc)
                        if dyn_desc:
                            note_parts.append(dyn_desc)
                        elif raw_desc and not raw_desc.strip().startswith("$"):
                            note_parts.append(_resolve_readable(raw_desc, self.labels))
                        if dyn_title and key_hints and key_hints not in " ".join(note_parts):
                            note_parts.append(f"触发场景：{key_hints}")
                        human_note = "；".join(note_parts)
                    self.items[item_id] = {
                        "id": item_id, "key": key, "namespace": namespace, "group": group,
                        "label": label,
                        "title_template": raw_title,
                        "description_template": raw_desc,
                        "filter": filter_key,
                        "filter_label": filter_label or None,
                        "filter_description": filter_desc or None,
                        "human_note": human_note or None,
                        "display": properties.get("display") or filter_props.get("display"),
                        "action_type": properties.get("type", "action") if namespace == "action" else None,
                        "dangerous": properties.get("is_dangerous", "no") == "yes",
                        "combined": properties.get("combine_into_one", "no") == "yes",
                    }
        for key, label in MILITARY_TYPES.items():
            self.items["military." + key] = {"id": "military." + key, "key": key, "namespace": "military",
                                               "label": label, "group": "military"}
        for key, label in STATE_TYPES.items():
            group = {"pending_event": "pending_events", "pending_interaction": "pending_interactions",
                     "activity_invitation": "activity_invitations"}.get(key, "personal_state")
            self.items["state." + key] = {"id": "state." + key, "key": key, "namespace": "state",
                                         "label": label, "group": group}
        self.filters = filters
        self.groups.setdefault("default", "其他消息")
        for item in self.items.values():
            self.groups.setdefault(item["group"], item["group"])

    def describe(self, namespace, key):
        identifier = namespace + "." + key
        return deepcopy(self.items.get(identifier, {"id": identifier, "key": key, "namespace": namespace,
                                                     "label": key, "group": "default"}))

    def options(self):
        return {"groups": [{"id": key, "label": label} for key, label in self.groups.items()],
                "items": [deepcopy(item) for item in self.items.values()]}

    def query(self, request):
        rows = list(self.items.values())
        if "group" in request:
            rows = [row for row in rows if row["group"] == request["group"]]
        if "namespace" in request:
            rows = [row for row in rows if row["namespace"] == request["namespace"]]
        if "search" in request:
            search = request["search"].casefold()
            rows = [row for row in rows if search in (row["id"] + row["label"]).casefold()]
        offset, limit = request.get("offset", 0), request.get("limit", 20)
        end = min(offset + limit, len(rows))
        return {"count": len(rows), "offset": offset, "next_offset": end if end < len(rows) else None,
                "items": deepcopy(rows[offset:end])}

    def format_event(self, event):
        event = deepcopy(event)
        namespace = event.get("namespace", "military")
        key = event.get("notification_type", event["type"])
        item = self.describe(namespace, key)
        if namespace == "message":
            data = event.get("data") or {}
            filter_key = data.get("filter") or item.get("filter")
            item["group"] = self.filters.get(filter_key, {}).get("group", item["group"])
            if data.get("title"):
                event["summary"] = plain_text(self.labels.name(data["title"]) or data["title"])
        elif item["id"] == "state.pending_interaction":
            data = event.get("data") or {}
            actor = data.get("actor") or {}
            actor_name = actor.get("name") or (f"角色{actor['id']}" if actor.get("id") is not None else "未知角色")
            actor_id = actor.get("id")
            interaction = data.get("interaction") or {}
            interaction_key = interaction.get("key") if isinstance(interaction, dict) else interaction
            interaction_label = plain_text(self.labels.name(interaction_key) or interaction_key or "外交请求")
            req_id = data.get("id")
            event["summary"] = f"{actor_name}向你发来消息：{interaction_label}"
            event["message"] = (
                f"【来自 {actor_name}" + (f"(id={actor_id})" if actor_id is not None else "")
                + f" 的消息】请求「{interaction_label}」"
                + (f"({interaction_key})" if interaction_key else "")
                + (f"，请求编号 {req_id}" if req_id is not None else "")
                + "。请及时使用 reply_character_interaction 回复 accept 或 reject。"
            )
        elif item["id"] == "state.activity_invitation":
            data = event.get("data") or {}
            host = data.get("host") or {}
            host_name = host.get("name") or (f"角色{host['id']}" if host.get("id") is not None else "未知角色")
            act_type = data.get("activity_type")
            act_key = act_type.get("key") if isinstance(act_type, dict) else act_type
            act_label = plain_text(self.labels.name(act_key) or act_key or "活动")
            act_id = data.get("id")
            event["summary"] = f"{host_name}向你发来邀请：{act_label}"
            event["message"] = (
                f"【来自 {host_name} 的邀请】邀请你参加「{act_label}」"
                + (f"，活动编号 {act_id}" if act_id is not None else "")
                + "。"
            )
        event.update(notification_id=item["id"], group=item["group"])
        event.setdefault("summary", item["label"])
        if "[" in event["summary"]:
            event["summary_is_template"] = True
        return event

