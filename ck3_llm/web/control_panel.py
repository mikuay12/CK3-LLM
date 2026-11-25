"""Unified local management console for CK3 AI VM: API config, initial prompt, and notifications."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ck3_llm.paths import PROJECT_ROOT
from ck3_llm.model.chat_channel import DEFAULT_CHAT_PATH, append_chat, clear_chat, read_chat
from ck3_llm.gameplay.game_notifications import SOURCE_COVERAGE
from ck3_llm.model.llm_memory import DEFAULT_MEMORY_PATH, LLMMemoryStore
from ck3_llm.model.model_runtime import ModelConfig, SYSTEM_PROMPT
from ck3_llm.protocol.nakama_native import DEFAULT_GAME_DIR
from ck3_llm.gameplay.notification_catalog import NotificationCatalog
from ck3_llm.gameplay.notification_policy import DEFAULT_POLICY_PATH, NotificationPolicy


DEFAULT_MODEL_CONFIG_PATH = PROJECT_ROOT / "model_config.json"
DEFAULT_MODEL_CONFIG = {
    "endpoint": "https://api.openai.com/v1/chat/completions",
    "model": "gpt-4o-mini",
    "api_key_env": "CK3_MODEL_API_KEY",
    "instructions": "控制当前选中的角色，与房主共同游玩。按需查询信息并自行决定具体行动；重大外交或宣战前先评估局势与实力对比。",
    "request_timeout": 45,
    "idle_seconds": 60,
    "max_query_chars": 6000,
    "context_chars": 32000,
    "response_format": "compact_text",
    "modal_gate": "strict",
    "fold_old_tool_results": True,
    "request_options": {"max_completion_tokens": 2048},
}


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_model_config_state(path):
    path = Path(path)
    exists = path.exists()
    error = None
    if exists:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("model_config.json must be a JSON object")
            merged = {**DEFAULT_MODEL_CONFIG, **data}
            ModelConfig(**merged)
            config = merged
        except (OSError, ValueError, TypeError) as exc:
            config = dict(DEFAULT_MODEL_CONFIG)
            error = str(exc)
    else:
        config = dict(DEFAULT_MODEL_CONFIG)

    env_name = config.get("api_key_env")
    api_key_present = bool(os.environ.get(env_name)) if env_name else False

    captures_dir = path.parent / "captures"
    latest_profile = None
    if captures_dir.is_dir():
        profiles = sorted(
            captures_dir.glob("local-game-*/session_profile.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if profiles:
            try:
                latest_profile = str(profiles[0].relative_to(path.parent))
            except ValueError:
                latest_profile = str(profiles[0])

    return {
        "exists": exists,
        "path": str(path),
        "config": config,
        "error": error,
        "api_key_present": api_key_present,
        "system_prompt": SYSTEM_PROMPT,
        "latest_session_profile": latest_profile,
    }


def save_model_config(path, data):
    if not isinstance(data, dict):
        raise ValueError("Model configuration must be a JSON object")
    allowed = set(DEFAULT_MODEL_CONFIG)
    unknown = set(data) - allowed
    if unknown:
        raise ValueError("Unknown model config fields: " + ", ".join(sorted(unknown)))
    cleaned = {**DEFAULT_MODEL_CONFIG, **data}
    if cleaned.get("api_key_env") == "":
        cleaned["api_key_env"] = None
    # Validate strictly via ModelConfig
    ModelConfig(**cleaned)
    _atomic_write_json(path, cleaned)
    return load_model_config_state(path)


def test_model_endpoint(payload):
    if not isinstance(payload, dict):
        raise ValueError("Test request must be a JSON object")
    config_data = payload.get("config")
    if not isinstance(config_data, dict):
        raise ValueError("Missing config object")
    cleaned = {**DEFAULT_MODEL_CONFIG, **config_data}
    if cleaned.get("api_key_env") == "":
        cleaned["api_key_env"] = None
    cfg = ModelConfig(**cleaned)
    override_key = payload.get("api_key")
    api_key = override_key if (isinstance(override_key, str) and override_key.strip()) else (
        os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
    )
    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": "Reply with OK only."},
            {"role": "user", "content": "ping"},
        ],
        "max_tokens": 16,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key.strip()
    req = Request(cfg.endpoint, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    started = time.monotonic()
    try:
        with urlopen(req, timeout=min(float(cfg.request_timeout), 20.0)) as resp:
            raw = resp.read(65536)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
            reply_text = (((parsed.get("choices") or [{}])[0]).get("message") or {}).get("content")
            return {"ok": True, "status": resp.status, "elapsed_ms": elapsed_ms, "reply": reply_text}
    except HTTPError as err:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        detail = ""
        try:
            detail = err.read(2048).decode("utf-8", errors="replace")
        except OSError:
            pass
        err.close()
        return {"ok": False, "status": err.code, "elapsed_ms": elapsed_ms, "error": f"HTTP {err.code}", "detail": detail[:400]}
    except (URLError, OSError, TimeoutError, ValueError) as err:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {"ok": False, "status": None, "elapsed_ms": elapsed_ms, "error": str(err)}


def control_panel_server(catalog, policy, model_config_path, *, chat_path=DEFAULT_CHAT_PATH, memory_path=DEFAULT_MEMORY_PATH, port=8766):
    static_dir = Path(__file__).with_name("static")
    assets = {
        "/": ("control_panel.html", "text/html; charset=utf-8"),
        "/assets/control-panel.css": ("control_panel.css", "text/css; charset=utf-8"),
        "/assets/control-panel.js": ("control_panel.js", "text/javascript; charset=utf-8"),
    }
    memory_store = LLMMemoryStore(memory_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def respond(self, status, value, *, content_type="application/json; charset=utf-8"):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self, limit=1048576):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= limit:
                raise ValueError("Request size is invalid")
            return json.loads(self.rfile.read(length))

        def do_GET(self):
            if self.path in assets:
                name, content_type = assets[self.path]
                self.respond(200, (static_dir / name).read_bytes(), content_type=content_type)
            elif self.path == "/api/settings":
                self.respond(200, {**catalog.options(), **policy.snapshot(), "coverage": SOURCE_COVERAGE})
            elif self.path == "/api/model-config":
                self.respond(200, load_model_config_state(model_config_path))
            elif self.path == "/api/chat" or self.path.startswith("/api/chat?"):
                from urllib.parse import parse_qs, urlsplit
                qs = parse_qs(urlsplit(self.path).query)
                after = (qs.get("after") or [None])[0]
                self.respond(200, {"messages": read_chat(chat_path, after_id=after), "chat_path": str(chat_path)})
            elif self.path == "/api/memory":
                self.respond(200, memory_store.snapshot())
            else:
                self.respond(404, {"error": "Unknown endpoint"})

        def do_PUT(self):
            if self.path == "/api/policy":
                try:
                    request = self._read_json()
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
                return
            if self.path == "/api/model-config":
                try:
                    request = self._read_json()
                    if not isinstance(request, dict) or "config" not in request:
                        raise ValueError("Supply config object")
                    saved = save_model_config(model_config_path, request["config"])
                    self.respond(200, saved)
                except (ValueError, TypeError) as error:
                    self.respond(400, {"error": str(error)})
                except OSError as error:
                    self.respond(500, {"error": str(error)})
                return
            if self.path == "/api/memory":
                try:
                    request = self._read_json()
                    if not isinstance(request, dict) or not request.get("id"):
                        raise ValueError("Supply memory id")
                    memory_store.update(
                        request["id"],
                        scope=request.get("scope"),
                        category=request.get("category"),
                        content=request.get("content"),
                        enabled=request.get("enabled"),
                    )
                    self.respond(200, memory_store.snapshot())
                except (ValueError, TypeError) as error:
                    self.respond(400, {"error": str(error)})
                except OSError as error:
                    self.respond(500, {"error": str(error)})
                return
            self.respond(404, {"error": "Unknown endpoint"})

        def do_POST(self):
            if self.path == "/api/model-test":
                try:
                    request = self._read_json()
                    result = test_model_endpoint(request)
                    self.respond(200, result)
                except (ValueError, TypeError) as error:
                    self.respond(400, {"error": str(error)})
                except OSError as error:
                    self.respond(500, {"error": str(error)})
                return
            if self.path == "/api/chat":
                try:
                    request = self._read_json()
                    if not isinstance(request, dict) or not isinstance(request.get("text"), str):
                        raise ValueError("Supply text message")
                    entry = append_chat(chat_path, role="player", text=request["text"])
                    self.respond(200, {"ok": True, "message": entry})
                except (ValueError, TypeError) as error:
                    self.respond(400, {"error": str(error)})
                except OSError as error:
                    self.respond(500, {"error": str(error)})
                return
            if self.path == "/api/memory":
                try:
                    request = self._read_json()
                    if not isinstance(request, dict):
                        raise ValueError("Supply memory object")
                    memory_store.add(
                        scope=request.get("scope", "campaign"),
                        category=request.get("category", "general"),
                        content=request.get("content", ""),
                        source="player",
                        enabled=request.get("enabled", True),
                    )
                    self.respond(200, memory_store.snapshot())
                except (ValueError, TypeError) as error:
                    self.respond(400, {"error": str(error)})
                except OSError as error:
                    self.respond(500, {"error": str(error)})
                return
            self.respond(404, {"error": "Unknown endpoint"})

        def do_DELETE(self):
            if self.path == "/api/chat":
                try:
                    clear_chat(chat_path)
                    self.respond(200, {"ok": True})
                except OSError as error:
                    self.respond(500, {"error": str(error)})
                return
            if self.path == "/api/memory" or self.path.startswith("/api/memory?"):
                from urllib.parse import parse_qs, urlsplit
                try:
                    qs = parse_qs(urlsplit(self.path).query)
                    item_id = (qs.get("id") or [None])[0]
                    scope = (qs.get("scope") or [None])[0]
                    if item_id:
                        memory_store.delete(item_id)
                    elif scope == "all" or scope is None:
                        memory_store.clear(scope=None)
                    else:
                        memory_store.clear(scope=scope)
                    self.respond(200, memory_store.snapshot())
                except (ValueError, TypeError) as error:
                    self.respond(400, {"error": str(error)})
                except OSError as error:
                    self.respond(500, {"error": str(error)})
                return
            self.respond(404, {"error": "Unknown endpoint"})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, default=DEFAULT_GAME_DIR)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--model-config", type=Path, default=DEFAULT_MODEL_CONFIG_PATH)
    parser.add_argument("--chat-file", type=Path, default=DEFAULT_CHAT_PATH)
    parser.add_argument("--memory-file", type=Path, default=DEFAULT_MEMORY_PATH)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    catalog = NotificationCatalog(args.game_dir / "binaries" / "ck3.exe")
    policy = NotificationPolicy(args.policy)
    with control_panel_server(
        catalog, policy, args.model_config, chat_path=args.chat_file, memory_path=args.memory_file, port=args.port
    ) as server:
        print(f"CK3 AI Management Console http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
