"""Regress completed inheritance, native confirmation and orderly departure."""

from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import struct
import tempfile
import unittest

from ck3_client import EventWriter, _disconnect_game_session, _maintain_succession
from jomini_codec import JominiCodec
from memory_world import MemoryWorld
from nakama_native import DEFAULT_GAME_DIR
from pdx_nakama_codec import encode_payload
from session_codec import encode_session_message
from succession_guard import SuccessionGuard


class SuccessionWorld:
    def __init__(self):
        self.items = [{"machineid": 1, "character_id": 33385, "has_open_succession": True},
                      {"machineid": 2, "character_id": 38035, "has_open_succession": True}]
        self.alive = True

    def query(self, request):
        if request["kind"] == "character":
            return {"data": {"identity": {"alive": self.alive}}}
        return {"data": {"items": deepcopy(self.items)}}


class SuccessionGuardTests(unittest.TestCase):
    def test_confirms_only_our_living_heir_without_selecting_a_character(self):
        world = SuccessionWorld()
        guard = SuccessionGuard()
        commands = guard.poll(world, 38035, 2, 0)
        self.assertEqual([c["type"] for c in commands], ["close_succession"])
        self.assertEqual(commands[0]["fields"], {"character": 38035, "machineid": 2})
        self.assertTrue(guard.pending)
        self.assertEqual(guard.poll(world, 38035, 2, 0.6), [])
        self.assertEqual(len(guard.poll(world, 38035, 2, 2)), 1)
        self.assertEqual(len(guard.poll(world, 38035, 2, 4)), 1)
        self.assertEqual(guard.poll(world, 38035, 2, 6), [])
        world.items[1]["has_open_succession"] = False
        self.assertEqual(guard.poll(world, 38035, 2, 7), [])
        self.assertFalse(guard.pending)

    def test_does_not_choose_destiny_or_acknowledge_other_players(self):
        for character, machine, alive in ((33336, 2, True), (None, 2, True),
                                          (38035, -1, True), (38035, 2, False),
                                          (38035, 3, True)):
            world = SuccessionWorld()
            world.alive = alive
            self.assertEqual(SuccessionGuard().poll(world, character, machine, 0), [])

    def test_new_inheritance_resets_bounded_retry(self):
        world = SuccessionWorld()
        guard = SuccessionGuard()
        for now in (0, 2, 4):
            guard.poll(world, 38035, 2, now)
        world.items[1]["character_id"] = 500
        self.assertEqual(guard.poll(world, 500, 2, 6)[0]["fields"]["character"], 500)

    def test_unsettled_heir_is_pending_without_forcing_a_destiny_choice(self):
        guard = SuccessionGuard()
        self.assertEqual(guard.poll(SuccessionWorld(), None, 2, 0), [])
        self.assertTrue(guard.pending)


class SuccessionMemoryTests(unittest.TestCase):
    def test_reads_persisted_state_and_retains_offline_machine_id(self):
        from process_memory import ProcessMemory
        from shadow_evaluator import BufferMemory

        class Memory(BufferMemory):
            pointer = ProcessMemory.pointer
            u32 = ProcessMemory.u32
            string = ProcessMemory.string

        memory = Memory(0)
        world = MemoryWorld.__new__(MemoryWorld)
        world.memory = memory
        world.state = {"world": 0x1000}
        memory.add_region(0x10A0, struct.pack("<Q", 0x10000))
        memory.add_region(0x32340, struct.pack("<QII", 0x40000, 2, 2))
        memory.add_region(0x40000, struct.pack("<QQ", 0x50000, 0x51000))
        for pointer, character, machine, open_ in ((0x50000, 33385, 1, False),
                                                  (0x51000, 38035, -1, True)):
            raw = bytearray(0x300)
            struct.pack_into("<I", raw, 0xB0, character)
            name = b"Host" if machine == 1 else b"Antigravity"
            raw[0xB8:0xB8 + len(name)] = name
            struct.pack_into("<QQ", raw, 0xC8, len(name), 15)
            struct.pack_into("<iI", raw, 0xD8, machine, 0x506C496E)
            raw[0x260] = raw[0x2C8] = open_
            memory.add_region(pointer, raw)
        records = world.player_successions()
        self.assertFalse(records[0]["has_open_succession"])
        self.assertEqual(records[1], {"name": "Antigravity", "character_id": 38035,
                                     "machineid": -1, "has_open_succession": True,
                                     "has_succession_data": True})
        self.assertEqual(world._query({"kind": "player_successions", "machineid": -1})["items"], [records[1]])


class SuccessionConnectionTests(unittest.TestCase):
    def test_received_commands_do_not_revert_native_heir_to_cached_dead_ruler(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            with EventWriter(Path(directory) / "capture", quiet=True) as writer:
                writer.live_memory_pid = 123
                writer.session_state.local_machineid = 2
                writer.session_state.players[2] = {"playable": 33336}
                writer.observations.set_character(38035)
                command = writer.game_codec.encode_command("client_ping", {"turn": 42, "machineid": 1},
                                                           {"identity": 9, "sender": 1, "receiver": 0})
                packet = encode_payload("local-ai", [{"word_8": 0, "payload_hex": command.hex()}], 1, "host")
                writer.write({"event": "match_data", "opcode": 255, "payload": packet})
                self.assertEqual(writer.observations.character_id, 38035)

    def test_departure_waits_for_native_succession_confirmation_before_disconnect(self):
        world = SuccessionWorld()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            with EventWriter(Path(directory) / "capture", quiet=True) as writer:
                writer.action_host = "host"
                writer.local_user_id = "local-ai"
                writer.session_state.local_machineid = 2
                writer.session_state.connected = True
                writer.observations.set_character(38035)
                writer.memory_session = {"active": True, "machineid": 2, "character_id": 38035}
                writer.live_observations = type("Live", (), {"progress_world": world,
                                                            "finish": lambda self: []})()
                writer.poll_world = lambda: []
                from transfer_protocol import FastTransferReceiver
                writer._transfer_receiver = FastTransferReceiver(writer.game_codec)

                class Peer:
                    def __init__(self):
                        self.sent = []
                        self.pending = []

                    def send_envelope(self, room, recipient, messages, **kwargs):
                        message = writer.game_codec.decode_pdx_message(messages[0], from_server=False)
                        self.sent.append(message)
                        if message["type"] == "close_succession":
                            self.pending.append({"event": "native_closed"})
                        elif message["type"] == "ClientRequestDisconnect":
                            self.assert_closed = not world.items[1]["has_open_succession"]
                            payload = encode_payload("local-ai", [{"word_8": 1, "payload_hex":
                                encode_session_message("ServerRespondDisconnect", {"reason": 0xFFFFFFFF}).hex()}], 1, "host")
                            self.pending.append({"event": "match_data", "opcode": 255, "payload": payload,
                                                 "presence": {"user_id": "host"}})
                        return b"submitted"

                    def poll(self):
                        events, self.pending = self.pending, []
                        if events and events[0]["event"] == "native_closed":
                            world.items[1]["has_open_succession"] = False
                            return []
                        return events

                peer = Peer()
                result = _disconnect_game_session(peer, "room", writer, timeout=0.5, counts=Counter())
                self.assertEqual(result, "confirmed")
                self.assertTrue(peer.assert_closed)
                self.assertEqual([c["type"] for c in peer.sent], ["close_succession", "ClientRequestDisconnect"])
                self.assertTrue(world.items[0]["has_open_succession"])


class SuccessionNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from verify_jomini_native import NativeSerializerOracle
        executable = DEFAULT_GAME_DIR / "binaries/ck3.exe"
        if not executable.exists():
            raise unittest.SkipTest("Verified ck3.exe is not installed")
        cls.oracle = NativeSerializerOracle(executable)

    def test_command_matches_captured_host_bytes_and_native_serializer(self):
        codec = JominiCodec()
        header = {"identity": 115366, "sender": 1, "receiver": 0, "attribs": 0}
        fields = {"character": 38295, "machineid": 1}
        captured = bytes.fromhex(
            "003c01000300f30101000300db0001001400a6c20100400201000c0001000000"
            "c60001000c00000000009c0601001400000000000400410001000300ec060100"
            "140097950000160001000c000100000004000400")
        self.assertEqual(codec.encode_command("close_succession", fields, header), captured)
        for fields in ({"machineid": 2}, {"machineid": 2, "character": 38035}):
            self.assertEqual(codec.encode_command("close_succession", fields, header),
                             self.oracle.encode("close_succession", fields, header))

    def test_native_confirmation_clears_only_matching_character_and_machine(self):
        oracle = self.oracle
        cpu, base, scratch = oracle.cpu, oracle.base, oracle.scratch
        cpu.mem_map(scratch + 0x10000, 0x30000)
        world, data, array = scratch + 0x8000, scratch + 0x10000, scratch + 0x5000
        host, guest, null = scratch + 0x6000, scratch + 0x6400, scratch + 0x6800
        helper = scratch + 0x7000
        cpu.mem_write(base + 0x5C7DBE8, struct.pack("<Q", world))
        cpu.mem_write(base + 0x5D3E0F0, struct.pack("<Q", null))
        cpu.mem_write(world + 0xA0, struct.pack("<Q", data))
        cpu.mem_write(data + 0x22340, struct.pack("<QII", array, 2, 2))
        cpu.mem_write(array, struct.pack("<QQ", host, guest))
        cpu.mem_write(base + 0x5CD663C, struct.pack("<i", 1))
        # Optional payload destruction is irrelevant to this test's private heap.
        cpu.mem_write(base + 0xB02960, b"\xC3")
        for character, machine, expected_open in ((33336, 2, True), (38035, 3, True), (38035, 2, False)):
            for pointer, who, owner in ((host, 33385, 1), (guest, 38035, 2), (null, 0xFFFFFFFF, -1)):
                raw = bytearray(0x300)
                struct.pack_into("<I", raw, 0xB0, who)
                struct.pack_into("<iI", raw, 0xD8, owner, 0x506C496E if owner >= 0 else 0)
                raw[0x260] = raw[0x2C8] = 1
                cpu.mem_write(pointer, bytes(raw))
            cpu.mem_write(base + 0x5CD64EA, b"\x01")
            cpu.mem_write(helper + 8, struct.pack("<Ii", character, machine))
            oracle._call(0x28A1BA0, helper)
            self.assertEqual(bool(cpu.mem_read(guest + 0x260, 1)[0]), expected_open)
            self.assertEqual(bool(cpu.mem_read(guest + 0x2C8, 1)[0]), expected_open)
            self.assertEqual(cpu.mem_read(host + 0x260, 1), b"\x01")
            self.assertEqual(cpu.mem_read(base + 0x5CD64EA, 1), b"\x01")


if __name__ == "__main__":
    unittest.main()
