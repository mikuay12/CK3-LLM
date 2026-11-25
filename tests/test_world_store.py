"""Regression tests for durable CK3 world snapshots and deletion slots."""

from pathlib import Path
import tempfile
import unittest

from ck3_llm.world.ai_observation import ObservationInterface
from ck3_llm.protocol.jomini_codec import CLOSE, EQUAL, JominiCodec, OPEN, U32, _property, _scalar, _word
from ck3_llm.protocol.mock_nakama import make_world_fixture
from ck3_llm.world.world_snapshot import WorldDecodeQueue, WorldSnapshot
from ck3_llm.world.world_store import SQLiteWorldStore


def _replace_title(raw, title_id, value):
    span = WorldSnapshot(raw).ranges("landed_titles", "landed_titles", title_id)[0]
    return raw[:span[0]] + value + raw[span[1]:]


class WorldStoreTests(unittest.TestCase):
    def setUp(self):
        self.raw, self.archive = make_world_fixture()
        self.none = _word({name: token for token, name in JominiCodec().tokens.items()}["none"])

    def test_title_deletion_slot_is_skipped_and_queries_stay_usable(self):
        world = WorldSnapshot(self.raw)
        span = world.ranges("landed_titles", "landed_titles")[0]
        deletion = _scalar(U32, 18602) + _word(EQUAL) + self.none
        with_slot = self.raw[:span[1] - 2] + deletion + self.raw[span[1] - 2:]
        world = WorldSnapshot(with_slot)
        self.assertEqual(world.get("landed_titles", "landed_titles", 18602), "none")
        self.assertEqual(world.observation(12345)["held_titles"],
                         [{"id": 1, "key": "c_fixture", "holder": 12345}])
        with self.assertRaises(KeyError):
            world.title(18602)
        with tempfile.TemporaryDirectory() as folder, SQLiteWorldStore(Path(folder) / "world.sqlite3") as store:
            result = store.import_snapshot(world, snapshot_tick=17)
            self.assertEqual(result["titles"], 1)
            self.assertEqual(result["deleted_title_slots"], 1)
            self.assertEqual(store.title("c_fixture")["id"], 1)
            with self.assertRaises(KeyError):
                store.title(18602)
            self.assertEqual(store.observation(12345), world.observation(12345))
            observer = ObservationInterface(store, 12345)
            self.assertEqual(observer.handle({"command": "observe"})["snapshot"]["tick"], None)
            self.assertEqual(observer.handle({"command": "query", "kind": "title",
                                              "id": "c_fixture"})["data"]["holder"], 12345)
            self.assertEqual(store.get("living", 12345, "first_name"), "Synthetic AI")

    def test_import_replacement_is_atomic_and_removes_old_entities(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "world.sqlite3"
            with SQLiteWorldStore(database) as store:
                first = store.import_snapshot(WorldSnapshot(self.raw), snapshot_tick=10)
                self.assertEqual(first["characters"], 1)
                store.append_observed_commands([{
                    "type": "set_character_focus", "channel": "command",
                    "default_state": {"identity": 42}, "fields": {"target": 12345},
                }], source_user_id="host", source_payload_file="000001-rx.bin")
                bad = WorldSnapshot(_replace_title(self.raw, 1, _scalar(U32, 7)))
                with self.assertRaisesRegex(ValueError, "not an object"):
                    store.import_snapshot(bad)
                self.assertEqual(store.source_sha256, first["sha256"])
                self.assertEqual(store.title(1)["data"]["holder"], 12345)
                second = store.import_snapshot(WorldSnapshot(_replace_title(self.raw, 1, self.none)),
                                               snapshot_tick=11)
                self.assertNotEqual(second["sha256"], first["sha256"])
                self.assertEqual(second["titles"], 0)
                with self.assertRaises(KeyError):
                    store.title("c_fixture")
                self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM observed_commands").fetchone()[0], 1)
                self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 2)
                self.assertEqual(store.import_snapshot(WorldSnapshot(_replace_title(self.raw, 1, self.none)))["reused"], True)
            with SQLiteWorldStore(database) as reopened:
                self.assertEqual(reopened.snapshot_metadata()["tick"], 11)
                self.assertEqual(reopened.character(12345)["data"]["first_name"], "Synthetic AI")
                self.assertEqual(reopened.get("date"), 51571296)

    def test_repeated_dead_character_records_are_preserved(self):
        tokens = {name: token for token, name in JominiCodec().tokens.items()}
        dead = _word(OPEN) + _property(tokens["first_name"], _scalar(U32, 7)) + _word(CLOSE)
        record = _scalar(U32, 3) + _word(EQUAL) + dead
        payload = self.raw + _property(tokens["dead_unprunable"],
                                       _word(OPEN) + record + record + _word(CLOSE))
        world = WorldSnapshot(payload)
        with tempfile.TemporaryDirectory() as folder, SQLiteWorldStore(Path(folder) / "world.sqlite3") as store:
            store.import_snapshot(world)
            self.assertEqual(store.character(3), world.character(3))
            self.assertEqual(store.get("dead_unprunable", 3), world.get("dead_unprunable", 3))
            with self.assertRaisesRegex(ValueError, "repeated"):
                store.get("dead_unprunable", 3, "first_name")

    def test_background_decode_can_import_into_sqlite(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "received.bin"
            snapshot.write_bytes(self.archive)
            database = Path(folder) / "world.sqlite3"
            queue = WorldDecodeQueue(database_path=database)
            queue.submit(snapshot, snapshot_tick=99)
            events = queue.finish()
            self.assertEqual(events[0]["event"], "world_snapshot_decoded")
            self.assertEqual(events[0]["database_import"]["tick"], 99)
            with SQLiteWorldStore(database) as store:
                self.assertEqual(store.character(12345)["data"]["first_name"], "Synthetic AI")
                self.assertEqual(store.snapshot_metadata()["source_path"], str(snapshot.resolve()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
