"""Receive-side serial and fast file transfers for an independent CK3 peer.

Serial mode requests chunk metadata, validates individual chunks and sends
per-chunk acknowledgements. Fast mode uses a handshake and bitmap status.
Native PostCommand(14) adds bit 0x20 to attributes 0x0f. Fast-mode status uses
the compiled 0.3-second heartbeat interval.
"""

from collections import deque

from ck3_llm.protocol.jomini_codec import JominiCodec
from ck3_llm.protocol.pdx_nakama_codec import FRAME_HEADER_SIZE, MAX_FRAME_SIZE
from ck3_llm.protocol.snapshot_transfer import validate_transfer_chunk, serial_chunk_metadata


TRANSFER_ATTRIBS = 0x2F
STATUS_INTERVAL = 0.3
MAX_BITMAP_CHUNKS = 8 * MAX_FRAME_SIZE


def encode_chunk_bitmap(chunk_count, chunk_ids):
    if type(chunk_count) is not int or not 0 <= chunk_count <= MAX_BITMAP_CHUNKS:
        raise ValueError("Invalid transfer chunk count")
    data = bytearray((chunk_count + 7) // 8)
    for chunk in chunk_ids:
        if type(chunk) is not int or not 0 <= chunk < chunk_count:
            raise ValueError("Chunk ID is outside the announced transfer")
        data[chunk // 8] |= 1 << (chunk % 8)
    return bytes(data)


def decode_chunk_bitmap(data, chunk_count):
    if type(chunk_count) is not int or not 0 <= chunk_count <= MAX_BITMAP_CHUNKS:
        raise ValueError("Invalid transfer chunk count")
    if len(data) != (chunk_count + 7) // 8:
        raise ValueError("Chunk status bitmap length disagrees with the transfer")
    if chunk_count % 8 and data[-1] >> (chunk_count % 8):
        raise ValueError("Chunk status bitmap sets unused tail bits")
    return {chunk for chunk in range(chunk_count) if data[chunk // 8] & (1 << (chunk % 8))}


class FastTransferReceiver:
    def __init__(self, codec=None, protocol="fast"):
        if protocol not in ("fast", "serial"):
            raise ValueError("Unsupported file-transfer protocol")
        self.codec = codec or JominiCodec()
        self.protocol = protocol
        self.transfers = {}
        self.pending = deque()
        self.next_identity = 0

    def note_submitted(self, command):
        if command.get("command") == "send_game_command":
            identity = command["header"]["identity"]
            if identity >= self.next_identity:
                self.next_identity = (identity + 1) & 0xFFFFFFFF

    def observe(self, recorded, now):
        if self.protocol == "serial":
            return self._observe_serial(recorded, now)
        if recorded.get("event") != "match_data" or recorded.get("opcode") != 255:
            return []
        presence = recorded.get("presence") or {}
        source = presence.get("user_id")
        events = []
        for message in recorded.get("pdx", {}).get("messages", []):
            command = message.get("jomini", {})
            name = command.get("type", "")
            if name not in ("filetransfer_init_command", "filetransfer_fast_cmd_handshake", "filetransfer_fast_cmd_chunk"):
                continue
            if message.get("transfer_error") or not source:
                continue
            fields = command["fields"]
            metadata = {field: fields[field] for field in ("filename", "size", "checksum")}
            key = (source, metadata["filename"], metadata["size"], metadata["checksum"])
            if name == "filetransfer_init_command":
                sender = command["default_state"]["sender"]
                if key not in self.transfers:
                    self.transfers[key] = {
                        "metadata": metadata, "presence": presence, "host": source,
                        "host_machineid": sender, "phase": "announced", "received": set(),
                        "chunk_size": None, "chunk_count": None, "dirty": False,
                        "last_status": None, "complete": False,
                    }
                    events.append({"event": "transfer_announced", **metadata, "host_user_id": source})
                continue
            transfer = self.transfers.get(key)
            if transfer is None:
                continue
            if name == "filetransfer_fast_cmd_handshake":
                chunk_size = fields["filetransfer_fast_fld_chunk_size"]
                if type(chunk_size) is not int or chunk_size <= 0:
                    raise ValueError("Transfer handshake requires a positive chunk size")
                if transfer["chunk_size"] not in (None, chunk_size):
                    raise ValueError("Transfer chunk size changed after handshake")
                chunk_count = (metadata["size"] + chunk_size - 1) // chunk_size
                empty_status = self.codec.encode_command(
                    "filetransfer_fast_cmd_chunkstatus", {**metadata, "data": {"hex": ""}},
                    {"identity": 0, "sender": 0, "receiver": 0, "attribs": TRANSFER_ATTRIBS},
                )
                maximum_bitmap_bytes = MAX_FRAME_SIZE - FRAME_HEADER_SIZE - 12 - len(empty_status)
                if (chunk_count + 7) // 8 > maximum_bitmap_bytes:
                    raise ValueError("Transfer bitmap exceeds the PDX frame capacity")
                transfer["chunk_size"] = chunk_size
                transfer["chunk_count"] = chunk_count
                transfer["phase"] = "receiving"
                transfer["dirty"] = metadata["size"] == 0
                transfer["last_status"] = now
                self.pending.append((transfer, "filetransfer_fast_cmd_initialize", metadata))
                events.append({"event": "transfer_handshake_received", **metadata, "chunk_size": chunk_size})
            elif transfer["phase"] == "receiving":
                chunk = fields["chunk_id"]
                size = len(bytes.fromhex(fields["data"]["hex"]))
                validate_transfer_chunk(chunk, size, metadata["size"], transfer["chunk_size"])
                transfer["received"].add(chunk)
                transfer["dirty"] = True
        return events

    def outgoing(self, local_machineid, now):
        if local_machineid is None:
            return []
        if self.protocol == "serial":
            self._schedule_serial(local_machineid, now)
            return self._pack_pending(local_machineid)
        for transfer in self.transfers.values():
            if transfer["phase"] == "announced":
                self.pending.append((transfer, "filetransfer_fast_cmd_request_handshake", transfer["metadata"]))
                transfer["phase"] = "handshake_requested"
            elif transfer["phase"] == "receiving":
                last = transfer["last_status"]
                heartbeat = not transfer["complete"] and (last is None or now - last >= STATUS_INTERVAL)
                if transfer["dirty"] or heartbeat:
                    bitmap = encode_chunk_bitmap(transfer["chunk_count"], transfer["received"])
                    fields = {**transfer["metadata"], "data": {"hex": bitmap.hex()}}
                    self.pending.append((transfer, "filetransfer_fast_cmd_chunkstatus", fields))
                    transfer["last_status"] = now
                    transfer["dirty"] = False
                    transfer["complete"] = len(transfer["received"]) == transfer["chunk_count"]
        return self._pack_pending(local_machineid)

    def _pack_pending(self, local_machineid):
        result = []
        while self.pending:
            transfer, name, fields = self.pending.popleft()
            result.append({
                "command": "send_game_command", "type": name, "fields": fields,
                "recipient": transfer["host"], "presences": [transfer["presence"]],
                "header": {"identity": self.next_identity, "sender": local_machineid,
                           "receiver": transfer["host_machineid"], "attribs": TRANSFER_ATTRIBS},
            })
            self.next_identity = (self.next_identity + 1) & 0xFFFFFFFF
        return result

    def _observe_serial(self, recorded, now):
        if recorded.get("event") != "match_data" or recorded.get("opcode") != 255:
            return []
        presence = recorded.get("presence") or {}
        source = presence.get("user_id")
        events = []
        for message in recorded.get("pdx", {}).get("messages", []):
            command = message.get("jomini", {})
            name = command.get("type")
            if name not in ("filetransfer_init_command", "filetransfer_send_chunk_metas_command",
                            "filetransfer_send_chunk_command"):
                continue
            if not source or message.get("transfer_error"):
                continue
            fields = command["fields"]
            metadata = {field: fields[field] for field in ("filename", "size", "checksum")}
            key = (source, metadata["filename"], metadata["size"], metadata["checksum"])
            if name == "filetransfer_init_command":
                if key not in self.transfers:
                    self.transfers[key] = {
                        "metadata": metadata, "presence": presence, "host": source,
                        "host_machineid": command["default_state"]["sender"],
                        "sizes": None, "received": set(), "acknowledge": set(),
                        "last_request": None, "inflight": None, "reported": 0,
                    }
                    events.append({"event": "transfer_announced", "protocol": "serial", **metadata,
                                   "host_user_id": source})
                continue
            transfer = self.transfers.get(key)
            if transfer is None:
                continue
            if name == "filetransfer_send_chunk_metas_command":
                sizes, _ = serial_chunk_metadata(fields)
                if transfer["sizes"] not in (None, sizes):
                    raise ValueError("Serial-transfer chunk sizes changed")
                transfer["sizes"] = list(sizes)
                events.append({"event": "transfer_chunk_metadata_received", **metadata,
                               "chunk_count": len(sizes)})
                continue
            chunk = fields["chunk_id"]
            sizes = transfer["sizes"]
            if sizes is None or type(chunk) is not int or not 0 <= chunk < len(sizes):
                raise ValueError("Serial-transfer chunk ID exceeds its metadata")
            if len(bytes.fromhex(fields["data"]["hex"])) != sizes[chunk]:
                raise ValueError("Serial-transfer chunk length disagrees with its metadata")
            transfer["received"].add(chunk)
            transfer["acknowledge"].add(chunk)
            if transfer["inflight"] and transfer["inflight"][0] == chunk:
                transfer["inflight"] = None
            count = len(transfer["received"])
            if count - transfer["reported"] >= 16 or count == len(sizes):
                transfer["reported"] = count
                events.append({"event": "transfer_progress", "filename": metadata["filename"],
                               "chunks_received": count, "chunks_total": len(sizes),
                               "bytes_received": sum(sizes[index] for index in transfer["received"]),
                               "size": metadata["size"]})
        return events

    def _schedule_serial(self, local_machineid, now):
        for transfer in self.transfers.values():
            metadata = transfer["metadata"]
            sizes = transfer["sizes"]
            if sizes is None:
                last = transfer["last_request"]
                if last is None or now - last >= 1:
                    self.pending.append((transfer, "filetransfer_request_chunk_metas_command", metadata))
                    transfer["last_request"] = now
                continue
            for chunk in sorted(transfer["acknowledge"]):
                self.pending.append((transfer, "filetransfer_chunk_recieved_command", {
                    **metadata, "machineid": local_machineid, "chunk_id": chunk,
                    "chunks_completed": len(transfer["received"]), "chunks_total": len(sizes),
                }))
            transfer["acknowledge"].clear()
            if len(transfer["received"]) == len(sizes):
                continue
            pending = transfer["inflight"]
            if pending is not None and now - pending[1] < 1:
                continue
            chunk = pending[0] if pending else next(index for index in range(len(sizes))
                                                   if index not in transfer["received"])
            self.pending.append((transfer, "filetransfer_request_chunk_command", {**metadata, "chunk_id": chunk}))
            transfer["inflight"] = (chunk, now)
