"""Launch one real CK3 client against the local room service with a local identity."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).parent / ".deps"))
import frida

from extract_jomini_tokens import SUPPORTED_EXE_SHA256
from local_nakama import LocalNakamaServer
from nakama_native import DEFAULT_GAME_DIR


LOGGED_IN_GROUP = 'visible = "[And( StatusIsLoggedIn, Not( StatusIsUserNameEmpty ) )]"'
LOGGED_OUT_GROUP = 'visible = "[Or( Not( StatusIsLoggedIn ), StatusIsUserNameEmpty )]"'


def _patch_frontend_bytes(original):
    text = original.decode("utf-8-sig")
    if text.count(LOGGED_IN_GROUP) != 1 or text.count(LOGGED_OUT_GROUP) != 1:
        raise ValueError("The frontend layout does not match the verified local-mode patch")
    text = text.replace(LOGGED_IN_GROUP, "visible = yes")
    text = text.replace(LOGGED_OUT_GROUP, "visible = no")
    return text.encode("utf-8")


def _find_original_backup(path, current, captures_root=None):
    candidates = [path.with_name(path.name + ".local_bridge_orig")]
    if captures_root is not None and captures_root.exists():
        found = sorted(captures_root.glob("local-game-*/frontend_main.original.gui"),
                       key=lambda item: item.stat().st_mtime, reverse=True)
        candidates.extend(found)
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            original = candidate.read_bytes()
            patched = _patch_frontend_bytes(original)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if current == patched:
            return original, patched
    return None


def prepare_frontend(path, *, captures_root=None):
    current = path.read_bytes()
    try:
        return current, _patch_frontend_bytes(current)
    except ValueError:
        recovered = _find_original_backup(path, current, captures_root=captures_root)
        if recovered is not None:
            return recovered
        raise


def read_commands(script, service, stopped):
    for line in sys.stdin:
        try:
            command = json.loads(line)
            name = command["command"]
            if name == "stop":
                stopped.set()
                return
            if name == "status":
                result = script.exports_sync.status()
            elif name == "retry_login":
                result = script.exports_sync.retry_login()
            elif name == "inspect_pointer":
                result = script.exports_sync.inspect_pointer(command["rva"], command.get("count", 16))
            elif name == "read_strings":
                result = script.exports_sync.read_strings(command["rva"], command["offsets"])
            elif name == "session_profile":
                result = script.exports_sync.session_profile()
            else:
                raise ValueError("Unknown bridge command")
            service.record("bridge_command_result", command=name, result=result)
        except Exception as error:
            service.record("bridge_command_error", message=str(error))


def attach_frontend(device, args):
    session = device.attach(args.attach_frontend)
    stopped = threading.Event()
    session.on("detached", lambda *args: stopped.set())
    helper = Path(__file__).with_name("local_frontend.js").read_text("utf-8")
    source = """
const base = Process.mainModule.base;
const expectedPort = __PORT__;
const getCommandLine = new NativeFunction(Process.getModuleByName('kernel32.dll').getExportByName('GetCommandLineW'), 'pointer', []);
const commandLine = getCommandLine().readUtf16String();
if (!commandLine.includes('-nakama_host=127.0.0.1') || !commandLine.includes('-nakama_port=' + expectedPort)) {
    throw new Error('The running game must use the local Nakama backend');
}
const connected = new NativeFunction(Process.getModuleByName('nakama-cpp.dll').getExportByName('NRtClient_isConnected'), 'bool', ['pointer']);
function localReady() {
    const wrapper = base.add(0x5cd1e78).readPointer();
    if (wrapper.isNull()) return false;
    const impl = wrapper.readPointer();
    const rt = impl.add(0x20).readPointer();
    return impl.add(0x2e0).readU32() === 4 && !rt.isNull() && !!connected(rt.add(0x10).readPointer());
}
__HELPER__
installLocalFrontendGates(localReady);
""".replace("__PORT__", str(args.port)).replace("__HELPER__", helper)
    script = session.create_script(source)

    def on_message(message, data):
        print(json.dumps(message, ensure_ascii=True), flush=True)

    script.on("message", on_message)
    try:
        script.load()
        stopped.wait()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            session.detach()
        except frida.InvalidOperationError:
            pass
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, default=DEFAULT_GAME_DIR)
    parser.add_argument("--port", type=int, default=7350)
    parser.add_argument("--username", default="LocalPlayer")
    parser.add_argument("--capture-dir", type=Path)
    parser.add_argument("--attach-frontend", type=int, help="Update the frontend gates of an already running local-mode game")
    args = parser.parse_args()
    executable = args.game_dir / "binaries" / "ck3.exe"
    if hashlib.sha256(executable.read_bytes()).hexdigest() != SUPPORTED_EXE_SHA256:
        raise ValueError("Local-mode offsets require the verified CK3 executable")
    device = frida.get_local_device()
    if args.attach_frontend:
        return attach_frontend(device, args)
    if any(process.name.lower() == "ck3.exe" for process in device.enumerate_processes()):
        raise RuntimeError("An existing CK3 process is running. Close it before launching local mode")
    capture = args.capture_dir or Path(__file__).parent / "captures" / (
        "local-game-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    )
    frontend = args.game_dir / "game" / "gui" / "frontend_main.gui"
    frontend_backup = frontend.with_name(frontend.name + ".local_bridge_orig")
    original, patched = prepare_frontend(frontend, captures_root=Path(__file__).parent / "captures")
    stopped = threading.Event()
    session = None
    with LocalNakamaServer("127.0.0.1", args.port, "ck3-local", capture) as service:
        (service.capture_dir / "frontend_main.original.gui").write_bytes(original)
        if not frontend_backup.exists():
            frontend_backup.write_bytes(original)
        manifest = {"game_executable": str(executable.resolve()), "frontend": str(frontend.resolve()),
                    "original_frontend_sha256": hashlib.sha256(original).hexdigest(),
                    "patched_frontend_sha256": hashlib.sha256(patched).hexdigest()}
        (service.capture_dir / "local_mode.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        try:
            frontend.write_bytes(patched)
            game_args = [str(executable), "-nakama_key=ck3-local", "-nakama_host=127.0.0.1",
                         f"-nakama_port={args.port}", "-nakama_ssl=0", "-debug_mode"]
            process_id = device.spawn(game_args, cwd=str(executable.parent))
            session = device.attach(process_id)
            session.on("detached", lambda *args: stopped.set())
            source = Path(__file__).with_name("local_frontend.js").read_text("utf-8")
            source += "\n" + Path(__file__).with_suffix(".js").read_text("utf-8")
            source = source.replace("__LOCAL_CONFIG__", json.dumps({
                "host": "127.0.0.1", "port": args.port, "username": args.username,
                "exe_sha256": SUPPORTED_EXE_SHA256,
            }))
            script = session.create_script(source)

            def on_message(message, data):
                if message["type"] == "send":
                    event = dict(message["payload"])
                    if event.get("event") == "local_session_profile":
                        profile_path = service.capture_dir / "session_profile.json"
                        temporary = profile_path.with_suffix(".tmp")
                        temporary.write_text(json.dumps(event["profile"], indent=2), encoding="utf-8")
                        temporary.replace(profile_path)
                    if data is not None:
                        event["payload"] = data
                    service.record(event.pop("event", "bridge_message"), **event)
                else:
                    service.record("bridge_script_error", details=message)

            script.on("message", on_message)
            script.load()
            device.resume(process_id)
            service.record("local_game_started", process_id=process_id, username=args.username)
            threading.Thread(target=read_commands, args=(script, service, stopped), daemon=True).start()
            while not stopped.wait(1):
                pass
        except KeyboardInterrupt:
            stopped.set()
        finally:
            if session is not None:
                try:
                    session.detach()
                except frida.InvalidOperationError:
                    pass
            if frontend.read_bytes() == patched:
                frontend.write_bytes(original)
                if frontend_backup.exists():
                    try:
                        frontend_backup.unlink()
                    except OSError:
                        pass
                service.record("frontend_restored")
            else:
                service.record("frontend_restore_conflict", backup=str(service.capture_dir / "frontend_main.original.gui"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
