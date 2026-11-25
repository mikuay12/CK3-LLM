"""Reassemble and verify CK3 files received through multiplayer.

The rolling checksum follows the hotjoin builder at RVA 0x369aa62 and the
file checksum function at RVA 0x382fac0 in the supported executable.
"""

import hashlib
from itertools import accumulate
import json
from pathlib import Path
import zipfile


class TransferChecksum:
    """Two byte-wise sums modulo 65535, with native positive-zero handling."""

    def __init__(self):
        self.first = 0
        self.second = 0

    def update(self, data):
        # Accumulate before reduction so iteration stays in the stdlib's C code.
        second = self.second + sum(accumulate(data, initial=self.first)) - self.first
        first = self.first + sum(data)
        if first:
            self.first = (first - 1) % 65535 + 1
        if second:
            self.second = (second - 1) % 65535 + 1

    @property
    def value(self):
        value = (self.first << 16) | self.second
        return value - (1 << 32) if value >= 1 << 31 else value


def validate_transfer_chunk(chunk_id, length, size, chunk_size):
    count = (size + chunk_size - 1) // chunk_size
    if type(chunk_id) is not int or not 0 <= chunk_id < count:
        raise ValueError("File-transfer chunk ID exceeds the negotiated count")
    expected = min(chunk_size, size - chunk_id * chunk_size)
    if length != expected:
        raise ValueError("File-transfer chunk length disagrees with its negotiated offset")


def serial_chunk_metadata(fields):
    sizes, checksums = fields.get("sizes", []), fields.get("checksums", [])
    if (not isinstance(sizes, list) or not isinstance(checksums, list)
            or len(sizes) != len(checksums)
            or any(type(value) is not int or not 0 < value < 1 << 32 for value in sizes)
            or any(type(value) is not int or not -(1 << 31) <= value < 1 << 31 for value in checksums)
            or sum(sizes) != fields["size"]):
        raise ValueError("Serial-transfer chunk metadata disagrees with the announced file")
    return sizes, checksums


def verify_transfer_checksum(path, filename, declared, transport_checksum):
    result = {"transport_checksum": transport_checksum,
              "computed_checksum": transport_checksum, "checksum_scope": "transport_file"}
    if transport_checksum != declared and filename.startswith("hotjoin_") and zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as archive:
                checksum = TransferChecksum()
                with archive.open(filename) as world:
                    for block in iter(lambda: world.read(1 << 20), b""):
                        checksum.update(block)
                result["zip_entry_checksum"] = checksum.value
                if checksum.value == declared:
                    result.update(computed_checksum=checksum.value, checksum_scope="zip_entry", checksum_entry=filename)
        except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as error:
            result["checksum_error"] = str(error)
    result["checksum_verification"] = "passed" if result["computed_checksum"] == declared else "failed"
    return result


class SnapshotAssembler:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.transfers = {}

    def feed(self, command, *, source=""):
        name = command.get("type", "")
        if name not in (
            "filetransfer_init_command",
            "filetransfer_fast_cmd_request_handshake", "filetransfer_fast_cmd_handshake",
            "filetransfer_fast_cmd_initialize", "filetransfer_fast_cmd_chunk",
            "filetransfer_send_chunk_metas_command", "filetransfer_send_chunk_command",
        ):
            return []
        fields = command["fields"]
        filename, size, checksum = fields["filename"], fields["size"], fields["checksum"]
        if (not isinstance(filename, str) or type(size) is not int or not 0 <= size < 1 << 32
                or type(checksum) is not int or not -(1 << 31) <= checksum < 1 << 31
                or not isinstance(source, str)):
            raise ValueError("Invalid file-transfer metadata")
        key = (source, filename, size, checksum)
        if key not in self.transfers:
            transfer_id = hashlib.sha256(json.dumps(key).encode("utf-8")).hexdigest()[:24]
            folder = self.directory / transfer_id
            folder.mkdir(parents=True, exist_ok=False)
            self.transfers[key] = {
                "folder": folder, "chunks": {}, "bytes_received": 0,
                "chunk_size": None, "complete": False,
                "chunk_sizes": None, "chunk_checksums": None,
            }
        transfer = self.transfers[key]
        if name == "filetransfer_send_chunk_metas_command":
            sizes, checksums = serial_chunk_metadata(fields)
            if transfer["chunk_sizes"] not in (None, sizes) or transfer["chunk_checksums"] not in (None, checksums):
                raise ValueError("Serial-transfer chunk metadata changed within one transfer")
            transfer["chunk_sizes"], transfer["chunk_checksums"] = list(sizes), list(checksums)
        if name == "filetransfer_fast_cmd_handshake":
            chunk_size = fields["filetransfer_fast_fld_chunk_size"]
            if type(chunk_size) is not int or chunk_size <= 0:
                raise ValueError("Transfer chunk size must be positive")
            if transfer["chunk_size"] not in (None, chunk_size):
                raise ValueError("Transfer chunk size changed within one transfer")
            for chunk_id, chunk in transfer["chunks"].items():
                validate_transfer_chunk(chunk_id, chunk["size"], size, chunk_size)
            transfer["chunk_size"] = chunk_size
        if name in ("filetransfer_fast_cmd_chunk", "filetransfer_send_chunk_command"):
            chunk_id = fields["chunk_id"]
            if type(chunk_id) is not int or not 0 <= chunk_id <= 0x7FFFFFFF:
                raise ValueError("Invalid file-transfer chunk ID")
            data = bytes.fromhex(fields["data"]["hex"])
            if transfer["complete"] and size == 0 and not data:
                return []
            if len(data) > size:
                raise ValueError("A file-transfer chunk exceeds the declared file size")
            if size and not data:
                raise ValueError("Nonempty file transfers require nonempty chunks")
            if size and transfer["chunk_size"] is not None:
                validate_transfer_chunk(chunk_id, len(data), size, transfer["chunk_size"])
            if name == "filetransfer_send_chunk_command":
                sizes = transfer["chunk_sizes"]
                if sizes is None or chunk_id >= len(sizes) or len(data) != sizes[chunk_id]:
                    raise ValueError("Serial-transfer chunk length disagrees with its metadata")
                checksum_state = TransferChecksum()
                checksum_state.update(data)
                if checksum_state.value != transfer["chunk_checksums"][chunk_id]:
                    raise ValueError("Serial-transfer chunk checksum failed")
            digest = hashlib.sha256(data).hexdigest()
            previous = transfer["chunks"].get(chunk_id)
            if previous is not None:
                if previous["sha256"] != digest:
                    raise ValueError("Conflicting retransmission of a file-transfer chunk")
                return []
            if transfer["complete"] or transfer["bytes_received"] + len(data) > size:
                raise ValueError("File-transfer chunks exceed the declared file size")
            chunk_path = transfer["folder"] / f"chunk-{chunk_id:010d}.bin"
            chunk_path.write_bytes(data)
            transfer["chunks"][chunk_id] = {"path": chunk_path, "size": len(data), "sha256": digest}
            transfer["bytes_received"] += len(data)
        if transfer["complete"] or transfer["bytes_received"] != size:
            return []
        chunks = transfer["chunks"]
        if size and (not chunks or max(chunks) != len(chunks) - 1):
            return []
        ordered = [chunks[index] for index in range(len(chunks))]
        temporary = transfer["folder"] / "received.partial"
        digest = hashlib.sha256()
        native_checksum = TransferChecksum()
        with temporary.open("wb") as output:
            for chunk in ordered:
                data = chunk["path"].read_bytes()
                digest.update(data)
                native_checksum.update(data)
                output.write(data)
        target = transfer["folder"] / "received.bin"
        temporary.replace(target)
        transfer["complete"] = True
        verification = verify_transfer_checksum(target, filename, checksum, native_checksum.value)
        return [{
            "event": "transfer_reassembled", "original_filename": filename,
            "file": str(target.resolve()), "size": size, "sha256": digest.hexdigest(),
            "source_user_id": source or None,
            "declared_checksum": checksum, **verification,
            "world_state_decoding": "pending" if verification["checksum_verification"] == "passed" else "skipped",
        }]
