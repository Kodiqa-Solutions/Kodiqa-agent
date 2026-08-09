"""Native Ollama tool calling: capability detection, /tune tools, message shapes,
streaming tool-call collection, and the native turn loop.

The classic text-[ACTION] path must stay byte-identical when the feature is off —
several tests below are regression guards for exactly that.
"""

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

from rich.console import Console

import kodiqa
from kodiqa import Kodiqa, _parse_ollama_tool_call
from ollama_manager import OllamaManager


def _console():
    return Console(file=io.StringIO(), force_terminal=False, width=100)


class TestParseOllamaToolCall:
    def test_object_arguments(self):
        out = _parse_ollama_tool_call({"function": {"name": "read_file", "arguments": {"path": "a.py"}}}, 0)
        assert out == {"id": "call_0", "name": "read_file", "input": {"path": "a.py"}}

    def test_string_arguments_are_parsed(self):
        """Some models emit OpenAI-style stringified arguments."""
        out = _parse_ollama_tool_call({"function": {"name": "grep", "arguments": '{"pattern": "x"}'}}, 2)
        assert out["input"] == {"pattern": "x"}
        assert out["id"] == "call_2"

    def test_unparsable_string_arguments_become_empty(self):
        out = _parse_ollama_tool_call({"function": {"name": "grep", "arguments": '{"pattern": '}}, 0)
        assert out["input"] == {}

    def test_explicit_id_wins_over_synthesized(self):
        out = _parse_ollama_tool_call({"id": "abc", "function": {"name": "tree", "arguments": {}}}, 5)
        assert out["id"] == "abc"

    def test_rejects_unusable_calls(self):
        assert _parse_ollama_tool_call(None, 0) is None
        assert _parse_ollama_tool_call({}, 0) is None
        assert _parse_ollama_tool_call({"function": "nope"}, 0) is None
        assert _parse_ollama_tool_call({"function": {"arguments": {}}}, 0) is None  # no name
        assert _parse_ollama_tool_call({"function": {"name": "", "arguments": {}}}, 0) is None

    def test_non_dict_arguments_become_empty(self):
        out = _parse_ollama_tool_call({"function": {"name": "tree", "arguments": ["x"]}}, 0)
        assert out["input"] == {}


class TestModelCapabilities:
    def _mgr(self):
        return OllamaManager(MagicMock())

    def test_reads_and_caches_capabilities(self, monkeypatch):
        mgr = self._mgr()
        calls = []

        def fake_post(url, json=None, timeout=None):
            calls.append(json["model"])
            return MagicMock(raise_for_status=lambda: None,
                             json=lambda: {"capabilities": ["completion", "tools"]})

        monkeypatch.setattr("ollama_manager.requests.post", fake_post)
        assert mgr.model_capabilities("qwen3-coder") == ["completion", "tools"]
        assert mgr.model_capabilities("qwen3-coder") == ["completion", "tools"]
        assert calls == ["qwen3-coder"]  # second call served from cache

    def test_supports_tools(self, monkeypatch):
        mgr = self._mgr()
        monkeypatch.setattr("ollama_manager.requests.post", lambda *a, **k: MagicMock(
            raise_for_status=lambda: None, json=lambda: {"capabilities": ["completion", "tools"]}))
        assert mgr.supports_tools("m") is True

    def test_text_only_model(self, monkeypatch):
        mgr = self._mgr()
        monkeypatch.setattr("ollama_manager.requests.post", lambda *a, **k: MagicMock(
            raise_for_status=lambda: None, json=lambda: {"capabilities": ["completion"]}))
        assert mgr.model_capabilities("m") == ["completion"]
        assert mgr.supports_tools("m") is False

    def test_old_ollama_without_capabilities_is_unknown(self, monkeypatch):
        mgr = self._mgr()
        monkeypatch.setattr("ollama_manager.requests.post", lambda *a, **k: MagicMock(
            raise_for_status=lambda: None, json=lambda: {"license": "x"}))
        assert mgr.model_capabilities("m") is None
        assert mgr.supports_tools("m") is False  # unknown never means "supported"

    def test_server_down_is_unknown_and_not_cached(self, monkeypatch):
        mgr = self._mgr()

        def boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr("ollama_manager.requests.post", boom)
        assert mgr.model_capabilities("m") is None
        assert "m" not in mgr._caps_cache  # a later server start must still resolve

    def test_empty_name_short_circuits(self):
        assert self._mgr().model_capabilities("") is None

    def test_invalidate(self, monkeypatch):
        mgr = self._mgr()
        mgr._caps_cache = {"a": ["tools"], "b": ["completion"]}
        mgr.invalidate_capabilities("a")
        assert "a" not in mgr._caps_cache and "b" in mgr._caps_cache
        mgr.invalidate_capabilities()
        assert mgr._caps_cache == {}


class TestToolsMode:
    def _agent(self, mode=None, supports=False):
        k = MagicMock()
        k.settings = {} if mode is None else {"ollama_native_tools": mode}
        k.model = "qwen3-coder"
        k.ollama.supports_tools.return_value = supports
        k._ollama_supports_tools = Kodiqa._ollama_supports_tools.__get__(k)
        k._ollama_tools_mode = Kodiqa._ollama_tools_mode.__get__(k)
        return k

    def test_default_is_off(self):
        assert Kodiqa._ollama_tools_mode(self._agent()) == "off"

    def test_garbage_value_falls_back_to_off(self):
        assert Kodiqa._ollama_tools_mode(self._agent("banana")) == "off"

    def test_off_never_activates(self):
        k = self._agent("off", supports=True)
        assert Kodiqa._ollama_native_tools_active(k) is False

    def test_auto_follows_capability(self):
        assert Kodiqa._ollama_native_tools_active(self._agent("auto", supports=True)) is True
        assert Kodiqa._ollama_native_tools_active(self._agent("auto", supports=False)) is False

    def test_on_forces_native(self):
        assert Kodiqa._ollama_native_tools_active(self._agent("on", supports=False)) is True


class TestLocalModelBadge:
    def _agent(self, caps):
        k = MagicMock()
        k.ollama.model_capabilities.return_value = caps
        return k

    def test_tools_badge(self):
        assert "tools" in Kodiqa._local_model_badge(self._agent(["completion", "tools"]), "m")

    def test_text_only_badge(self):
        assert "text-only" in Kodiqa._local_model_badge(self._agent(["completion"]), "m")

    def test_unknown_stays_silent(self):
        assert Kodiqa._local_model_badge(self._agent(None), "m") == ""


class TestOllamaMessageShapes:
    def test_assistant_msg_keeps_arguments_as_object(self):
        """Ollama rejects a JSON-string `arguments` — it must stay a dict."""
        msg = Kodiqa._assistant_msg(MagicMock(), "ollama", "hi",
                                    [{"id": "call_0", "name": "read_file", "input": {"path": "a.py"}}])
        assert msg["role"] == "assistant" and msg["content"] == "hi"
        assert msg["tool_calls"] == [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]
        assert "id" not in msg["tool_calls"][0]

    def test_assistant_msg_without_tools(self):
        msg = Kodiqa._assistant_msg(MagicMock(), "ollama", "just text", [])
        assert msg == {"role": "assistant", "content": "just text"}

    def test_append_tool_results_labels_with_tool_name(self):
        k = MagicMock()
        k.history = []
        Kodiqa._append_tool_results(k, "ollama", [("call_0", "contents")],
                                    [{"id": "call_0", "name": "read_file", "input": {}}])
        assert k.history == [{"role": "tool", "content": "contents", "tool_name": "read_file"}]

    def test_append_tool_results_without_names(self):
        k = MagicMock()
        k.history = []
        Kodiqa._append_tool_results(k, "ollama", [("call_0", "x")])
        assert k.history == [{"role": "tool", "content": "x"}]

    def test_openai_kind_is_untouched(self):
        """Regression: the existing OpenAI shape must not change."""
        k = MagicMock()
        k.history = []
        Kodiqa._append_tool_results(k, "openai", [("id1", "res")])
        assert k.history == [{"role": "tool", "tool_call_id": "id1", "content": "res"}]


class TestBuildOllamaMessages:
    def _agent(self, history):
        k = MagicMock()
        k.history = history
        return k

    def test_native_preserves_tool_calls_and_tool_results(self):
        history = [
            {"role": "user", "content": "read a.py"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]},
            {"role": "tool", "content": "file body", "tool_name": "read_file"},
        ]
        msgs = Kodiqa._build_ollama_messages(self._agent(history), "SYS", native=True)
        assert msgs[0] == {"role": "system", "content": "SYS"}
        assert msgs[2]["tool_calls"][0]["function"]["arguments"] == {"path": "a.py"}
        assert msgs[3] == {"role": "tool", "content": "file body", "tool_name": "read_file"}

    def test_native_still_flattens_openai_format_turns(self):
        """A turn that ran on a cloud provider (arguments = JSON string) must NOT be
        passed through — Ollama would reject it. It falls back to the text flatten."""
        history = [
            {"role": "assistant", "content": "sure",
             "tool_calls": [{"id": "x", "type": "function",
                             "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]},
            {"role": "tool", "tool_call_id": "x", "content": "body"},
        ]
        msgs = Kodiqa._build_ollama_messages(self._agent(history), "SYS", native=True)
        assert all("tool_calls" not in m for m in msgs)
        assert msgs[-1]["role"] == "user" and "[tool result]" in msgs[-1]["content"]

    def test_classic_mode_flattens_everything(self):
        """Regression: with native off, behavior is exactly as before."""
        history = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]},
            {"role": "tool", "content": "body", "tool_name": "read_file"},
        ]
        msgs = Kodiqa._build_ollama_messages(self._agent(history), "SYS")
        assert all("tool_calls" not in m for m in msgs)
        assert all(m["role"] in ("system", "user", "assistant") for m in msgs)


class TestStreamOllamaTools:
    def _agent(self):
        k = MagicMock()
        k.model = "qwen3-coder"
        k.compact_mode = True
        k.console = _console()
        k._stream_interrupted = False
        k._ollama_options.return_value = {}
        k._output_rate.return_value = 0.0
        k._start_stream_interrupt.return_value = lambda: None
        return k

    def _fake_stream(self, monkeypatch, lines, capture=None):
        def fake_retry(fn, **kwargs):
            if capture is not None:
                fn()  # run the lambda so we can inspect the request body
            return MagicMock(raise_for_status=lambda: None,
                             iter_lines=lambda: iter(lines),
                             close=lambda: None)

        monkeypatch.setattr(kodiqa, "_retry_api_call", fake_retry)
        if capture is not None:
            monkeypatch.setattr(kodiqa.requests, "post",
                                lambda url, **kw: capture.update(kw.get("json") or {}) or MagicMock())

    def test_classic_mode_returns_plain_text(self, monkeypatch):
        """Regression: no tools → the old string return, and no `tools` in the body."""
        body = {}
        self._fake_stream(monkeypatch, [
            b'{"message": {"content": "hel"}}',
            b'{"message": {"content": "lo"}}',
            b'{"done": true}',
        ], capture=body)
        out = Kodiqa._stream_ollama(self._agent(), [{"role": "user", "content": "x"}])
        assert out == "hello"
        assert "tools" not in body

    def test_native_mode_returns_dict_and_sends_tools(self, monkeypatch):
        body = {}
        self._fake_stream(monkeypatch, [
            b'{"message": {"content": "ok"}}',
            b'{"message": {"tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]}}',
            b'{"done": true}',
        ], capture=body)
        tools = [{"type": "function", "function": {"name": "read_file"}}]
        out = Kodiqa._stream_ollama(self._agent(), [{"role": "user", "content": "x"}], tools=tools)
        assert out["text"] == "ok"
        assert out["tool_calls"] == [{"id": "call_0", "name": "read_file", "input": {"path": "a.py"}}]
        assert body["tools"] == tools

    def test_tool_calls_on_the_done_chunk_are_not_dropped(self, monkeypatch):
        """Some Ollama versions attach tool_calls to the final chunk."""
        self._fake_stream(monkeypatch, [
            b'{"done": true, "message": {"tool_calls": [{"function": {"name": "tree", "arguments": {}}}]}}',
        ])
        out = Kodiqa._stream_ollama(self._agent(), [], tools=[{"x": 1}])
        assert [tc["name"] for tc in out["tool_calls"]] == ["tree"]

    def test_multiple_tool_calls_get_distinct_ids(self, monkeypatch):
        self._fake_stream(monkeypatch, [
            b'{"message": {"tool_calls": [{"function": {"name": "a", "arguments": {}}}, '
            b'{"function": {"name": "b", "arguments": {}}}]}}',
            b'{"done": true}',
        ])
        out = Kodiqa._stream_ollama(self._agent(), [], tools=[{"x": 1}])
        assert [tc["id"] for tc in out["tool_calls"]] == ["call_0", "call_1"]

    def test_empty_tools_list_still_returns_dict(self, monkeypatch):
        """tools=[] means "native mode, nothing to offer" — not the classic path."""
        self._fake_stream(monkeypatch, [b'{"message": {"content": "hi"}}', b'{"done": true}'])
        out = Kodiqa._stream_ollama(self._agent(), [], tools=[])
        assert out == {"text": "hi", "tool_calls": []}


class TestNativeTurn:
    def _agent(self, response):
        k = MagicMock()
        k.history = []
        k.console = _console()
        k._stream_interrupted = False
        k._stream_ollama.return_value = response
        k._get_openai_tools.return_value = [{"type": "function"}]
        k._assistant_msg = Kodiqa._assistant_msg.__get__(k)
        k._append_tool_results = Kodiqa._append_tool_results.__get__(k)
        k._run_tool_calls.return_value = ([("call_0", "result")], "", "")
        return k

    def test_tool_call_continues_the_loop(self):
        k = self._agent({"text": "", "tool_calls": [
            {"id": "call_0", "name": "read_file", "input": {"path": "a.py"}}]})
        assert Kodiqa._run_ollama_native_turn(k) is True
        assert k.history[0]["tool_calls"][0]["function"]["name"] == "read_file"
        assert k.history[1] == {"role": "tool", "content": "result", "tool_name": "read_file"}

    def test_plain_text_ends_the_turn(self):
        k = self._agent({"text": "all done", "tool_calls": []})
        assert Kodiqa._run_ollama_native_turn(k) is False
        assert k.history == [{"role": "assistant", "content": "all done"}]

    def test_stream_failure_ends_the_turn(self):
        k = self._agent(None)
        assert Kodiqa._run_ollama_native_turn(k) is False
        assert k.history == []

    def test_empty_response_is_not_stored(self):
        """Mirrors the native loops: a content-less, tool-less turn poisons history."""
        k = self._agent({"text": "", "tool_calls": []})
        assert Kodiqa._run_ollama_native_turn(k) is False
        assert k.history == []

    def test_interrupt_stores_partial_text_only(self):
        k = self._agent({"text": "partial", "tool_calls": [
            {"id": "call_0", "name": "read_file", "input": {}}]})
        k._stream_interrupted = True
        assert Kodiqa._run_ollama_native_turn(k) is False
        assert k.history == [{"role": "assistant", "content": "partial"}]

    def test_review_note_is_appended_as_user_message(self):
        k = self._agent({"text": "", "tool_calls": [
            {"id": "call_0", "name": "write_file", "input": {}}]})
        k._run_tool_calls.return_value = ([("call_0", "ok")], "", "[Edit review] rejected")
        assert Kodiqa._run_ollama_native_turn(k) is True
        assert k.history[-1] == {"role": "user", "content": "[Edit review] rejected"}


class _FakeOllamaHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for Ollama: /api/show reports capabilities, /api/chat streams
    NDJSON with a native tool call. Records the request bodies it received."""

    received = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        type(self).received.append((self.path, body))
        if self.path == "/api/show":
            payload = json.dumps({"capabilities": ["completion", "tools"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        chunks = [
            {"message": {"role": "assistant", "content": "reading it"}},
            {"message": {"role": "assistant", "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]}},
            {"done": True, "done_reason": "stop"},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for c in chunks:
            self.wfile.write(json.dumps(c).encode() + b"\n")
            self.wfile.flush()


class TestOllamaNativeE2E:
    """End-to-end over a real socket: capability probe + a native tool-calling turn."""

    def _serve(self):
        _FakeOllamaHandler.received = []
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllamaHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"

    def test_capabilities_and_tool_turn_over_http(self, monkeypatch):
        srv, url = self._serve()
        try:
            monkeypatch.setattr("ollama_manager.OLLAMA_URL", url)
            monkeypatch.setattr(kodiqa, "OLLAMA_URL", url)

            mgr = OllamaManager(MagicMock())
            assert mgr.supports_tools("qwen3-coder") is True

            k = MagicMock()
            k.model = "qwen3-coder"
            k.compact_mode = True
            k.console = _console()
            k._stream_interrupted = False
            k._ollama_options.return_value = {"num_ctx": 16384}
            k._output_rate.return_value = 0.0
            k._start_stream_interrupt.return_value = lambda: None
            tools = [{"type": "function", "function": {"name": "read_file"}}]
            out = Kodiqa._stream_ollama(k, [{"role": "user", "content": "read a.py"}], tools=tools)

            assert out["text"] == "reading it"
            assert out["tool_calls"] == [
                {"id": "call_0", "name": "read_file", "input": {"path": "a.py"}}]
            chat_body = dict(_FakeOllamaHandler.received)["/api/chat"]
            assert chat_body["tools"] == tools
            assert chat_body["stream"] is True
            assert chat_body["options"] == {"num_ctx": 16384}
        finally:
            srv.shutdown()
            srv.server_close()
