"""Regressions for the independent client's active transfer protocol."""

import io
from pathlib import Path
import tempfile
import unittest
import zipfile

from ck3_llm.protocol.jomini_codec import JominiCodec
from ck3_llm.protocol.pdx_nakama_codec import encode_payload, decode_payload
from ck3_llm.protocol.transfer_protocol import FastTransferReceiver, encode_chunk_bitmap, decode_chunk_bitmap
from ck3_llm.protocol.snapshot_transfer import SnapshotAssembler, TransferChecksum
from ck3_llm.protocol.mock_nakama import SAMPLE_TRANSFER


METADATA = {"filename": "hotjoin_0", "size": 8, "checksum": 123}


class TransferProtocolTests(unittest.TestCase):
    def setUp(self):
        self.codec = JominiCodec()
        self.receiver = FastTransferReceiver()

    def packet(self, name, fields):
        raw = self.codec.encode_command(name, fields, {"identity": 42, "sender": 1, "receiver": 2})
        frame = encode_payload("local-b", [{"word_8": 0, "payload_hex": raw.hex()}], 42, "host")
        pdx = decode_payload(frame)
        for message in pdx["messages"]:
            message["jomini"] = self.codec.decode_pdx_message(message)
        return {"event": "match_data", "opcode": 255, "presence": {
            "user_id": "host", "session_id": "host-session", "username": "Host",
        }, "pdx": pdx}

    def start(self):
        self.receiver.observe(self.packet("filetransfer_init_command", METADATA), 0)
        commands = self.receiver.outgoing(2, 0)
        self.assertEqual(commands[0]["type"], "filetransfer_fast_cmd_request_handshake")
        self.receiver.observe(self.packet("filetransfer_fast_cmd_handshake", {
            **METADATA, "filetransfer_fast_fld_chunk_size": 3,
        }), 0.1)
        commands = self.receiver.outgoing(2, 0.1)
        self.assertEqual([command["type"] for command in commands], ["filetransfer_fast_cmd_initialize"])

    def chunk(self, chunk, data, now=0.2):
        return self.receiver.observe(self.packet("filetransfer_fast_cmd_chunk", {
            **METADATA, "chunk_id": chunk, "data": {"hex": data.hex()},
        }), now)

    def test_negotiation_missing_chunk_and_final_bitmap(self):
        self.start()
        self.chunk(2, b"gh")
        self.chunk(0, b"abc")
        commands = self.receiver.outgoing(2, 0.2)
        self.assertEqual(commands[0]["fields"]["data"], {"hex": "05"})
        self.assertEqual(commands[0]["header"], {"identity": 2, "sender": 2, "receiver": 1, "attribs": 47})
        self.chunk(1, b"def")
        commands = self.receiver.outgoing(2, 0.21)
        self.assertEqual(commands[0]["fields"]["data"], {"hex": "07"})
        self.assertEqual(self.receiver.outgoing(2, 1), [])

    def test_heartbeat_and_duplicate_chunks(self):
        self.start()
        self.assertEqual(self.receiver.outgoing(2, 0.39), [])
        self.assertEqual(self.receiver.outgoing(2, 0.41)[0]["fields"]["data"], {"hex": "00"})
        self.chunk(0, b"abc")
        self.receiver.outgoing(2, 0.42)
        self.chunk(0, b"abc")
        self.assertEqual(self.receiver.outgoing(2, 0.43)[0]["fields"]["data"], {"hex": "01"})

    def test_waits_for_assigned_identity_and_tracks_manual_sequence(self):
        self.receiver.observe(self.packet("filetransfer_init_command", METADATA), 0)
        self.assertEqual(self.receiver.outgoing(None, 0), [])
        self.receiver.note_submitted({"command": "send_game_command", "header": {"identity": 99}})
        command = self.receiver.outgoing(2, 0)[0]
        self.assertEqual(command["header"]["identity"], 100)
        self.assertEqual(command["presences"][0]["session_id"], "host-session")

    def test_serial_transfer_requests_its_own_metadata_protocol(self):
        self.receiver = FastTransferReceiver(protocol="serial")
        self.receiver.observe(self.packet("filetransfer_init_command", METADATA), 0)
        self.assertEqual(self.receiver.outgoing(None, 0), [])
        first = self.receiver.outgoing(2, 0)
        self.assertEqual([command["type"] for command in first], ["filetransfer_request_chunk_metas_command"])
        self.assertEqual(self.receiver.outgoing(2, 0.5), [])
        self.assertEqual(self.receiver.outgoing(2, 1.1)[0]["type"], "filetransfer_request_chunk_metas_command")

    def test_serial_chunk_checksums_retry_acknowledgement_and_world_checksum(self):
        def checksum(data):
            state = TransferChecksum()
            state.update(data)
            return state.value

        with zipfile.ZipFile(io.BytesIO(SAMPLE_TRANSFER)) as archive:
            world = archive.read("hotjoin_0")
        metadata = {"filename": "hotjoin_0", "size": len(SAMPLE_TRANSFER), "checksum": checksum(world)}
        chunks = [SAMPLE_TRANSFER[:17], SAMPLE_TRANSFER[17:]]
        chunk_metadata = {**metadata, "machineid": 1, "sizes": list(map(len, chunks)),
                          "checksums": list(map(checksum, chunks))}
        self.receiver = FastTransferReceiver(protocol="serial")
        with tempfile.TemporaryDirectory() as directory:
            assembler = SnapshotAssembler(Path(directory) / "transfers")
            for name, fields in (("filetransfer_init_command", metadata),
                                 ("filetransfer_send_chunk_metas_command", chunk_metadata)):
                packet = self.packet(name, fields)
                assembler.feed(packet["pdx"]["messages"][0]["jomini"], source="host")
                self.receiver.observe(packet, 0)
                outgoing = self.receiver.outgoing(2, 0)
            self.assertEqual(outgoing[0]["type"], "filetransfer_request_chunk_command")
            self.assertEqual(outgoing[0]["fields"]["chunk_id"], 0)
            self.assertEqual(self.receiver.outgoing(2, 1.1)[0]["fields"]["chunk_id"], 0)
            corrupt = self.packet("filetransfer_send_chunk_command", {
                **metadata, "chunk_id": 0, "data": {"hex": bytes(len(chunks[0])).hex()},
            })
            with self.assertRaisesRegex(ValueError, "checksum"):
                assembler.feed(corrupt["pdx"]["messages"][0]["jomini"], source="host")
            corrupt["pdx"]["messages"][0]["transfer_error"] = "checksum failed"
            self.receiver.observe(corrupt, 1.2)
            self.assertEqual(self.receiver.outgoing(2, 1.2), [])
            reassembled = []
            for index, data in enumerate(chunks):
                packet = self.packet("filetransfer_send_chunk_command", {
                    **metadata, "chunk_id": index, "data": {"hex": data.hex()},
                })
                reassembled.extend(assembler.feed(packet["pdx"]["messages"][0]["jomini"], source="host"))
                self.receiver.observe(packet, 1.3 + index)
                outgoing = self.receiver.outgoing(2, 1.3 + index)
                acknowledgement = outgoing[0]
                self.assertEqual(acknowledgement["type"], "filetransfer_chunk_recieved_command")
                self.assertEqual(acknowledgement["fields"]["chunks_completed"], index + 1)
                self.assertEqual(acknowledgement["fields"]["machineid"], 2)
            self.assertEqual(self.receiver.outgoing(2, 10), [])
            self.assertEqual(len(reassembled), 1)
            self.assertEqual(reassembled[0]["checksum_scope"], "zip_entry")
            self.assertEqual(reassembled[0]["checksum_verification"], "passed")
            self.assertEqual(Path(reassembled[0]["file"]).read_bytes(), SAMPLE_TRANSFER)

    def test_serial_invalid_chunk_metadata_preserves_pending_state(self):
        self.receiver = FastTransferReceiver(protocol="serial")
        self.receiver.observe(self.packet("filetransfer_init_command", METADATA), 0)
        for sizes, checksums in (([8], []), ([7], [1]), ([0, 8], [0, 1])):
            packet = self.packet("filetransfer_send_chunk_metas_command", {
                **METADATA, "machineid": 1, "sizes": sizes, "checksums": checksums,
            })
            with self.subTest(sizes=sizes), self.assertRaises(ValueError):
                self.receiver.observe(packet, 0.1)
        transfer, = self.receiver.transfers.values()
        self.assertIsNone(transfer["sizes"])

    def test_invalid_chunks_do_not_set_ack_bits(self):
        self.chunk(0, b"abc")  # Unannounced data remains available to passive capture.
        self.assertEqual(self.receiver.transfers, {})
        self.start()
        for chunk, data in ((-1, b"abc"), (3, b"abc"), (0, b"a"), (2, b"ghi")):
            with self.subTest(chunk=chunk, data=data), self.assertRaises(ValueError):
                self.chunk(chunk, data)
        self.assertEqual(self.receiver.outgoing(2, 0.41)[0]["fields"]["data"], {"hex": "00"})

    def test_bitmap_boundaries_and_invalid_tail_bits(self):
        bitmap = encode_chunk_bitmap(9, [0, 1, 8, 8])
        self.assertEqual(bitmap, b"\x03\x01")
        self.assertEqual(decode_chunk_bitmap(bitmap, 9), {0, 1, 8})
        self.assertEqual(encode_chunk_bitmap(0, []), b"")
        for data, count in ((b"\x00", 9), (b"\x00\x80", 9)):
            with self.subTest(data=data), self.assertRaises(ValueError):
                decode_chunk_bitmap(data, count)
        with self.assertRaises(ValueError):
            encode_chunk_bitmap(8, [8])

    def test_checksum_vectors_and_incremental_updates(self):
        for data, expected in (
            (b"", 0), (bytes(1024), 0), (b"abcd", 0x018A03D4),
            (b"\xff" * 257, -1), (b"\xff" * 258, 0x00FF00FF),
        ):
            with self.subTest(length=len(data)):
                checksum = TransferChecksum()
                checksum.update(data)
                self.assertEqual(checksum.value, expected)
                incremental = TransferChecksum()
                for start in range(0, len(data), 7):
                    incremental.update(data[start:start + 7])
                    incremental.update(b"")
                self.assertEqual(incremental.value, expected)

    def test_oversized_bitmap_handshake_preserves_previous_state(self):
        metadata = {**METADATA, "size": 0xFFFFFFFF}
        self.receiver.observe(self.packet("filetransfer_init_command", metadata), 0)
        self.receiver.outgoing(2, 0)
        with self.assertRaises(ValueError):
            self.receiver.observe(self.packet("filetransfer_fast_cmd_handshake", {
                **metadata, "filetransfer_fast_fld_chunk_size": 1,
            }), 0.1)
        transfer, = self.receiver.transfers.values()
        self.assertIsNone(transfer["chunk_size"])
        self.assertEqual(transfer["phase"], "handshake_requested")

    def test_bitmap_capacity_accounts_for_long_filenames_and_headers(self):
        metadata = {**METADATA, "filename": "x" * 65535}
        status = self.codec.encode_command("filetransfer_fast_cmd_chunkstatus", {
            **metadata, "data": {"hex": ""},
        }, {"identity": 0, "sender": 2, "receiver": 1, "attribs": 47})
        maximum_bytes = (1 << 20) - 60 - len(status)
        metadata["size"] = maximum_bytes * 8
        self.receiver.observe(self.packet("filetransfer_init_command", metadata), 0)
        self.receiver.outgoing(2, 0)
        self.receiver.observe(self.packet("filetransfer_fast_cmd_handshake", {
            **metadata, "filetransfer_fast_fld_chunk_size": 1,
        }), 0.1)
        self.receiver.outgoing(2, 0.1)
        command, = self.receiver.outgoing(2, 0.41)
        payload = self.codec.encode_command(command["type"], command["fields"], command["header"])
        frame = encode_payload("host", [{"payload_hex": payload.hex()}], 1, "local-b")
        self.assertEqual(decode_payload(frame)["frame_size"], 1 << 20)

        receiver = FastTransferReceiver(self.codec)
        metadata["size"] += 1
        receiver.observe(self.packet("filetransfer_init_command", metadata), 0)
        receiver.outgoing(2, 0)
        with self.assertRaises(ValueError):
            receiver.observe(self.packet("filetransfer_fast_cmd_handshake", {
                **metadata, "filetransfer_fast_fld_chunk_size": 1,
            }), 0.1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
