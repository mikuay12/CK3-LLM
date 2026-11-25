"""Track the session identity and player data explicitly received from CK3."""

from copy import deepcopy


def _machine_id(value):
    if type(value) is not int or not -(1 << 31) <= value < (1 << 32):
        raise ValueError("Invalid received machine ID")
    return value - (1 << 32) if value >= 1 << 31 else value


class SessionState:
    def __init__(self):
        self.local_machineid = None
        self.connected = False
        self.peers = {}
        self.players = {}

    def snapshot(self):
        return deepcopy({
            "local_machineid": self.local_machineid, "session_connected": self.connected,
            "peers": list(self.peers.values()), "players": list(self.players.values()),
        })

    def feed(self, message):
        name = message.get("type")
        fields = message.get("fields") or {}
        if name == "ServerRequestAddPeer":
            machine = _machine_id(fields["peer_id"])
            peer = {"machineid": machine, "peer_value": fields["peer_value"]}
            assigned = bool(fields["assign_local"])
            if self.peers.get(machine) == peer and (not assigned or self.local_machineid == machine):
                return []
            self.peers[machine] = peer
            if assigned:
                self.local_machineid = machine
        elif name == "ServerRequestRemovePeer":
            machine = _machine_id(fields["peer_id"])
            if machine not in self.peers:
                return []
            del self.peers[machine]
            if machine == self.local_machineid:
                self.local_machineid = None
                self.connected = False
        elif name == "ServerRespondConnected":
            if self.connected:
                return []
            self.connected = True
        elif name in ("ServerRequestDisconnect", "ServerRespondDisconnect"):
            self.clear()
        elif name == "sync_players_command":
            columns = [fields.get(key, []) for key in ("machineid", "players", "character", "flags")]
            if any(not isinstance(column, list) for column in columns):
                raise ValueError("Player synchronization requires array fields")
            count = len(columns[0])
            if not count or any(len(column) != count for column in columns):
                raise ValueError("Player synchronization arrays must have the same nonzero length")
            players = {}
            for machine, player_name, playable, flags in zip(*columns):
                machine = _machine_id(machine)
                if machine in players:
                    raise ValueError("Player synchronization contains duplicate machine IDs")
                if (not isinstance(player_name, str) or type(playable) is not int
                        or not 0 <= playable < (1 << 32) or type(flags) is not int or not 0 <= flags <= 255):
                    raise ValueError("Invalid player synchronization data")
                players[machine] = {"machineid": machine, "name": player_name,
                                    "playable": playable, "flags": flags}
            if self.players == players:
                return []
            self.players = players
        elif name in ("set_character_controller", "set_player_here_command", "set_ready_command"):
            machine = _machine_id(fields["machineid"])
            player = self.players.get(machine)
            if player is None:
                return []
            updated = dict(player)
            if name == "set_character_controller":
                playable = fields["playable"]
                if type(playable) is not int or not 0 <= playable < 1 << 32:
                    raise ValueError("Invalid received character ID")
                updated["playable"] = playable
            elif name == "set_player_here_command":
                updated["flags"] |= 0x40
            else:
                if type(fields["status"]) is not bool:
                    raise ValueError("Invalid received ready status")
                updated["flags"] = (updated["flags"] | 2) if fields["status"] else (updated["flags"] & ~2)
            if updated == player:
                return []
            self.players[machine] = updated
        else:
            return []
        return [{"event": "session_state", **self.snapshot()}]

    def clear(self):
        self.local_machineid = None
        self.connected = False
        self.peers.clear()
        self.players.clear()


class SynchronizationState:
    """Track announced synchronization positions and received tick messages."""

    def __init__(self):
        self.clear()

    def clear(self):
        self.hotjoin = None
        self.peer_turns = {}
        self.received_ticks = {"daily_tick": 0, "event_tick": 0}

    def snapshot(self):
        return deepcopy({
            "hotjoin": self.hotjoin, "peer_turns": list(self.peer_turns.values()),
            "received_ticks_since_hotjoin": self.received_ticks,
        })

    def feed(self, message):
        name = message.get("type")
        fields = message.get("fields") or {}
        if name == "hotjoin_sync_command":
            data = fields["data"]
            if not isinstance(data, dict):
                raise ValueError("Hotjoin synchronization requires a data object")
            tick = data["tick"]
            if type(tick) is not int or not 0 <= tick < 1 << 32:
                raise ValueError("Invalid announced hotjoin tick")
            if type(data["pause_game"]) is not bool or type(data["host_has_started_game"]) is not bool:
                raise ValueError("Invalid announced hotjoin flags")
            peers = data.get("id", [])
            if peers == {}:
                peers = []
            if not isinstance(peers, list):
                raise ValueError("Hotjoin synchronization requires a peer ID array")
            peer_ids = [_machine_id(peer["value"] if isinstance(peer, dict) else peer) for peer in peers]
            announced = {
                "tick": tick, "peer_ids": peer_ids,
                "sender": _machine_id(message["default_state"]["sender"]),
                "pause_game": data["pause_game"], "host_has_started_game": data["host_has_started_game"],
            }
            if self.hotjoin == announced:
                return []
            self.clear()
            self.hotjoin = announced
        elif name == "client_ping":
            machine = _machine_id(fields["machineid"])
            turn = fields["turn"]
            if type(turn) is not int or not 0 <= turn < 1 << 32:
                raise ValueError("Invalid announced client turn")
            peer = {"machineid": machine, "turn": turn}
            if self.peer_turns.get(machine) == peer:
                return []
            self.peer_turns[machine] = peer
        elif name in self.received_ticks:
            if self.hotjoin is None:
                return []
            self.received_ticks[name] += 1
        elif name in ("ServerRequestDisconnect", "ServerRespondDisconnect"):
            self.clear()
        else:
            return []
        return [{"event": "synchronization_state", **self.snapshot()}]
