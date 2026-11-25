"""Correlate model requests with submitted commands and host echoes."""

from collections import OrderedDict
from copy import deepcopy
import json
import time


def session_identity(context):
    return tuple(context.get(key) for key in
                 ("room", "host", "machineid", "character_id", "world", "world_data", "connected", "active"))


def signature(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def request_identifier(request):
    identifier = request.get("request_id")
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("Tracked actions require a nonempty request_id")
    return identifier


def _comparable_fields(command_type, fields):
    if command_type != "start_activity_command" or not isinstance(fields, dict):
        return fields
    data = fields.get("data")
    if not isinstance(data, dict):
        return fields
    normalized = dict(data)
    for key in ("tenets", "doctrines"):
        values = normalized.get(key)
        if isinstance(values, list):
            stripped = [item for item in values if item != ""]
            if stripped:
                normalized[key] = stripped
            else:
                normalized.pop(key, None)
    return {**fields, "data": normalized}


class ActionFeedback:
    def __init__(self, *, capacity=2048, echo_timeout=30):
        self.records = OrderedDict()
        self.by_header = {}
        self.host = None
        self.context = None
        self.last_tick = None
        self.capacity = capacity
        self.echo_timeout = echo_timeout

    def synchronize(self, context):
        identity = session_identity(context)
        tick = context.get("tick")
        rewound = tick is not None and self.last_tick is not None and tick < self.last_tick
        events = []
        if self.context is not None and (identity != self.context or rewound):
            events = self.cancel_pending("session_changed")
            self.by_header.clear()
        self.context = identity
        self.last_tick = tick
        self.host = context.get("host")
        return events

    def replay(self, request):
        identifier = request_identifier(request)
        record = self.records.get(identifier)
        if record is None:
            return None
        if record["signature"] != signature(request):
            raise ValueError("request_id was already used for a different action")
        return {**self.event(identifier), "duplicate": True}

    def prepare(self, request, native):
        identifier = request_identifier(request)
        if identifier in self.records:
            raise ValueError("The action request was already prepared")
        if len(self.records) >= self.capacity:
            disposable = next((key for key, row in self.records.items()
                               if row["status"] in ("host_echoed", "send_failed", "session_changed",
                                                    "disconnected", "connection_closed")), None)
            if disposable is None:
                raise ValueError("Action feedback storage is full of unconfirmed requests")
            removed = self.records.pop(disposable)
            if self.by_header.get(removed["header_key"]) == disposable:
                self.by_header.pop(removed["header_key"], None)
        header = native["default_state"]
        key = (header["sender"], header["identity"])
        previous = self.by_header.get(key)
        if previous is not None and self.records[previous]["status"] != "host_echoed":
            raise ValueError("Another action already uses this command identity")
        name = request["type"] if request["command"] == "send_game_command" else request["command"]
        self.records[identifier] = {"signature": signature(request), "action": name, "status": "prepared",
                                    "native": deepcopy(native), "header_key": key, "sent_at": None, "error": None}
        self.by_header[key] = identifier

    def event(self, identifier):
        return {"event": "action_feedback", **self.status(identifier)}

    def status(self, identifier):
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("Action status requires the submitted request_id as id")
        if identifier not in self.records:
            raise ValueError("The action request was not found in this session's retained feedback")
        record = self.records[identifier]
        result = {"request_id": identifier, "action": record["action"], "status": record["status"],
                  "effect_verification": "model_state_query_required"}
        if record["error"] is not None:
            result["error"] = record["error"]
        return result

    def submitted(self, identifier):
        record = self.records[identifier]
        if record["status"] == "host_echoed":
            return None
        record.update(status="submitted", sent_at=time.monotonic())
        return self.event(identifier)

    def failed(self, identifier, error):
        record = self.records.get(identifier)
        if record is None:
            return None
        if record["status"] == "host_echoed":
            return None
        record.update(status="send_failed", error=str(error))
        return self.event(identifier)

    def observe(self, command, source_user):
        if self.host is None or source_user != self.host or command.get("channel") != "command":
            return []
        header = command.get("default_state") or {}
        key = (header.get("sender"), header.get("identity"))
        identifier = self.by_header.get(key)
        if identifier is None:
            return []
        record = self.records[identifier]
        expected = record["native"]
        if (record["status"] == "host_echoed" or command.get("type") != expected["type"]
                or _comparable_fields(expected["type"], command.get("fields"))
                != _comparable_fields(expected["type"], expected["fields"])):
            return []
        record.update(status="host_echoed", error=None)
        return [self.event(identifier)]

    def poll(self):
        events = []
        now = time.monotonic()
        for identifier, record in self.records.items():
            if record["status"] == "submitted" and now - record["sent_at"] >= self.echo_timeout:
                record["status"] = "delivery_unconfirmed"
                events.append(self.event(identifier))
        return events

    def cancel_pending(self, reason):
        events = []
        for identifier, record in self.records.items():
            if record["status"] in ("prepared", "submitted", "delivery_unconfirmed"):
                record["status"] = reason
                events.append(self.event(identifier))
        self.by_header.clear()
        return events
