"""Exercise the model loop through local HTTP, memory queues and SDK dispatch."""

from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ck3_client import EventWriter, run_connection
from jomini_codec import JominiCodec
from model_runtime import ChatCompletionModel, ModelConfig, ModelRequestError, ModelRuntime, normalize_reply
from pdx_nakama_codec import encode_payload


def reply(name=None, arguments=None, *, identifier="call-1", content=None):
    result = {"role": "assistant", "content": content}
    if name:
        result["tool_calls"] = [{"id": identifier, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]
    return result


def config(**options):
    return ModelConfig(endpoint="http://127.0.0.1:1/v1/chat/completions", model="offline-test", **options)


CONTEXT = {"room": "room", "host": "host", "machineid": 2, "character_id": 42,
           "connected": True, "active": True, "world": 100, "world_data": 200, "tick": 10}


class ScriptedModel:
    def __init__(self, responses):
        self.responses = queue.SimpleQueue()
        for response in responses:
            self.responses.put(response)
        self.messages = []

    def complete(self, messages):
        self.messages.append(deepcopy(messages))
        response = self.responses.get(timeout=2)
        if isinstance(response, Exception):
            raise response
        return response(messages) if callable(response) else response


@contextmanager
def endpoint(respond):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append({"path": self.path, "headers": dict(self.headers), "payload": payload})
            status, result = respond(payload)
            encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *arguments):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class RuntimeTests(unittest.TestCase):
    def runtime(self, responses, **options):
        model = ScriptedModel(responses)
        runtime = ModelRuntime(config(**options), model)
        self.addCleanup(runtime.close)
        return runtime, model

    def until(self, runtime, condition, context=CONTEXT):
        deadline = time.monotonic() + 2
        events = []
        while time.monotonic() < deadline:
            events.extend(runtime.poll(context))
            if condition():
                return events
            time.sleep(0.002)
        self.fail("Model runtime did not reach the expected state")

    def test_query_action_feedback_and_notification_directed_wait(self):
        runtime, model = self.runtime([
            reply("game_query", {"request": {"command": "observe", "fields": ["military"]}}),
            reply("game_action", {"action": "move_unit", "request_id": "move-1", "parameters": {
                "valid_for": "player", "unit": 5, "target": 2368,
                "unit_order_type": "set", "type": "land", "gathering_army": "reset"}}),
            reply("wait_for_update", {"seconds": 60}),
            reply(content="已收到本角色通知"),
        ])
        self.until(runtime, lambda: runtime.pending is not None)
        command = runtime.next_command(CONTEXT)
        self.assertEqual(command["command"], "observe")
        self.assertEqual(command["max_chars"], 6000)
        self.assertIsNone(runtime.next_command(CONTEXT))
        runtime.feed({"event": "interface_response", "request_id": command["request_id"],
                      "result": {"data": {"military": {"current_strength": 1000}}}}, CONTEXT)
        self.until(runtime, lambda: runtime.pending is not None)
        command = runtime.next_command(CONTEXT)
        self.assertEqual(command["fields"]["target"], 2368)
        self.assertEqual(command["fields"]["unit"], 5)
        runtime.feed({"event": "action_feedback", "request_id": "move-1", "status": "submitted"}, CONTEXT)
        self.until(runtime, lambda: runtime.waiting is not None)
        count = len(model.messages)
        runtime.feed({"event": "game_notification", "character_id": 99, "type": "battle_result"}, CONTEXT)
        runtime.poll(CONTEXT)
        self.assertEqual(len(model.messages), count)
        runtime.feed({"event": "game_notification", "character_id": 42, "type": "battle_result", "id": "1.2"}, CONTEXT)
        self.until(runtime, lambda: len(model.messages) == count + 1)
        latest = json.dumps(model.messages[-1], ensure_ascii=False)
        self.assertIn("battle_result", latest)
        tool_results = [json.loads(message["content"]) for message in model.messages[-1] if message["role"] == "tool"]
        self.assertEqual(tool_results[-1], {"woke_by": "notification"})
        for internal in ('"tick"', '"world"', '"world_data"', '"machineid"'):
            self.assertNotIn(internal, latest)

    def test_wait_without_notification_interrupt_runs_until_timer(self):
        runtime, model = self.runtime([reply("wait_for_update", {"seconds": 60, "wake_on_notification": False}),
                                      reply(content="定时继续")])
        self.until(runtime, lambda: runtime.waiting is not None)
        runtime.feed({"event": "game_notification", "character_id": 42, "type": "battle_started"}, CONTEXT)
        runtime.poll(CONTEXT)
        self.assertEqual(len(model.messages), 1)
        runtime.waiting["until"] = time.monotonic() - 1
        self.until(runtime, lambda: len(model.messages) == 2)
        results = [json.loads(item["content"]) for item in model.messages[-1] if item["role"] == "tool"]
        self.assertEqual(results[-1], {"woke_by": "timer"})

    def test_inflight_model_reply_is_discarded_on_character_world_or_rewind(self):
        for changes in ({"character_id": 99}, {"world_data": 201}, {"tick": 9}, {"connected": False}):
            with self.subTest(changes=changes):
                entered = threading.Event()
                release = threading.Event()

                def slow_model(messages):
                    entered.set()
                    release.wait(timeout=2)
                    return reply("game_action", {"action": "disband_army", "request_id": "old", "parameters": {
                        "valid_for": "player", "army": 5}})

                runtime, model = self.runtime([slow_model, reply("wait_for_update", {"seconds": 60})])
                runtime.poll(CONTEXT)
                self.assertTrue(entered.wait(timeout=1))
                changed = {**CONTEXT, **changes}
                runtime.poll(changed)
                release.set()
                self.until(runtime, lambda: runtime.future is None or runtime.waiting is not None, changed)
                self.assertIsNone(runtime.next_command(changed))
                self.assertFalse(runtime.action_ids)
                runtime.close()

    def test_queued_action_and_late_query_response_are_invalidated(self):
        runtime, _ = self.runtime([reply("game_query", {"request": {"kind": "character", "id": 42}})])
        self.until(runtime, lambda: runtime.pending is not None)
        identifier = runtime.pending["request"]["request_id"]
        changed = {**CONTEXT, "active": False}
        self.assertIsNone(runtime.next_command(changed))
        runtime.feed({"event": "interface_response", "request_id": identifier, "result": {"secret": "old world"}}, changed)
        self.assertFalse(runtime.history)
        self.assertIsNone(runtime.pending)

    def test_invalid_tools_return_errors_without_submitting_commands(self):
        invalid = [reply("game_query", {"request": {"command": "send_raw", "payload_hex": "00"}}),
                   reply("game_action", {"action": "send_raw", "parameters": {}, "request_id": "raw-1"}),
                   reply("wait_for_update", {"seconds": -1}),
                   reply("game_query", {"request": {"kind": "character", "id": 42, "max_chars": 6001}})]
        for response in invalid:
            with self.subTest(response=response):
                runtime, model = self.runtime([response, reply("wait_for_update", {"seconds": 60})])
                self.until(runtime, lambda: runtime.waiting is not None)
                results = [json.loads(item["content"]) for item in model.messages[-1] if item["role"] == "tool"]
                self.assertIn("error", results[-1])
                self.assertIsNone(runtime.next_command(CONTEXT))
                runtime.close()

    def test_multiple_tool_calls_are_returned_together_as_errors(self):
        response = reply("game_query", {"request": {"kind": "players"}})
        response["tool_calls"] += reply("game_query", {"request": {"kind": "armies"}}, identifier="call-2")["tool_calls"]
        runtime, model = self.runtime([response, reply("wait_for_update", {"seconds": 60})])
        self.until(runtime, lambda: runtime.waiting is not None)
        tools = [item for item in model.messages[-1] if item["role"] == "tool"]
        self.assertEqual([item["tool_call_id"] for item in tools], ["call-1", "call-2"])
        self.assertTrue(all("one tool" in item["content"] for item in tools))
        self.assertIsNone(runtime.next_command(CONTEXT))

    def test_context_budget_removes_whole_tool_exchanges(self):
        runtime, _ = self.runtime([], max_query_chars=1000, context_chars=16000)
        runtime.synchronize(CONTEXT)
        for number in range(30):
            message = reply("game_query", {"request": {"kind": "players"}}, identifier=str(number))
            runtime.history.append([message, {"role": "tool", "tool_call_id": str(number), "content": "x" * 1000}])
        runtime.history.append([{"role": "user", "content": "当前通知"}])
        messages, removed = runtime._messages()
        self.assertGreater(removed, 0)
        self.assertLess(len(json.dumps(messages, ensure_ascii=False)), 16000)
        calls = {call["id"] for item in messages for call in item.get("tool_calls", [])}
        results = {item["tool_call_id"] for item in messages if item["role"] == "tool"}
        self.assertEqual(calls, results)
        self.assertIn("29", results)
        self.assertTrue(any("context_trimmed" in (item.get("content") or "") for item in messages))

    def test_notification_backlog_reports_budget_overflow_and_detail_queries(self):
        runtime, _ = self.runtime([])
        runtime.synchronize(CONTEXT)
        for number in range(300):
            runtime.feed({"event": "game_notification", "character_id": 42, "id": str(number),
                          "type": "pending_event", "data": "x" * 4000}, CONTEXT)
        message = runtime._take_updates()
        data = json.loads(message["content"])
        self.assertLessEqual(len(message["content"]), runtime.config.max_query_chars)
        self.assertEqual(data["omitted_count"], 44)
        self.assertEqual(data["pending_count"], len(runtime.updates))
        self.assertGreater(data["pending_count"], 0)
        self.assertTrue(all(item["details_query"]["id"] == item["id"] for item in data["events"]))

    def test_transient_http_failure_backs_off_and_retains_notifications(self):
        runtime, model = self.runtime([ModelRequestError("HTTP 429", retryable=True),
                                      reply("wait_for_update", {"seconds": 60, "wake_on_notification": False})])
        events = self.until(runtime, lambda: runtime.failures == 1)
        self.assertFalse(runtime.disabled)
        self.assertEqual(events[-1]["retry_in_seconds"], 2)
        runtime.feed({"event": "game_notification", "character_id": 42, "type": "war_started"}, CONTEXT)
        runtime.poll(CONTEXT)
        self.assertEqual(len(model.messages), 1)
        runtime.next_call = 0
        self.until(runtime, lambda: runtime.waiting is not None)
        self.assertIn("war_started", json.dumps(model.messages[-1]))

    def test_permanent_model_failure_leaves_command_execution_inactive(self):
        runtime, model = self.runtime([ModelRequestError("HTTP 401")])
        self.until(runtime, lambda: runtime.disabled)
        runtime.poll(CONTEXT)
        self.assertEqual(len(model.messages), 1)
        self.assertIsNone(runtime.next_command(CONTEXT))

    def test_strict_modal_gate_prioritizes_and_requires_resolving_windows(self):
        runtime, model = self.runtime([
            reply("wait_for_update", {"seconds": 60}),
            reply("game_action", {"action": "move_unit", "request_id": "mv-1", "parameters": {
                "valid_for": "player", "unit": 5, "target": 2368,
                "unit_order_type": "set", "type": "land", "gathering_army": "reset"}}),
            reply("game_action", {"action": "reply_character_interaction", "request_id": "rep-1", "parameters": {
                "character_interaction": 9001, "accept": "accept"}}),
            reply("wait_for_update", {"seconds": 60}),
        ], modal_gate="strict", response_format="compact_text")
        runtime.synchronize(CONTEXT)
        # Feed a normal notification first, then a modal interaction; modal must jump ahead.
        runtime.feed({"event": "game_notification", "character_id": 42, "type": "war_started",
                      "notification_id": "military.war_started", "data": {"war_id": 1}}, CONTEXT)
        runtime.feed({"event": "game_notification", "character_id": 42, "type": "pending_interaction",
                      "notification_id": "state.pending_interaction", "summary": "Harold向你发来消息：召集加入战争",
                      "data": {"id": 9001, "interaction": {"key": "call_ally_interaction"}}}, CONTEXT)
        self.assertEqual(runtime.updates[0]["type"], "pending_interaction")
        # Both wait_for_update and non-modal move_unit must be blocked until reply_character_interaction is sent.
        self.until(runtime, lambda: runtime.pending is not None)
        command = runtime.next_command(CONTEXT)
        self.assertEqual(command["type"], "reply_character_interaction")
        self.assertEqual(command["fields"]["character_interaction"], 9001)
        self.assertFalse(runtime.active_modals)
        runtime.feed({"event": "action_feedback", "request_id": "rep-1", "status": "submitted"}, CONTEXT)
        self.until(runtime, lambda: runtime.waiting is not None)
        # Verify the first user prompt carried the sticky modal header and two errors were returned before unlock.
        tool_msgs = [m["content"] for m in model.messages[-1] if m["role"] == "tool"]
        self.assertIn("禁止 wait_for_update", tool_msgs[0])
        self.assertIn("严格弹窗门控", tool_msgs[1])
        user_msgs = [m["content"] for m in model.messages[0] if m["role"] == "user"]
        self.assertIn("必须优先处理的窗口事务", user_msgs[-1])

    def test_old_tool_results_fold_and_player_chat_wakes_and_replies(self):
        from chat_channel import append_chat, read_chat
        with tempfile.TemporaryDirectory() as tmp:
            chat_file = Path(tmp) / "chat.jsonl"
            model = ScriptedModel([
                reply("wait_for_update", {"seconds": 60}),
                reply(content="收到！我马上集结芒斯特军队去帮你。"),
            ])
            rt = ModelRuntime(
                config(response_format="compact_text", fold_old_tool_results=True),
                model,
                chat_path=chat_file,
            )
            self.addCleanup(rt.close)
            self.until(rt, lambda: rt.waiting is not None)
            # Player sends a chat message; ModelRuntime must wake from wait_for_update and write back reply.
            append_chat(chat_file, role="player", text="快来帮我打仗！")
            self.until(rt, lambda: len(model.messages) == 2 and rt.future is None)
            sent_text = json.dumps(model.messages[-1], ensure_ascii=False)
            self.assertIn("快来帮我打仗！", sent_text)
            history = read_chat(chat_file)
            self.assertEqual([m["role"] for m in history], ["player", "assistant"])
            self.assertIn("芒斯特军队", history[-1]["text"])
            # Verify old tool result folding shrinks older tool blocks while keeping latest 2 intact.
            for idx in range(5):
                rt.history.append([reply("game_query", {"request": {"kind": "players"}}, identifier=f"f-{idx}"),
                                   {"role": "tool", "tool_call_id": f"f-{idx}", "content": "y" * 800}])
            msgs, _ = rt._messages()
            tool_contents = [m["content"] for m in msgs if m["role"] == "tool" and m["tool_call_id"].startswith("f-")]
            self.assertTrue(all('"folded":true' in c for c in tool_contents[:3]))
            self.assertTrue(all(len(c) == 800 for c in tool_contents[3:]))

    def test_memory_manage_tool_injects_and_clears_by_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_file = Path(tmp) / "llm_memory.json"
            model = ScriptedModel([
                reply("memory_manage", {"operation": "add", "scope": "campaign", "category": "pact",
                                        "content": "都夫林留给房主拿诺斯文化，绝不宣战。"}),
                reply("memory_manage", {"operation": "add", "scope": "experience", "category": "lesson",
                                        "content": "收到召集参战请求必须立刻回复防止超时。"}),
                reply("wait_for_update", {"seconds": 60}),
            ])
            rt = ModelRuntime(config(response_format="compact_text"), model, memory_path=mem_file)
            self.addCleanup(rt.close)
            self.until(rt, lambda: rt.waiting is not None)
            # Both memories must now be injected into the system prompt on the final turn.
            sys_prompt = model.messages[-1][0]["content"]
            self.assertIn("都夫林留给房主", sys_prompt)
            self.assertIn("收到召集参战请求必须立刻回复", sys_prompt)
            # Clearing campaign scope must keep experience intact.
            rt.memory_store.clear(scope="campaign")
            remaining = rt.memory_store.list_items()
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["scope"], "experience")


class HTTPTests(unittest.TestCase):
    def test_configurable_endpoint_and_authentication_use_tool_call_protocol(self):
        response = reply("game_query", {"request": {"kind": "players"}})
        with endpoint(lambda payload: (200, {"choices": [{"message": response, "finish_reason": "tool_calls"}]})) as (url, received):
            with patch.dict("os.environ", {"CK3_TEST_MODEL_KEY": "test-key"}):
                client = ChatCompletionModel(ModelConfig(endpoint=url, model="local-model", api_key_env="CK3_TEST_MODEL_KEY",
                                                        request_options={"max_completion_tokens": 1024}))
            self.assertEqual(client.complete([{"role": "user", "content": "当前世界"}]), response)
        self.assertEqual(received[0]["path"], "/v1/chat/completions")
        self.assertEqual(received[0]["headers"]["Authorization"], "Bearer test-key")
        payload = received[0]["payload"]
        self.assertEqual(payload["model"], "local-model")
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["max_completion_tokens"], 1024)

    def test_http_status_and_truncated_responses_are_reported(self):
        for status, body, retryable in ((429, {}, True), (401, {}, False),
                                        (200, {"choices": [{"message": reply(), "finish_reason": "length"}]}, False)):
            with self.subTest(status=status, body=body), endpoint(lambda payload: (status, body)) as (url, _):
                with self.assertRaises(ModelRequestError) as raised:
                    ChatCompletionModel(ModelConfig(endpoint=url, model="test")).complete([])
                self.assertEqual(raised.exception.retryable, retryable)

    def test_configuration_and_reply_validation(self):
        for options in ({"idle_seconds": 0}, {"request_timeout": float("nan")},
                        {"max_query_chars": True}, {"context_chars": 100}, {"request_options": {"messages": []}}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                config(**options)
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(ValueError):
            ChatCompletionModel(config(api_key_env="CK3_MISSING_TEST_KEY"))
        message = {**reply(content="好了"), "reasoning_content": "兼容接口推理文本"}
        self.assertEqual(normalize_reply(message), message)
        with self.assertRaises(ValueError):
            normalize_reply({"role": "assistant", "content": [{"text": "invalid"}]})


class SDKModelTests(unittest.TestCase):
    def test_normal_sdk_entry_queries_moves_receives_echo_and_checks_status(self):
        responses = queue.SimpleQueue()
        inference_poll_counts = []
        for message in (
            reply("game_query", {"request": {"command": "observe", "fields": ["military"]}}),
            reply("game_action", {"action": "move_unit", "parameters": {"valid_for": "player", "unit": 5, "target": 2368,
                                                                        "unit_order_type": "set", "type": "land", "gathering_army": "reset"},
                                  "request_id": "model-move-1"}),
            reply("game_query", {"request": {"kind": "action_status", "id": "model-move-1"}}),
            reply("game_query", {"request": {"kind": "unit_movement", "id": 5, "fields": ["path"]}}),
            reply("wait_for_update", {"seconds": 60, "wake_on_notification": False}),
        ):
            responses.put(message)

        class MemoryQueue:
            session = {**CONTEXT, "local_player": {"machineid": 2, "flags": 0x40}}

            def __init__(self):
                self.events = []
                self.path = []

            def set_player(self, machine):
                pass

            def feed(self, command):
                pass

            def poll(self, character=None):
                events, self.events = self.events, []
                return events

            def submit(self, request, character):
                data = {"military": {"current_strength": 1500}}
                if request.get("kind") == "unit_movement":
                    data = {"path": self.path}
                self.events.append({"event": "interface_response", "request_id": request["request_id"],
                                    "result": {"kind": "observation", "character_id": character,
                                               "data": data}})

            def finish(self):
                return []

        class Peer:
            def __init__(self):
                self.events = []
                self.sent = []
                self.polls = 0

            def connect(self):
                self.events.append({"event": "connected"})

            def join(self, room, metadata):
                self.events.append({"event": "room_joined", "match_id": room})

            def poll(self):
                self.polls += 1
                events, self.events = self.events, []
                return events

            def send_envelope(self, room, recipient, messages, magic=0xcafe, presences=None):
                command = JominiCodec().decode_pdx_message(messages[0], from_server=False)
                if command["channel"] == "command":
                    self.sent.append(command)
                    writer.live_observations.path = [command["fields"]["target"]]
                    self.events.append({"event": "match_data", "opcode": 255,
                                        "payload": encode_payload("observer", messages, len(self.sent), "host"),
                                        "presence": {"user_id": "host"}})
                return b"sent"

        def respond(payload):
            if responses.qsize() == 5:
                # HTTP inference can take time while the native network loop continues.
                before = peer.polls
                time.sleep(0.12)
                inference_poll_counts.append(peer.polls - before)
            return 200, {"choices": [{"message": responses.get(timeout=1), "finish_reason": "tool_calls"}]}

        with endpoint(respond) as (url, received), tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with EventWriter(Path(folder) / "capture", quiet=True) as writer:
                writer.live_memory_pid = 123
                writer.live_observations = MemoryQueue()
                writer.session_state.connected = True
                writer.session_state.local_machineid = 2
                writer.observations.set_character(42)
                writer.model_runtime = ModelRuntime(ModelConfig(endpoint=url, model="test"))
                peer = Peer()
                result = run_connection(peer, "room", writer, duration=0.5,
                                        handshake={"command": "send_session_message", "recipient": "host",
                                                   "type": "ClientRequestConnected", "fields": {}})
                log = [json.loads(line) for line in (Path(folder) / "capture/events.jsonl").read_text().splitlines()]
                self.assertIn("model-move-1", writer.actions.records,
                              [event for event in log if event["event"] in ("model_error", "command_error", "model_reply")])
                self.assertEqual(writer.actions.status("model-move-1")["status"], "host_echoed")
            events = [json.loads(line) for line in (Path(folder) / "capture/events.jsonl").read_text().splitlines()]
        self.assertEqual(result["reason"], "duration_elapsed")
        self.assertGreaterEqual(peer.polls, 15)
        self.assertGreaterEqual(inference_poll_counts[0], 3)
        self.assertEqual([command["type"] for command in peer.sent], ["move_unit"])
        self.assertEqual(peer.sent[0]["fields"]["target"], 2368)
        self.assertEqual(len(received), 5)
        last_messages = received[-1]["payload"]["messages"]
        results = [json.loads(item["content"]) for item in last_messages if item["role"] == "tool"]
        self.assertEqual(results[-2]["result"]["data"]["status"], "host_echoed")
        self.assertEqual(results[-1]["result"]["data"]["path"], [2368])
        self.assertFalse(any(event["event"] == "model_error" for event in events))


if __name__ == "__main__":
    unittest.main()
