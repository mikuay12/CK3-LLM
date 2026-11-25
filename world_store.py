"""Persistent, queryable CK3 hotjoin snapshot with explicit source provenance.

The rows represent the last *verified complete snapshot*. Observed commands are
kept separately; receiving a command does not prove its resulting world state.
"""

import argparse
from datetime import datetime, timezone
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys

from jomini_codec import OPEN
from world_snapshot import WorldSnapshot, format_game_date


ROOT_VALUES = ("date", "speed", "currently_played_characters", "played_character", "meta_data", "player_event")
TITLE_SUMMARY_FIELDS = ("key", "holder", "liege", "de_facto_liege", "de_jure_liege", "capital", "name")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class SQLiteWorldStore:
    """Active snapshot, indexed entities and an append-only observed-command log."""

    def __init__(self, path, *, source_world=None):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY,
                sha256 TEXT NOT NULL UNIQUE,
                date_raw INTEGER NOT NULL,
                date_text TEXT NOT NULL,
                snapshot_tick INTEGER,
                source_path TEXT,
                world_bytes INTEGER NOT NULL,
                imported_at TEXT NOT NULL,
                character_count INTEGER NOT NULL,
                province_count INTEGER NOT NULL,
                title_count INTEGER NOT NULL,
                deleted_title_slots INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS active_snapshot (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                snapshot_id INTEGER NOT NULL REFERENCES snapshots(id)
            );
            CREATE TABLE IF NOT EXISTS root_values (
                key TEXT PRIMARY KEY,
                data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER NOT NULL,
                section TEXT NOT NULL,
                instance INTEGER NOT NULL,
                data_json TEXT NOT NULL,
                PRIMARY KEY (section, id, instance)
            );
            CREATE INDEX IF NOT EXISTS characters_by_id ON characters(id);
            CREATE TABLE IF NOT EXISTS provinces (
                id INTEGER PRIMARY KEY,
                data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS titles (
                id INTEGER PRIMARY KEY,
                script_key TEXT UNIQUE,
                holder_id INTEGER,
                data_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS titles_by_holder ON titles(holder_id);
            CREATE TABLE IF NOT EXISTS observed_commands (
                id INTEGER PRIMARY KEY,
                observed_at TEXT NOT NULL,
                snapshot_id INTEGER REFERENCES snapshots(id),
                direction TEXT NOT NULL,
                source_user_id TEXT,
                source_payload_file TEXT,
                command_type TEXT NOT NULL,
                command_identity INTEGER,
                data_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS commands_by_type ON observed_commands(command_type);
        """)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(characters)")}
        if "instance" not in columns:
            with self.connection:
                self.connection.execute("ALTER TABLE characters RENAME TO characters_legacy")
                self.connection.execute("""
                    CREATE TABLE characters (
                        id INTEGER NOT NULL, section TEXT NOT NULL,
                        instance INTEGER NOT NULL, data_json TEXT NOT NULL,
                        PRIMARY KEY (section, id, instance)
                    )
                """)
                self.connection.execute("""
                    INSERT INTO characters (id, section, instance, data_json)
                    SELECT id, section, 0, data_json FROM characters_legacy
                """)
                self.connection.execute("DROP TABLE characters_legacy")
                self.connection.execute("CREATE INDEX characters_by_id ON characters(id)")
        self._source_world = source_world

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @property
    def source_sha256(self):
        return self.snapshot_metadata()["sha256"]

    def snapshot_metadata(self):
        row = self.connection.execute("""
            SELECT s.id, s.sha256, s.date_raw, s.date_text, s.snapshot_tick,
                   s.source_path, s.world_bytes, s.imported_at,
                   s.character_count, s.province_count, s.title_count,
                   s.deleted_title_slots
            FROM active_snapshot AS a JOIN snapshots AS s ON s.id = a.snapshot_id
            WHERE a.singleton = 1
        """).fetchone()
        if row is None:
            raise ValueError("No complete world snapshot has been imported")
        fields = ("id", "sha256", "date_raw", "date", "tick", "source_path",
                  "world_bytes", "imported_at", "characters", "provinces",
                  "titles", "deleted_title_slots")
        return dict(zip(fields, row))

    @staticmethod
    def _records(world, section, *path):
        if section not in world._root:
            return
        for record_id, spans in world.entries(*path).items():
            if type(record_id) is not int:
                raise ValueError(f"Invalid {section} record ID {record_id!r}")
            for instance, span in enumerate(spans):
                if world._tag(span[0]) != OPEN:
                    if world._decode_range(span) == "none":
                        yield record_id, instance, None
                        continue
                    raise ValueError(f"{section} record {record_id} is not an object")
                data = world._decode_range(span)
                if not isinstance(data, dict):
                    raise ValueError(f"{section} record {record_id} is not a named object")
                yield record_id, instance, data

    def import_snapshot(self, world, *, source_path=None, snapshot_tick=None):
        """Replace active entities atomically after complete snapshot verification."""
        if not isinstance(world, WorldSnapshot):
            raise TypeError("import_snapshot requires a verified WorldSnapshot")
        if snapshot_tick is not None and (type(snapshot_tick) is not int or not 0 <= snapshot_tick <= 0xFFFFFFFF):
            raise ValueError("snapshot_tick must be a 32-bit unsigned integer")
        source_path = str(Path(source_path).resolve()) if source_path is not None else None
        date_raw = world.get("date")
        date_text = format_game_date(date_raw)
        current = self.connection.execute("""
            SELECT s.id, s.sha256 FROM active_snapshot AS a
            JOIN snapshots AS s ON s.id = a.snapshot_id WHERE a.singleton = 1
        """).fetchone()
        if current is not None and current[1] == world.source_sha256:
            with self.connection:
                self.connection.execute("""
                    UPDATE snapshots SET source_path = COALESCE(?, source_path),
                        snapshot_tick = COALESCE(?, snapshot_tick) WHERE id = ?
                """, (source_path, snapshot_tick, current[0]))
                events = world.get("player_event") if "player_event" in world._root else []
                self.connection.execute("INSERT OR IGNORE INTO root_values VALUES (?, ?)",
                                        ("player_event", _json(events)))
            self._source_world = world
            return {"reused": True, **self.snapshot_metadata()}

        counts = {"characters": 0, "provinces": 0, "titles": 0, "deleted_title_slots": 0}
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute("DELETE FROM root_values")
            self.connection.execute("DELETE FROM characters")
            self.connection.execute("DELETE FROM provinces")
            self.connection.execute("DELETE FROM titles")
            for name in ROOT_VALUES:
                if name in world._root:
                    self.connection.execute("INSERT INTO root_values VALUES (?, ?)",
                                            (name, _json(world.get(name))))
            self.connection.execute("INSERT OR IGNORE INTO root_values VALUES ('player_event', '[]')")
            for section in ("living", "dead_unprunable"):
                for record_id, instance, data in self._records(world, section, section):
                    if data is None:
                        continue
                    self.connection.execute("INSERT INTO characters VALUES (?, ?, ?, ?)",
                                            (record_id, section, instance, _json(data)))
                    counts["characters"] += 1
            for record_id, instance, data in self._records(world, "provinces", "provinces"):
                if data is None:
                    continue
                if instance:
                    raise ValueError(f"Duplicate province ID {record_id}")
                self.connection.execute("INSERT INTO provinces VALUES (?, ?)",
                                        (record_id, _json(data)))
                counts["provinces"] += 1
            if "landed_titles" in world._root:
                for record_id, instance, data in self._records(world, "landed_titles", "landed_titles", "landed_titles"):
                    if data is None:
                        counts["deleted_title_slots"] += 1
                        continue
                    if instance:
                        raise ValueError(f"Duplicate title ID {record_id}")
                    script_key = data.get("key")
                    holder_id = data.get("holder")
                    if script_key is not None and not isinstance(script_key, str):
                        raise ValueError(f"Title {record_id} has a non-string script key")
                    if type(holder_id) is not int:
                        holder_id = None
                    self.connection.execute("INSERT INTO titles VALUES (?, ?, ?, ?)",
                                            (record_id, script_key, holder_id, _json(data)))
                    counts["titles"] += 1
            imported_at = datetime.now(timezone.utc).isoformat()
            self.connection.execute("""
                INSERT INTO snapshots
                  (sha256, date_raw, date_text, snapshot_tick, source_path,
                   world_bytes, imported_at, character_count, province_count,
                   title_count, deleted_title_slots)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sha256) DO UPDATE SET
                    snapshot_tick=excluded.snapshot_tick,
                    source_path=COALESCE(excluded.source_path, snapshots.source_path)
            """, (world.source_sha256, date_raw, date_text, snapshot_tick,
                  source_path, len(world.data), imported_at,
                  counts["characters"], counts["provinces"], counts["titles"],
                  counts["deleted_title_slots"]))
            snapshot_id = self.connection.execute("SELECT id FROM snapshots WHERE sha256 = ?",
                                                  (world.source_sha256,)).fetchone()[0]
            self.connection.execute("""
                INSERT INTO active_snapshot VALUES (1, ?)
                ON CONFLICT(singleton) DO UPDATE SET snapshot_id=excluded.snapshot_id
            """, (snapshot_id,))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        self._source_world = world
        return {"reused": False, **self.snapshot_metadata()}

    def _entity(self, table, reference, *, section=None):
        if table == "characters":
            rows = self.connection.execute("""
                SELECT section, data_json FROM characters WHERE id = ?
                ORDER BY CASE section WHEN 'living' THEN 0 ELSE 1 END, instance
            """, (reference,)).fetchall()
            if not rows or (section is not None and rows[0][0] != section):
                raise KeyError(f"characters {reference} was not found")
            chosen_section = rows[0][0]
            values = [json.loads(data_json) for row_section, data_json in rows
                      if row_section == chosen_section]
            return chosen_section, values[0] if len(values) == 1 else values
        row = self.connection.execute(
            f"SELECT data_json FROM {table} WHERE id = ?",
            (reference,),
        ).fetchone()
        if row is None:
            raise KeyError(f"{table} {reference} was not found")
        return json.loads(row[0])

    def character(self, character_id):
        if type(character_id) is not int:
            raise ValueError("Character ID must be an integer")
        section, data = self._entity("characters", character_id)
        return {"id": character_id, "section": section, "data": data}

    def province(self, province_id):
        if type(province_id) is not int:
            raise ValueError("Province ID must be an integer")
        return {"id": province_id, "data": self._entity("provinces", province_id)}

    def title(self, reference):
        if isinstance(reference, str):
            row = self.connection.execute("SELECT id, data_json FROM titles WHERE script_key = ?",
                                          (reference,)).fetchone()
        elif type(reference) is int:
            row = self.connection.execute("SELECT id, data_json FROM titles WHERE id = ?",
                                          (reference,)).fetchone()
        else:
            raise ValueError("Title reference must be an integer ID or script name")
        if row is None:
            raise KeyError(f"Title {reference} was not found")
        return {"id": row[0], "data": json.loads(row[1])}

    def _fallback(self):
        if self._source_world is None:
            metadata = self.snapshot_metadata()
            source_path = metadata["source_path"]
            if not source_path or not Path(source_path).is_file():
                raise KeyError("This world path needs the original received snapshot file")
            world = WorldSnapshot.from_file(source_path)
            if world.source_sha256 != metadata["sha256"]:
                raise ValueError("Original snapshot checksum no longer matches the database")
            self._source_world = world
        return self._source_world

    def get(self, *path):
        if not path:
            return self._fallback().get()
        head = path[0]
        if len(path) == 1:
            row = self.connection.execute("SELECT data_json FROM root_values WHERE key = ?", (head,)).fetchone()
            if row is not None:
                return json.loads(row[0])
        if len(path) >= 2 and head in ("living", "dead_unprunable", "provinces"):
            reference = path[1]
            if isinstance(reference, str) and reference.isdecimal():
                reference = int(reference)
            if type(reference) is int:
                if head == "provinces":
                    value = self.province(reference)["data"]
                else:
                    found = self.character(reference)
                    if found["section"] != head:
                        raise KeyError(f"Character {reference} was not in {head}")
                    value = found["data"]
                    if isinstance(value, list) and len(path) > 2:
                        raise ValueError("A repeated world property needs an explicit instance")
                return self._descend(value, path[2:])
        if len(path) >= 3 and path[:2] == ("landed_titles", "landed_titles"):
            reference = path[2]
            if isinstance(reference, str) and reference.isdecimal():
                reference = int(reference)
            return self._descend(self.title(reference)["data"], path[3:])
        return self._fallback().get(*path)

    @staticmethod
    def _descend(value, path):
        for part in path:
            if isinstance(value, dict):
                key = str(part)
                value = value[key]
            elif isinstance(value, list) and str(part).isdecimal():
                value = value[int(part)]
            else:
                raise KeyError(path)
        return deepcopy(value)

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
        titles = []
        for title_id, data_json in self.connection.execute(
            "SELECT id, data_json FROM titles WHERE holder_id = ? ORDER BY id", (character_id,)
        ):
            data = json.loads(data_json)
            titles.append({"id": title_id, **{key: data[key] for key in TITLE_SUMMARY_FIELDS if key in data}})
        date_raw = self.get("date")
        return {"event": "world_observation", "date": format_game_date(date_raw),
                "date_raw": date_raw, "character": character, "held_titles": titles,
                "source": "decoded_world_snapshot"}

    def append_observed_commands(self, commands, *, direction="rx", source_user_id=None,
                                 source_payload_file=None):
        """Record protocol observations without applying unverified state changes."""
        observed_at = datetime.now(timezone.utc).isoformat()
        self.append_observed_events(({
            "time": observed_at, "direction": direction, "source_user_id": source_user_id,
            "source_payload_file": source_payload_file, "command": command,
        } for command in commands))

    def append_observed_events(self, events):
        """Batch log commands, each with the packet's original time and source."""
        snapshot_id = self.snapshot_metadata()["id"]
        with self.connection:
            self.connection.executemany("""
                INSERT INTO observed_commands
                    (observed_at, snapshot_id, direction, source_user_id,
                     source_payload_file, command_type, command_identity, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, ((event["time"], snapshot_id, event.get("direction", "rx"),
                   event.get("source_user_id"), event.get("source_payload_file"),
                   event["command"]["type"],
                   (event["command"].get("default_state") or {}).get("identity"),
                   _json(event["command"]))
                  for event in events))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    importer = subparsers.add_parser("import", help="Import a verified complete world snapshot")
    importer.add_argument("snapshot", type=Path)
    importer.add_argument("--database", type=Path, required=True)
    importer.add_argument("--tick", type=int)
    requester = subparsers.add_parser("request", help="Answer a compact observation request")
    requester.add_argument("--database", type=Path, required=True)
    requester.add_argument("--character", type=int)
    requester.add_argument("--request-file", type=Path)
    args = parser.parse_args()
    try:
        with SQLiteWorldStore(args.database) as store:
            if args.action == "import":
                result = store.import_snapshot(WorldSnapshot.from_file(args.snapshot),
                                               source_path=args.snapshot, snapshot_tick=args.tick)
            else:
                from ai_observation import ObservationInterface
                request = ({"command": "observe"} if args.request_file is None else
                           json.loads(args.request_file.read_text(encoding="utf-8-sig")))
                interface = ObservationInterface(store, args.character)
                interface.snapshot_tick = store.snapshot_metadata()["tick"]
                result = interface.handle(request)
        print(_json(result))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(_json({"event": "world_store_error", "message": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
