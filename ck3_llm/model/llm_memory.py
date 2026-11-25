"""Two-tier JSON memory store (campaign memory + cross-session experience) for CK3 LLM."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from ck3_llm.paths import PROJECT_ROOT
from ck3_llm.gameplay.notification_policy import _file_lock


DEFAULT_MEMORY_PATH = PROJECT_ROOT / "llm_memory.json"
VALID_SCOPES = ("campaign", "experience")
VALID_CATEGORIES = ("pact", "strategy", "diplomacy", "lesson", "general")
CATEGORY_LABELS = {
    "pact": "房主约定",
    "strategy": "战略计划",
    "diplomacy": "外交情报",
    "lesson": "游玩经验",
    "general": "通用备忘",
}
CATEGORY_PRIORITY = {"pact": 0, "strategy": 1, "diplomacy": 2, "lesson": 3, "general": 4}
MAX_ITEMS_PER_SCOPE = 30
MAX_CONTENT_CHARS = 200


def _empty_store():
    return {"version": 1, "items": []}


def _validate_item_fields(scope, category, content):
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {', '.join(VALID_SCOPES)}")
    if category not in VALID_CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(VALID_CATEGORIES)}")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Memory content must be a nonempty string")
    text = " ".join(content.strip().split())
    if len(text) > MAX_CONTENT_CHARS:
        text = text[: MAX_CONTENT_CHARS - 1] + "…"
    return scope, category, text


class LLMMemoryStore:
    """Thread- and process-safe JSON memory store with hot-reload and prompt rendering."""

    def __init__(self, path=DEFAULT_MEMORY_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._revision = "missing"
        self._data = _empty_store()
        self.reload()

    def reload(self):
        with self._lock:
            try:
                raw = self.path.read_bytes()
                rev = hashlib.sha256(raw).hexdigest()
                if rev == self._revision:
                    return
                parsed = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
                    raise ValueError("Invalid llm_memory.json structure")
                items = [item for item in parsed["items"] if isinstance(item, dict) and item.get("id") and item.get("content")]
                self._data = {"version": 1, "items": items}
                self._revision = rev
            except FileNotFoundError:
                self._data = _empty_store()
                self._revision = "missing"
            except (OSError, ValueError):
                return

    def _save_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(self._data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._revision = hashlib.sha256(raw).hexdigest()

    def list_items(self, *, scope=None, enabled_only=False):
        self.reload()
        with self._lock:
            rows = deepcopy(self._data["items"])
        if scope is not None:
            rows = [r for r in rows if r.get("scope") == scope]
        if enabled_only:
            rows = [r for r in rows if r.get("enabled", True)]
        return rows

    def add(self, *, scope="campaign", category="general", content="", source="llm", enabled=True):
        scope, category, text = _validate_item_fields(scope, category, content)
        if source not in ("llm", "player"):
            raise ValueError("source must be 'llm' or 'player'")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, _file_lock(self.path):
            self.reload()
            # Deduplicate exact same scope + content
            for existing in self._data["items"]:
                if existing.get("scope") == scope and existing.get("content") == text:
                    existing["category"] = category
                    existing["enabled"] = bool(enabled)
                    existing["updated_at"] = now
                    self._save_locked()
                    return deepcopy(existing)
            # Enforce per-scope cap
            same_scope = [i for i in self._data["items"] if i.get("scope") == scope]
            if len(same_scope) >= MAX_ITEMS_PER_SCOPE:
                # Drop oldest disabled first, otherwise oldest item in that scope
                victim = next((i for i in same_scope if not i.get("enabled", True)), same_scope[0])
                self._data["items"] = [i for i in self._data["items"] if i.get("id") != victim["id"]]
            entry = {
                "id": uuid.uuid4().hex[:10],
                "scope": scope,
                "category": category,
                "content": text,
                "enabled": bool(enabled),
                "source": source,
                "created_at": now,
                "updated_at": now,
            }
            self._data["items"].append(entry)
            self._save_locked()
            return deepcopy(entry)

    def update(self, item_id, *, scope=None, category=None, content=None, enabled=None):
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("Memory item id is required")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, _file_lock(self.path):
            self.reload()
            for item in self._data["items"]:
                if item.get("id") == item_id:
                    new_scope = item["scope"] if scope is None else scope
                    new_cat = item["category"] if category is None else category
                    new_content = item["content"] if content is None else content
                    new_scope, new_cat, new_content = _validate_item_fields(new_scope, new_cat, new_content)
                    item["scope"] = new_scope
                    item["category"] = new_cat
                    item["content"] = new_content
                    if enabled is not None:
                        item["enabled"] = bool(enabled)
                    item["updated_at"] = now
                    self._save_locked()
                    return deepcopy(item)
            raise ValueError(f"Memory item not found: {item_id}")

    def delete(self, item_id):
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("Memory item id is required")
        with self._lock, _file_lock(self.path):
            self.reload()
            before = len(self._data["items"])
            self._data["items"] = [i for i in self._data["items"] if i.get("id") != item_id]
            if len(self._data["items"]) == before:
                raise ValueError(f"Memory item not found: {item_id}")
            self._save_locked()
            return True

    def clear(self, *, scope=None):
        if scope is not None and scope not in VALID_SCOPES:
            raise ValueError(f"scope must be one of {', '.join(VALID_SCOPES)}")
        with self._lock, _file_lock(self.path):
            self.reload()
            before = len(self._data["items"])
            if scope is None:
                self._data["items"] = []
            else:
                self._data["items"] = [i for i in self._data["items"] if i.get("scope") != scope]
            removed = before - len(self._data["items"])
            self._save_locked()
            return removed

    def render_prompt_block(self, max_chars=1500):
        items = self.list_items(enabled_only=True)
        if not items:
            return ""
        campaign = sorted(
            [i for i in items if i.get("scope") == "campaign"],
            key=lambda x: (CATEGORY_PRIORITY.get(x.get("category"), 9), x.get("updated_at", "")),
        )
        experience = sorted(
            [i for i in items if i.get("scope") == "experience"],
            key=lambda x: (CATEGORY_PRIORITY.get(x.get("category"), 9), x.get("updated_at", "")),
        )
        lines = ["\n【长期记忆与本局约定】（可用 memory_manage 工具增删改）"]
        if campaign:
            lines.append("本局记忆:")
            for item in campaign:
                cat = CATEGORY_LABELS.get(item.get("category"), item.get("category", ""))
                lines.append(f"- [{item['id']}|{cat}] {item['content']}")
        if experience:
            lines.append("跨局经验:")
            for item in experience:
                cat = CATEGORY_LABELS.get(item.get("category"), item.get("category", ""))
                lines.append(f"- [{item['id']}|{cat}] {item['content']}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[: max_chars - 12] + "\n...(更多略)"
        return text

    def snapshot(self):
        items = self.list_items()
        return {
            "path": str(self.path),
            "items": items,
            "counts": {
                "total": len(items),
                "campaign": sum(1 for i in items if i.get("scope") == "campaign"),
                "experience": sum(1 for i in items if i.get("scope") == "experience"),
                "enabled": sum(1 for i in items if i.get("enabled", True)),
            },
            "prompt_preview": self.render_prompt_block(),
            "categories": CATEGORY_LABELS,
        }
