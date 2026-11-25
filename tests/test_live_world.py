"""Regressions for native indexes and transactional, idempotent live queries."""

import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from ck3_llm.engine.live_world import LiveObservationQueue, LiveWorldReader, _range_indexes
from ck3_llm.protocol.mock_nakama import make_world_fixture
from ck3_llm.world.world_snapshot import WorldSnapshot


def range_rows(world, start, end, wrapped):
    position = start + 2 if wrapped else start
    limit = end - 2 if wrapped else end
    rows = bytearray()
    while position < limit:
        key_end = world._token_end(position)[1]
        value_start = key_end + 2
        value_end = world._value_end(value_start)
        rows.extend(struct.pack("<IIII", position, key_end, value_start, value_end))
        position = value_end
    return bytes(rows)


class FakeNativeWorld:
    def __init__(self):
        self.raw, _ = make_world_fixture()
        self.calls = 0
        self.malformed = False
        self.turn = 200
        self.date_offset = 0
        self.exports_sync = self

    def inspect(self, request):
        return {"in_game": True, "players": [{"machineid": 1, "name": "Host",
                "character_id": 12345, "flags": 215, "turn": self.turn}]}

    def shutdown(self):
        return True

    def clock(self):
        return {"tick": self.turn}

    def snapshot(self, request):
        self.calls += 1
        world = WorldSnapshot(self.raw)
        rows = range_rows(world, 0, len(self.raw), False)
        Path(request["world_path"]).write_bytes(self.raw)
        Path(request["index_path"]).write_bytes(rows[:-1] if self.malformed else rows)
        players = self.inspect({})["players"]
        return {"source": "live_host_memory", "method": "native_hotjoin_serializer", "pid": 42,
                "captured_at": "2026-11-14T14:00:00Z", "bytes": len(self.raw),
                "tick": self.turn, "date_raw": world.get("date") + self.date_offset,
                "tick_source": "game_synchronization_manager",
                "players_before": players, "players_after": players,
                "index_groups": [{"start": 0, "end": len(self.raw), "wrapped": False, "count": len(rows) // 16}]}


class FakeSession:
    def detach(self):
        pass


class LiveWorldTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.native = FakeNativeWorld()
        self.probe_patch = patch("ck3_llm.engine.live_world._open_probe", return_value=(FakeSession(), self.native))
        self.probe_patch.start()
        self.reader = LiveWorldReader(directory=self.folder.name)

    def tearDown(self):
        self.reader.close()
        self.probe_patch.stop()
        self.folder.cleanup()

    def test_native_ranges_match_python_index_and_reject_bad_boundaries(self):
        world = WorldSnapshot(self.native.raw)
        rows = range_rows(world, 0, len(world.data), False)
        indexed = WorldSnapshot(self.native.raw, range_indexes={(0, len(world.data), False): rows})
        self.assertEqual(indexed._root, world._root)
        self.assertEqual(indexed.observation(), world.observation())
        for invalid in (rows[:-1], rows[:-16], bytes(16) + rows[16:]):
            with self.subTest(size=len(invalid)), self.assertRaises(ValueError):
                WorldSnapshot(self.native.raw, range_indexes={(0, len(world.data), False): invalid})

    def test_fresh_queries_and_cached_query_refresh_semantics(self):
        request = {"command": "observe", "max_chars": 2000}
        first = self.reader.handle(request)["result"]
        self.assertEqual(first["snapshot"]["source"], "live_host_memory")
        self.assertEqual(first["snapshot"]["continuous_state_updates"], "on_demand")
        self.assertEqual(first["character_id"], 12345)
        self.assertEqual(first["mode"], "full")
        second = self.reader.handle(request)["result"]
        self.assertEqual(second["mode"], "delta")
        self.assertEqual(second["personal"], {})
        self.assertEqual(self.native.calls, 2)
        self.native.turn += 1
        cached = self.reader.handle({**request, "refresh": False})["result"]
        self.assertTrue(cached["snapshot"]["refresh_needed"])
        self.assertTrue(cached["snapshot"]["cached"])
        self.assertEqual(self.native.calls, 2)
        fresh = self.reader.handle(request)["result"]
        self.assertFalse(fresh["snapshot"]["refresh_needed"])
        self.assertFalse(fresh["snapshot"]["cached"])
        self.assertEqual(fresh["snapshot"]["tick"], self.native.turn)

    def test_idempotent_requests_reuse_response_and_delta(self):
        request = {"command": "observe", "request_id": "first"}
        first = self.reader.handle(request)
        self.assertEqual(first, self.reader.handle(request))
        self.assertEqual(self.native.calls, 1)
        first["result"]["personal"].clear()
        self.assertTrue(self.reader.handle(request)["result"]["personal"])
        with self.assertRaises(ValueError):
            self.reader.handle({**request, "max_chars": 700})
        delta = self.reader.handle({"command": "observe", "refresh": False})["result"]
        self.assertEqual(delta["personal"], {})

    def test_failed_refresh_keeps_previous_world_and_removes_part_files(self):
        self.reader.refresh()
        previous = self.reader.metadata
        world = self.reader.observations.world
        self.native.malformed = True
        with self.assertRaises(ValueError):
            self.reader.refresh()
        self.assertEqual(self.reader.metadata, previous)
        self.assertIs(self.reader.observations.world, world)
        self.assertEqual(list(Path(self.folder.name).glob("*.part")), [])
        self.assertEqual(len(list(Path(self.folder.name).glob("*.bin"))), 1)
        self.assertTrue(Path(previous["file"]).exists())

    def test_selective_queries_and_saved_manifest(self):
        result = self.reader.handle({"command": "query", "kind": "character", "id": 12345,
                                     "fields": ["first_name", "skill"]})["result"]
        self.assertEqual(result["data"]["first_name"], "Synthetic AI")
        metadata = self.reader.metadata
        manifest = json.loads(Path(metadata["file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["sha256"], self.reader.observations.world.source_sha256)
        self.assertEqual(manifest["record_counts"]["living"], 1)
        indexes = _range_indexes(metadata["index_file"], metadata["index_groups"])
        self.assertEqual(WorldSnapshot(Path(metadata["file"]).read_bytes(), range_indexes=indexes)._root,
                         self.reader.observations.world._root)

    def test_native_clock_mismatch_preserves_previous_world(self):
        self.reader.refresh()
        previous = self.reader.metadata
        self.native.date_offset = 24
        with self.assertRaisesRegex(ValueError, "native clock"):
            self.reader.refresh()
        self.assertEqual(self.reader.metadata, previous)
        self.assertEqual(len(list(Path(self.folder.name).glob("*.bin"))), 1)

    def test_status_before_capture_does_not_require_an_active_world(self):
        with patch.object(self.native, "inspect", return_value={"in_game": False, "players": []}):
            status = self.reader.handle({"command": "observation_status"})["result"]
        self.assertFalse(status["world_ready"])
        self.assertIsNone(status["snapshot"])
        self.assertEqual(self.native.calls, 0)

    def test_queued_live_queries_keep_relevant_commands_and_failures(self):
        requests = LiveObservationQueue(42, self.folder.name)
        requests.reader = self.reader
        try:
            requests.submit({"command": "observe", "request_id": "before"}, 12345)
            next(iter(requests.pending)).result(timeout=5)
            self.assertEqual(requests.poll()[0]["result"]["mode"], "full")
            requests.feed({"type": "set_character_focus", "default_state": {"identity": 5, "sender": 1},
                           "fields": {"target": 12345, "actor": 12345, "focus_type": "martial_strategy_focus"}})
            requests.submit({"command": "observe", "request_id": "cached", "refresh": False}, 12345)
            next(iter(requests.pending)).result(timeout=5)
            result = requests.poll()[0]["result"]
            self.assertTrue(result["snapshot"]["refresh_needed"])
            self.assertEqual(result["observed_commands"][0]["type"], "set_character_focus")
            self.assertEqual(self.native.calls, 1)
            self.native.malformed = True
            requests.submit({"command": "observe", "request_id": "bad"}, 12345)
            future = next(iter(requests.pending))
            with self.assertRaises(ValueError):
                future.result(timeout=5)
            self.assertEqual(requests.poll()[0]["event"], "interface_error")
            self.native.malformed = False
            requests.submit({"command": "observe", "request_id": "recovered"}, 12345)
            next(iter(requests.pending)).result(timeout=5)
            recovered = requests.poll()[0]["result"]
            self.assertFalse(recovered["snapshot"]["refresh_needed"])
        finally:
            requests.finish()


if __name__ == "__main__":
    unittest.main()
