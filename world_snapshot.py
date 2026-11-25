"""Index CK3 binary world data and extract selected objects as JSON.

Accepts raw binary gamestate data and ZIP/SAV containers. The native hotjoin
path packages its world data in a ZIP entry named hotjoin_<number>. Existing
same-version saves provide offline verification fixtures for this parser.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import struct
import zipfile

from jomini_codec import (
    BLOB, BOOL, CLOSE, EQUAL, F32, FIXED_POINT, I32, JominiCodec,
    OPEN, RGB, STRING, U32, U64, UNQUOTED_STRING,
)


FIXED_WIDTH = {I32: 4, U32: 4, F32: 4, BOOL: 1, FIXED_POINT: 8, U64: 8}
MAX_WORLD_BYTES = 1 << 30


def format_game_date(raw):
    if type(raw) is not int or raw < 0:
        raise ValueError("Game date requires a nonnegative encoded hour count")
    days, hour = divmod(raw, 24)
    year, day = divmod(days, 365)
    month = 1
    for length in (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30):
        if day < length:
            break
        day -= length
        month += 1
    text = f"{year - 5000}.{month}.{day + 1}"
    return text if hour == 0 else f"{text} {hour:02d}:00"


def unpack_world(data, *, max_bytes=MAX_WORLD_BYTES):
    """Return binary world bytes and the observed container layers."""
    layers = []
    for _ in range(4):
        stream = io.BytesIO(data)
        if zipfile.is_zipfile(stream):
            with zipfile.ZipFile(stream) as archive:
                entries = [entry for entry in archive.infolist() if not entry.is_dir()]
                selected = [entry for entry in entries if entry.filename == "gamestate"]
                if not selected:
                    selected = [entry for entry in entries if entry.filename.startswith("hotjoin_")]
                if not selected and len(entries) == 1:
                    selected = entries
                if len(selected) != 1:
                    raise ValueError("World ZIP needs one identifiable gamestate or hotjoin entry")
                entry = selected[0]
                if entry.file_size > max_bytes:
                    raise ValueError("Uncompressed world exceeds the configured byte limit")
                layers.append({"kind": "zip", "entry": entry.filename, "size": entry.file_size})
                data = archive.read(entry)
            continue
        if data.startswith(b"SAV"):
            end = data.find(b"\n", 0, 64)
            if end < 0:
                raise ValueError("Unrecognized SAV container header")
            layers.append({"kind": "sav_header", "header": data[:end].decode("ascii")})
            data = data[end + 1:]
            continue
        if len(data) > max_bytes:
            raise ValueError("World data exceeds the configured byte limit")
        if len(data) < 6 or struct.unpack_from("<H", data, 2)[0] != EQUAL:
            raise ValueError("Expected tokenized binary world data")
        return data, layers
    raise ValueError("Too many nested world container layers")


def _plain(node):
    if node["kind"] == "rgb":
        return {"rgb": _plain(node["value"])}
    if node["kind"] != "object":
        return node["value"]
    named = {}
    repeated = set()
    anonymous = []
    for item in node["items"]:
        value = _plain(item["value"])
        if "key" not in item:
            anonymous.append(value)
            continue
        key = item["key"]["value"]
        if key in repeated:
            named[key].append(value)
        elif key in named:
            named[key] = [named[key], value]
            repeated.add(key)
        else:
            named[key] = value
    if named and anonymous:
        return {"properties": named, "values": anonymous}
    return named if named else anonymous


class WorldSnapshot:
    def __init__(self, data, *, max_bytes=MAX_WORLD_BYTES, range_indexes=None):
        self.source_sha256 = hashlib.sha256(data).hexdigest()
        self.data, self.layers = unpack_world(data, max_bytes=max_bytes)
        self.codec = JominiCodec()
        self._indexes = {}
        self._range_indexes = dict(range_indexes or {})
        self._title_keys = None
        self._title_summaries = None
        self._root = self._index(0, len(self.data), wrapped=False)

    @classmethod
    def from_file(cls, path, **kwargs):
        return cls(Path(path).read_bytes(), **kwargs)

    def _tag(self, position):
        if position + 2 > len(self.data):
            raise ValueError(f"Truncated world token at offset {position}")
        return struct.unpack_from("<H", self.data, position)[0]

    def _token_end(self, position):
        tag = self._tag(position)
        end = position + 2
        if tag in FIXED_WIDTH:
            end += FIXED_WIDTH[tag]
        elif tag in (STRING, UNQUOTED_STRING, BLOB):
            length_width = 4 if tag == BLOB else 2
            if end + length_width > len(self.data):
                raise ValueError(f"Truncated world string/blob length at offset {position}")
            size = struct.unpack_from("<I" if tag == BLOB else "<H", self.data, end)[0]
            end += length_width + size
        elif tag < 0x16 and tag not in (OPEN, CLOSE, EQUAL) and tag not in self.codec.tokens:
            raise ValueError(f"Unknown reserved world token {tag:#x} at offset {position}")
        if end > len(self.data):
            raise ValueError(f"Truncated world scalar at offset {position}")
        return tag, end

    def _value_end(self, start):
        tag, position = self._token_end(start)
        if tag == RGB and self._tag(position) == OPEN:
            tag, position = self._token_end(position)
        if tag in (EQUAL, CLOSE):
            raise ValueError(f"Unexpected world value token at offset {start}")
        if tag != OPEN:
            return position
        depth = 1
        while depth:
            tag, position = self._token_end(position)
            if tag == OPEN:
                depth += 1
                if depth > 128:
                    raise ValueError("World nesting exceeds the parser limit")
            elif tag == CLOSE:
                depth -= 1
        return position

    def _key(self, start, end):
        document = self.codec.decode_document(self.data[start:end])
        if len(document) != 1 or "key" in document[0]:
            raise ValueError(f"Invalid world property key at offset {start}")
        key = document[0]["value"]
        if key["kind"] not in ("token", "string", "i32", "u32", "u64"):
            raise ValueError(f"Invalid world property key type at offset {start}")
        return key["value"]

    def _index(self, start, end, *, wrapped=True):
        cache_key = (start, end, wrapped)
        if cache_key in self._indexes:
            return self._indexes[cache_key]
        position = start
        limit = end
        if wrapped:
            if self._tag(start) != OPEN or self._tag(end - 2) != CLOSE:
                raise ValueError("Requested world path is not an object")
            position += 2
            limit -= 2
        entries = {}
        native_rows = self._range_indexes.pop(cache_key, None)
        if native_rows is not None:
            if len(native_rows) % 16:
                raise ValueError("Invalid native world range index length")
            for key_start, key_end, value_start, value_end in struct.iter_unpack("<IIII", native_rows):
                if (key_start != position or key_end + 2 != value_start or
                        not key_start < key_end < value_start < value_end <= limit):
                    raise ValueError("Native world index crosses an object boundary")
                if self._token_end(key_start)[1] != key_end or self._tag(key_end) != EQUAL:
                    raise ValueError("Native world index has an invalid property key")
                value_tag, scalar_end = self._token_end(value_start)
                object_start = scalar_end if value_tag == RGB else value_start
                if self._tag(object_start) == OPEN:
                    if value_end < object_start + 4 or self._tag(value_end - 2) != CLOSE:
                        raise ValueError("Native world index has an invalid object value")
                elif value_tag in (EQUAL, CLOSE) or scalar_end != value_end:
                    raise ValueError("Native world index has an invalid scalar value")
                entries.setdefault(self._key(key_start, key_end), []).append((value_start, value_end))
                position = value_end
            if position != limit:
                raise ValueError("Native world index does not cover the whole object")
            self._indexes[cache_key] = entries
            return entries
        while position < limit:
            _, key_end = self._token_end(position)
            key = self._key(position, key_end)
            if self._tag(key_end) != EQUAL:
                raise ValueError(f"Expected a world property assignment at offset {key_end}")
            value_start = key_end + 2
            value_end = self._value_end(value_start)
            if value_end > limit:
                raise ValueError("World value crosses its enclosing object boundary")
            entries.setdefault(key, []).append((value_start, value_end))
            position = value_end
        self._indexes[cache_key] = entries
        return entries

    def ranges(self, *path):
        entries = self._root
        for index, key in enumerate(path):
            if key not in entries and isinstance(key, str) and key.isdecimal():
                key = int(key)
            ranges = entries[key]
            if index == len(path) - 1:
                return ranges
            if len(ranges) != 1:
                raise ValueError("A repeated world property needs an explicit instance")
            entries = self._index(*ranges[0])
        return [(0, len(self.data))]

    def _decode_range(self, span):
        start, end = span
        document = self.codec.decode_document(self.data[start:end], max_size=end - start)
        if len(document) != 1 or "key" in document[0]:
            raise ValueError("Expected one world property value")
        return _plain(document[0]["value"])

    def get(self, *path):
        values = [self._decode_range(span) for span in self.ranges(*path)]
        return values[0] if len(values) == 1 else values

    def entries(self, *path):
        spans = self.ranges(*path)
        if len(spans) != 1:
            raise ValueError("Cannot enumerate a repeated world property")
        return self._index(*spans[0])

    def summary(self):
        counts = {str(key): len(spans) for key, spans in self._root.items()}
        result = {"encoding": "jomini_binary", "world_bytes": len(self.data),
                  "containers": self.layers, "sections": counts, "source_sha256": self.source_sha256}
        for name in ("meta_data", "date", "speed", "currently_played_characters", "played_character"):
            if name in self._root:
                result[name] = self.get(name)
        result["record_counts"] = {
            name: len(self.entries(name)) for name in ("living", "dead_unprunable", "provinces")
            if name in self._root
        }
        if "landed_titles" in self._root:
            result["record_counts"]["landed_titles"] = len(self.entries("landed_titles", "landed_titles"))
        return result

    def character(self, character_id):
        for section in ("living", "dead_unprunable", "characters"):
            if section not in self._root:
                continue
            entries = self.entries(section)
            if character_id in entries:
                return {"id": character_id, "section": section, "data": self.get(section, character_id)}
        raise KeyError(f"Character {character_id} was not found")

    def _prepare_titles(self):
        if self._title_summaries is not None:
            return
        keys = {}
        summaries = []
        for title_id, spans in self.entries("landed_titles", "landed_titles").items():
            if len(spans) != 1:
                raise ValueError("Duplicate title ID in world data")
            if self._tag(spans[0][0]) != OPEN:
                if self._decode_range(spans[0]) == "none":
                    continue  # A deleted title retains a scalar slot in hotjoin worlds.
                raise ValueError(f"Title {title_id} is not an object or a deletion slot")
            fields = self._index(*spans[0])
            title = {"id": title_id}
            for field in ("key", "holder", "liege", "de_facto_liege", "de_jure_liege", "capital", "name"):
                if field in fields and (field != "holder" or len(fields[field]) == 1):
                    title[field] = self._decode_range(fields[field][0])
            if "key" in title:
                keys[title["key"]] = title_id
            summaries.append(title)
        self._title_keys = keys
        self._title_summaries = summaries

    def title(self, reference):
        if isinstance(reference, str):
            self._prepare_titles()
            reference = self._title_keys[reference]
        if type(reference) is not int:
            raise ValueError("Title reference must be an integer ID or script name")
        spans = self.ranges("landed_titles", "landed_titles", reference)
        if len(spans) != 1:
            raise ValueError("Duplicate title ID in world data")
        if self._tag(spans[0][0]) != OPEN and self._decode_range(spans[0]) == "none":
            raise KeyError(f"Title {reference} was deleted")
        return {"id": reference, "data": self._decode_range(spans[0])}

    def province(self, province_id):
        if type(province_id) is not int:
            raise ValueError("Province ID must be an integer")
        return {"id": province_id, "data": self.get("provinces", province_id)}

    def observation(self, character_id=None):
        if character_id is None:
            current = self.get("currently_played_characters")
            if not isinstance(current, list) or not current:
                raise ValueError("World has no single default player character")
            character_id = current[0]
        character = self.character(character_id)
        birth = character["data"].get("birth")
        if type(birth) is int and birth >= 0:
            character["birth_date"] = format_game_date(birth)
        self._prepare_titles()
        titles = [deepcopy(title) for title in self._title_summaries if title.get("holder") == character_id]
        date_raw = self.get("date")
        return {"event": "world_observation", "date": format_game_date(date_raw), "date_raw": date_raw,
                "character": character, "held_titles": titles,
                "source": "decoded_world_snapshot"}


def _decode_received_world(path, database_path=None, snapshot_tick=None):
    world = WorldSnapshot.from_file(path)
    summary = world.summary()
    if "landed_titles" in summary["sections"]:
        world._prepare_titles()
    if database_path is not None:
        from world_store import SQLiteWorldStore
        with SQLiteWorldStore(database_path) as store:
            import_result = store.import_snapshot(world, source_path=path, snapshot_tick=snapshot_tick)
    event = {"event": "world_snapshot_decoded", "file": str(Path(path).resolve()),
             "source_sha256": world.source_sha256, "world_bytes": len(world.data),
             "record_counts": summary["record_counts"], "sections": summary["sections"],
             "meta_data": summary.get("meta_data"), "date_raw": summary.get("date")}
    if database_path is not None:
        event["database"] = str(Path(database_path).resolve())
        event["database_import"] = import_result
    if summary.get("currently_played_characters"):
        event["observation"] = world.observation()
    return world, event


class WorldDecodeQueue:
    """Decode completed transfers off the SDK's callback/polling thread."""

    def __init__(self, database_path=None):
        self.executor = None
        self.pending = {}
        self.latest_snapshot = None
        self.database_path = database_path

    def submit(self, path, snapshot_tick=None):
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ck3-world")
        future = self.executor.submit(_decode_received_world, path, self.database_path, snapshot_tick)
        self.pending[future] = str(path)

    def poll(self):
        events = []
        for future in list(self.pending):
            if not future.done():
                continue
            path = self.pending.pop(future)
            try:
                world, event = future.result()
                self.latest_snapshot = world
                events.append(event)
            except Exception as error:
                events.append({"event": "world_decode_error", "file": path,
                               "message": str(error), "error_type": type(error).__name__})
        return events

    def finish(self):
        if self.executor is not None:
            self.executor.shutdown(wait=True)
            self.executor = None
        return self.poll()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--character", type=int)
    parser.add_argument("--path", nargs="+", help="Read a property path such as county_manager counties c_anjou")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        world = WorldSnapshot.from_file(args.snapshot)
        if args.path:
            result = world.get(*args.path)
        elif args.summary:
            result = world.summary()
        else:
            result = world.observation(args.character)
        text = json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False)
        if args.output:
            args.output.write_text(text + "\n", encoding="utf-8")
            print(json.dumps({"output": str(args.output.resolve()), "result": "decoded"}))
        else:
            print(text)
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(json.dumps({"event": "world_decode_error", "message": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
