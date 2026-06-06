"""Characterization tests for the unified chat-loop seams (STEP 2 refactor).

These lock in the provider-specific formatting that _run_native_chat delegates to,
so the Claude / OpenAI-compatible loops can stay collapsed into one driver without
silently changing the wire formats.
"""

import json
from unittest.mock import MagicMock

from kodiqa import Kodiqa


class TestAssistantMessage:
    def test_claude_text_only(self):
        msg = Kodiqa._assistant_msg(MagicMock(), "claude", "hello", [])
        assert msg == {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}

    def test_claude_with_tool_calls(self):
        tcs = [{"id": "t1", "name": "read_file", "input": {"path": "x"}}]
        msg = Kodiqa._assistant_msg(MagicMock(), "claude", "", tcs)
        assert msg["role"] == "assistant"
        assert msg["content"] == [
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}},
        ]

    def test_openai_text_only(self):
        msg = Kodiqa._assistant_msg(MagicMock(), "openai", "hi", [])
        assert msg == {"role": "assistant", "content": "hi"}

    def test_openai_empty_text_is_none(self):
        msg = Kodiqa._assistant_msg(MagicMock(), "openai", "", [])
        assert msg["content"] is None

    def test_openai_with_tool_calls_serializes_args(self):
        tcs = [{"id": "t1", "name": "grep", "input": {"q": "foo"}}]
        msg = Kodiqa._assistant_msg(MagicMock(), "openai", "", tcs)
        tc = msg["tool_calls"][0]
        assert tc["id"] == "t1"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "grep"
        assert json.loads(tc["function"]["arguments"]) == {"q": "foo"}


class TestAppendToolResults:
    def test_claude_text_result(self):
        k = MagicMock()
        k.history = []
        Kodiqa._append_tool_results(k, "claude", [("t1", "output")])
        assert k.history == [{
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "output"}],
        }]

    def test_claude_image_result(self):
        k = MagicMock()
        k.history = []
        Kodiqa._append_tool_results(k, "claude", [("t1", "__IMAGE__:image/png:BASE64DATA")])
        block = k.history[0]["content"][0]
        assert block["tool_use_id"] == "t1"
        img = block["content"][0]
        assert img["type"] == "image"
        assert img["source"] == {"type": "base64", "media_type": "image/png", "data": "BASE64DATA"}

    def test_openai_results_are_separate_messages(self):
        k = MagicMock()
        k.history = []
        Kodiqa._append_tool_results(k, "openai", [("t1", "a"), ("t2", "b")])
        assert k.history == [
            {"role": "tool", "tool_call_id": "t1", "content": "a"},
            {"role": "tool", "tool_call_id": "t2", "content": "b"},
        ]


class TestBuildSystemPrompt:
    def _agent(self):
        k = MagicMock()
        k.cwd = "/proj"
        k.model = "m"
        k._persona = None
        k.memory.get_context.return_value = ""
        k._load_context_file.return_value = ""
        k._git_context.return_value = ""
        k._shell_env_context.return_value = ""
        k._build_pinned_context.return_value = ""
        return k

    def test_base_only(self):
        k = self._agent()
        out = Kodiqa._build_system_prompt(k, "BASE cwd={cwd} model={model} mem={memories}")
        assert out == "BASE cwd=/proj model=m mem="

    def test_appends_extras_and_prepends_persona(self):
        k = self._agent()
        from config import PERSONAS
        persona = next(iter(PERSONAS))
        k._persona = persona
        k._git_context.return_value = "GIT"
        k._build_pinned_context.return_value = "PIN"
        out = Kodiqa._build_system_prompt(k, "BASE")
        assert out.startswith(PERSONAS[persona]["prompt"])
        assert "GIT" in out and "PIN" in out


class TestMaybeLintFix:
    def test_no_errors_returns_false(self):
        k = MagicMock()
        assert Kodiqa._maybe_lint_fix(k, "") is False

    def test_disabled_returns_false(self):
        k = MagicMock()
        k.lint_auto_fix = False
        assert Kodiqa._maybe_lint_fix(k, "errors") is False

    def test_queues_fix_and_continues(self):
        k = MagicMock()
        k.lint_auto_fix = True
        k._lint_fix_count = 0
        k.history = []
        assert Kodiqa._maybe_lint_fix(k, "ruff: E501") is True
        assert k._lint_fix_count == 1
        assert any("Fix these lint errors" in m["content"] for m in k.history)

    def test_caps_at_three(self):
        k = MagicMock()
        k.lint_auto_fix = True
        k._lint_fix_count = 3  # already at the cap
        k.history = []
        assert Kodiqa._maybe_lint_fix(k, "errors") is False
        assert k._lint_fix_count == 0  # reset


class TestNativeLoopDelegation:
    def test_chat_claude_delegates(self):
        k = MagicMock()
        Kodiqa._chat_claude(k, "hi")
        k._run_native_chat.assert_called_once_with("hi", "claude")

    def test_chat_openai_delegates(self):
        k = MagicMock()
        Kodiqa._chat_openai_compat(k, "hi", "deepseek")
        k._run_native_chat.assert_called_once_with("hi", "openai", "deepseek")
