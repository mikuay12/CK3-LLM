"""Regress native rendered text, coherent reads and recipient attribution."""

import unittest
from unittest.mock import patch

from building_economy import APPLICATION_RVA, HOLDING_VIEW_CHAIN
from live_objects import DirectWorldReader
from memory_world import MemoryWorld
from test_siege_progress import BufferMemory


class FeedFixture:
    def __init__(self):
        self.world = MemoryWorld.__new__(MemoryWorld)
        self.world.base = 0x140000000
        self.memory = self.world.memory = BufferMemory()
        self.objects = [0x20000000 + i * 0x1000 for i in range(4)]
        self.handler = 0x20010000
        self.array = 0x20011000
        self.item = 0x20012000
        self.type = 0x20014000
        address = self.world.base + APPLICATION_RVA
        for pointer, (vtable, next_offset) in zip(self.objects, HOLDING_VIEW_CHAIN[:-1]):
            self.memory.put(address, "Q", pointer)
            self.memory.put(pointer, "Q", self.world.base + vtable)
            address = pointer + next_offset
        self.memory.put(self.objects[-1] + 0x478, "Q", self.handler)
        self.memory.put(self.handler, "Q", self.world.base + 0x44fb590)
        self.memory.put(self.handler + 0xe8, "QII", self.array, 1, 1)
        self.memory.put(self.array, "Q", self.item)
        self.memory.put(self.item, "QQQ", self.handler, self.type, 0)
        self.string(self.type + 0x18, "message_test")
        self.memory.put(self.type + 0x38, "I", 0x4744624f)
        self.string(self.item + 0x11d0, "建筑完成")
        self.string(self.item + 0x11f0, "原生正文 150", external=True)
        self.string(self.item + 0x1210, "\x15E; 效果\x15!")
        self.date = 365 * 24 * 5954
        self.memory.put(self.item + 0x1178, "II", self.date, self.date)

    def string(self, address, value, external=False):
        raw = value.encode("utf-8")
        if external or len(raw) > 15:
            data = address + 0x100000
            self.memory.write(data, raw)
            self.memory.put(address, "Q", data)
            self.memory.put(address + 0x10, "QQ", len(raw), max(16, len(raw)))
        else:
            self.memory.write(address, raw.ljust(16, b"\0"))
            self.memory.put(address + 0x10, "QQ", len(raw), 15)

    def query(self, **fields):
        return self.world._query({"kind": "message_feed", **fields})


class GuiMessageFeedTests(unittest.TestCase):
    def test_native_strings_and_dates_are_preserved_with_unknown_recipient(self):
        result = FeedFixture().query()
        self.assertEqual(result["status"], "cached")
        self.assertEqual(result["freshness"], "untracked")
        self.assertIsNone(result["recipient_character_id"])
        self.assertEqual(result["recipient_status"], "native_result_not_located")
        item, = result["items"]
        self.assertEqual(item["title"], "建筑完成")
        self.assertEqual(item["description"], "原生正文 150")
        self.assertEqual(item["effects_text"], "\x15E; 效果\x15!")
        self.assertEqual(item["created_date"], "954.1.1")
        self.assertEqual(item["type"], "message_test")
        self.assertIsNone(item["filter"])

    def test_character_filter_requires_native_recipient_ownership(self):
        with self.assertRaisesRegex(ValueError, "recipient ownership"):
            FeedFixture().query(character=42)

    def test_null_definition_and_empty_pagination(self):
        fixture = FeedFixture()
        fixture.memory.put(fixture.item + 0x10, "Q", fixture.type)
        fixture.memory.put(fixture.type + 0x38, "I", 0x4e756c6c)
        self.assertIsNone(fixture.query()["items"][0]["filter"])
        self.assertEqual(fixture.query(offset=1)["items"], [])
        self.assertIsNone(fixture.query(offset=1)["next_offset"])

    def test_missing_or_invalid_gui_parent_has_bounded_result(self):
        for pointer in (*range(4), "feed"):
            with self.subTest(pointer=pointer):
                fixture = FeedFixture()
                target = fixture.handler if pointer == "feed" else fixture.objects[pointer]
                fixture.memory.put(target, "Q", 0)
                result = fixture.query()
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["items"], [])
        fixture = FeedFixture()
        fixture.memory.put(fixture.item, "Q", 0)
        with self.assertRaisesRegex(ValueError, "parent"):
            fixture.query()

    def test_vector_and_string_bounds_are_checked(self):
        fixture = FeedFixture()
        fixture.memory.put(fixture.handler + 0xf0, "II", 1, 2)
        with self.assertRaisesRegex(ValueError, "vector"):
            fixture.query()
        fixture = FeedFixture()
        fixture.memory.put(fixture.item + 0x11e0, "QQ", 65537, 65537)
        with self.assertRaisesRegex(ValueError, "string"):
            fixture.query()

    def test_concurrent_item_text_and_reference_changes_are_rejected(self):
        for target in ("parent", "vector", "text"):
            with self.subTest(target=target):
                fixture = FeedFixture()
                original = fixture.memory.read
                changed = False

                def read(address, size, **options):
                    nonlocal changed
                    data = original(address, size, **options)
                    if not changed and address == fixture.item + 0x1180:
                        destinations = {"parent": fixture.item, "vector": fixture.array,
                                        "text": fixture.item + 0x11d0}
                        fixture.memory.write(destinations[target], b"\xff")
                        changed = True
                    return data

                fixture.memory.read = read
                with self.assertRaisesRegex(ValueError, "changed during reading"):
                    fixture.query()

    def test_public_query_paginates_and_selects_native_fields(self):
        fixture = FeedFixture()

        class World:
            def __init__(self, pid=None):
                self.memory = fixture.memory

            def query(self, request):
                return {"data": fixture.world._query(request),
                        "runtime": {"date": fixture.date, "tick": 1, "world": 100}}

            def close(self):
                pass

        with patch("live_objects.MemoryWorld", World), DirectWorldReader(character_id=42) as reader:
            result = reader.handle({"kind": "message_feed", "fields": ["title", "created_date"], "max_chars": 600})
            self.assertEqual(result["data"]["items"], [{"title": "建筑完成", "created_date": "954.1.1"}])
            self.assertIsNone(result["data"]["recipient_character_id"])
