"""Regress notification recipient isolation, filtering, budgets and offline settings."""

from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import struct
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from ck3_llm.paths import CAPTURES_DIR
from ck3_llm.gameplay.game_notifications import NotificationHub, _json
from ck3_llm.world.live_objects import DirectObservationQueue, DirectWorldReader
from ck3_llm.world.memory_world import MemoryWorld, page
from ck3_llm.gameplay.notification_catalog import NotificationCatalog, definitions
from ck3_llm.gameplay.notification_policy import DEFAULT_POLICY, NotificationPolicy, validate_policy
from ck3_llm.web.notification_settings import settings_server


class Reader:
    def __init__(self):
        self.runtime = {"world": 1, "world_data": 2, "tick": 10}
        self.date = "1185.3.10"
        self.rows = {"alerts": [], "player_messages": [], "events": [], "sieges": [],
                     "incoming_interactions": [], "activity_invitations": []}
        self.fail = set()
        self.military = []
        self.province = {"occupying_character_id": None, "occupying_realm": None}
        self.character = {
            "identity": {"alive": True, "faith": {"id": 12}, "culture": {"id": 3}},
            "domain": {"title_ids": [11], "liege_id": 9, "succession_ids": [5], "laws": []},
            "family": {"spouse_ids": [4], "child_ids": [5, 6]},
            "traits": [{"id": 2}], "resources": {"gold": 10, "stress": 0},
        }

    def session_snapshot(self):
        return deepcopy(self.runtime)

    def handle(self, request):
        kind = request["kind"]
        if kind in self.fail:
            raise OSError(f"{kind} source unavailable")
        if kind == "character":
            data = self.character
        elif kind == "province":
            data = self.province
        else:
            data = page(self.rows[kind], request)
        return {"date": self.date, "data": deepcopy(data)}


class Military:
    def __init__(self, reader):
        self.reader = reader

    def poll(self, character_id):
        if "military" in self.reader.fail:
            raise OSError("Military source unavailable")
        result, self.reader.military = self.reader.military, []
        return deepcopy(result)


def alert(identifier=1, character=42, target=9):
    return {"id": identifier, "type": "action_test", "character_id": character, "visible": True,
            "scope": {"root": {"type": "char", "id": character},
                      "targets": {"recipient": {"type": "char", "id": target}}}}


def creation(character=42, target=9, identity=100):
    return {"channel": "command", "type": "create_important_action",
            "default_state": {"sender": 1, "identity": identity},
            "fields": {"player": 2, "character": character, "important_action_type": "action_test",
                       "scope": {"root": {"type": "char", "identity": character}, "seed": 0,
                                 "event_targets": {"recipient": {"type": "char", "identity": target}}}}}


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.reader = Reader()
        self.policy = NotificationPolicy(root / "policy.json")
        self.catalog = NotificationCatalog(root / "binaries" / "ck3.exe")
        self.hub = NotificationHub(self.reader, catalog=self.catalog, policy=self.policy, military_factory=Military)

    def tearDown(self):
        self.temporary.cleanup()

    def poll(self, character=42, budget=6000):
        return self.hub.poll(character, max_chars=budget)

    def test_native_alerts_are_grouped_and_isolated_by_character(self):
        self.reader.rows["alerts"] = [alert(), alert(2, target=10), alert(3, character=99)]
        events = self.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["count"], 2)
        self.assertEqual(events[0]["character_id"], 42)
        self.assertEqual(self.poll(), [])
        self.reader.rows["alerts"] = []
        self.assertEqual(self.poll()[0]["type"], "alert_removed")

    def test_failed_source_does_not_swallow_other_sources_or_acknowledge_retry(self):
        self.reader.fail.add("military")
        self.reader.rows["alerts"] = [alert()]
        self.reader.military = [{"event": "game_notification", "type": "battle_result", "character_id": 42}]
        events = self.poll()
        self.assertEqual({event["event"] for event in events}, {"notification_error", "game_notification"})
        self.assertEqual(self.poll(), [])
        self.reader.fail.clear()
        self.assertEqual(self.poll()[0]["type"], "battle_result")
        self.assertEqual(self.hub.status()["source_errors"], {})

    def test_failed_pagination_keeps_alert_baseline_and_commands(self):
        self.reader.rows["alerts"] = [alert(number) for number in range(101)]
        original = self.reader.handle
        def handle(request):
            if request["kind"] == "alerts" and request["offset"]:
                raise OSError("Second page failed")
            return original(request)
        with patch.object(self.reader, "handle", side_effect=handle):
            self.assertEqual(self.poll()[0]["event"], "notification_error")
        events = self.poll(budget=100000)
        self.assertEqual(events[0]["data"]["count"], 101)

    def test_unresolved_action_type_is_retried_without_acknowledging_the_alert(self):
        self.reader.rows["alerts"] = [{**alert(), "type": None}]
        self.assertEqual(self.poll()[0]["event"], "notification_error")
        self.assertIn("important_actions", self.hub.status()["source_errors"])
        self.reader.rows["alerts"] = [alert()]
        self.assertEqual(self.poll()[0]["type"], "alert_added")

    def test_network_recipient_is_character_and_memory_duplicate_is_suppressed(self):
        self.hub.feed(creation(character=99))
        self.hub.feed(creation())
        events = self.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "native_important_action_command")
        self.hub.feed(creation())
        self.reader.rows["alerts"] = [alert()]
        self.assertEqual(self.poll(), [])
        self.reader.rows["alerts"] = []
        self.assertEqual(self.poll()[0]["type"], "alert_removed")
        self.hub.feed(creation(identity=101))
        self.assertEqual(self.poll()[0]["type"], "alert_added")

    def test_command_and_memory_boolean_and_null_scopes_deduplicate(self):
        command = creation()
        command["fields"]["scope"]["root"] = {}
        command["fields"]["scope"]["event_targets"].update(
            flag={"type": "boolean", "identity": 1}, disabled={"type": "boolean"},
            missing={"type": "char", "identity": 0xffffffff})
        self.hub.feed(command)
        self.assertEqual(len(self.poll()), 1)
        item = alert()
        item["scope"]["root"] = None
        item["scope"]["targets"].update(flag={"type": "boolean", "value": True},
                                          disabled={"type": "boolean", "value": False},
                                          missing={"type": "char", "id": None})
        self.reader.rows["alerts"] = [item]
        self.assertEqual(self.poll(), [])

    def test_real_captured_creation_uses_same_recipient_isolation(self):
        path = CAPTURES_DIR / "notifications-20261115" / "important_action_network_check.json"
        if not path.exists():
            self.skipTest("Local protocol evidence is unavailable")
        examples = [example for capture in json.loads(path.read_text(encoding="utf-8"))
                    for example in capture["examples"] if example["type"] == "create_important_action"]
        self.assertGreater(len(examples), 0)
        for example in examples:
            self.hub.feed({"type": example["type"], "fields": example["fields"], "default_state": example["header"]})
        self.assertEqual(self.poll(character=49779), [])
        for example in examples:
            self.hub.feed({"type": example["type"], "fields": example["fields"], "default_state": example["header"]})
        events = self.poll(character=33590188)
        self.assertEqual(len(events), len(examples))
        self.assertTrue(all(event["character_id"] == 33590188 for event in events))

    def test_retained_future_messages_wait_until_arrival_and_repeat_after_removal(self):
        self.reader.rows["player_messages"] = [{"id": "message:1", "character_id": 42,
                                               "type": "friend_has_died", "arrival_date": "1185.4.1"}]
        self.assertEqual(self.poll(), [])
        self.reader.date = "1185.4.1"
        self.assertEqual(self.poll()[0]["notification_id"], "message.friend_has_died")
        self.assertEqual(self.poll(), [])
        self.reader.rows["player_messages"] = []
        self.poll()
        self.reader.rows["player_messages"] = [{"id": "message:1", "character_id": 42,
                                               "type": "friend_has_died", "arrival_date": "1185.4.1"}]
        self.assertEqual(len(self.poll()), 1)

    def test_initial_pending_events_deliver_once_and_filter_other_roles(self):
        self.reader.rows["events"] = [{"id": 7, "character_id": 42, "event": "test.1"},
                                     {"id": 8, "character_id": 99, "event": "test.2"}]
        events = self.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "pending_event")
        self.assertEqual(self.poll(), [])

    def test_incoming_requests_and_activity_invitations_use_registered_groups_and_policy(self):
        self.reader.rows["incoming_interactions"] = [{"id": 10, "character_id": 42, "role": "recipient"},
                                                     {"id": 11, "character_id": 99, "role": "recipient"}]
        self.reader.rows["activity_invitations"] = [{"id": 20, "character_id": 42}, {"id": 21, "character_id": 99}]
        events = self.poll()
        self.assertEqual({event["notification_id"] for event in events},
                         {"state.pending_interaction", "state.activity_invitation"})
        self.assertEqual({event["group"] for event in events}, {"pending_interactions", "activity_invitations"})
        self.assertTrue(all(event["character_id"] == 42 for event in events))
        self.assertEqual(self.poll(), [])
        self.assertIn("activity_invitations", self.hub.status()["coverage"])
        self.policy.save({**DEFAULT_POLICY, "disabled_groups": ["pending_interactions", "activity_invitations"]},
                         revision=self.policy.revision)
        self.reader.rows["incoming_interactions"].append({"id": 12, "character_id": 42, "role": "intermediary"})
        self.reader.rows["activity_invitations"].append({"id": 22, "character_id": 42})
        self.assertEqual(self.poll(), [])

    def test_semantic_changes_ignore_first_snapshot_and_small_resource_changes(self):
        self.assertEqual(self.poll(), [])
        self.reader.character["resources"].update(gold=11, stress=90)
        self.reader.character["family"]["child_ids"].reverse()
        self.assertEqual(self.poll(), [])
        self.reader.character["resources"].update(gold=-1, stress=100)
        self.reader.character["domain"]["title_ids"].append(12)
        events = self.poll()
        self.assertEqual({event["type"] for event in events}, {"debt_entered", "stress_level_changed", "titles_changed"})
        self.assertEqual(self.poll(), [])
        self.reader.character["resources"]["gold"] = 0
        self.assertEqual(self.poll()[0]["type"], "debt_cleared")

    def test_policy_hot_reload_filters_types_groups_and_queued_events(self):
        self.reader.rows["alerts"] = [alert()]
        self.hub.collect(42)
        self.assertEqual(len(self.hub.pending), 1)
        self.policy.save({**DEFAULT_POLICY, "disabled_types": ["action.action_test"]}, revision=self.policy.revision)
        self.hub.collect(42)
        self.assertEqual(self.hub.take()["events"], [])
        self.policy.save({**DEFAULT_POLICY, "disabled_groups": ["military"]}, revision=self.policy.revision)
        self.reader.military = [{"event": "game_notification", "type": "battle_result", "character_id": 42}]
        self.assertEqual(self.poll(), [])

    def test_budget_retains_unconsumed_events_and_details_are_available(self):
        self.reader.rows["events"] = [{"id": index, "character_id": 42, "event": "test." + str(index),
                                      "description": "long" * 3000} for index in range(3)]
        self.hub.collect(42)
        with self.assertRaises(ValueError):
            self.hub.take(20)
        self.assertEqual(len(self.hub.pending), 3)
        delivered = self.hub.take(650)
        self.assertLessEqual(len(_json(delivered)), 650)
        self.assertEqual(len(delivered["events"]), 1)
        self.assertEqual(delivered["pending_count"], 2)
        compact = delivered["events"][0]
        full = self.hub.lookup(compact["details_query"]["id"])
        self.assertEqual(len(full["data"]["description"]), 12000)
        full["data"].clear()
        self.assertIn("description", self.hub.lookup(compact["id"])["data"])
        self.assertEqual(len(self.hub.take(100000)["events"]), 2)

    def test_world_replacement_rollback_and_role_change_reset_pending_and_details(self):
        self.reader.rows["alerts"] = [alert()]
        first = self.poll()[0]
        self.reader.runtime["world_data"] = 3
        self.assertEqual(self.poll()[0]["type"], "alert_added")
        with self.assertRaises(ValueError):
            self.hub.lookup(first["id"])
        self.reader.runtime["tick"] = 1
        self.assertEqual(self.poll()[0]["type"], "alert_added")
        self.hub.collect(42)
        self.assertEqual(self.poll(character=99), [])
        self.assertEqual(self.hub.details, {})

    def test_world_rewind_during_poll_discards_mixed_notifications(self):
        self.reader.rows["alerts"] = [alert()]
        original = self.reader.handle
        def handle(request):
            if request["kind"] == "alerts":
                self.reader.runtime["tick"] = 1
            return original(request)
        with patch.object(self.reader, "handle", side_effect=handle):
            self.assertEqual(self.poll(), [])
        self.assertEqual(self.poll()[0]["type"], "alert_added")

    def test_siege_notifications_ignore_daily_ticks_and_report_breaches_and_occupation(self):
        siege = {"id": 7, "province_id": 10, "side": "defender", "effects": {"breach": 0},
                 "progress_points": 15, "phase_days_remaining": 12}
        self.reader.rows["sieges"] = [siege]
        self.assertEqual(self.poll()[0]["type"], "siege_started")
        siege.update(progress_points=16, phase_days_remaining=11)
        self.assertEqual(self.poll(), [])
        siege["effects"]["breach"] = 1
        self.assertEqual(self.poll()[0]["type"], "siege_breach")
        self.reader.rows["sieges"] = []
        self.reader.province["occupying_character_id"] = 99
        event = self.poll()[0]
        self.assertEqual(event["type"], "siege_ended")
        self.assertEqual(event["data"]["occupation"]["occupying_character_id"], 99)

    def test_failed_occupation_read_does_not_consume_siege_end(self):
        self.reader.rows["sieges"] = [{"id": 7, "province_id": 10, "side": "attacker", "effects": {"breach": 0}}]
        self.poll()
        self.reader.rows["sieges"] = []
        self.reader.fail.add("province")
        self.assertEqual(self.poll()[0]["event"], "notification_error")
        self.reader.fail.clear()
        self.assertEqual(self.poll()[0]["type"], "siege_ended")


class CatalogTests(unittest.TestCase):
    def test_script_parser_handles_comments_strings_and_nested_definitions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "definitions.txt"
            path.write_text('''# comment
@ignored = { title = ignored }
message = { title = "hello # { text" nested = { title = nested } group = social }
second = { desc = test_desc flag = a flag = b }
''', encoding="utf-8")
            rows = definitions(path)
            self.assertEqual([row["key"] for row in rows], ["message", "second"])
            self.assertEqual(rows[0]["properties"]["title"], "hello # { text")
            self.assertEqual(rows[0]["blocks"], ["nested"])
            self.assertEqual(rows[1]["properties"]["flag"], ["a", "b"])
            path.write_text("test = {", encoding="utf-8")
            with self.assertRaises(ValueError):
                definitions(path)

    def test_catalog_resolves_labels_groups_and_selective_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = {
                "common/message_group_types/groups.txt": "social = {}",
                "common/message_filter_types/filters.txt": "death = { group = social }",
                "common/messages/messages.txt": "friend_has_died = { title = death_title message_filter_type = death }",
                "localization/simp_chinese/test.yml": 'l_simp_chinese:\n message_group_type_social: "人际关系"\n death_title: "朋友去世"\n',
            }
            for name, text in files.items():
                path = root / "game" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8-sig")
            catalog = NotificationCatalog(root / "binaries" / "ck3.exe")
            result = catalog.query({"search": "朋友", "limit": 1})
            self.assertEqual(result["items"][0]["id"], "message.friend_has_died")
            self.assertEqual(result["items"][0]["group"], "social")
            self.assertEqual(catalog.groups["social"], "人际关系")
            self.assertGreater(catalog.query({"namespace": "military", "limit": 1})["next_offset"], 0)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "preferences.json"
        self.policy = NotificationPolicy(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_invalid_reload_preserves_last_valid_and_revision_tracks_corrupt_file(self):
        saved = self.policy.save({**DEFAULT_POLICY, "disabled_groups": ["military"]}, revision="missing")
        self.path.write_text("invalid json", encoding="utf-8")
        result = self.policy.snapshot()
        self.assertIsNotNone(result["error"])
        self.assertNotEqual(result["revision"], saved["revision"])
        self.assertEqual(result["policy"]["disabled_groups"], ["military"])
        self.assertIsNotNone(self.policy.snapshot()["error"])
        with self.assertRaises(FileExistsError):
            self.policy.save(DEFAULT_POLICY, revision=saved["revision"])
        self.policy.save(DEFAULT_POLICY, revision=result["revision"])
        self.assertIsNone(self.policy.snapshot()["error"])

    def test_independent_simultaneous_saves_reject_stale_revision(self):
        second = NotificationPolicy(self.path)
        barrier = threading.Barrier(2)
        def save(policy, group):
            barrier.wait()
            try:
                policy.save({**DEFAULT_POLICY, "disabled_groups": [group]}, revision="missing")
                return "saved"
            except FileExistsError:
                return "stale"
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(save, self.policy, "military"), executor.submit(save, second, "default")]
            self.assertEqual(sorted(future.result() for future in futures), ["saved", "stale"])

    def test_atomic_replace_failure_retains_original_and_removes_temporary_file(self):
        self.policy.save(DEFAULT_POLICY, revision="missing")
        before = self.path.read_bytes()
        with patch("ck3_llm.gameplay.notification_policy.os.replace", side_effect=OSError("Replace failed")):
            with self.assertRaises(OSError):
                self.policy.save({**DEFAULT_POLICY, "disabled_types": ["military.war_started"]}, revision=self.policy.revision)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual({path.name for path in self.path.parent.iterdir()}, {self.path.name, self.path.name + ".lock"})

    def test_transient_read_error_recovers_with_unchanged_valid_file(self):
        self.policy.save(DEFAULT_POLICY, revision="missing")
        with patch.object(Path, "read_bytes", side_effect=PermissionError("File busy")):
            self.assertIsNotNone(self.policy.snapshot()["error"])
        self.assertIsNone(self.policy.snapshot()["error"])

    def test_validation_rejects_unknown_fields_and_malformed_arrays(self):
        for value in ([], {"version": True}, {"version": 2}, {**DEFAULT_POLICY, "extra": []},
                      {**DEFAULT_POLICY, "disabled_groups": "military"},
                      {**DEFAULT_POLICY, "disabled_types": [False]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_policy(value)

    def test_checklist_http_saves_reloads_and_rejects_stale_edits(self):
        catalog = NotificationCatalog(self.path.parent / "binaries" / "ck3.exe")
        with settings_server(catalog, self.policy, port=0) as server:
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            worker.start()
            url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(url) as response:
                    self.assertIn("勾选要关闭的通知", response.read().decode("utf-8"))
                with urllib.request.urlopen(url + "/api/settings") as response:
                    settings = json.load(response)
                request = urllib.request.Request(url + "/api/policy", method="PUT", data=json.dumps({
                    "policy": {**DEFAULT_POLICY, "disabled_groups": ["military"]}, "revision": settings["revision"]
                }).encode("utf-8"), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.load(response)["policy"]["disabled_groups"], ["military"])
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 409)
                error.exception.close()
                bad = urllib.request.Request(url + "/api/policy", method="PUT", data=b"[]")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(bad)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            finally:
                server.shutdown()
                worker.join()


class PublicInterfaceTests(unittest.TestCase):
    def test_new_kinds_use_public_projection_and_budgets(self):
        source = Reader()
        class World:
            def __init__(self, pid=None):
                self.memory = type("Memory", (), {"executable": Path("C:/missing/binaries/ck3.exe")})()

            def query(self, request):
                kind = request["kind"]
                data = {**source.runtime, "players": []} if kind == "session" else source.handle(request)["data"]
                return {"data": deepcopy(data), "runtime": {**source.runtime, "date": 365 * 24 * 6185}}

            def close(self):
                pass
        with tempfile.TemporaryDirectory() as folder, patch("ck3_llm.world.live_objects.MemoryWorld", World):
            with DirectWorldReader(character_id=42, notification_policy_path=Path(folder) / "policy.json") as reader:
                source.rows["alerts"] = [alert()]
                result = reader.handle({"kind": "alerts", "fields": ["id", "character_id"]})
                self.assertEqual(result["data"]["items"], [{"id": 1, "character_id": 42}])
                types = reader.handle({"kind": "notification_types", "namespace": "military", "fields": ["id", "label"], "limit": 1})
                self.assertEqual(types["data"]["next_offset"], 1)
                hub = reader.notification_hub()
                hub.military_factory = lambda reader: Military(source)
                hub.reset(42)
                delivered = reader.handle({"command": "notifications", "max_chars": 900})
                self.assertLessEqual(len(_json(delivered)), 900)
                self.assertNotIn("tick", _json(delivered))
                identifier = delivered["events"][0]["id"]
                detail = reader.handle({"kind": "notification", "id": identifier})
                self.assertEqual(detail["data"]["character_id"], 42)
                source.runtime["world_data"] = 99
                with self.assertRaises(ValueError):
                    reader.handle({"kind": "notification", "id": identifier})
                source.runtime["world_data"] = 2
                delivered = reader.handle({"command": "notifications", "max_chars": 900})
                identifier = delivered["events"][0]["id"]
                reader.set_character(99)
                with self.assertRaises(ValueError):
                    reader.handle({"kind": "notification", "id": identifier})


class LayoutMemory:
    def __init__(self):
        self.bytes = {}
        self.strings = {}

    def put(self, address, format, *values):
        self.bytes.update({address + index: value for index, value in enumerate(struct.pack(format, *values))})

    def read(self, address, count):
        return bytes(self.bytes.get(address + index, 0) for index in range(count))

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u8(self, address):
        return self.read(address, 1)[0]

    def i32(self, address):
        return struct.unpack("<i", self.read(address, 4))[0]

    def fixed(self, address):
        return struct.unpack("<q", self.read(address, 8))[0] / 100000

    def pointer(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def string(self, address):
        return self.strings.get(address, "")


class NativeLayoutTests(unittest.TestCase):
    def test_regiment_exposes_native_chunks_with_unknown_aggregate_totals(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = memory = LayoutMemory()
        world.lookup = lambda kind, value: 0x1000
        memory.put(0x1018, "<II", 200, 130)
        memory.put(0x103c, "<II", 100, 70)
        result = world.regiment(1)
        self.assertIsNone(result["current"])
        self.assertIsNone(result["maximum"])
        self.assertEqual(result["strength_chunks"][:2], [{"maximum": 200, "current": 130},
                                                         {"maximum": 100, "current": 70}])

    def test_battle_report_exposes_stored_counts_with_unknown_casualty_total(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = memory = LayoutMemory()
        world.base = 0
        memory.put(0x1000, "<Q", 0x4757718)
        memory.put(0x1010, "<qq", 12345678, 12000000)
        result = world.combat_result_side(0x1000)
        self.assertEqual(result["initial_soldiers"], 123.45678)
        self.assertEqual(result["surviving_soldiers"], 120)
        self.assertIsNone(result["casualties"])
        self.assertEqual(result["casualties_status"], "native_result_not_located")

    def test_siege_layout_applies_serializable_subobject_offset(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = memory = LayoutMemory()
        world.lookup = lambda kind, value: {("siege", 7): 0x1000, ("army", 8): 0x4000,
                                            ("unit", 9): 0x5000, ("title", 11): 0x6000}[kind, value]
        world.character_name = lambda value: {"id": value} if value is not None else None
        world.province = lambda value: {"title": {"id": 11, "holder_id": 43}, "realm": {"ruler": {"id": 44}}}
        memory.put(0x1200, "<Q", 0x3000)
        memory.put(0x3010, "<I", 67)
        memory.put(0x3738, "<I", 11)
        memory.put(0x6128, "<I", 43)
        memory.put(0x1208, "<I", 8)
        memory.put(0x4124, "<I", 9)
        memory.put(0x5174, "<I", 42)
        memory.put(0x13d0, "<q", 1500000)
        memory.put(0x143c, "<i", 12)
        memory.put(0x1444, "<I", 365 * 24 * 6185)
        memory.put(0x13d8, "<5I", 1, 2, 0, 0, 0)
        with patch.object(world, "siege_progress", return_value={"percent": 10}) as progress:
            siege = world.siege(7)
        progress.assert_called_once_with(0x1000, 0x3000)
        self.assertEqual(siege["province_id"], 67)
        self.assertEqual(siege["owner_army_id"], 8)
        self.assertEqual(siege["attacker"]["id"], 42)
        self.assertEqual(siege["progress_points"], 15)
        self.assertEqual(siege["phase_days_remaining"], 12)
        self.assertEqual(siege["effects"]["breach"], 1)

    def test_siege_filter_includes_vassals_under_the_controlled_duke(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = LayoutMemory()
        world.rows = lambda kind: [(1, 0), (2, 0), (3, 0)]
        sieges = {1: {"id": 1, "attacker": {"id": 42}, "defender": {"id": 99}},
                  2: {"id": 2, "attacker": {"id": 99}, "defender": {"id": 43}},
                  3: {"id": 3, "attacker": {"id": 99}, "defender": {"id": 77}}}
        world.siege = lambda value: deepcopy(sieges[value])
        world.siege_context = lambda value, **options: deepcopy(sieges[value])
        world.lookup = lambda kind, value: value
        world.liege = lambda value: {43: 42, 42: 9}.get(value)
        filtered = world._query({"kind": "sieges", "character": 42})
        self.assertEqual([(item["id"], item["side"]) for item in filtered["items"]], [(1, "attacker"), (2, "defender")])
        self.assertEqual(world._query({"kind": "sieges"})["count"], 3)

    def test_siege_character_filter_reads_full_details_only_for_relevant_sieges(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = LayoutMemory()
        world.lookup = lambda kind, value: value
        world.rows = lambda kind: [(1, 100), (2, 200)]
        world.siege_context = lambda value, **options: {"id": value, "attacker": {"id": 42 if value == 1 else 99},
                                                       "defender": None}
        world.siege = lambda value: {"id": value}
        with patch.object(world, "siege", wraps=world.siege) as details:
            result = world._query({"kind": "sieges", "character": 42})
        self.assertEqual(result["items"], [{"id": 1, "side": "attacker"}])
        details.assert_called_once_with(1)

    def test_scope_context_resolves_named_character_targets(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = memory = LayoutMemory()
        world.base = 0
        world._scope_tokens = {"0x11": "char", "0x12": "boolean"}
        world.character_name = lambda value: {"id": value, "name": "Target"}
        memory.put(0x5506b90, "<QII", 0x1000, 8, 8)
        memory.put(0x1000 + 80 + 4, "<I", 0x11)
        memory.put(0x1000 + 160 + 4, "<I", 0x12)
        memory.put(0x2000, "<HHIQ", 1, 0, 0, 42)
        memory.put(0x2018, "<QII", 0x3000, 1, 1)
        memory.put(0x3000, "<I", 0)
        memory.put(0x3008, "<HHIQ", 1, 0, 0, 9)
        memory.put(0x5dd6570 + 0x30, "<QII", 0x4000, 1, 1)
        memory.strings[0x4000] = "recipient"
        scope = world.scope_context(0x2000)
        self.assertEqual(scope["root"]["id"], 42)
        self.assertEqual(scope["targets"]["recipient"]["id"], 9)
        memory.put(0x3008, "<HHIQ", 1, 0, 0, 0xffffffff)
        self.assertIsNone(world.scope_context(0x2000)["targets"]["recipient"]["id"])
        memory.put(0x2018, "<Q", 0)
        with self.assertRaisesRegex(ValueError, "targets"):
            world.scope_context(0x2000)

    def test_retained_messages_keep_repeated_identical_instances_and_owner(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = memory = LayoutMemory()
        world.base = 0
        world.lookup = lambda kind, value: 0x1000
        world.world_data = lambda: 0
        world.vector = lambda pointer, width: [0x3000, 0x4000]
        world.scope_value = lambda pointer: None
        world.scope_targets = lambda pointer: {}
        world.key = lambda pointer: "friend_has_died" if pointer else None
        for player, character in ((0x3000, 42), (0x4000, 99)):
            memory.put(player, "<Q", 0x473cdd8)
            memory.put(player + 0xb0, "<I", character)
        memory.put(0x3000 + 0x248, "<QII", 0x100000, 2, 2)
        for pointer in (0x100000, 0x1011a0):
            memory.put(pointer, "<Q", 0x5000)
            memory.put(pointer + 0x1170, "<I", 100)
            memory.put(pointer + 0x1178, "<I", 200)
        messages = world.player_messages(42)
        self.assertEqual(len(messages), 2)
        self.assertNotEqual(messages[0]["id"], messages[1]["id"])
        self.assertTrue(all(message["character_id"] == 42 for message in messages))
        memory.put(0x3000 + 0x248, "<Q", 0)
        with self.assertRaises(ValueError):
            world.player_messages(42)

    def test_alert_query_requires_recipient_and_filters_before_decoding_scopes(self):
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = memory = LayoutMemory()
        world.world_data = lambda: 0
        world.vector = lambda pointer: [1, 2, 3]
        world.lookup = lambda kind, value: value * 0x1000
        world.important_action = lambda value: {"id": value}
        for identifier, character, visible in ((1, 42, 1), (2, 99, 1), (3, 42, 0)):
            memory.put(identifier * 0x1000 + 0x24, "<I", character)
            memory.put(identifier * 0x1000 + 0x190, "<B", visible)
        self.assertEqual(world._query({"kind": "alerts", "character": 42})["items"], [{"id": 1}])
        with self.assertRaises(ValueError):
            world._query({"kind": "alerts"})


class QueueWorld:
    def __init__(self, pid=None):
        self.memory = type("Memory", (), {"executable": Path("C:/missing/binaries/ck3.exe")})()
        self.source = Reader()
        self.source.character["military"] = {"unit_ids": [], "war_ids": []}
        self.source.rows.update(combat_results=[], combats=[])
        self.active = True

    def query(self, request):
        if not self.active:
            raise ValueError("An active game is required")
        runtime = {**self.source.runtime, "date": 365 * 24 * 6185}
        if request["kind"] == "session":
            data = {**runtime, "players": [{"machineid": 2, "name": "AIObserver", "character_id": 42,
                                            "flags": 0x40, "reported_turn": 9}]}
        else:
            data = self.source.handle(request)["data"]
        return {"data": data, "runtime": runtime}

    def close(self):
        pass


class BackgroundProgressTests(unittest.TestCase):
    def test_old_async_notifications_cannot_cross_machines_worlds_or_rewinds(self):
        for machine, world_data, tick in ((4, 2, 10), (2, 3, 11), (2, 2, 1)):
            with self.subTest(machine=machine, world_data=world_data, tick=tick):
                queue = DirectObservationQueue(123)
                try:
                    queue.set_player(machine)
                    queue.session = {"machineid": machine, "active": True, "character_id": 42,
                                     "world": 1, "world_data": world_data, "tick": tick,
                                     "sampled_at": time.monotonic()}
                    queue.next_notification_poll = queue.next_progress_poll = float("inf")
                    queue.notification_future = Future()
                    queue.notification_future.set_result((
                        {"machineid": 2, "active": True, "character_id": 42, "world": 1,
                         "world_data": 2, "tick": 10, "sampled_at": 0},
                        [{"event": "game_notification", "character_id": 42, "type": "battle_result"}]))
                    self.assertEqual(queue.poll(), [])
                    self.assertEqual(queue.session["world_data"], world_data)
                    self.assertEqual(queue.session["tick"], tick)
                finally:
                    queue.finish()

    def test_progress_continues_while_a_large_query_occupies_the_worker(self):
        started, release = threading.Event(), threading.Event()
        runtime = {"world": 1, "world_data": 2, "tick": 10}

        class BusyWorld(QueueWorld):
            def __init__(self, pid=None):
                super().__init__(pid)
                self.source.runtime = runtime

            def query(self, request):
                if request["kind"] == "character":
                    started.set()
                    if not release.wait(timeout=3):
                        raise TimeoutError("Test query was not released")
                return super().query(request)

        with tempfile.TemporaryDirectory() as folder, patch("ck3_llm.world.live_objects.MemoryWorld", BusyWorld):
            queue = DirectObservationQueue(123, notification_policy_path=Path(folder) / "preferences.json")
            try:
                queue.set_player(2)
                queue.submit({"kind": "character", "id": 42}, 42)
                self.assertTrue(started.wait(timeout=2))
                queue.poll(42)
                self.assertEqual(queue.session["tick"], 10)
                self.assertIsNot(queue.progress_world, queue.reader.memory)
                runtime["tick"] = 11
                queue.next_progress_poll = 0
                queue.poll(42)
                self.assertEqual(queue.session["tick"], 11)
                self.assertTrue(any(not future.done() for future in queue.pending))
            finally:
                release.set()
                queue.finish()

    def test_notification_failure_preserves_fresh_progress(self):
        queue = DirectObservationQueue(123)
        try:
            queue.set_player(2)
            queue.session = {"active": True, "character_id": 42, "machineid": 2, "tick": 10,
                             "sampled_at": time.monotonic()}
            queue.next_progress_poll = queue.next_notification_poll = float("inf")
            queue.notification_future = Future()
            queue.notification_future.set_exception(ValueError("Notification read failed"))
            events = queue.poll(42)
            self.assertEqual(events[0]["event"], "notification_error")
            self.assertTrue(queue.session["active"])
            self.assertEqual(queue.session["tick"], 10)
        finally:
            queue.finish()

    def test_queue_discovers_local_character_without_received_roster_and_handles_unload(self):
        with tempfile.TemporaryDirectory() as folder, patch("ck3_llm.world.live_objects.MemoryWorld", QueueWorld):
            queue = DirectObservationQueue(123, notification_policy_path=Path(folder) / "preferences.json")
            try:
                queue.set_player(2)
                queue.poll()
                queue.notification_future.result(timeout=3)
                self.assertEqual(queue.poll(), [])
                self.assertEqual(queue.session["character_id"], 42)
                self.assertEqual(queue.session["tick"], 10)
                queue.reader.memory.active = False
                with self.assertRaises(ValueError):
                    queue._notifications(42, 2)
                self.assertIsNone(queue.reader.character_id)
            finally:
                queue.finish()

    def run_progress(self, active):
        from ck3_llm.client import EventWriter, run_connection
        from ck3_llm.protocol.jomini_codec import JominiCodec

        class Peer:
            def __init__(self):
                self.events = []
                self.sent = []

            def connect(self):
                self.events.append({"event": "connected"})

            def join(self, room_id, metadata):
                self.events.append({"event": "room_joined", "match_id": room_id})

            def poll(self):
                result, self.events = self.events, []
                return result

            def send_envelope(self, room_id, recipient, messages, magic=0xcafe, presences=None):
                self.sent.append(JominiCodec().decode_pdx_message(messages[0], from_server=False))
                return b"sent"

        class MemoryQueue:
            def __init__(self):
                self.session = {"active": active, "character_id": 42, "tick": 321,
                                "local_player": {"machineid": 2, "flags": 0x40}}

            def set_player(self, machineid):
                pass

            def poll(self, character_id=None):
                return []

            def finish(self):
                return []

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(output):
            with EventWriter(Path(folder) / "client", quiet=True) as writer:
                writer.live_memory_pid = 123
                writer.live_observations = MemoryQueue()
                writer.session_state.connected = True
                writer.session_state.local_machineid = 2
                writer.decoded_worlds = 1
                peer = Peer()
                run_connection(peer, "room", writer, duration=0.05, ack_received_turn=True,
                               handshake={"command": "send_session_message", "recipient": "host",
                                          "type": "ClientRequestConnected", "fields": {}})
                character = writer.observations.character_id
            events = [json.loads(line) for line in (Path(folder) / "client" / "events.jsonl").read_text().splitlines()]
        return peer.sent, events, output.getvalue(), character

    def test_memory_progress_acknowledges_actual_world_without_host_ping_or_automatic_pause(self):
        sent, events, output, character = self.run_progress(True)
        commands = [command for command in sent if command["channel"] == "command"]
        self.assertEqual([command["type"] for command in commands], ["client_ping"])
        self.assertEqual(commands[0]["fields"], {"turn": 321, "machineid": 2})
        self.assertEqual(character, 42)
        acknowledgement = next(event for event in events if event["event"] == "received_turn_acknowledged")
        self.assertEqual(acknowledgement["source"], "shared_game_memory")
        self.assertFalse(acknowledgement["world_simulated"])
        self.assertNotIn('"turn"', output)

    def test_unloaded_world_clears_controlled_character_and_does_not_acknowledge(self):
        sent, events, output, character = self.run_progress(False)
        self.assertIsNone(character)
        self.assertFalse(any(command["type"] == "client_ping" for command in sent))
        self.assertFalse(any(event["event"] == "received_turn_acknowledged" for event in events))


if __name__ == "__main__":
    unittest.main()
