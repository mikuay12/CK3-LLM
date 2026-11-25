"""Regressions for large-world scalars, indexed records and received ZIPs."""

import io
import struct
from pathlib import Path
import tempfile
import unittest
import zipfile

from ck3_llm.protocol.jomini_codec import (
    CLOSE, F32, FIXED_POINT, JominiCodec, OPEN, RGB, U32, U64,
    _property, _scalar, _word,
)
from ck3_llm.protocol.mock_nakama import make_world_fixture
from ck3_llm.world.world_snapshot import WorldDecodeQueue, WorldSnapshot, format_game_date


class WorldSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.raw, self.archive = make_world_fixture()
        self.tokens = {name: token for token, name in JominiCodec().tokens.items()}

    def test_raw_world_indexes_and_observation(self):
        world = WorldSnapshot(self.raw)
        self.assertEqual(world.get("living", "12345", "first_name"), "Synthetic AI")
        self.assertEqual(world.get("living", 12345, "skill"), [6, 8, 6, 3, 8, 5])
        self.assertEqual(world.summary()["record_counts"], {"living": 1, "provinces": 1, "landed_titles": 1})
        observation = world.observation()
        self.assertEqual(observation["date"], "887.2.19")
        self.assertEqual(observation["character"]["id"], 12345)
        self.assertEqual(observation["held_titles"], [{"id": 1, "key": "c_fixture", "holder": 12345}])
        with self.assertRaises(KeyError):
            world.character(999)

    def test_native_world_scalars_and_rgb(self):
        values = b"".join([
            _property(self.tokens["gold"], _word(FIXED_POINT) + struct.pack("<q", -123456)),
            _property(self.tokens["piety"], _word(U64) + struct.pack("<Q", (1 << 64) - 1)),
            _property(self.tokens["health"], _word(F32) + struct.pack("<f", 4.5)),
            _property(self.tokens["color"], _word(RGB) + _word(OPEN)
                      + b"".join(_scalar(U32, n) for n in [1, 2, 3]) + _word(CLOSE)),
        ])
        world = WorldSnapshot(self.raw + _property(self.tokens["variables"], _word(OPEN) + values + _word(CLOSE)))
        self.assertEqual(world.get("variables"), {"gold": -1.23456, "piety": (1 << 64) - 1,
                                                   "health": 4.5, "color": {"rgb": [1, 2, 3]}})

    def test_zip_hotjoin_and_sav_prefixed_zip(self):
        for payload in (self.archive, b"SAV0103fixture\n" + self.archive):
            with self.subTest(prefix=payload[:3]):
                world = WorldSnapshot(payload)
                self.assertEqual(world.data, self.raw)
                self.assertEqual(world.layers[0]["entry"], "hotjoin_0")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("gamestate", self.raw)
        self.assertEqual(WorldSnapshot(output.getvalue()).data, self.raw)
        with self.assertRaises(ValueError):
            WorldSnapshot(self.archive, max_bytes=len(self.raw) - 1)

    def test_repeated_and_mixed_properties_preserve_values(self):
        item = _property(self.tokens["value"], _scalar(U32, 7))
        mixed = _scalar(U32, 18) + item + item
        payload = self.raw + _property(self.tokens["variables"], _word(OPEN) + mixed + _word(CLOSE))
        world = WorldSnapshot(payload)
        self.assertEqual(world.get("variables"), {"properties": {"value": [7, 7]}, "values": [18]})
        duplicate = self.raw + _property(self.tokens["speed"], _scalar(U32, 2))
        self.assertEqual(WorldSnapshot(duplicate).get("speed"), [1, 2])

    def test_invalid_world_boundaries_and_format(self):
        for payload in (self.raw[:-1], b"meta_data={version=1}", b"", self.raw[:5]):
            with self.subTest(payload=payload[:10]), self.assertRaises(ValueError):
                WorldSnapshot(payload)
        with self.assertRaises(ValueError):
            WorldSnapshot(self.raw, max_bytes=len(self.raw) - 1)
        with self.assertRaises(ValueError):
            format_game_date(-1)

    def test_background_decode_completion_and_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            valid = Path(folder) / "received.bin"
            valid.write_bytes(self.archive)
            invalid = Path(folder) / "invalid.bin"
            invalid.write_bytes(b"invalid world fixture")
            minimal = Path(folder) / "minimal.bin"
            minimal.write_bytes(_property(self.tokens["date"], _scalar(U32, 51571296)))
            queue = WorldDecodeQueue()
            queue.submit(valid)
            queue.submit(invalid)
            queue.submit(minimal)
            events = queue.finish()
            self.assertEqual([event["event"] for event in events], ["world_snapshot_decoded", "world_decode_error", "world_snapshot_decoded"])
            self.assertEqual(events[0]["observation"]["character"]["id"], 12345)
            self.assertEqual(events[2]["record_counts"], {})
            self.assertEqual(queue.finish(), [])

    def test_date_epoch_and_non_leap_calendar(self):
        self.assertEqual(format_game_date(51571296), "887.2.19")
        self.assertEqual(format_game_date(51307320), "857.1.1")
        self.assertEqual(format_game_date((5004 * 365 + 59) * 24), "4.3.1")
        self.assertEqual(format_game_date(51571297), "887.2.19 01:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
