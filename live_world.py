"""Capture the complete current CK3 world and answer budgeted live queries.

The game serializes its world between main-thread update iterations. Native
token indexing makes the resulting binary world available to the existing
observation interface without replaying or simulating the host's commands.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import queue
import sys
import threading
import time
import uuid

from ai_observation import ObservationInterface
from jomini_codec import JominiCodec
from live_memory import _answer, _json, _open_probe, frida
from world_snapshot import MAX_WORLD_BYTES, WorldSnapshot, format_game_date


def _native_source(timeout, host_machineid):
    directory = Path(__file__).resolve().parent
    names = {name: token for token, name in JominiCodec().tokens.items()}
    config = {
        "timeout_ms": round(timeout * 1000), "max_bytes": MAX_WORLD_BYTES,
        "host_machineid": host_machineid,
        "tokens": {name: names[name] for name in ("living", "dead_unprunable", "provinces", "landed_titles")},
    }
    source = directory.joinpath("live_world.js").read_text(encoding="utf-8")
    return source.replace("__NATIVE_WORLD_CONFIG__", _json(config)).replace(
        "__WORLD_INDEX_SOURCE__", json.dumps(directory.joinpath("world_index.c").read_text(encoding="utf-8"))
    )


def _range_indexes(path, groups):
    data = Path(path).read_bytes()
    position = 0
    indexes = {}
    for group in groups:
        start, end, count = (group[name] for name in ("start", "end", "count"))
        if any(type(value) is not int or value < 0 for value in (start, end, count)) or start > end:
            raise ValueError("Invalid native world index metadata")
        key = (start, end, group["wrapped"])
        limit = position + count * 16
        if type(group["wrapped"]) is not bool or key in indexes or limit > len(data):
            raise ValueError("Invalid native world index group")
        indexes[key] = data[position:limit]
        position = limit
    if position != len(data):
        raise ValueError("Native world index has trailing bytes")
    return indexes


class LiveWorldReader:
    def __init__(self, pid=None, *, directory=None, character_id=None, host_machineid=1, timeout=15):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or
                timeout <= 0 or timeout > 300):
            raise ValueError("timeout must be between 0 and 300 seconds")
        if type(host_machineid) is not int or not 0 <= host_machineid <= 63:
            raise ValueError("Invalid host machine ID")
        self.host_machineid = host_machineid
        self.directory = Path(directory).resolve() if directory is not None else (
            Path(__file__).resolve().parent / "captures" /
            f"live-world-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        self.observations = ObservationInterface(character_id=character_id)
        self.metadata = None
        self._responses = {}
        self._lock = threading.RLock()
        self.session, self.script = _open_probe(pid, extra_source=_native_source(timeout, host_machineid))

    def close(self):
        with self._lock:
            if self.session is not None:
                try:
                    self.script.exports_sync.shutdown()
                except frida.InvalidOperationError:
                    pass
                finally:
                    try:
                        self.session.detach()
                    except frida.InvalidOperationError:
                        pass
                    self.session = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def players(self):
        return self.script.exports_sync.inspect({"kind": "players"})["players"]

    def _host(self, players):
        host = next((player for player in players if player["machineid"] == self.host_machineid), None)
        if host is None:
            raise ValueError(f"Host player {self.host_machineid} is unavailable in the current world")
        return host

    def refresh(self):
        with self._lock:
            identifier = f"world-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
            world_path = self.directory / f"{identifier}.bin"
            index_path = self.directory / f"{identifier}.index"
            world_part = world_path.with_suffix(".bin.part")
            index_part = index_path.with_suffix(".index.part")
            manifest = world_path.with_suffix(".json")
            manifest_part = manifest.with_suffix(".json.part")
            started = time.perf_counter()
            try:
                native = self.script.exports_sync.snapshot({
                    "world_path": world_part.as_posix(), "index_path": index_part.as_posix(),
                })
                raw = world_part.read_bytes()
                if len(raw) != native["bytes"]:
                    raise ValueError("Native world file size does not match its capture")
                indexes = _range_indexes(index_part, native["index_groups"])
                world = WorldSnapshot(raw, range_indexes=indexes)
                if not {"date", "living", "provinces", "landed_titles"}.issubset(world._root):
                    raise ValueError("The serialized world is missing required sections")
                counts = {name: sum(map(len, world.entries(name).values()))
                          for name in ("living", "dead_unprunable", "provinces") if name in world._root}
                counts["landed_titles"] = sum(map(len, world.entries("landed_titles", "landed_titles").values()))
                host = self._host(native["players_before"])
                if world.get("date") != native["date_raw"]:
                    raise ValueError("Serialized world date does not match the captured native clock")
                metadata = {
                    **native, "host_reported_turn": host["turn"], "host_machineid": self.host_machineid,
                    "date_raw": world.get("date"), "date": format_game_date(world.get("date")),
                    "sha256": world.source_sha256, "index_sha256": hashlib.sha256(index_part.read_bytes()).hexdigest(),
                    "file": str(world_path), "index_file": str(index_path), "record_counts": counts,
                    "sections": {str(key): len(spans) for key, spans in world._root.items()},
                    "ready_ms": round((time.perf_counter() - started) * 1000, 3),
                }
                manifest_part.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
                world_part.replace(world_path)
                index_part.replace(index_path)
                manifest_part.replace(manifest)
                source = {key: metadata[key] for key in (
                    "source", "method", "pid", "captured_at", "tick_source", "host_reported_turn"
                )}
                source["continuous_state_updates"] = "on_demand"
                self.observations.set_snapshot(world, metadata["tick"], source_metadata=source)
                self.metadata = metadata
                return deepcopy(metadata)
            finally:
                world_part.unlink(missing_ok=True)
                index_part.unlink(missing_ok=True)
                manifest_part.unlink(missing_ok=True)

    def _synchronization(self):
        host = self._host(self.players())
        clock = self.script.exports_sync.clock()
        self.observations.source_metadata["latest_host_reported_turn"] = host["turn"]
        self.observations.set_synchronization({
            "hotjoin": {"sender": self.host_machineid},
            "peer_turns": [{"machineid": self.host_machineid, "turn": clock["tick"]}],
        })

    def handle(self, request):
        if not isinstance(request, dict):
            raise ValueError("Live world request must be a JSON object")
        request_id = request.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or not request_id):
            raise ValueError("request_id must be a nonempty string")
        signature = json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with self._lock:
            if request_id in self._responses:
                previous_signature, result = self._responses[request_id]
                if signature != previous_signature:
                    raise ValueError("request_id was already used for a different live world request")
                return deepcopy(result)
            result = self._handle(request)
            if request_id is not None:
                self._responses[request_id] = (signature, deepcopy(result))
            return result

    def _handle(self, request):
        if request.get("kind") in ("players", "character_focus", "controlled_character"):
            return _answer(self.script, request)
        command = request.get("command")
        if command == "refresh_world":
            metadata = self.refresh()
            return {"event": "live_world_ready", "request_id": request.get("request_id"), **metadata}
        if command not in ("observe", "query", "observation_status"):
            raise ValueError("Live world commands are refresh_world, observe, query and observation_status")
        refresh = request.get("refresh", command != "observation_status")
        if type(refresh) is not bool:
            raise ValueError("refresh must be a boolean")
        captured = command != "observation_status" and (refresh or self.observations.world is None)
        if captured:
            self.refresh()
        self.observations.source_metadata["cached"] = not captured
        if command == "observe" and "character" not in request and self.observations.character_id is None:
            host = self._host(self.metadata["players_before"])
            if host["character_id"] != 0xFFFFFFFF:
                self.observations.set_character(host["character_id"])
        if self.observations.world is not None:
            self._synchronization()
        result = self.observations.handle(request)
        return {"event": "live_world_response", "request_id": request.get("request_id"), "result": result}


class LiveObservationQueue:
    """Keep complete-world captures off the Nakama SDK polling thread."""

    def __init__(self, pid, directory):
        self.pid = pid
        self.directory = directory
        self.reader = None
        self.executor = None
        self.pending = {}
        self.commands = queue.SimpleQueue()

    def _handle(self, request):
        if self.reader is None:
            self.reader = LiveWorldReader(self.pid, directory=self.directory)
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            self.reader.observations.feed(command)
        response = self.reader.handle(request)
        return response.get("result", response)

    def feed(self, command):
        self.commands.put(deepcopy(command))

    def submit(self, request, character_id):
        request = deepcopy(request)
        if request.get("command") == "observe" and "character" not in request:
            request["character"] = character_id
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ck3-live-world")
        future = self.executor.submit(self._handle, request)
        self.pending[future] = request.get("request_id")

    def poll(self):
        events = []
        for future in list(self.pending):
            if not future.done():
                continue
            request_id = self.pending.pop(future)
            try:
                events.append({"event": "interface_response", "request_id": request_id, "result": future.result()})
            except Exception as error:
                events.append({"event": "interface_error", "request_id": request_id,
                               "message": str(error), "error_type": type(error).__name__})
        return events

    def finish(self):
        if self.executor is not None:
            self.executor.shutdown(wait=True)
            self.executor = None
        events = self.poll()
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        return events


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--directory", type=Path, help="Local capture directory for complete worlds and verification manifests")
    parser.add_argument("--character", type=int, help="Default character for personal observations")
    parser.add_argument("--request-file", type=Path)
    parser.add_argument("--stdin", action="store_true", help="Answer one JSON request per line with fresh worlds by default")
    args = parser.parse_args()
    try:
        with LiveWorldReader(args.pid, directory=args.directory, character_id=args.character) as reader:
            if args.stdin:
                for line in sys.stdin:
                    if not line.strip():
                        continue
                    try:
                        print(_json(reader.handle(json.loads(line))), flush=True)
                    except (OSError, ValueError, KeyError, TypeError, StopIteration,
                            frida.InvalidOperationError, frida.RPCException) as error:
                        print(_json({"event": "live_world_error", "message": str(error)}), flush=True)
            else:
                request = ({"command": "refresh_world"} if args.request_file is None else
                           json.loads(args.request_file.read_text(encoding="utf-8-sig")))
                print(_json(reader.handle(request)))
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError, TypeError, StopIteration, frida.ProcessNotFoundError,
            frida.InvalidOperationError, frida.RPCException) as error:
        print(_json({"event": "live_world_error", "message": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
