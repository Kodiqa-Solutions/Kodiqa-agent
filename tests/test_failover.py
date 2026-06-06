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
