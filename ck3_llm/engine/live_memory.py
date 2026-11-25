"""Read selected live CK3 state directly from the host process, without a save.

This is a small, version-bound read-only interface. It returns current player
state and character focus; more world fields require verified native layouts.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys

import frida

from ck3_llm.version import SUPPORTED_EXE_SHA256


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _pick_game_process(device, pid):
    processes = [process for process in device.enumerate_processes()
                 if process.name.lower() == "ck3.exe"]
    if pid is not None:
        for process in processes:
            if process.pid == pid:
                return process.pid
        raise ValueError(f"CK3 process {pid} was not found")
    if len(processes) != 1:
        raise ValueError("Start CK3 first, or specify --pid if multiple CK3 processes are running")
    return processes[0].pid


def _open_probe(pid, *, extra_source=""):
    device = frida.get_local_device()
    session = device.attach(_pick_game_process(device, pid))
    try:
        version = session.create_script("rpc.exports={executable(){return Process.mainModule.path}}")
        version.load()
        executable = Path(version.exports_sync.executable())
        version.unload()
        actual_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
        if actual_hash != SUPPORTED_EXE_SHA256:
            raise ValueError("This CK3 executable does not match the verified memory offsets")
        source = Path(__file__).with_suffix(".js").read_text(encoding="utf-8")
        script = session.create_script(source + "\n" + extra_source)
        script.load()
        return session, script
    except Exception:
        session.detach()
        raise


def _answer(script, request):
    if not isinstance(request, dict):
        raise ValueError("Live query must be a JSON object")
    request_id = request.get("request_id")
    kind = request.get("kind")
    if kind not in ("players", "character_focus", "controlled_character"):
        raise ValueError("Supported live queries: players, character_focus, controlled_character")
    if kind == "character_focus" and (type(request.get("id")) is not int or
                                       not 0 <= request["id"] < 0xffffffff):
        raise ValueError("character_focus requires a valid unsigned character ID")
    if kind == "controlled_character" and (type(request.get("machineid", 1)) is not int or
                                           not 0 <= request.get("machineid", 1) <= 63):
        raise ValueError("controlled_character requires a valid player machine ID")
    result = script.exports_sync.inspect(request)
    return {"request_id": request_id, "kind": kind, "source": "live_host_memory",
            "result": result}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, help="Target CK3 process ID; autodetected when only one is running")
    parser.add_argument("--request-file", type=Path, help="One JSON request; defaults to querying players")
    parser.add_argument("--stdin", action="store_true", help="Answer one JSON request per input line")
    args = parser.parse_args()
    session = None
    try:
        session, script = _open_probe(args.pid)
        if args.stdin:
            for line in sys.stdin:
                if not line.strip():
                    continue
                try:
                    print(_json(_answer(script, json.loads(line))), flush=True)
                except (ValueError, TypeError, frida.RPCException) as error:
                    print(_json({"event": "live_query_error", "message": str(error)}), flush=True)
        else:
            request = ({"kind": "players"} if args.request_file is None else
                       json.loads(args.request_file.read_text(encoding="utf-8-sig")))
            print(_json(_answer(script, request)))
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, frida.ProcessNotFoundError, frida.RPCException) as error:
        print(_json({"event": "live_query_error", "message": str(error)}))
        return 1
    finally:
        if session is not None:
            try:
                session.detach()
            except frida.InvalidOperationError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
