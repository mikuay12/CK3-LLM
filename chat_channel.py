"""Simple, process-safe JSONL chat channel between the player web UI and ModelRuntime."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
import uuid

from notification_policy import _file_lock


DEFAULT_CHAT_PATH = Path(__file__).with_name("chat_channel.jsonl")


def append_chat(path, *, role, text, meta=None):
    path = Path(path)
    text = str(text or "").strip()
    if not text:
        raise ValueError("Chat message text must be nonempty")
    if role not in ("player", "assistant", "system"):
        raise ValueError("Unsupported chat role")
    entry = {
        "id": uuid.uuid4().hex[:12],
        "time": time.strftime("%H:%M:%S"),
        "timestamp": time.time(),
        "role": role,
        "text": text,
    }
    if meta:
        entry["meta"] = meta
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    with _file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as stream:
            stream.write(line)
            stream.flush()
    return entry


def read_chat(path, *, after_id=None, limit=200):
    path = Path(path)
    if not path.exists():
        return []
    messages = []
    with path.open("r", encoding="utf-8") as stream:
        for raw in stream:
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
                if isinstance(item, dict) and item.get("id") and item.get("role"):
                    messages.append(item)
            except ValueError:
                continue
    if after_id:
        for idx, item in enumerate(messages):
            if item.get("id") == after_id:
                messages = messages[idx + 1:]
                break
    return messages[-limit:]


def clear_chat(path):
    path = Path(path)
    with _file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
