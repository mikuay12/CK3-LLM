"""Standalone CK3 Nakama transport prototype for Windows and Python 3.10+.

Run `py ck3_client.py demo` for an offline loopback demonstration using the
installed game DLL. `connect-local` performs a verified local-game handshake,
player registration and serial world-snapshot reception. Direct-memory mode
supports optional model tool calling and reads the host game's native world
state through a read-only process handle.
"""

import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import re
import sqlite3
import sys
import threading
import time
from urllib.parse import quote, urlencode
import urllib.request
import uuid

from nakama_native import DEFAULT_GAME_DIR, NakamaClient, NakamaSDK, load_token
from extract_jomini_tokens import SUPPORTED_EXE_SHA256
from jomini_codec import JominiCodec, activity_plan_fields, declare_war_fields
from session_codec import encode_session_message, session_responses
from session_state import SessionState, SynchronizationState
from pdx_nakama_codec import GAME_DATA_OPCODE, decode_payload
from snapshot_transfer import SnapshotAssembler
from world_snapshot import WorldDecodeQueue
from world_store import SQLiteWorldStore
from ai_observation import ObservationInterface
from transfer_protocol import FastTransferReceiver
from action_feedback import ActionFeedback


class EventWriter:
    """Record JSON events and exact MatchData bytes in a new capture directory."""

    def __init__(self, directory, secrets=(), quiet=False, live_world_pid=None, live_memory_pid=None,
                 notification_policy_path=None, notification_cooldown=0.0, transcript_path=None,
                 llm_format="compact_text"):
        if live_world_pid is not None and live_memory_pid is not None:
            raise ValueError("Choose one live observation source")
        self.game_codec = JominiCodec()
        self.actions = ActionFeedback()
        self.action_room = None
        self.action_host = None
        self.model_runtime = None
        self._transcript = None
        self._llm_format = llm_format
        self.interactions = None
        self.shadow_evaluator = None
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        if transcript_path is not None:
            resolved = Path(transcript_path)
            resolved.parent.mkdir(parents=True, exist_ok=True)
            self._transcript = resolved.open("a", encoding="utf-8")
        self._log = (self.directory / "events.jsonl").open("x", encoding="utf-8")
        self._secrets = [value for secret in secrets for value in (secret, quote(secret, safe=""))]
        self._packet_number = 0
        self.quiet = quiet
        self._transfers = SnapshotAssembler(self.directory / "transfers")
        self.reassembled_files = 0
        self.decoded_worlds = 0
        self.failed_transfers = 0
        self.world_decode_errors = 0
        self.session_state = SessionState()
        self.synchronization_state = SynchronizationState()
        self._world = WorldDecodeQueue(database_path=(
            self.directory / "world.sqlite3" if live_world_pid is None and live_memory_pid is None else None
        ))
        self._store = None
        self._command_log_backfilled = False
        self.observations = ObservationInterface()
        self.live_observations = None
        self.live_memory_pid = live_memory_pid
        self.memory_session = None
        self.local_user_id = None
        self.disconnect_peer = None
        self.disconnect_confirmed = False
        self.server_disconnect_requested = False
        if live_world_pid is not None:
            from live_world import LiveObservationQueue
            self.live_observations = LiveObservationQueue(live_world_pid, self.directory / "live_world")
        elif live_memory_pid is not None:
            from live_objects import DirectObservationQueue
            options = {} if notification_policy_path is None else {"notification_policy_path": notification_policy_path}
            if notification_cooldown:
                options["notification_cooldown"] = notification_cooldown
            self.live_observations = DirectObservationQueue(live_memory_pid, **options)

    def get_shadow_evaluator(self, sdk=None):
        """Return an attached or lazily initialized ShadowEvaluator backed by live memory."""
        if self.shadow_evaluator is not None:
            return self.shadow_evaluator
        live = self.live_observations
        reader = None
        for container in (
            live,
            getattr(live, "reader", None),
            getattr(live, "progress_world", None),
            getattr(live, "objects", None),
            getattr(live, "_objects", None),
        ):
            candidate = getattr(container, "memory", None)
            if candidate is None:
                continue
            if hasattr(candidate, "read") and hasattr(candidate, "base"):
                reader = candidate
                break
            nested = getattr(candidate, "memory", None)
            if hasattr(nested, "read") and hasattr(nested, "base"):
                reader = nested
                break
        if reader is None:
            raise ValueError("send_interaction requires attached game memory or a ShadowEvaluator to evaluate ai_accept")
        from shadow_evaluator import ShadowEvaluator
        executable = Path(sdk.dll_path).with_name("ck3.exe") if sdk is not None else DEFAULT_GAME_DIR / "binaries/ck3.exe"
        self.shadow_evaluator = ShadowEvaluator(executable, memory_source=reader)
        return self.shadow_evaluator

    @staticmethod
    def _observed_events(event):
        if event.get("event") != "match_data" or event.get("routing_ignored"):
            return []
        source_user = (event.get("presence") or {}).get("user_id")
        return [{"time": event["time"], "direction": "rx",
                 "source_user_id": source_user,
                 "source_payload_file": event.get("payload_file"),
                 "command": command}
                for message in event.get("pdx", {}).get("messages", [])
                if (command := message.get("jomini", {})).get("channel") == "command"
                and not command["type"].startswith("filetransfer_")
                and command["type"] != "client_ping"]

    def _backfill_observed_commands(self):
        if self._command_log_backfilled or self._store is None:
            return
        def previous_events():
            with (self.directory / "events.jsonl").open(encoding="utf-8") as log:
                for line in log:
                    yield from self._observed_events(json.loads(line))
        self._store.append_observed_events(previous_events())
        self._command_log_backfilled = True

    def _redact(self, value):
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[redacted]")
            return re.sub(r"(?i)([?&]token=)[^&\s]+", r"\1[redacted]", value)
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        return value

    def write(self, event):
        event = dict(event)
        if event.get("event") == "connected":
            self.local_user_id = event.get("user_id")
        if event.get("event") == "transfer_reassembled":
            self.reassembled_files += 1
            if event.get("checksum_verification") == "failed":
                self.failed_transfers += 1
            else:
                hotjoin = self.synchronization_state.hotjoin or {}
                self._world.submit(event["file"], snapshot_tick=hotjoin.get("tick"))
        if event.get("event") == "world_snapshot_decoded":
            self.decoded_worlds += 1
            snapshot = self._world.latest_snapshot
            if snapshot is not None and snapshot.source_sha256 == event.get("source_sha256"):
                hotjoin = self.synchronization_state.hotjoin or {}
                if self._store is not None:
                    self._store.close()
                self._store = None
                if "database" in event:
                    self._store = SQLiteWorldStore(event["database"], source_world=snapshot)
                self.observations.set_snapshot(self._store or snapshot, hotjoin.get("tick"))
        if event.get("event") == "world_decode_error":
            self.world_decode_errors += 1
        transfer_events = []
        state_events = []
        if event.get("event") == "disconnected":
            state_events.extend(self.actions.cancel_pending("disconnected"))
            self.session_state.clear()
            state_events.append({"event": "session_state", **self.session_state.snapshot()})
            self.synchronization_state.clear()
            state_events.append({"event": "synchronization_state", **self.synchronization_state.snapshot()})
            self.observations.set_character(None)
            self.memory_session = None
        event.setdefault("time", datetime.now(timezone.utc).isoformat())
        payload = event.pop("payload", None)
        if payload is not None:
            self._packet_number += 1
            direction = "tx" if event["event"] == "send_submitted" else "rx"
            filename = f"{self._packet_number:06d}-{direction}.bin"
            (self.directory / filename).write_bytes(payload)
            event.update(
                payload_file=filename, payload_size=len(payload),
                payload_sha256=hashlib.sha256(payload).hexdigest(),
            )
            if event.get("opcode") == GAME_DATA_OPCODE:
                try:
                    event["pdx"] = decode_payload(payload)
                    recipient = event["pdx"].get("recipient")
                    if (direction == "rx" and recipient and self.local_user_id
                            and recipient != self.local_user_id):
                        event["routing_ignored"] = "different_recipient"
                    for message in event["pdx"]["messages"]:
                        try:
                            message["jomini"] = self.game_codec.decode_pdx_message(
                                message, from_server=direction == "rx",
                            )
                            control = message["jomini"]
                            if direction == "tx" and control.get("type") == "ClientRequestDisconnect":
                                self.disconnect_peer = recipient
                                self.disconnect_confirmed = False
                            if direction == "rx" and not event.get("routing_ignored"):
                                source = (event.get("presence") or {}).get("user_id")
                                if control.get("type") == "ServerRespondDisconnect":
                                    if not self.disconnect_peer or source != self.disconnect_peer:
                                        message["state_ignored"] = "unsolicited_disconnect_response"
                                        continue
                                    self.disconnect_confirmed = True
                                    self.disconnect_peer = None
                                elif control.get("type") == "ServerRequestDisconnect":
                                    if self.action_host and source != self.action_host:
                                        message["state_ignored"] = "different_host"
                                        continue
                                    self.server_disconnect_requested = True
                                if control.get("type") in ("ServerRequestDisconnect", "ServerRespondDisconnect"):
                                    state_events.extend(self.actions.cancel_pending("disconnected"))
                                    self.observations.set_character(None)
                                    self.memory_session = None
                                state_events.extend(self.actions.observe(message["jomini"],
                                                                          (event.get("presence") or {}).get("user_id")))
                                try:
                                    state_events.extend(self.session_state.feed(message["jomini"]))
                                except (ValueError, KeyError, TypeError) as error:
                                    message["state_error"] = str(error)
                                try:
                                    state_events.extend(self.synchronization_state.feed(message["jomini"]))
                                except (ValueError, KeyError, TypeError) as error:
                                    message["synchronization_error"] = str(error)
                                try:
                                    player = self.session_state.players.get(self.session_state.local_machineid)
                                    if player is not None and self.live_memory_pid is None:
                                        character = player["playable"]
                                        self.observations.set_character(character if character != 0xFFFFFFFF else None)
                                    relevant = self.observations.feed(message["jomini"])
                                    if self.live_observations is not None:
                                        if self.live_memory_pid is not None:
                                            self.live_observations.feed(message["jomini"])
                                        elif relevant is not None:
                                            self.live_observations.feed(relevant)
                                except (ValueError, KeyError, TypeError) as error:
                                    message["observation_error"] = str(error)
                                try:
                                    transfer_events.extend(self._transfers.feed(
                                        message["jomini"],
                                        source=(event.get("presence") or {}).get("user_id", ""),
                                    ))
                                except (ValueError, KeyError, TypeError) as error:
                                    message["transfer_error"] = str(error)
                        except ValueError as error:
                            message["jomini_decode_error"] = str(error)
                except ValueError as error:
                    event["pdx_decode_error"] = str(error)
            if direction == "rx":
                self.observations.set_synchronization(self.synchronization_state.snapshot())
        event = self._redact(event)
        line = json.dumps(event, ensure_ascii=True)
        self._log.write(line + "\n")
        self._log.flush()
        if self._store is not None:
            try:
                if not self._command_log_backfilled:
                    self._backfill_observed_commands()
                elif event.get("event") == "match_data":
                    observed = self._observed_events(event)
                    if observed:
                        self._store.append_observed_events(observed)
            except (ValueError, TypeError, KeyError, sqlite3.Error) as error:
                self._log.write(json.dumps({"event": "database_command_error", "message": str(error)},
                                           ensure_ascii=True) + "\n")
                self._log.flush()
        if not self.quiet or event["event"] not in ("match_data", "send_submitted", "synchronization_state", "received_turn_acknowledged"):
            output = event
            if self.quiet and event["event"] == "world_snapshot_decoded":
                output = {key: event[key] for key in ("event", "time", "file", "world_bytes", "record_counts", "date_raw")}
            print(json.dumps(output, ensure_ascii=True), flush=True)
        if self.model_runtime is not None:
            self.model_runtime.feed(event, self.action_context())
        elif self._transcript is not None and event.get("event") in (
            "game_notification", "notification_error", "interface_response", "interface_error",
            "action_feedback", "command_error", "game_world_loaded",
        ):
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            if self._llm_format == "compact_text" and event.get("event") in ("game_notification", "action_feedback", "command_error", "notification_error"):
                from llm_formatter import format_notification_line
                body = format_notification_line(event)
            elif self._llm_format == "compact_text" and event.get("event") == "interface_response":
                from llm_formatter import format_tool_result_text
                body = format_tool_result_text(event)
            else:
                body = json.dumps({k: v for k, v in event.items() if k != "time"}, ensure_ascii=False, indent=2)
            self._transcript.write(f"\n[{ts}] [{event['event']}]\n{body}\n")
            self._transcript.flush()
        for transfer_event in transfer_events:
            self.write(transfer_event)
        for state_event in state_events:
            self.write(state_event)
        return event

    def close(self):
        self.finish_world()
        if self._store is not None:
            self._store.close()
        if self._transcript is not None:
            self._transcript.close()
            self._transcript = None
        self._log.close()

    def poll_world(self):
        for event in self._world.poll():
            self.write(event)
        if self.live_observations is not None:
            if self.live_memory_pid is not None and hasattr(self.live_observations, "set_player"):
                self.live_observations.set_player(self.session_state.local_machineid)
            updates = (self.live_observations.poll(self.observations.character_id)
                       if self.live_memory_pid is not None else self.live_observations.poll())
            session = getattr(self.live_observations, "session", None)
            if self.live_memory_pid is not None and isinstance(session, dict):
                self.memory_session = session
                self.observations.set_character(session.get("character_id") if session.get("active") else None)
            for event in updates:
                self.write(event)
            return updates
        return []

    def action_context(self):
        session = self.memory_session or {}
        return {**session, "room": self.action_room, "host": self.action_host,
                "machineid": self.session_state.local_machineid,
                "character_id": self.observations.character_id, "connected": self.session_state.connected}

    def synchronize_actions(self):
        for event in self.actions.synchronize(self.action_context()) + self.actions.poll():
            self.write(event)

    def finish_world(self):
        if self.model_runtime is not None:
            self.model_runtime.close()
        for event in self._world.finish():
            self.write(event)
        if self.live_observations is not None:
            for event in self.live_observations.finish():
                self.write(event)
        for event in self.actions.cancel_pending("connection_closed"):
            self.write(event)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _capture_path(mode):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(__file__).parent / "captures" / f"{mode}-{stamp}-{uuid.uuid4().hex[:8]}"


def replay_capture(source, destination=None, *, quiet=False):
    """Replay saved incoming MatchData through the normal receive pipeline."""
    source = Path(source).resolve()
    destination = destination or _capture_path("replay")
    frames = 0
    decode_errors = 0
    commands = Counter()
    last_synchronization = None
    with (source / "events.jsonl").open(encoding="utf-8-sig") as log, EventWriter(destination, quiet=quiet) as writer:
        writer.write({"event": "replay_started", "source_capture": str(source),
                      "capture_dir": str(writer.directory)})
        for line in log:
            original = json.loads(line)
            if original.get("event") == "connected":
                writer.local_user_id = original.get("user_id")
            if original.get("event") == "send_submitted":
                sent = original.get("pdx") or {}
                for message in sent.get("messages", []):
                    if message.get("jomini", {}).get("type") == "ClientRequestDisconnect":
                        writer.disconnect_peer = sent.get("recipient")
                        writer.disconnect_confirmed = False
            if original.get("event") != "match_data":
                continue
            packet_path = (source / original["payload_file"]).resolve()
            if not packet_path.is_relative_to(source):
                raise ValueError("The replay packet must be inside its capture directory")
            payload = packet_path.read_bytes()
            expected_hash = original.get("payload_sha256")
            if expected_hash and hashlib.sha256(payload).hexdigest() != expected_hash:
                raise ValueError("The replay packet checksum disagrees with its capture log")
            event = {key: original[key] for key in ("time", "match_id", "opcode", "presence") if key in original}
            recorded = writer.write({**event, "event": "match_data", "payload": payload})
            frames += 1
            decode_errors += int("pdx_decode_error" in recorded)
            for message in recorded.get("pdx", {}).get("messages", []):
                decode_errors += int(any(key.endswith("error") for key in message))
                command = message.get("jomini", {})
                if command.get("channel") == "command":
                    commands[command["type"]] += 1
            if writer.synchronization_state.hotjoin is not None:
                last_synchronization = writer.synchronization_state.snapshot()
        writer.finish_world()
        result = {
            "event": "replay_completed", "received_frames": frames, "decode_errors": decode_errors,
            "reassembled_files": writer.reassembled_files, "decoded_worlds": writer.decoded_worlds,
            "failed_transfers": writer.failed_transfers, "world_decode_errors": writer.world_decode_errors,
            "command_messages": sum(commands.values()), "command_types": dict(commands),
            "last_announced_synchronization": last_synchronization,
        }
        writer.write(result)
        return result


def execute_command(client, room_id, command, codec=None):
    """Encode a supported game command or submit explicit protocol bytes."""
    if not isinstance(command, dict):
        raise ValueError("Command must be a JSON object")
    name = command.get("command")
    if name == "send_game_command":
        codec = codec or JominiCodec()
        payload = codec.encode_command(command["type"], command["fields"], command["header"])
        return client.send_envelope(
            room_id, command["recipient"], [{"word_8": 0, "payload_hex": payload.hex()}],
            presences=command.get("presences"),
        )
    if name == "send_session_message":
        payload = encode_session_message(
            command["type"], command["fields"], trailing_hex=command.get("trailing_hex"),
        )
        return client.send_envelope(
            room_id, command["recipient"], [{"word_8": 1, "payload_hex": payload.hex()}],
            presences=command.get("presences"),
        )
    if name == "send_raw":
        payload = bytes.fromhex(command["payload_hex"])
        client.send_raw(room_id, command["opcode"], payload, command.get("presences"))
        return payload
    if name == "send_envelope":
        magic = command.get("magic", "0xcafe")
        if isinstance(magic, str):
            magic = int(magic, 0)
        return client.send_envelope(
            room_id, command.get("recipient", ""), command["messages"],
            magic, command.get("presences"),
        )
    raise ValueError("Supported send commands are send_game_command, send_session_message, send_raw and send_envelope")


def _read_stdin(commands):
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            commands.put(json.loads(line))
        except ValueError:
            commands.put({"command": "invalid_json"})


def _disconnect_game_session(client, room_id, writer, *, timeout, counts):
    """Finish the native leave handshake before closing the Nakama transport."""
    if writer.disconnect_confirmed:
        return "confirmed"
    if not writer.action_host or (writer.session_state.local_machineid is None
                                  and not writer.disconnect_peer and not writer.server_disconnect_requested):
        return "not_connected"
    try:
        if not writer.disconnect_peer and not writer.server_disconnect_requested:
            _settle_succession_before_disconnect(client, room_id, writer, timeout=timeout, counts=counts)
            if not writer.server_disconnect_requested and not writer.disconnect_confirmed:
                command = {"command": "send_session_message", "recipient": writer.action_host,
                           "type": "ClientRequestDisconnect", "fields": {"reason": 0}}
                execute_command(client, room_id, command, writer.game_codec)
                writer.disconnect_peer = writer.action_host
                writer.write({"event": "game_disconnect_requested", "recipient": writer.action_host})
        deadline = time.monotonic() + timeout
        while True:
            for event in client.poll():
                counts[event["event"]] += 1
                recorded = writer.write(event)
                for command in session_responses(recorded):
                    execute_command(client, room_id, command, writer.game_codec)
                if event["event"] == "disconnected":
                    return "transport_disconnected"
            if writer.disconnect_confirmed or writer.server_disconnect_requested:
                result = "confirmed" if writer.disconnect_confirmed else "host_requested"
                writer.write({"event": "game_disconnect_completed", "status": result})
                return result
            if time.monotonic() >= deadline:
                writer.write({"event": "game_disconnect_timeout", "timeout": timeout})
                return "timed_out"
            time.sleep(0.02)
    except (OSError, RuntimeError, ValueError) as error:
        writer.write({"event": "game_disconnect_error", "message": str(error)})
        return "send_failed"


def _maintain_succession(client, room_id, writer, guard, transfer_receiver):
    """Confirm only our already-inherited character; do not toggle ordinary pause."""
    world = getattr(writer.live_observations, "progress_world", None)
    session = writer.memory_session or {}
    machine = writer.session_state.local_machineid
    if (world is None or not session.get("active") or not writer.session_state.connected
            or machine is None or session.get("machineid") != machine):
        return False
    character = session.get("character_id")
    was_pending = guard.pending
    previous_context = guard.context
    responses = guard.poll(world, character, machine, time.monotonic())
    for response in responses:
        command = {**response, "recipient": writer.action_host,
                   "header": {"identity": transfer_receiver.next_identity, "sender": machine,
                              "receiver": 0, **response["header"]}}
        execute_command(client, room_id, command, writer.game_codec)
        transfer_receiver.note_submitted(command)
        writer.write({"event": "succession_close_requested", "request_id": response["request_id"],
                      "machineid": machine, "character_id": character})
    if was_pending and not guard.pending and previous_context == (character, machine):
        writer.write({"event": "succession_confirmation_completed", "machineid": machine,
                      "character_id": character, "source": "native_player_in_game"})
    return guard.pending


def _settle_succession_before_disconnect(client, room_id, writer, *, timeout, counts):
    transfer = getattr(writer, "_transfer_receiver", None)
    if transfer is None or writer.live_observations is None:
        return
    from succession_guard import SuccessionGuard
    guard = SuccessionGuard(interval=0.05, retry_seconds=0.5)
    deadline = time.monotonic() + min(timeout, 3.0)
    try:
        while True:
            writer.poll_world()
            if not _maintain_succession(client, room_id, writer, guard, transfer):
                return
            if time.monotonic() >= deadline:
                writer.write({"event": "succession_cleanup_timeout", "machineid": writer.session_state.local_machineid,
                              "character_id": writer.observations.character_id})
                return
            for event in client.poll():
                counts[event["event"]] += 1
                recorded = writer.write(event)
                for command in session_responses(recorded):
                    execute_command(client, room_id, command, writer.game_codec)
            time.sleep(0.02)
    except (ValueError, KeyError, OSError, RuntimeError) as error:
        writer.write({"event": "succession_cleanup_error", "message": str(error)})


def run_connection(client, room_id, writer, *, metadata=None, timeout=15,
                   duration=60, commands=None, stop_on_echo=False,
                   handshake=None, player_name=None, transfer_protocol="fast", ack_received_turn=False,
                   pause_on_military_event=False, disconnect_timeout=3, ally_call_actor_ids=(),
                   ally_call_machineids=(), player_marriage_machineids=(), auto_disband_at_peace=False):
    """Pump the SDK with bounded transport, room and game handshakes."""
    if player_name and not handshake:
        raise ValueError("Player registration requires a host handshake profile")
    if ack_received_turn and not handshake:
        raise ValueError("Received-turn acknowledgements require the host handshake profile")
    if pause_on_military_event and not handshake:
        raise ValueError("Military decision pauses require the host handshake profile")
    commands = commands if commands is not None else queue.SimpleQueue()
    writer.action_room = room_id
    writer.action_host = handshake.get("recipient") if handshake else None
    counts = Counter()
    phase = "connecting"
    deadline = time.monotonic() + timeout
    joined_at = None
    outgoing = set()
    transfer_receiver = FastTransferReceiver(writer.game_codec, protocol=transfer_protocol)
    writer._transfer_receiver = transfer_receiver
    pending_session_responses = []
    player_announced = False
    world_loaded = False
    stop_reason = None
    last_received_turn = None
    last_acknowledged_turn = None
    join_pause_seen = False
    join_pause_released = False
    from ally_call_guard import AllyCallGuard, MarriageProposalGuard, PeaceArmyGuard
    ally_calls = AllyCallGuard(ally_call_actor_ids, machines=ally_call_machineids)
    marriages = MarriageProposalGuard(machines=player_marriage_machineids)
    peace_armies = PeaceArmyGuard() if auto_disband_at_peace else None
    ally_call_error = None
    from succession_guard import SuccessionGuard
    succession = SuccessionGuard()
    succession_error = None
    client.connect()
    while True:
        updates = writer.poll_world()
        writer.synchronize_actions()
        causes = [event["type"] for event in updates if event.get("event") == "game_notification"
                  and event.get("character_id") == writer.observations.character_id
                  and event["type"] in ("battle_started", "battle_result", "retreat_ended", "war_started")]
        if pause_on_military_event and causes and writer.session_state.connected:
            machine = writer.session_state.local_machineid
            command = {"command": "send_game_command", "recipient": handshake["recipient"],
                       "type": "pause_game_command",
                       "header": {"identity": transfer_receiver.next_identity, "sender": machine,
                                  "receiver": 0, "attribs": 8},
                       "fields": {"player": machine, "value": True}}
            execute_command(client, room_id, command, writer.game_codec)
            transfer_receiver.note_submitted(command)
            writer.write({"event": "military_decision_pause_requested", "causes": causes,
                          "character_id": writer.observations.character_id})
        for event in client.poll():
            name = event["event"]
            counts[name] += 1
            recorded = writer.write(event)
            if name == "match_data" and handshake and not recorded.get("routing_ignored"):
                source = (recorded.get("presence") or {}).get("user_id")
                if source == handshake["recipient"]:
                    for message in recorded.get("pdx", {}).get("messages", []):
                        received = message.get("jomini", {})
                        fields = received.get("fields") or {}
                        if (received.get("type") == "pause_game_command"
                                and fields.get("player") == writer.session_state.local_machineid
                                and fields.get("value") is True):
                            join_pause_seen = True
            if ack_received_turn and name == "match_data" and not recorded.get("routing_ignored"):
                source = (recorded.get("presence") or {}).get("user_id")
                hotjoin = writer.synchronization_state.hotjoin or {}
                for message in recorded.get("pdx", {}).get("messages", []):
                    received = message.get("jomini", {})
                    fields = received.get("fields") or {}
                    if (received.get("type") == "client_ping" and source == handshake["recipient"]
                            and fields.get("machineid") == hotjoin.get("sender")):
                        turn = fields.get("turn")
                        if type(turn) is int and 0 <= turn < 1 << 32 and (
                            last_received_turn is None or 0 < (turn - last_received_turn) % (1 << 32) < 1 << 31
                        ):
                            last_received_turn = turn
            responses = session_responses(recorded)
            for command in responses:
                if command["type"] == "ClientRespondDisconnect":
                    execute_command(client, room_id, command, writer.game_codec)
                else:
                    pending_session_responses.append(command)
            try:
                if not recorded.get("routing_ignored"):
                    for update in transfer_receiver.observe(recorded, time.monotonic()):
                        writer.write(update)
            except (ValueError, KeyError, TypeError) as error:
                writer.write({"event": "transfer_protocol_error", "message": str(error)})
            if name == "connected":
                phase = "joining_nakama_room"
                deadline = time.monotonic() + timeout
                client.join(room_id, metadata)
            elif name == "room_joined" and event["match_id"] == room_id:
                joined_at = time.monotonic()
                phase = "negotiating_game_session" if handshake else "listening"
                deadline = time.monotonic() + timeout
                if handshake:
                    pending_session_responses.append(handshake)
            elif name in ("error", "join_error", "callback_error"):
                raise RuntimeError(f"{name}: {event.get('message', '')}")
            elif name == "disconnected":
                raise ConnectionError(f"Peer disconnected with code {event['code']}")
            elif name == "match_data" and stop_on_echo and event["payload"] in outgoing:
                return {"reason": "echo_received", "counts": dict(counts)}
        if writer.server_disconnect_requested:
            stop_reason = stop_reason or "server_disconnect"
        if stop_reason:
            native_disconnect = _disconnect_game_session(
                client, room_id, writer, timeout=disconnect_timeout, counts=counts,
            )
            return {"reason": stop_reason, "native_disconnect": native_disconnect, "counts": dict(counts)}
        if phase == "negotiating_game_session" and writer.session_state.connected:
            phase = "listening"
        if phase != "listening" and time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out while {phase}")
        if joined_at is not None:
            writer.synchronize_actions()
            if writer.model_runtime is not None:
                for event in writer.model_runtime.poll(writer.action_context()):
                    writer.write(event)
            if player_name and writer.session_state.connected and not player_announced:
                machine = writer.session_state.local_machineid
                if machine is None:
                    raise RuntimeError("The host confirmed connection without assigning a machine ID")
                pending_session_responses.append({
                    "command": "send_game_command", "recipient": handshake["recipient"],
                    "type": "add_player",
                    "header": {"identity": 0, "sender": machine, "receiver": 0, "attribs": 14},
                    "fields": {"name": player_name, "machineid": machine, "is_host": False},
                })
                player_announced = True
                writer.write({"event": "game_player_registration_requested", "name": player_name,
                              "machineid": machine})
            if player_name and writer.decoded_worlds and not world_loaded:
                machine = writer.session_state.local_machineid
                if not writer.session_state.connected or machine is None:
                    raise RuntimeError("The world was decoded after its game session disconnected")
                pending_session_responses.append({
                    "command": "send_game_command", "recipient": handshake["recipient"],
                    "type": "set_player_here_command",
                    "header": {"identity": transfer_receiver.next_identity, "sender": machine,
                               "receiver": 0, "attribs": 14},
                    "fields": {"machineid": machine},
                })
                world_loaded = True
                writer.write({"event": "game_world_loaded", "machineid": machine})
            if player_name and world_loaded and join_pause_seen and not join_pause_released:
                machine = writer.session_state.local_machineid
                player = writer.session_state.players.get(machine)
                if player is not None and player["flags"] & 0x40:
                    pending_session_responses.append({
                        "command": "send_game_command", "recipient": handshake["recipient"],
                        "type": "pause_game_command",
                        "header": {"identity": 0, "sender": machine, "receiver": 0, "attribs": 8},
                        "fields": {"player": machine, "value": False},
                    })
                    join_pause_released = True
                    writer.write({"event": "join_pause_release_requested", "machineid": machine})
            # Native calls stay on this thread even when stdin is read elsewhere.
            progress_world = getattr(writer.live_observations, "progress_world", None)
            if world_loaded and progress_world is not None:
                try:
                    _maintain_succession(client, room_id, writer, succession, transfer_receiver)
                    succession_error = None
                except (ValueError, KeyError, OSError, RuntimeError) as error:
                    if str(error) != succession_error:
                        writer.write({"event": "succession_guard_error", "message": str(error)})
                        succession_error = str(error)
            if world_loaded and progress_world is not None and (ally_calls.actors or ally_calls.machines or marriages.machines or peace_armies):
                try:
                    for response in ally_calls.poll(progress_world, writer.observations.character_id, time.monotonic()):
                        commands.put(response)
                        writer.write({"event": "ally_call_response_queued", "request_id": response["request_id"],
                                      "interaction_id": response["fields"]["character_interaction"],
                                      "character_id": writer.observations.character_id})
                    for response in marriages.poll(progress_world, writer.observations.character_id, time.monotonic()):
                        commands.put(response)
                        writer.write({"event": "marriage_proposal_response_queued", "request_id": response["request_id"],
                                      "interaction_id": response["fields"]["character_interaction"],
                                      "character_id": writer.observations.character_id})
                    if peace_armies is not None:
                        for response in peace_armies.poll(progress_world, writer.observations.character_id, time.monotonic()):
                            commands.put(response)
                            writer.write({"event": "peace_army_disband_queued", "request_id": response["request_id"],
                                          "army_id": response["fields"]["army"],
                                          "character_id": writer.observations.character_id})
                    ally_call_error = None
                except (ValueError, KeyError, OSError, RuntimeError) as error:
                    if str(error) != ally_call_error:
                        writer.write({"event": "ally_call_guard_error", "message": str(error)})
                        ally_call_error = str(error)
            for _ in range(64):
                try:
                    command = commands.get_nowait()
                except queue.Empty:
                    command = (writer.model_runtime.next_command(writer.action_context())
                               if writer.model_runtime is not None else None)
                    if command is None:
                        break
                if isinstance(command, dict) and command.get("command") == "stop":
                    stop_reason = "stop_command"
                    break
                action_request = None
                try:
                    if (isinstance(command, dict)
                            and command.get("command") in (
                                "send_game_command", "declare_war", "send_interaction",
                                "start_activity_command", "set_activity_intent_command",
                            )
                            and command.get("request_id") is not None):
                        previous = writer.actions.replay(command)
                        if previous is not None:
                            writer.write(previous)
                            continue
                        action_request = json.loads(json.dumps(command, allow_nan=False))
                    if isinstance(command, dict) and command.get("command") == "declare_war":
                        optional = ("titles", "claimant", "uses_hook", "has_hook")
                        allowed = {"command", "request_id", "character", "target", "casus_belli", *optional}
                        if set(command) - allowed:
                            raise ValueError("Unknown declare_war request fields")
                        actor = command.get("character", writer.observations.character_id)
                        if actor is None:
                            raise ValueError("Choose a character before declaring war")
                        fields = declare_war_fields(actor, command["target"], command["casus_belli"],
                                                    **{key: command[key] for key in optional if key in command})
                        command = {"command": "send_game_command", "type": "send_character_interaction",
                                   "request_id": command.get("request_id"), "header": {"attribs": 14}, "fields": fields}
                    if isinstance(command, dict) and command.get("command") == "start_activity_command":
                        optional = ("intent", "guest_delay_days", "invite_duration", "options",
                                    "special_guests", "invitations", "travel_plan",
                                    "variables", "tenets", "doctrines")
                        allowed = {"command", "request_id", "host", "type", "start_date", "phases", *optional}
                        if set(command) - allowed:
                            raise ValueError("Unknown start_activity_command request fields")
                        host = command.get("host", writer.observations.character_id)
                        if host is None:
                            raise ValueError("Choose a character before starting an activity")
                        fields = activity_plan_fields(
                            command["type"], host, command["start_date"], command["phases"],
                            **{key: command[key] for key in optional if key in command},
                        )
                        command = {"command": "send_game_command", "type": "start_activity_command",
                                   "request_id": command.get("request_id"), "header": {"attribs": 14}, "fields": fields}
                    if isinstance(command, dict) and command.get("command") == "set_activity_intent_command":
                        allowed = {"command", "request_id", "character", "intent", "target"}
                        if set(command) - allowed:
                            raise ValueError("Unknown set_activity_intent_command request fields")
                        actor = command.get("character", writer.observations.character_id)
                        intent_data = {"type": command["intent"]}
                        if "target" in command and command["target"] != 0xFFFFFFFF:
                            intent_data["target"] = command["target"]
                        fields = {"intent": intent_data}
                        if actor is not None:
                            fields["character"] = actor
                        command = {"command": "send_game_command", "type": "set_activity_intent_command",
                                   "request_id": command.get("request_id"), "header": {"attribs": 14}, "fields": fields}
                    if isinstance(command, dict) and command.get("command") == "send_interaction":
                        optional = ("option_flags", "secondary_actor", "secondary_recipient", "target", "special_fields", "ai_intermediary_accept")
                        allowed = {"command", "request_id", "character", "recipient", "interaction", "ai_accept", *optional}
                        if set(command) - allowed:
                            raise ValueError("Unknown send_interaction request fields")
                        actor = command.get("character", writer.observations.character_id)
                        if actor is None:
                            raise ValueError("Choose a character before sending an interaction")
                        sdk = getattr(client, "sdk", None)
                        if writer.interactions is None:
                            from interaction_catalog import InteractionCatalog
                            executable = Path(sdk.dll_path).with_name("ck3.exe") if sdk is not None else DEFAULT_GAME_DIR / "binaries/ck3.exe"
                            writer.interactions = InteractionCatalog(executable)
                        extra = {
                            key: command[key]
                            for key in ("option_flags", "secondary_actor", "secondary_recipient", "target", "special_fields")
                            if key in command
                        }
                        if "ai_accept" in command:
                            accept_kwargs = {"ai_accept": command["ai_accept"]}
                            if "ai_intermediary_accept" in command:
                                accept_kwargs["ai_intermediary_accept"] = command["ai_intermediary_accept"]
                            fields = writer.interactions.build(actor, command["recipient"], command["interaction"],
                                                               **extra, **accept_kwargs)
                        else:
                            definition = writer.interactions.lookup(command["interaction"])
                            option_flags = command.get("option_flags")
                            names = [option["flag"] for option in definition["send_options"]]
                            if option_flags is not None and (not isinstance(option_flags, dict) or set(option_flags) - set(names)):
                                raise ValueError("Unknown interaction option flags. Query interaction.send_options for supported flags")
                            ordered_options = [bool((option_flags or {}).get(name, False)) for name in names]
                            evaluator = writer.get_shadow_evaluator(sdk=sdk)
                            evaluation = evaluator.evaluate_interaction_acceptance(
                                actor,
                                command["recipient"],
                                command["interaction"],
                                secondary_actor=command.get("secondary_actor", 0xFFFFFFFF),
                                secondary_recipient=command.get("secondary_recipient", 0xFFFFFFFF),
                                target=command.get("target"),
                                options=ordered_options,
                            )
                            fields = writer.interactions.build(
                                actor,
                                command["recipient"],
                                command["interaction"],
                                **extra,
                                ai_accept=evaluation["ai_accept"],
                                ai_intermediary_accept=evaluation["ai_intermediary_accept"],
                            )
                        command = {"command": "send_game_command", "type": "send_character_interaction",
                                   "request_id": command.get("request_id"), "header": {"attribs": 14}, "fields": fields}
                    if isinstance(command, dict) and command.get("command") in (
                            "observe", "query", "observation_status", "refresh_world", "schema", "notifications"):
                        if command.get("command") == "query" and command.get("kind") == "action_status":
                            from live_objects import _fit_response, _request, _select
                            _, budget = _request(command)
                            data = _select(writer.actions.status(command.get("id")), command.get("fields"))
                            writer.write({"event": "interface_response", "request_id": command.get("request_id"),
                                          "result": _fit_response({"kind": "action_status", "data": data}, budget)})
                            continue
                        if writer.live_observations is not None:
                            writer.live_observations.submit(command, writer.observations.character_id)
                        else:
                            writer.write({"event": "interface_response", "request_id": command.get("request_id"),
                                          "result": writer.observations.handle(command)})
                        continue
                    if isinstance(command, dict) and command.get("command") == "send_game_command":
                        defaults = {"identity": transfer_receiver.next_identity, "receiver": 0}
                        if writer.session_state.local_machineid is not None:
                            defaults["sender"] = writer.session_state.local_machineid
                        command = {**command, "header": {**defaults, **command.get("header", {})}}
                        if "recipient" not in command and handshake:
                            command["recipient"] = handshake["recipient"]
                    if action_request is not None:
                        native = writer.game_codec.decode_command(writer.game_codec.encode_command(
                            command["type"], command["fields"], command["header"]))
                        writer.actions.prepare(action_request, native)
                        # Reserve the identity even if sending fails after handing bytes to the SDK.
                        transfer_receiver.note_submitted(command)
                    payload = execute_command(client, room_id, command, writer.game_codec)
                    if action_request is None:
                        transfer_receiver.note_submitted(command)
                    if action_request is not None:
                        feedback = writer.actions.submitted(action_request["request_id"])
                        if feedback is not None:
                            writer.write(feedback)
                    if stop_on_echo:
                        outgoing.add(payload)
                except (ValueError, KeyError, TypeError, OverflowError, RuntimeError, OSError) as error:
                    if action_request is not None:
                        feedback = writer.actions.failed(action_request["request_id"], error)
                        if feedback is not None:
                            writer.write(feedback)
                    request_id = command.get("request_id") if isinstance(command, dict) else None
                    writer.write({"event": "command_error", "request_id": request_id, "message": str(error)})
            if stop_reason:
                # Collect the SDK's queued send callbacks on the next poll.
                continue
            for command in pending_session_responses:
                if command.get("command") == "send_game_command":
                    command["header"]["identity"] = transfer_receiver.next_identity
                payload = execute_command(client, room_id, command, writer.game_codec)
                transfer_receiver.note_submitted(command)
                if stop_on_echo:
                    outgoing.add(payload)
            pending_session_responses.clear()
            for command in transfer_receiver.outgoing(writer.session_state.local_machineid, time.monotonic()):
                payload = execute_command(client, room_id, command, writer.game_codec)
                if stop_on_echo:
                    outgoing.add(payload)
            progress_source = "received_host_commands"
            acknowledgement_turn = last_received_turn
            memory_session = writer.memory_session
            if writer.live_memory_pid is not None:
                # This peer shares A's read-only world. Progress is handled by
                # the runtime even when no host ping is delivered to B.
                player = (memory_session or {}).get("local_player") or {}
                acknowledgement_turn = None
                if ((memory_session or {}).get("active")
                        and player.get("machineid") == writer.session_state.local_machineid
                        and player.get("flags", 0) & 0x40):
                    acknowledgement_turn = memory_session.get("tick")
                progress_source = "shared_game_memory"
            if (ack_received_turn and writer.session_state.connected and writer.decoded_worlds
                    and writer.session_state.local_machineid is not None
                    and acknowledgement_turn is not None and acknowledgement_turn != last_acknowledged_turn):
                machine = writer.session_state.local_machineid
                command = {
                    "command": "send_game_command", "recipient": handshake["recipient"], "type": "client_ping",
                    "header": {"identity": transfer_receiver.next_identity, "sender": machine, "receiver": 0, "attribs": 41},
                    "fields": {"turn": acknowledgement_turn, "machineid": machine},
                }
                execute_command(client, room_id, command, writer.game_codec)
                transfer_receiver.note_submitted(command)
                last_acknowledged_turn = acknowledgement_turn
                writer.write({"event": "received_turn_acknowledged", "machineid": machine,
                              "turn": acknowledgement_turn, "source": progress_source, "world_simulated": False})
            elapsed = time.monotonic() - joined_at
            if stop_on_echo and elapsed >= timeout:
                raise TimeoutError("The mock peer did not echo the submitted payload")
            if duration and elapsed >= duration:
                if stop_on_echo:
                    raise TimeoutError("The demo duration elapsed before receiving an echo")
                stop_reason = "duration_elapsed"
        time.sleep(0.02)


def _session(args, sdk, *, token, host, port, ssl, room_id,
             commands=None, stop_on_echo=False, synthetic=False):
    capture_dir = args.capture_dir or _capture_path(args.command)
    transcript_arg = getattr(args, "model_transcript", None)
    model_enabled = getattr(args, "model_settings", None) is not None
    with EventWriter(capture_dir, secrets=[token], quiet=getattr(args, "quiet", False),
                     live_world_pid=getattr(args, "live_world_pid", None),
                     live_memory_pid=getattr(args, "live_memory_pid", None),
                     notification_policy_path=getattr(args, "notification_policy", None),
                     notification_cooldown=getattr(args, "notification_cooldown", 0.0),
                     transcript_path=(None if (model_enabled or transcript_arg is None)
                                      else (Path(capture_dir) / "llm_transcript.txt" if transcript_arg == "auto" else transcript_arg)),
                     llm_format=getattr(args, "llm_format", "compact_text")) as writer:
        if model_enabled:
            from chat_channel import DEFAULT_CHAT_PATH
            from llm_memory import DEFAULT_MEMORY_PATH
            from model_runtime import ModelRuntime
            transcript_path = (writer.directory / "llm_transcript.txt") if transcript_arg == "auto" else transcript_arg
            chat_arg = getattr(args, "chat_file", "default")
            chat_path = None if chat_arg in (None, "", "none") else (DEFAULT_CHAT_PATH if chat_arg == "default" else Path(chat_arg))
            mem_arg = getattr(args, "memory_file", "default")
            memory_path = None if mem_arg in (None, "", "none") else (DEFAULT_MEMORY_PATH if mem_arg == "default" else Path(mem_arg))
            writer.model_runtime = ModelRuntime(
                args.model_settings, transcript_path=transcript_path, chat_path=chat_path, memory_path=memory_path
            )
        writer.write({
            "event": "prototype_started", "synthetic": synthetic,
            "sdk_version": sdk.version, "host": host, "port": port, "ssl": ssl,
            "room_id": room_id, "capture_dir": str(writer.directory),
            "ck3_game_login": "pending", "world_snapshot": "pending",
        })
        try:
            with NakamaClient(sdk, host, port, ssl, token, args.protocol) as client:
                try:
                    result = run_connection(
                        client, room_id, writer, metadata=getattr(args, "metadata", None),
                        timeout=args.timeout, duration=args.duration,
                        commands=commands, stop_on_echo=stop_on_echo,
                        handshake=getattr(args, "handshake", None),
                        player_name=getattr(args, "player_name", None),
                        transfer_protocol=getattr(args, "file_transfer_protocol", "fast"),
                        ack_received_turn=getattr(args, "ack_received_turn", False),
                        pause_on_military_event=getattr(args, "pause_on_military_event", False),
                        ally_call_actor_ids=getattr(args, "auto_accept_ally_calls_from", ()),
                        ally_call_machineids=getattr(args, "auto_accept_player_calls_from", ()),
                        player_marriage_machineids=getattr(args, "auto_accept_player_marriages_from", ()),
                        auto_disband_at_peace=getattr(args, "auto_disband_at_peace", False),
                    )
                except (KeyboardInterrupt, OSError, RuntimeError, ValueError, TypeError):
                    _disconnect_game_session(client, room_id, writer, timeout=3, counts=Counter())
                    raise
                if synthetic and writer.reassembled_files != 1:
                    raise RuntimeError("The demo did not reassemble the expected synthetic transfer")
        except KeyboardInterrupt:
            writer.write({"event": "stopped", "reason": "keyboard_interrupt"})
            return 130
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            writer.write({"event": "failed", "message": str(error)})
            return 1
        writer.finish_world()
        if synthetic and writer.decoded_worlds != 1:
            raise RuntimeError("The demo did not decode the expected synthetic network world")
        writer.write({"event": "stopped", **result})
        if synthetic:
            writer.write({
                "event": "demo_passed", "synthetic": True,
                "protocol": args.protocol, "ck3_interoperability": "pending",
            })
        return 0


def _positive(value):
    value = float(value)
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError("Value must be a finite positive number")
    return value


def _duration(value):
    value = float(value)
    if not 0 <= value < float("inf"):
        raise argparse.ArgumentTypeError("Duration must be finite and nonnegative")
    return value


def load_local_profile(path, username):
    """Read the room and compatibility bytes exported by the running host."""
    profile = json.loads(Path(path).read_text("utf-8-sig"))
    if not isinstance(profile, dict) or not isinstance(profile.get("handshake"), dict):
        raise ValueError("The local session profile requires an object with handshake fields")
    if profile.get("exe_sha256") != SUPPORTED_EXE_SHA256:
        raise ValueError("The local session profile requires the verified CK3 executable")
    for field in ("host", "match_id", "host_user_id"):
        if not isinstance(profile.get(field), str) or not profile[field] or "\0" in profile[field]:
            raise ValueError("The local session profile requires " + field)
    if type(profile.get("port")) is not int or not 1 <= profile["port"] <= 65535:
        raise ValueError("The local session profile requires a valid port")
    if type(profile.get("ssl")) is not bool:
        raise ValueError("The local session profile requires a boolean ssl value")
    fields = {**profile["handshake"], "name": username}
    encode_session_message("ClientRequestHandshake", fields)
    handshake = {"command": "send_session_message", "type": "ClientRequestHandshake",
                 "recipient": profile["host_user_id"], "fields": fields}
    return profile, handshake


def authenticate_local(profile, identity, username):
    authorization = "Basic " + base64.b64encode(b"ck3-local:").decode()
    scheme = "https" if profile["ssl"] else "http"
    url = f"{scheme}://{profile['host']}:{profile['port']}/v2/account/authenticate/custom?"
    url += urlencode({"username": username})
    request = urllib.request.Request(url, data=json.dumps({"id": identity}).encode(), headers={
        "Authorization": authorization, "Content-Type": "application/json",
    })
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["token"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--game-dir", type=Path, default=DEFAULT_GAME_DIR)
    subcommands.add_parser("sdk-info", parents=[common], help="Check the installed DLL")
    replay = subcommands.add_parser("replay", help="Decode saved incoming packets offline")
    replay.add_argument("source_capture", type=Path)
    replay.add_argument("--capture-dir", type=Path, help="New directory for replay results")
    replay.add_argument("--quiet", action="store_true")
    transport = argparse.ArgumentParser(add_help=False)
    transport.add_argument("--protocol", choices=("protobuf", "json"), default="protobuf")
    transport.add_argument("--capture-dir", type=Path, help="New directory for this session")
    transport.add_argument("--timeout", type=_positive, default=15)
    transport.add_argument("--duration", type=_duration, default=60, help="Seconds after room join, 0 waits until stopped")
    transport.add_argument("--quiet", action="store_true", help="Keep packet details in the capture files and show lifecycle events")
    transport.add_argument("--notification-policy", type=Path, help="Notification preference file for direct-memory observations")
    demo = subcommands.add_parser("demo", parents=[common, transport], help="Run a synthetic loopback peer")
    demo.add_argument("--transfer-protocol", action="store_true", help="Exercise active snapshot handshake, bitmap feedback and retransmission")
    connect = subcommands.add_parser("connect", parents=[common, transport], help="Connect with an existing Nakama session")
    connect.add_argument("--host", required=True)
    connect.add_argument("--port", type=int, default=443)
    connect.add_argument("--ssl", action=argparse.BooleanOptionalAction, default=True)
    connect.add_argument("--room-id", required=True, help="Actual Nakama match ID")
    connect.add_argument("--token-file", type=Path, required=True)
    connect.add_argument("--metadata-file", type=Path)
    connect.add_argument("--stdin", action="store_true", help="Read send_game_command, send_session_message, send_raw, send_envelope and stop JSON commands")
    connect.add_argument("--live-world-pid", type=int, help="Read observations from this local CK3 process on demand")
    connect.add_argument("--live-memory-pid", type=int, help="Query CK3 objects directly with a read-only process handle")
    connect.add_argument("--model-config", type=Path, help="Enable model tool calling using this endpoint configuration")
    connect.add_argument("--model-transcript", nargs="?", const="auto", default=None,
                         help="Record all prompts sent to the LLM and its replies to a local UTF-8 text file (omit path to save into capture_dir/llm_transcript.txt)")
    connect.add_argument("--notification-cooldown", type=float, default=60.0,
                         help="Minimum interval in seconds for recurring/flapping notifications (default 60; 0 to disable)")
    connect.add_argument("--llm-format", choices=("compact_text", "json"), default="compact_text",
                         help="Format for VM -> LLM notifications and query results (default compact_text)")
    connect.add_argument("--modal-gate", choices=("strict", "no_wait", "off"), default="strict",
                         help="Priority gate for blocking window events/interactions/invitations (default strict)")
    connect.add_argument("--chat-file", default="default",
                         help="JSONL chat channel shared with control_panel.py (default chat_channel.jsonl; 'none' to disable)")
    connect.add_argument("--memory-file", default="default",
                         help="JSON memory store for LLM campaign memory and experience (default llm_memory.json; 'none' to disable)")
    connect.add_argument("--pause-on-military-event", action="store_true",
                         help="Pause on battle start/result, retreat completion or a new war for model decisions")
    local = subcommands.add_parser("connect-local", parents=[common, transport], help="Join the running local CK3 host using its exported session profile")
    local.add_argument("--session-profile", type=Path, required=True)
    local.add_argument("--username", default="AIObserver")
    local.add_argument("--identity", default="ck3-ai-observer")
    local.add_argument("--file-transfer-protocol", choices=("serial", "fast"), default="serial")
    local.add_argument("--stdin", action="store_true", help="Read JSON commands from stdin")
    local.add_argument("--live-world-pid", type=int, help="Read observations from this local CK3 process on demand")
    local.add_argument("--live-memory-pid", type=int, help="Query CK3 objects directly with a read-only process handle")
    local.add_argument("--model-config", type=Path, help="Enable model tool calling using this endpoint configuration")
    local.add_argument("--model-transcript", nargs="?", const="auto", default=None,
                       help="Record all prompts sent to the LLM and its replies to a local UTF-8 text file (omit path to save into capture_dir/llm_transcript.txt)")
    local.add_argument("--notification-cooldown", type=float, default=60.0,
                       help="Minimum interval in seconds for recurring/flapping notifications (default 60; 0 to disable)")
    local.add_argument("--llm-format", choices=("compact_text", "json"), default="compact_text",
                       help="Format for VM -> LLM notifications and query results (default compact_text)")
    local.add_argument("--modal-gate", choices=("strict", "no_wait", "off"), default="strict",
                       help="Priority gate for blocking window events/interactions/invitations (default strict)")
    local.add_argument("--chat-file", default="default",
                       help="JSONL chat channel shared with control_panel.py (default chat_channel.jsonl; 'none' to disable)")
    local.add_argument("--memory-file", default="default",
                       help="JSON memory store for LLM campaign memory and experience (default llm_memory.json; 'none' to disable)")
    local.add_argument("--pause-on-military-event", action="store_true",
                       help="Pause on battle start/result, retreat completion or a new war for model decisions")
    local.add_argument("--ack-received-turn", action="store_true",
                       help="Acknowledge received host turns for transport-progress experiments")
    for target in (connect, local):
        target.add_argument("--auto-accept-ally-calls-from", type=int, action="append", default=[],
                            help="Standing model decision: immediately accept war calls only from this character ID (repeatable)")
        target.add_argument("--auto-accept-player-calls-from", type=int, action="append", default=[],
                            help="Accept war calls from the current character of this machine ID, including after succession")
        target.add_argument("--auto-accept-player-marriages-from", type=int, action="append", default=[],
                            help="Accept marriage proposals from the current character of this machine ID, including after succession")
        target.add_argument("--auto-disband-at-peace", action="store_true",
                            help="Standing model decision: disband idle armies after the final active war ends")
    args = parser.parse_args()
    try:
        if args.command == "replay":
            result = replay_capture(args.source_capture, args.capture_dir, quiet=args.quiet)
            return int(any(result[key] for key in ("decode_errors", "failed_transfers", "world_decode_errors")))
        if getattr(args, "model_config", None) is not None:
            from dataclasses import replace
            from model_runtime import ModelConfig
            if args.live_memory_pid is None:
                raise ValueError("Model tool calling requires --live-memory-pid for current character and world tracking")
            loaded = ModelConfig.load(args.model_config)
            raw_cfg = json.loads(args.model_config.read_text(encoding="utf-8-sig"))
            if "response_format" not in raw_cfg and getattr(args, "llm_format", None):
                loaded = replace(loaded, response_format=args.llm_format)
            if "modal_gate" not in raw_cfg and getattr(args, "modal_gate", None):
                loaded = replace(loaded, modal_gate=args.modal_gate)
            if "fold_old_tool_results" not in raw_cfg:
                loaded = replace(loaded, fold_old_tool_results=True)
            args.model_settings = loaded
        sdk = NakamaSDK(args.game_dir)
        if args.command == "sdk-info":
            print(json.dumps({
                "dll": str(sdk.dll_path), "sdk_version": sdk.version,
                "python_bits": 64, "ck3_interoperability": "pending",
            }))
            return 0
        commands = queue.SimpleQueue()
        if args.command == "demo":
            from mock_nakama import MOCK_HOST, MOCK_ROOM, MockNakamaServer
            if not args.transfer_protocol:
                commands.put({
                    "command": "send_game_command", "recipient": MOCK_HOST,
                    "type": "set_character_controller",
                    "header": {"identity": 0, "sender": 1, "receiver": 0},
                    "fields": {"machineid": 1, "playable": 12345},
                })
            with MockNakamaServer("transfer_protocol" if args.transfer_protocol else "normal") as peer:
                result = _session(
                    args, sdk, token=peer.token, host="127.0.0.1", port=peer.port,
                    ssl=False, room_id=MOCK_ROOM, commands=commands,
                    stop_on_echo=True, synthetic=True,
                )
                if peer.errors:
                    raise RuntimeError("Local test peer failed: " + ", ".join(peer.errors))
                if args.transfer_protocol and result == 0 and (
                    not peer.transfer_complete or not peer.transfer_retransmissions
                ):
                    raise RuntimeError("The demo did not finish bitmap acknowledgement and retransmission")
                return result
        if args.command == "connect-local":
            profile, args.handshake = load_local_profile(args.session_profile, args.username)
            token = authenticate_local(profile, args.identity, args.username)
            args.host, args.port, args.ssl = profile["host"], profile["port"], profile["ssl"]
            args.room_id = profile["match_id"]
            args.player_name = args.username
        else:
            token = load_token(args.token_file)
        args.metadata = {}
        if getattr(args, "metadata_file", None):
            args.metadata = json.loads(args.metadata_file.read_text("utf-8-sig"))
            if not isinstance(args.metadata, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in args.metadata.items()
            ):
                raise ValueError("Metadata must be a JSON object containing string values")
        if args.stdin:
            threading.Thread(target=_read_stdin, args=(commands,), daemon=True).start()
        return _session(
            args, sdk, token=token, host=args.host, port=args.port, ssl=args.ssl,
            room_id=args.room_id, commands=commands,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        print(json.dumps({"event": "failed", "message": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
