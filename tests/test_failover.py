"""Tests for cross-provider failover."""

from unittest.mock import MagicMock

import kodiqa
from kodiqa import Kodiqa


class TestFailoverCandidates:
    def test_auto_lists_providers_with_keys(self):
        k = MagicMock()
        k.settings = {}
        k.claude_key = "x"
        k.api_keys = {"openai": "", "deepseek": "y", "groq": "", "mistral": "", "qwen": "z"}
        out = Kodiqa._failover_candidates(k)
        pairs = [(kind, prov) for (kind, prov, _m) in out]
        assert ("claude", None) in pairs
        assert ("openai", "deepseek") in pairs
        assert ("openai", "qwen") in pairs
        assert ("openai", "openai") not in pairs  # no key → excluded

    def test_explicit_chain_resolves_in_order(self):
        k = MagicMock()
        k.settings = {"failover_chain": ["deepseek", "claude"]}
        k.claude_key = "x"
        k.api_keys = {"deepseek": "y"}
        k._resolve_model_name = lambda a: {"deepseek": "deepseek-chat", "claude": "claude-sonnet-4-6"}[a]
        k._get_provider_for_model = lambda m: "deepseek" if m == "deepseek-chat" else None
        out = Kodiqa._failover_candidates(k)
        assert out[0] == ("openai", "deepseek", "deepseek-chat")
        assert out[1] == ("claude", None, "claude-sonnet-4-6")


class TestStreamFailover:
    def _agent(self):
        k = MagicMock()
        k.failover_enabled = True
        k.model = "claude-sonnet-4-6"
        k._stream_interrupted = False
        k._build_claude_messages.return_value = []
        k._build_openai_messages.return_value = []
        k._provider_label = Kodiqa._provider_label.__get__(k)
        return k

    def test_fails_over_to_next_provider(self):
        k = self._agent()
        k._failover_candidates.return_value = [
            ("claude", None, "claude-sonnet-4-6"),
            ("openai", "deepseek", "deepseek-chat"),
        ]
        k._call_claude_stream.return_value = None                       # primary down
        k._call_openai_compat_stream.return_value = {"text": "hi", "tool_calls": []}
        resp, kind, prov = Kodiqa._stream_native_with_failover(k, "claude", None, "SYS")
        assert resp == {"text": "hi", "tool_calls": []}
        assert kind == "openai" and prov == "deepseek"
        assert k.model == "deepseek-chat"  # active model switched to the one that worked

    def test_disabled_does_not_fail_over(self):
        k = self._agent()
        k.failover_enabled = False
        k._call_claude_stream.return_value = None
        resp, _, _ = Kodiqa._stream_native_with_failover(k, "claude", None, "SYS")
        assert resp is None
        k._call_openai_compat_stream.assert_not_called()

    def test_interrupt_never_fails_over(self):
        k = self._agent()
        k._failover_candidates.return_value = [("openai", "deepseek", "deepseek-chat")]
        k._call_claude_stream.return_value = None
        k._stream_interrupted = True   # user aborted
        resp, _, _ = Kodiqa._stream_native_with_failover(k, "claude", None, "SYS")
        assert resp is None
        k._call_openai_compat_stream.assert_not_called()

    def test_success_on_primary_no_failover(self):
        k = self._agent()
        k._failover_candidates.return_value = [("openai", "deepseek", "deepseek-chat")]
        k._call_claude_stream.return_value = {"text": "ok", "tool_calls": []}
        resp, kind, prov = Kodiqa._stream_native_with_failover(k, "claude", None, "SYS")
        assert kind == "claude" and prov is None
        k._call_openai_compat_stream.assert_not_called()

    def test_client_400_does_not_fail_over(self):
        """A malformed-request 400 (e.g. orphan tool message) would fail identically
        on every provider — it must NOT cascade to the next provider (which is how
        a valid-but-paid DeepSeek setup used to silently fall through to Qwen)."""
        k = self._agent()
        k.model = "deepseek-chat"
        k._failover_candidates.return_value = [("openai", "qwen", "qwen3-max")]

        def deepseek_400(messages, provider):
            k._stream_no_failover = True   # what _call_openai_compat_stream does on a 400
            return None
        k._call_openai_compat_stream.side_effect = deepseek_400
        resp, kind, prov = Kodiqa._stream_native_with_failover(k, "openai", "deepseek", "SYS")
        assert resp is None
        assert kind == "openai" and prov == "deepseek"   # stayed put, never tried qwen
        assert k._call_openai_compat_stream.call_count == 1

    def test_no_failover_flag_reset_each_run(self):
        """A stale _stream_no_failover from a prior turn must not suppress a
        legitimate failover on the next turn."""
        k = self._agent()
        k._stream_no_failover = True   # left over from a previous request
        k.model = "claude-sonnet-4-6"
        k._failover_candidates.return_value = [("openai", "deepseek", "deepseek-chat")]
        k._call_claude_stream.return_value = None
        k._call_openai_compat_stream.return_value = {"text": "hi", "tool_calls": []}
        resp, kind, prov = Kodiqa._stream_native_with_failover(k, "claude", None, "SYS")
        assert resp == {"text": "hi", "tool_calls": []}
        assert kind == "openai" and prov == "deepseek"


class TestFailoverCommand:
    def _agent(self, monkeypatch):
        monkeypatch.setattr(kodiqa, "save_settings", lambda s: None)
        k = MagicMock()
        k.settings = {}
        return k

    def test_toggle_off_on(self, monkeypatch):
        k = self._agent(monkeypatch)
        Kodiqa._cmd_failover(k, "off")
        assert k.failover_enabled is False and k.settings["failover"] is False
        Kodiqa._cmd_failover(k, "on")
        assert k.failover_enabled is True and k.settings["failover"] is True

    def test_set_explicit_chain(self, monkeypatch):
        k = self._agent(monkeypatch)
        Kodiqa._cmd_failover(k, "deepseek claude")
        assert k.settings["failover_chain"] == ["deepseek", "claude"]

    def test_auto_clears_chain(self, monkeypatch):
        k = self._agent(monkeypatch)
        k.settings["failover_chain"] = ["x"]
        Kodiqa._cmd_failover(k, "auto")
        assert "failover_chain" not in k.settings

    def test_registered_command(self):
        assert "/failover" in Kodiqa._COMMAND_HANDLERS


class TestStreamInterruptedInit:
    """Regression: a request that fails BEFORE _start_stream_interrupt runs (e.g. a
    404/401) used to hit `if self._stream_interrupted` in failover and AttributeError,
    crashing the whole REPL. __init__ must initialize the attribute."""

    def test_fresh_instance_has_stream_interrupted(self, monkeypatch):
        # Avoid touching the user's real Ollama/network during construction.
        monkeypatch.setattr(kodiqa, "save_default_config", lambda: None)
        k = Kodiqa()
        assert k._stream_interrupted is False

    def test_early_failure_does_not_raise(self):
        """Primary stream returns None before any interrupt monitor ran; with the
        attribute defaulted, failover resolves cleanly instead of AttributeError."""
        k = MagicMock()
        k.failover_enabled = False
        k.model = "qwen3-coder"
        k._stream_interrupted = False  # the default __init__ now guarantees
        k._build_openai_messages.return_value = []
        k._call_openai_compat_stream.return_value = None  # 404 → None, no monitor started
        resp, kind, prov = Kodiqa._stream_native_with_failover(k, "openai", "qwen", "SYS")
        assert resp is None and kind == "openai" and prov == "qwen"


class TestOpenAIMessageInvariant:
    """_build_openai_messages must produce a structurally valid OpenAI history —
    every `tool` message responds to a preceding assistant `tool_calls`, every
    declared tool_call gets a response — even when self.history is mixed-format or
    has dangling entries (the cause of the DeepSeek 400 'Messages with role tool
    must be a response to a preceding message with tool_calls')."""

    def _build(self, history):
        k = MagicMock()
        k.history = history
        return Kodiqa._build_openai_messages(k, "SYS")

    def _assert_valid(self, messages):
        """Every tool message must directly follow an assistant whose tool_calls
        include its id (allowing several tool messages back-to-back)."""
        open_ids = set()
        for m in messages:
            if m["role"] == "assistant":
                open_ids = {tc["id"] for tc in m.get("tool_calls", [])}
            elif m["role"] == "tool":
                assert m["tool_call_id"] in open_ids, f"orphan tool msg: {m}"
                open_ids.discard(m["tool_call_id"])
            else:
                open_ids = set()

    def test_orphan_tool_message_dropped(self):
        # a `tool` entry with no preceding assistant tool_calls (interrupt/compaction)
        history = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "call_0", "content": "stale"},
            {"role": "user", "content": "continue"},
        ]
        msgs = self._build(history)
        assert not any(m["role"] == "tool" for m in msgs)
        self._assert_valid(msgs)

    def test_valid_openai_pair_preserved(self):
        history = [
            {"role": "user", "content": "read x"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call_0", "type": "function",
                             "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_0", "content": "file body"},
            {"role": "assistant", "content": "done"},
        ]
        msgs = self._build(history)
        self._assert_valid(msgs)
        assert any(m["role"] == "tool" and m["content"] == "file body" for m in msgs)

    def test_claude_format_tool_use_converted(self):
        # A turn stored in Claude format (e.g. ran on Claude via failover) must be
        # converted to OpenAI tool_calls + tool messages, not silently dropped.
        history = [
            {"role": "user", "content": "read x"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "reading"},
                {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "x"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "file body"},
            ]},
            {"role": "user", "content": "continue"},
        ]
        msgs = self._build(history)
        self._assert_valid(msgs)
        asst = [m for m in msgs if m["role"] == "assistant"][0]
        assert asst["tool_calls"][0]["id"] == "toolu_1"
        assert any(m["role"] == "tool" and m["content"] == "file body" for m in msgs)

    def test_unanswered_tool_call_backfilled(self):
        # assistant declared a tool_call but no tool result followed (rejected edit,
        # interrupt) — must get a stub so the API never sees an unanswered call.
        history = [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call_0", "type": "function",
                             "function": {"name": "edit_file", "arguments": "{}"}}]},
            {"role": "user", "content": "continue"},
        ]
        msgs = self._build(history)
        self._assert_valid(msgs)
        assert any(m["role"] == "tool" and m["tool_call_id"] == "call_0" for m in msgs)
