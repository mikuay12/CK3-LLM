"""Serve the offline notification checklist and atomically save preferences."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

from ck3_llm.gameplay.game_notifications import SOURCE_COVERAGE
from ck3_llm.protocol.nakama_native import DEFAULT_GAME_DIR
from ck3_llm.gameplay.notification_catalog import NotificationCatalog
from ck3_llm.gameplay.notification_policy import DEFAULT_POLICY_PATH, NotificationPolicy


def settings_server(catalog, policy, *, port=8766):
    page = Path(__file__).with_name("static") / "notification_settings.html"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def respond(self, status, value, *, html=False):
            data = value if html else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                self.respond(200, page.read_bytes(), html=True)
            elif self.path == "/api/settings":
                self.respond(200, {**catalog.options(), **policy.snapshot(), "coverage": SOURCE_COVERAGE})
            else:
                self.respond(404, {"error": "Unknown settings endpoint"})

        def do_PUT(self):
            if self.path != "/api/policy":
                self.respond(404, {"error": "Unknown settings endpoint"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1048576:
                    raise ValueError("Preference request size is invalid")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict) or set(request) != {"policy", "revision"}:
                    raise ValueError("Supply policy and revision")
                if not isinstance(request["revision"], str):
                    raise ValueError("revision must be a string")
                saved = policy.save(request["policy"], revision=request["revision"])
                self.respond(200, saved)
            except FileExistsError as error:
                self.respond(409, {"error": str(error)})
            except (ValueError, TypeError) as error:
                self.respond(400, {"error": str(error)})
            except OSError as error:
                self.respond(500, {"error": str(error)})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, default=DEFAULT_GAME_DIR)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    catalog = NotificationCatalog(args.game_dir / "binaries" / "ck3.exe")
    with settings_server(catalog, NotificationPolicy(args.policy), port=args.port) as server:
        print(f"Notification checklist http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
