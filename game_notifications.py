"""Character-scoped notification producers and budgeted in-memory delivery."""

from collections import OrderedDict, defaultdict, deque
from copy import deepcopy
import json
import time
import uuid

from notification_catalog import NotificationCatalog
from notification_policy import NotificationPolicy


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _items(reader, kind, character_id):
    rows = []
    offset = 0
    date = None
    while offset is not None:
        request = {"kind": kind, "character": character_id, "offset": offset, "limit": 100, "max_chars": 200000}
        if kind == "events":
            request = {**request, "id": character_id}
        response = reader.handle(request)
        page = response["data"]
        rows.extend(page["items"])
        next_offset = page["next_offset"]
        if next_offset is not None and next_offset <= offset:
            raise ValueError("Notification source returned an invalid continuation")
        offset = next_offset
        date = response.get("date", date)
    return rows, date


def _scope_identity(value):
    """Match command and memory scopes independently of optional display names."""
    if not isinstance(value, dict):
        return value
    if "type" in value:
        if value.get("resolved") is False:
            return {"type": value["type"], "resolved": False}
        identity = value.get("id", value.get("identity", value.get("value", 0)))
        if value["type"] == "boolean":
            identity = bool(identity)
        elif identity == 0xffffffff:
            identity = None
        return {"type": value["type"], "value": identity}
    return {key: _scope_identity(item) for key, item in value.items()}


def _action_identity(item):
    scope = item.get("scope") or {}
    scope = {"root": scope.get("root") or None, "targets": scope.get("targets", scope.get("event_targets", {}))}
    return _json([item["type"], _scope_identity(scope)])


class ImportantActionNotifications:
    def __init__(self, reader):
        self.reader = reader
        self.previous = {}
        self.announced_commands = OrderedDict()
        self.seen_commands = OrderedDict()
        self.commands = deque()

    def feed(self, command):
        if command.get("type") == "create_important_action":
            self.commands.append(deepcopy(command))

    def poll(self, character_id):
        rows, date = _items(self.reader, "alerts", character_id)
        current = {row["id"]: row for row in rows if row.get("character_id") == character_id and row.get("visible", True)}
        if any(not isinstance(row.get("type"), str) or not row["type"] for row in current.values()):
            raise ValueError("An important action type is unavailable")
        events = []
        grouped = defaultdict(list)
        current_keys = {_action_identity(item) for item in current.values()}
        removed = set(self.previous) - set(current)
        for entity_id in sorted(removed):
            item = self.previous[entity_id]
            self.announced_commands.pop(_action_identity(item), None)
            grouped[(item["type"], "removed")].append(item)
        for entity_id, item in current.items():
            previous = self.previous.get(entity_id)
            if previous == item:
                continue
            key = _action_identity(item)
            if previous is None and key in self.announced_commands:
                continue
            grouped[(item["type"], "added" if previous is None else "updated")].append(item)
        # Creation commands can expose short-lived hints between memory polls.
        # Their player field is a machine ID. Recipient isolation uses character.
        seen_commands = self.seen_commands.copy()
        announced = self.announced_commands.copy()
        for command in self.commands:
            fields = command.get("fields") or {}
            if fields.get("character") != character_id:
                continue
            command_key = _json([command.get("default_state"), fields])
            if command_key in seen_commands:
                continue
            seen_commands[command_key] = None
            item = {"type": fields.get("important_action_type"), "character_id": character_id,
                    "scope": deepcopy(fields.get("scope") or {})}
            if not isinstance(item["type"], str) or not item["type"]:
                continue
            key = _action_identity(item)
            if key in current_keys or key in announced:
                continue
            announced[key] = None
            events.append({"event": "game_notification", "namespace": "action", "type": "alert_added",
                           "notification_type": item["type"], "character_id": character_id,
                           "date": date, "source": "native_important_action_command", "data": item})
        for (key, change), items in grouped.items():
            events.append({"event": "game_notification", "namespace": "action", "type": "alert_" + change,
                           "notification_type": key, "character_id": character_id, "date": date,
                           "source": "native_active_important_actions", "data": {"count": len(items), "items": items}})
        self.previous = deepcopy(current)
        self.commands.clear()
        self.announced_commands = OrderedDict(list(announced.items())[-4096:])
        self.seen_commands = OrderedDict(list(seen_commands.items())[-4096:])
        return events


class PlayerMessageNotifications:
    def __init__(self, reader):
        self.reader = reader
        self.previous = set()

    def poll(self, character_id):
        rows, date = _items(self.reader, "player_messages", character_id)
        now = tuple(map(int, date.split(".")))
        due = [row for row in rows if row.get("character_id") == character_id
               and tuple(map(int, row["arrival_date"].split("."))) <= now]
        events = [{"event": "game_notification", "namespace": "message", "type": "player_message",
                   "notification_type": row.get("type") or "custom_message", "character_id": character_id,
                   "date": date, "source": "native_player_message_queue", "data": row}
                  for row in due if row["id"] not in self.previous]
        self.previous = {row["id"] for row in due}
        return events


def _extract_text(obj):
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        if isinstance(obj.get("text"), str) and obj["text"].strip():
            return obj["text"].strip()
        for variant in obj.get("variants") or []:
            found = _extract_text(variant)
            if found:
                return found
    return ""


def _prefetch_event_preview(reader, event_id, character_id):
    try:
        ev = reader.handle({"kind": "event", "id": event_id, "character": character_id, "max_chars": 32000})["data"]
        opts = reader.handle({"kind": "event_options", "id": event_id, "character": character_id,
                              "limit": 12, "max_chars": 64000})["data"]
    except Exception:
        return None
    defn = ev.get("definition") or {}
    title = _extract_text(defn.get("title")) or ev.get("event")
    descs = [_extract_text(d) for d in (defn.get("descriptions") or [])]
    desc_text = " ".join(" ".join(d.split()) for d in descs if d)
    if len(desc_text) > 260:
        desc_text = desc_text[:257] + "..."
    raw_items = list(opts.get("items") or [])
    visible_items = [opt for opt in raw_items if opt.get("shown") is not False]
    if visible_items:
        raw_items = visible_items
    compact_opts = []
    for opt in raw_items:
        entry = {
            "index": opt.get("index"),
            "label": _extract_text(opt.get("name")) or f"选项{opt.get('index')}",
            "available": opt.get("available"),
        }
        if opt.get("effect_summary"):
            entry["effect_summary"] = opt["effect_summary"][:200]
        if opt.get("probabilities"):
            entry["probabilities"] = opt["probabilities"][:5]
        compact_opts.append(entry)
    return {"title": title, "desc": desc_text or None, "options": compact_opts}


def _prefetch_interaction_preview(reader, interaction_id, character_id):
    try:
        detail = reader.handle({"kind": "incoming_interaction", "id": interaction_id,
                                "character": character_id, "max_chars": 6000})["data"]
    except Exception:
        return None
    preview = {}
    if detail.get("response_options"):
        preview["response_options"] = detail["response_options"]
    display = detail.get("display") or {}
    text = display.get("text") or display.get("description")
    if isinstance(text, str) and text.strip():
        preview["text"] = " ".join(text.split())[:240]
    return preview or None


def _prefetch_invitation_preview(reader, activity_id, character_id):
    try:
        detail = reader.handle({"kind": "activity_invitation", "id": activity_id,
                                "character": character_id, "max_chars": 6000})["data"]
    except Exception:
        return None
    preview = {}
    if detail.get("response_options"):
        preview["response_options"] = detail["response_options"]
    if detail.get("location"):
        preview["location"] = detail["location"]
    return preview or None


class PendingEventNotifications:
    def __init__(self, reader):
        self.reader = reader
        self.previous = set()

    def poll(self, character_id):
        rows, date = _items(self.reader, "events", character_id)
        rows = [row for row in rows if row.get("character_id") == character_id]
        events = []
        for row in rows:
            if row["id"] in self.previous:
                continue
            enriched = deepcopy(row)
            preview = _prefetch_event_preview(self.reader, row["id"], character_id)
            if preview:
                enriched["inline_preview"] = preview
            events.append({"event": "game_notification", "namespace": "state", "type": "pending_event",
                           "character_id": character_id, "date": date, "source": "native_pending_events",
                           "data": enriched})
        self.previous = {row["id"] for row in rows}
        return events


class PendingInteractionNotifications:
    def __init__(self, reader):
        self.reader = reader
        self.previous = set()

    def poll(self, character_id):
        rows, date = _items(self.reader, "incoming_interactions", character_id)
        rows = [row for row in rows if row.get("character_id") == character_id]
        events = []
        for row in rows:
            if (row["id"], row["role"]) in self.previous:
                continue
            enriched = deepcopy(row)
            preview = _prefetch_interaction_preview(self.reader, row["id"], character_id)
            if preview:
                enriched["inline_preview"] = preview
            events.append({"event": "game_notification", "namespace": "state", "type": "pending_interaction",
                           "character_id": character_id, "date": date, "source": "native_pending_interactions",
                           "data": enriched})
        self.previous = {(row["id"], row["role"]) for row in rows}
        return events


class ActivityInvitationNotifications:
    def __init__(self, reader):
        self.reader = reader
        self.previous = set()

    def poll(self, character_id):
        rows, date = _items(self.reader, "activity_invitations", character_id)
        rows = [row for row in rows if row.get("character_id") == character_id]
        events = []
        for row in rows:
            if row["id"] in self.previous:
                continue
            enriched = deepcopy(row)
            preview = _prefetch_invitation_preview(self.reader, row["id"], character_id)
            if preview:
                enriched["inline_preview"] = preview
            events.append({"event": "game_notification", "namespace": "state", "type": "activity_invitation",
                           "character_id": character_id, "date": date, "source": "native_activity_invitations",
                           "data": enriched})
        self.previous = {row["id"] for row in rows}
        return events


class CharacterStateNotifications:
    """Derive explicit state transitions for the controlled character."""

    def __init__(self, reader):
        self.reader = reader
        self.previous = None

    def poll(self, character_id):
        response = self.reader.handle({"kind": "character", "id": character_id,
                                       "fields": ["identity", "domain", "family", "traits", "resources"],
                                       "max_chars": 200000})
        data = response["data"]
        identity, domain = data.get("identity") or {}, data.get("domain") or {}
        gold = (data.get("resources") or {}).get("gold")
        stress = (data.get("resources") or {}).get("stress")
        family = deepcopy(data.get("family"))
        if family:
            for key in ("spouse_ids", "child_ids"):
                family[key] = sorted(family.get(key, []))
        current = {
            "character_died": identity.get("alive"),
            "titles_changed": sorted(domain.get("title_ids", [])),
            "primary_title_changed": (domain.get("primary_title") or {}).get("id"),
            "capital_changed": domain.get("capital_title_id"),
            "government_changed": domain.get("government"),
            "liege_changed": domain.get("liege_id"),
            "succession_changed": domain.get("succession_ids", []),
            "laws_changed": sorted(domain.get("laws", []), key=str),
            "family_changed": family,
            "traits_changed": sorted(data.get("traits") or [], key=_json),
            "faith_changed": identity.get("faith"),
            "culture_changed": identity.get("culture"),
            "in_debt": gold < 0 if gold is not None else None,
            "stress_level_changed": min(3, max(0, stress // 100)) if stress is not None else None,
        }
        events = []
        if self.previous is not None:
            for key, value in current.items():
                before = self.previous[key]
                if before == value:
                    continue
                event_type = key
                if key == "in_debt":
                    if before is None or value is None:
                        continue
                    event_type = "debt_entered" if value else "debt_cleared"
                if key == "character_died" and not (before is True and value is False):
                    continue
                events.append({"event": "game_notification", "namespace": "state", "type": event_type,
                               "character_id": character_id, "date": response["date"],
                               "source": "semantic_state_change", "data": {"before": before, "after": value}})
        self.previous = deepcopy(current)
        return events


class SiegeNotifications:
    def __init__(self, reader):
        self.reader = reader
        self.previous = {}

    def poll(self, character_id):
        rows, date = _items(self.reader, "sieges", character_id)
        current = {row["id"]: row for row in rows}
        events = []
        for identifier, siege in current.items():
            previous = self.previous.get(identifier)
            event_type = "siege_started" if previous is None else None
            if previous is not None and siege["effects"]["breach"] > previous["effects"]["breach"]:
                event_type = "siege_breach"
            if event_type:
                events.append({"event": "game_notification", "type": event_type, "character_id": character_id,
                               "date": date, "source": "native_siege_state", "data": siege})
        for identifier in self.previous.keys() - current.keys():
            previous = self.previous[identifier]
            province = self.reader.handle({"kind": "province", "id": previous["province_id"],
                                           "fields": ["occupying_character_id", "occupying_realm"]})
            events.append({"event": "game_notification", "type": "siege_ended", "character_id": character_id,
                           "date": province["date"], "source": "native_siege_state",
                           "data": {"siege_id": identifier, "province_id": previous["province_id"],
                                    "side": previous["side"], "occupation": province["data"]}})
        self.previous = deepcopy(current)
        return events


SOURCE_COVERAGE = {
    "military": "已接入保留战报、交战、行军、撤退和战争变化",
    "sieges": "已接入本人主持或本人封臣领地受围的围城、缺口、解除、原生进度点与阶段剩余天数，百分比、速度和 ETA 的原生结果待定位",
    "important_actions": "已接入按角色保留的提示和创建命令，AI 角色自身的原生提示生成仍待实机核对",
    "player_messages": "已接入原生保留消息队列，该队列的日常消息覆盖仍待核对",
    "pending_events": "已接入待处理事件、原生选项数量和作用域名称替换，原生条件结果与复杂动态正文待定位",
    "pending_interactions": "已接入按接收角色和待回复中间人筛选的原生人物请求队列，详情读取已有原生信件正文缓存，动态标题和按钮可用性待补，实机验收待做",
    "activity_invitations": "已接入角色的原生活动邀请列表和模型指定的参加、拒绝指令，动态正文、按钮可用性、自定义旅行草案与实机验收待做",
    "character_state": "已接入控制角色的头衔、领主、继承、家庭、特质、信仰、文化、法律、负债、压力等级和死亡变化",
    "message_feed": "message_feed 可按需读取本机 GUI 已生成的标题、正文和效果文字。接收角色归属与自动通知接入待补，日志集合访问器已定位",
    "suggestions": "已收录静态目录，运行时来源待接入",
}


class NotificationHub:
    def __init__(self, reader, *, catalog=None, policy=None, military_factory=None, persistent_cooldown=0.0):
        if military_factory is None:
            from live_objects import DirectNotifications
            military_factory = DirectNotifications
        self.reader = reader
        self.catalog = catalog or NotificationCatalog(reader.memory.memory.executable)
        self.policy = policy or NotificationPolicy()
        self.military_factory = military_factory
        self.persistent_cooldown = max(0.0, float(persistent_cooldown))
        self.character_id = None
        self.epoch = None
        self.last_tick = None
        self.sources = {}
        self.errors = {}
        self.transient_strikes = defaultdict(int)
        self.pending = deque()
        self.details = OrderedDict()
        self.seen_oneshot = OrderedDict()
        self.last_emitted = {}
        self.sequence = 0
        self.generation = uuid.uuid4().hex[:12]
        self.commands = deque()

    def reset(self, character_id=None):
        self.character_id = character_id
        self.sources = {
            "military": self.military_factory(self.reader),
            "important_actions": ImportantActionNotifications(self.reader),
            "player_messages": PlayerMessageNotifications(self.reader),
            "pending_events": PendingEventNotifications(self.reader),
            "pending_interactions": PendingInteractionNotifications(self.reader),
            "activity_invitations": ActivityInvitationNotifications(self.reader),
            "character_state": CharacterStateNotifications(self.reader),
            "sieges": SiegeNotifications(self.reader),
        }
        self.pending.clear()
        self.details.clear()
        self.seen_oneshot.clear()
        self.last_emitted.clear()
        self.errors.clear()
        self.generation = uuid.uuid4().hex[:12]
        self.sequence = 0

    def feed(self, command):
        if command.get("type") == "create_important_action":
            self.commands.append(deepcopy(command))

    def _context(self, runtime):
        return runtime.get("world"), runtime.get("world_data")

    def synchronize(self, character_id, runtime):
        epoch = self._context(runtime)
        tick = runtime.get("tick")
        rewound = tick is not None and self.last_tick is not None and tick < self.last_tick
        if character_id != self.character_id or epoch != self.epoch or rewound:
            if self.epoch is not None and (epoch != self.epoch or rewound):
                self.commands.clear()
            self.reset(character_id)
        self.epoch, self.last_tick = epoch, tick

    def collect(self, character_id, *, runtime=None):
        if runtime is None:
            runtime = self.reader.session_snapshot()
        self.synchronize(character_id, runtime)
        epoch = self.epoch
        started_tick = runtime.get("tick")
        while self.commands:
            self.sources["important_actions"].feed(self.commands.popleft())
        self.policy.reload()
        for source, producer in self.sources.items():
            try:
                updates = producer.poll(character_id)
                observed = self.reader.runtime
                rewound = started_tick is not None and observed.get("tick", started_tick) < started_tick
                if self._context(observed) != epoch or rewound:
                    self.reset(character_id)
                    self.epoch = self._context(observed)
                    self.last_tick = observed.get("tick")
                    return
                if source in self.errors:
                    del self.errors[source]
                self.transient_strikes.pop(source, None)
                for event in updates:
                    if event.get("character_id") != character_id:
                        continue
                    event = self.catalog.format_event(event)
                    if self.policy.allows(event):
                        self._enqueue(event)
            except (OSError, ValueError, KeyError, TypeError) as error:
                message = str(error)
                transient = ("world kept changing" in message.casefold()
                             or "siege province is unavailable" in message.casefold())
                if transient:
                    self.transient_strikes[source] += 1
                    if self.transient_strikes[source] < 3:
                        continue
                if self.errors.get(source) != message:
                    self._enqueue({"event": "notification_error", "source": source,
                                   "character_id": character_id, "message": message})
                self.errors[source] = message
        if self.policy.error and self.errors.get("policy") != self.policy.error:
            self._enqueue({"event": "notification_error", "source": "policy", "character_id": character_id,
                           "message": self.policy.error})
        if self.policy.error:
            self.errors["policy"] = self.policy.error
        else:
            self.errors.pop("policy", None)
        # Preferences also apply to events waiting for a later budgeted delivery.
        self.pending = deque(event for event in self.pending if self.policy.allows(event))

    def _try_coalesce(self, event):
        if event.get("event") != "game_notification":
            return False
        notification_id = event.get("notification_id")
        character_id = event.get("character_id")
        if event.get("source") == "semantic_state_change" and isinstance(event.get("data"), dict):
            for index in range(len(self.pending) - 1, -1, -1):
                existing = self.pending[index]
                if (existing.get("source") == "semantic_state_change"
                        and existing.get("notification_id") == notification_id
                        and existing.get("character_id") == character_id
                        and isinstance(existing.get("data"), dict)):
                    existing["data"]["after"] = deepcopy(event["data"].get("after"))
                    existing["date"] = event.get("date", existing.get("date"))
                    if existing["data"].get("before") == existing["data"].get("after"):
                        del self.pending[index]
                    return True
        if event.get("namespace") == "message" and notification_id:
            for existing in reversed(self.pending):
                if (existing.get("namespace") == "message"
                        and existing.get("notification_id") == notification_id
                        and existing.get("character_id") == character_id
                        and existing.get("date") == event.get("date")):
                    data = existing.get("data")
                    if isinstance(data, dict) and "items" in data and "count" in data:
                        data["items"].append(deepcopy(event.get("data")))
                        data["count"] = len(data["items"])
                    else:
                        existing["data"] = {"count": 2, "items": [data, deepcopy(event.get("data"))]}
                    return True
        if notification_id == "military.movement_stopped" and isinstance(event.get("data"), dict):
            unit_key = event["data"].get("unit", event["data"].get("unit_id"))
            if unit_key is not None:
                for existing in reversed(self.pending):
                    existing_data = existing.get("data")
                    if (existing.get("notification_id") == notification_id
                            and existing.get("character_id") == character_id
                            and isinstance(existing_data, dict)
                            and existing_data.get("unit", existing_data.get("unit_id")) == unit_key):
                        existing["data"] = deepcopy(event["data"])
                        existing["date"] = event.get("date", existing.get("date"))
                        return True
        return False

    @staticmethod
    def _oneshot_key(event):
        if event.get("event") != "game_notification":
            return None
        data = event.get("data")
        if not isinstance(data, dict) or data.get("id") is None:
            return None
        notification_id = event.get("notification_id")
        if notification_id == "state.pending_event":
            return ("pending_event", data["id"])
        if notification_id == "state.pending_interaction":
            return ("pending_interaction", data["id"], data.get("role"))
        if notification_id == "state.activity_invitation":
            return ("activity_invitation", data["id"])
        if notification_id == "military.battle_result":
            return ("battle_result", data["id"])
        return None

    @staticmethod
    def _persistent_key(event):
        if event.get("event") != "game_notification":
            return None
        notification_id = event.get("notification_id")
        if event.get("namespace") == "action" and notification_id:
            return ("action", notification_id)
        if notification_id in ("military.siege_breach", "military.battle_phase_changed", "military.movement_started"):
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            target = data.get("id", data.get("unit", data.get("unit_id")))
            return ("military", notification_id, target)
        if event.get("source") == "semantic_state_change" and notification_id != "state.character_died":
            return ("state", notification_id)
        return None

    def _enqueue(self, event):
        event = deepcopy(event)
        oneshot = self._oneshot_key(event)
        if oneshot is not None:
            if oneshot in self.seen_oneshot:
                return
            self.seen_oneshot[oneshot] = None
            while len(self.seen_oneshot) > 4096:
                self.seen_oneshot.popitem(last=False)
        if self.persistent_cooldown > 0:
            persistent = self._persistent_key(event)
            if persistent is not None:
                now = time.monotonic()
                previous = self.last_emitted.get(persistent)
                if previous is not None and now - previous < self.persistent_cooldown:
                    return
                self.last_emitted[persistent] = now
        if self._try_coalesce(event):
            return
        self.sequence += 1
        event["id"] = f"{self.generation}.{self.sequence}"
        self.pending.append(event)

    def take(self, max_chars=6000, *, metadata=None):
        if type(max_chars) is not int or max_chars <= 0:
            raise ValueError("max_chars must be a positive integer")
        result = {**(metadata or {}), "events": [], "pending_count": len(self.pending)}
        if len(_json(result)) > max_chars:
            raise ValueError("max_chars is too small for notification metadata")
        delivered = 0
        for original in self.pending:
            event = deepcopy(original)
            result["events"].append(event)
            result["pending_count"] = len(self.pending) - delivered - 1
            if len(_json(result)) > max_chars:
                result["events"].pop()
                if delivered:
                    result["pending_count"] += 1
                    break
                event.pop("data", None)
                if "summary" in event:
                    event["summary"] = event["summary"][:200]
                event["details_query"] = {"kind": "notification", "id": event["id"]}
                result["events"].append(event)
                if len(_json(result)) > max_chars:
                    raise ValueError("max_chars cannot fit one notification. Increase max_chars")
            delivered += 1
        for _ in range(delivered):
            event = self.pending.popleft()
            self.details[event["id"]] = event
        while len(self.details) > 2048:
            self.details.popitem(last=False)
        return result

    def poll(self, character_id, *, max_chars=6000, runtime=None):
        self.collect(character_id, runtime=runtime)
        return self.take(max_chars)["events"]

    def lookup(self, identifier):
        if identifier in self.details:
            return deepcopy(self.details[identifier])
        for event in self.pending:
            if event["id"] == identifier:
                return deepcopy(event)
        raise ValueError("Notification details expired or belong to a different character or world")

    def status(self):
        return {"character_id": self.character_id, "pending_count": len(self.pending),
                "coverage": dict(SOURCE_COVERAGE), "source_errors": dict(self.errors),
                "policy_error": self.policy.error}
