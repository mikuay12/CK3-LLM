"""Universal persistent-cursor event watcher for Coding Agents (Antigravity, Codex CLI, etc.).

Reads ck3_client.py's events.jsonl from a persisted byte_offset cursor, prioritizes
modal popups (pending_interaction, pending_event, activity_invitation), formats them
compactly (with hot-prefetched options/consequences), advances the cursor atomically,
and exits cleanly on the next batch or timeout.
"""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from ck3_llm.paths import PROJECT_ROOT
from ck3_llm.model.llm_formatter import format_notification_line
from ck3_llm.model.llm_memory import DEFAULT_MEMORY_PATH, LLMMemoryStore


WATCHED_EVENTS = frozenset({
    "game_notification",
    "notification_error",
    "command_error",
    "game_world_loaded",
    "stopped",
})
MODAL_PRIORITY = {
    "pending_interaction": 0,
    "pending_event": 1,
    "activity_invitation": 2,
    "war_started": 3,
    "battle_started": 4,
    "battle_result": 5,
    "siege_started": 6,
    "siege_ended": 7,
}


def find_latest_events_file(base_dir):
    captures = Path(base_dir) / "captures"
    if not captures.is_dir():
        raise FileNotFoundError(f"Captures directory not found: {captures}")
    candidates = sorted(
        captures.glob("connect-local-*/events.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("No connect-local-*/events.jsonl found under captures/")
    return candidates[0]


def load_cursor(cursor_path, events_file):
    cursor_path = Path(cursor_path)
    if not cursor_path.exists():
        return {"events_file": str(events_file), "byte_offset": 0, "last_event_id": None}
    try:
        data = json.loads(cursor_path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and data.get("events_file") == str(events_file):
            offset = int(data.get("byte_offset", 0))
            size = events_file.stat().st_size if events_file.exists() else 0
            if 0 <= offset <= size:
                return data
    except (OSError, ValueError, TypeError):
        pass
    return {"events_file": str(events_file), "byte_offset": 0, "last_event_id": None}


def save_cursor(cursor_path, cursor_data):
    cursor_path = Path(cursor_path)
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**cursor_data, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=cursor_path.parent, delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, cursor_path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _priority_key(event):
    if event.get("event") == "game_notification":
        return (0, MODAL_PRIORITY.get(event.get("type"), 50))
    if event.get("event") in ("command_error", "notification_error"):
        return (1, 0)
    return (2, 0)


def read_new_events(events_file, byte_offset):
    """Read complete JSONL lines from byte_offset; return (matched_events, new_offset)."""
    events = []
    with Path(events_file).open("rb") as stream:
        stream.seek(byte_offset)
        while True:
            pos = stream.tell()
            raw_line = stream.readline()
            if not raw_line:
                break
            if not raw_line.endswith(b"\n"):
                # Incomplete trailing write; rewind so next read gets the full line.
                stream.seek(pos)
                break
            try:
                record = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(record, dict) and record.get("event") in WATCHED_EVENTS:
                events.append(record)
        new_offset = stream.tell()
    return events, new_offset


def _load_agent_context(base_dir):
    parts = []
    cfg_path = Path(base_dir) / "model_config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            inst = (cfg.get("instructions") or "").strip()
            if inst:
                parts.append(f"【管理台预设定提示词】\n{inst}")
        except (OSError, ValueError):
            pass
    mem_path = Path(base_dir) / DEFAULT_MEMORY_PATH.name
    if mem_path.exists():
        block = LLMMemoryStore(mem_path).render_prompt_block(max_chars=1500).strip()
        if block:
            parts.append(block)
    return "\n\n".join(parts)


def watch_once(events_file, cursor_path, *, timeout=0.0, coalesce=0.35, poll_interval=0.25, reset_cursor=False):
    events_file = Path(events_file).resolve()
    cursor_path = Path(cursor_path).resolve()

    if reset_cursor:
        size = events_file.stat().st_size if events_file.exists() else 0
        cursor = {"events_file": str(events_file), "byte_offset": size, "last_event_id": None}
        save_cursor(cursor_path, cursor)
        return {"status": "reset", "events": [], "cursor": cursor}

    cursor = load_cursor(cursor_path, events_file)
    offset = int(cursor.get("byte_offset", 0))
    deadline = (time.monotonic() + timeout) if timeout and timeout > 0 else None

    while True:
        if events_file.exists():
            matched, new_offset = read_new_events(events_file, offset)
            if matched:
                if coalesce > 0:
                    time.sleep(coalesce)
                    more, new_offset = read_new_events(events_file, new_offset)
                    matched.extend(more)
                matched.sort(key=_priority_key)
                last_id = next((e.get("id") for e in reversed(matched) if e.get("id")), cursor.get("last_event_id"))
                cursor = {"events_file": str(events_file), "byte_offset": new_offset, "last_event_id": last_id}
                save_cursor(cursor_path, cursor)
                return {"status": "events", "events": matched, "cursor": cursor}
            if new_offset != offset:
                # Advance past non-watched transport packets so we don't re-scan them next time.
                offset = new_offset
                cursor = {"events_file": str(events_file), "byte_offset": offset, "last_event_id": cursor.get("last_event_id")}
                save_cursor(cursor_path, cursor)
        if deadline is not None and time.monotonic() >= deadline:
            return {"status": "timeout", "events": [], "cursor": cursor}
        time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, help="connect-local-* capture directory (defaults to latest)")
    parser.add_argument("--events-file", type=Path, help="Explicit path to events.jsonl")
    parser.add_argument("--cursor-file", type=Path, help="Cursor bookmark JSON path (defaults to <capture_dir>/agent_cursor.json)")
    parser.add_argument("--timeout", type=float, default=0.0,
                        help="Seconds to wait before returning empty (0 = wait forever; Codex recommended: 20-30)")
    parser.add_argument("--coalesce", type=float, default=0.35, help="Short burst-merging window in seconds")
    parser.add_argument("--format", choices=("compact", "json"), default="compact")
    parser.add_argument("--include-context", action="store_true",
                        help="Also output model_config.json instructions and enabled llm_memory.json entries")
    parser.add_argument("--reset-cursor", action="store_true", help="Move cursor to current EOF and exit")
    args = parser.parse_args()

    base_dir = PROJECT_ROOT
    if args.events_file:
        events_file = args.events_file
    elif args.capture_dir:
        events_file = args.capture_dir / "events.jsonl"
    else:
        events_file = find_latest_events_file(base_dir)

    cursor_path = args.cursor_file or (events_file.parent / "agent_cursor.json")
    result = watch_once(
        events_file, cursor_path,
        timeout=args.timeout, coalesce=args.coalesce, reset_cursor=args.reset_cursor,
    )

    if args.format == "json":
        out = dict(result)
        if args.include_context:
            out["agent_context"] = _load_agent_context(base_dir)
        print(json.dumps(out, ensure_ascii=False), flush=True)
        return 0

    if args.include_context:
        ctx = _load_agent_context(base_dir)
        if ctx:
            print(ctx, flush=True)
            print("-" * 60, flush=True)

    status = result["status"]
    events = result["events"]
    offset = result["cursor"]["byte_offset"]
    if status == "reset":
        print(f"[游标已重置] events={events_file} offset={offset}", flush=True)
        return 0
    if status == "timeout":
        print(f"[等待超时，无新事件] offset={offset}", flush=True)
        return 0

    modals = [e for e in events if e.get("event") == "game_notification" and e.get("type") in ("pending_interaction", "pending_event", "activity_invitation")]
    header = f"【收到 {len(events)} 条新事件" + (f"（含 {len(modals)} 项优先窗口事务）" if modals else "") + f"】cursor_offset={offset}"
    print(header, flush=True)
    for event in events:
        if event.get("event") in ("game_world_loaded", "stopped"):
            print(f"- [{event['event']}] {json.dumps({k: v for k, v in event.items() if k != 'time'}, ensure_ascii=False)}", flush=True)
        else:
            print("- " + format_notification_line(event).replace("\n", "\n  "), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
