"""Regression tests for three fixes:

1. do_edit_file / do_edit_file_all with an empty old_string (file corruption).
2. A mid-stream network drop must return None so failover/retry engages, instead of
   escaping the stream loop and killing the turn.
3. The user's own model is retried once before any cross-provider failover.

Plus the Ollama `thinking` field, which was silently dropped.
"""

import io
from unittest.mock import MagicMock

import requests
from rich.console import Console

import actions
import kodiqa
from actions import do_edit_file, do_edit_file_all
from kodiqa import Kodiqa


def _console():
    return Console(file=io.StringIO(), force_terminal=False, width=100)


class TestEmptyOldStringGuards:
    """`"" in text` is always True and `text.replace("", x)` inserts x between EVERY
    character — an empty old_string used to destroy the file."""

    def test_replace_all_rejects_empty_old_string(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("abc")
        out = do_edit_file_all(str(f), "", "X")
        assert "cannot be empty" in out
        assert f.read_text() == "abc"  # untouched

    def test_edit_file_rejects_empty_old_string(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("abc")
        out = do_edit_file(str(f), "", "X")
        assert "cannot be empty" in out
        assert f.read_text() == "abc"

    def test_replace_all_rejects_empty_path(self):
        assert "path is required" in do_edit_file_all("", "a", "b")

    def test_replace_all_still_works_normally(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\ny = 1\n")
        actions.set_batch_mode(False)
        out = do_edit_file_all(str(f), "1", "2")
        assert "Replaced 2" in out
        assert f.read_text() == "x = 2\ny = 2\n"

    def test_edit_file_still_works_normally(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("hello world")
        actions.set_batch_mode(False)
        do_edit_file(str(f), "world", "there")
        assert f.read_text() == "hello there"


class _DropOnRead:
    """A response whose stream dies partway through iter_lines()."""

    def __init__(self, lines, exc):
        self._lines, self._exc = lines, exc
        self.closed = False

    def raise_for_status(self):
        pass

    def close(self):
        self.closed = True

    def iter_lines(self):
        yield from self._lines
        raise self._exc


class TestMidStreamDrop:
    def _agent(self):
        k = MagicMock()
        k.model = "qwen3-coder:latest"
        k.compact_mode = True
        k.console = _console()
        k._stream_interrupted = False
        k._ollama_options.return_value = {}
        k._output_rate.return_value = 0.0
        k._start_stream_interrupt.return_value = lambda: None
        k._stream_dropped = Kodiqa._stream_dropped.__get__(k)
        return k

    def _patch(self, monkeypatch, resp):
        monkeypatch.setattr(kodiqa, "_retry_api_call", lambda fn, **kw: resp)

    def test_drop_before_any_output_returns_none(self, monkeypatch):
        resp = _DropOnRead([], requests.exceptions.ChunkedEncodingError("peer reset"))
        self._patch(monkeypatch, resp)
        assert Kodiqa._stream_ollama(self._agent(), []) is None
        assert resp.closed  # connection still released

    def test_drop_after_partial_output_returns_none(self, monkeypatch):
        """The partial answer is discarded rather than passed off as a complete turn."""
        resp = _DropOnRead([b'{"message": {"content": "half an ans"}}'],
                           requests.exceptions.ConnectionError("dropped"))
        self._patch(monkeypatch, resp)
        assert Kodiqa._stream_ollama(self._agent(), []) is None

    def test_drop_in_native_mode_also_returns_none(self, monkeypatch):
        self._patch(monkeypatch, _DropOnRead([], OSError("socket died")))
        assert Kodiqa._stream_ollama(self._agent(), [], tools=[{"x": 1}]) is None

    def test_keyboard_interrupt_is_not_treated_as_a_drop(self, monkeypatch):
        """Ctrl+C must keep its own path: partial text is returned, not discarded."""
        resp = _DropOnRead([b'{"message": {"content": "partial"}}'], KeyboardInterrupt())
        self._patch(monkeypatch, resp)
        k = self._agent()
        out = Kodiqa._stream_ollama(k, [])
        assert out == "partial"
        assert k._stream_interrupted is True

    def test_stream_dropped_reports_partial_vs_clean(self):
        k = MagicMock()
        k.console = _console()
        assert Kodiqa._stream_dropped(k, OSError("x"), "Claude", partial=False) is None
        clean = k.console.file.getvalue()
        assert "before any output" in clean and "discarded" not in clean

        k.console = _console()
        Kodiqa._stream_dropped(k, OSError("x"), "Claude", partial=True)
        assert "after partial output" in k.console.file.getvalue()


class TestSameModelRetry:
    def _agent(self):
        k = MagicMock()
        k.failover_enabled = True
        k.model = "claude-sonnet-4-6"
        k.console = _console()
        k._stream_interrupted = False
        k._stream_no_failover = False
        k._build_claude_messages.return_value = []
        k._build_openai_messages.return_value = []
        k._provider_label = Kodiqa._provider_label.__get__(k)
        k._attempt_stream = Kodiqa._attempt_stream.__get__(k)
        k._heal_history.return_value = ""  # nothing repairable in these scenarios
        k._failover_candidates.return_value = [
            ("claude", None, "claude-sonnet-4-6"),
            ("openai", "deepseek", "deepseek-chat"),
        ]
        return k

    def test_transient_failure_retries_the_same_model_first(self):
        k = self._agent()
        k._call_claude_stream.side_effect = [None, {"text": "recovered", "tool_calls": []}]
        resp, kind, prov = Kodiqa._stream_native_with_failover(k, "claude", None, "SYS")
        assert resp == {"text": "recovered", "tool_calls": []}
        assert kind == "claude" and prov is None
        assert k.model == "claude-sonnet-4-6"  # never moved off the user's choice
        assert k._call_openai_compat_stream.call_count == 0
        assert "Retrying" in k.console.file.getvalue()

    def test_falls_over_only_after_the_retry_also_fails(self):
        k = self._agent()
        k._call_claude_stream.return_value = None
        k._call_openai_compat_stream.return_value = {"text": "hi", "tool_calls": []}
        resp, kind, prov = Kodiqa._stream_native_with_failover(k, "claude", None, "SYS")
        assert k._call_claude_stream.call_count == 2  # original + one retry
        assert (kind, prov) == ("openai", "deepseek")
        assert resp == {"text": "hi", "tool_calls": []}

    def test_retry_happens_even_with_failover_disabled(self):
        """Retrying your own model is not a failover, so /failover off keeps it."""
        k = self._agent()
        k.failover_enabled = False
        k._call_claude_stream.side_effect = [None, {"text": "ok", "tool_calls": []}]
        resp, _, _ = Kodiqa._stream_native_with_failover(k, "claude", None, "SYS")
        assert resp == {"text": "ok", "tool_calls": []}
        assert k._call_openai_compat_stream.call_count == 0

    def test_client_error_skips_the_retry(self):
        """A 400/422 fails identically on a retry — don't waste a call."""
        k = self._agent()

        def fail_client_side(*a, **kw):
            k._stream_no_failover = True
            return None

        k._call_claude_stream.side_effect = fail_client_side
        resp, _, _ = Kodiqa._stream_native_with_failover(k, "claude", None, "SYS")
        assert resp is None
        assert k._call_claude_stream.call_count == 1

    def test_interrupt_skips_the_retry(self):
        k = self._agent()

        def interrupted(*a, **kw):
            k._stream_interrupted = True
            return None

        k._call_claude_stream.side_effect = interrupted
        resp, _, _ = Kodiqa._stream_native_with_failover(k, "claude", None, "SYS")
        assert resp is None
        assert k._call_claude_stream.call_count == 1


class TestHealHistory:
    """A malformed message used to stay in self.history and 400 on EVERY later turn —
    the session was dead until /clear. The builders only sanitize what they SEND."""

    def _agent(self, history):
        k = MagicMock()
        k.history = history
        k._last_context_tokens = 999
        return k

    def test_removes_empty_assistant_turn(self):
        k = self._agent([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": ""},          # poison
            {"role": "user", "content": "still there?"},
        ])
        note = Kodiqa._heal_history(k)
        assert "empty assistant turn" in note
        assert [m["role"] for m in k.history] == ["user", "user"]
        assert k._last_context_tokens == 0  # stale estimate reset

    def test_removes_orphan_openai_tool_message(self):
        k = self._agent([
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "ghost", "content": "result"},  # poison
        ])
        assert "orphan tool result" in Kodiqa._heal_history(k)
        assert [m["role"] for m in k.history] == ["user"]

    def test_removes_orphan_claude_tool_result_block(self):
        k = self._agent([
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "ghost", "content": "x"},
                {"type": "text", "text": "and a question"},
            ]},
        ])
        assert "orphan tool result" in Kodiqa._heal_history(k)
        assert k.history[0]["content"] == [{"type": "text", "text": "and a question"}]

    def test_drops_a_user_message_left_with_nothing(self):
        k = self._agent([
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "ghost", "content": "x"}]},
        ])
        Kodiqa._heal_history(k)
        assert k.history == []

    def test_healthy_history_is_left_alone(self):
        history = [
            {"role": "user", "content": "read a.py"},
            {"role": "assistant", "content": "sure", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "body"},
            {"role": "assistant", "content": "here it is"},
        ]
        k = self._agent(list(history))
        assert Kodiqa._heal_history(k) == ""
        assert k.history == history
        assert k._last_context_tokens == 999  # untouched when nothing changed

    def test_claude_tool_use_pairs_are_kept(self):
        history = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "body"}]},
        ]
        k = self._agent(list(history))
        assert Kodiqa._heal_history(k) == ""
        assert k.history == history

    def test_ollama_native_tool_results_are_kept(self):
        """They are positional and carry no tool_call_id — must not look like orphans."""
        history = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "read_file", "arguments": {}}}]},
            {"role": "tool", "content": "body", "tool_name": "read_file"},
        ]
        k = self._agent(list(history))
        assert Kodiqa._heal_history(k) == ""
        assert k.history == history

    def test_backfills_an_unanswered_tool_call(self):
        """Verified live: DeepSeek 400s with 'insufficient tool messages following
        tool_calls'. Dropping the assistant turn would lose real work — stub the
        missing answer instead."""
        k = self._agent([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "user", "content": "still there?"},
        ])
        assert "unanswered tool call" in Kodiqa._heal_history(k)
        assert [m["role"] for m in k.history] == ["user", "assistant", "tool", "user"]
        assert k.history[2] == {"role": "tool", "tool_call_id": "c1",
                                "content": "[no result returned]"}

    def test_backfills_in_claude_format_for_a_claude_turn(self):
        k = self._agent([
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "read_file"}]},
        ])
        assert "unanswered tool call" in Kodiqa._heal_history(k)
        assert k.history[-1]["role"] == "user"
        assert k.history[-1]["content"][0]["tool_use_id"] == "t1"

    def test_backfills_a_trailing_unanswered_call(self):
        k = self._agent([
            {"role": "assistant", "content": "working",
             "tool_calls": [{"id": "c9", "type": "function",
                             "function": {"name": "grep", "arguments": "{}"}}]},
        ])
        Kodiqa._heal_history(k)
        assert k.history[-1]["tool_call_id"] == "c9"

    def test_answered_tool_call_is_not_backfilled(self):
        history = [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "body"},
            {"role": "user", "content": "thanks"},
        ]
        k = self._agent(list(history))
        assert Kodiqa._heal_history(k) == ""
        assert k.history == history

    def test_context_length_error_is_not_a_shape_problem(self):
        k = self._agent([{"role": "assistant", "content": ""}])
        assert Kodiqa._heal_history(k, "This model's maximum context length is 65536 tokens") == ""
        assert len(k.history) == 1  # nothing deleted — /compact is the answer


class TestHealAndRetryIntegration:
    def _agent(self):
        k = MagicMock()
        k.failover_enabled = False
        k.model = "deepseek-v4-pro"
        k.console = _console()
        k._stream_interrupted = False
        k._stream_no_failover = False
        k._last_client_error = ""
        k._build_openai_messages.return_value = []
        k._provider_label = Kodiqa._provider_label.__get__(k)
        k._attempt_stream = Kodiqa._attempt_stream.__get__(k)
        k._report_unhealable_client_error = Kodiqa._report_unhealable_client_error.__get__(k)
        return k

    def test_400_is_healed_and_retried(self):
        k = self._agent()
        calls = {"n": 0}

        def stream(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                k._stream_no_failover = True
                k._last_client_error = "messages: invalid role sequence"
                return None
            return {"text": "recovered", "tool_calls": []}

        k._call_openai_compat_stream.side_effect = stream
        k._heal_history.return_value = "1 empty assistant turn"
        resp, _, _ = Kodiqa._stream_native_with_failover(k, "openai", "deepseek", "SYS")
        assert resp == {"text": "recovered", "tool_calls": []}
        assert calls["n"] == 2
        assert "Repaired the conversation" in k.console.file.getvalue()

    def test_nothing_to_heal_means_no_extra_call(self):
        k = self._agent()

        def stream(*a, **kw):
            k._stream_no_failover = True
            k._last_client_error = "unsupported parameter"
            return None

        k._call_openai_compat_stream.side_effect = stream
        k._heal_history.return_value = ""
        resp, _, _ = Kodiqa._stream_native_with_failover(k, "openai", "deepseek", "SYS")
        assert resp is None
        assert k._call_openai_compat_stream.call_count == 1
        assert "/clear" in k.console.file.getvalue()

    def test_repair_is_attempted_only_once(self):
        """A heal that doesn't actually fix it must not loop."""
        k = self._agent()

        def stream(*a, **kw):
            k._stream_no_failover = True
            k._last_client_error = "invalid messages"
            return None

        k._call_openai_compat_stream.side_effect = stream
        k._heal_history.return_value = "1 orphan tool result"
        resp, _, _ = Kodiqa._stream_native_with_failover(k, "openai", "deepseek", "SYS")
        assert resp is None
        assert k._call_openai_compat_stream.call_count == 2  # original + one repaired retry
        assert k._heal_history.call_count == 1

    def test_too_long_conversation_points_at_compact(self):
        k = self._agent()

        def stream(*a, **kw):
            k._stream_no_failover = True
            k._last_client_error = "maximum context length exceeded"
            return None

        k._call_openai_compat_stream.side_effect = stream
        k._heal_history.return_value = ""
        Kodiqa._stream_native_with_failover(k, "openai", "deepseek", "SYS")
        assert "/compact" in k.console.file.getvalue()


class TestOllamaThinkingField:
    def _agent(self):
        k = MagicMock()
        k.model = "gpt-oss:latest"
        k.compact_mode = True
        k.console = _console()
        k._stream_interrupted = False
        k._ollama_options.return_value = {}
        k._output_rate.return_value = 0.0
        k._start_stream_interrupt.return_value = lambda: None
        return k

    def test_thinking_is_not_mistaken_for_answer_text(self, monkeypatch):
        """Ollama streams reasoning in a structured `thinking` field — it must be
        counted and reported, never mixed into the assistant's answer."""
        monkeypatch.setattr(kodiqa, "_retry_api_call", lambda fn, **kw: MagicMock(
            raise_for_status=lambda: None, close=lambda: None,
            iter_lines=lambda: iter([
                b'{"message": {"thinking": "let me work this out carefully"}}',
                b'{"message": {"content": "the answer"}}',
                b'{"done": true}',
            ])))
        k = self._agent()
        out = Kodiqa._stream_ollama(k, [])
        assert out == "the answer"
        assert "reasoning:" in k.console.file.getvalue()

    def test_no_reasoning_line_when_model_does_not_think(self, monkeypatch):
        monkeypatch.setattr(kodiqa, "_retry_api_call", lambda fn, **kw: MagicMock(
            raise_for_status=lambda: None, close=lambda: None,
            iter_lines=lambda: iter([b'{"message": {"content": "hi"}}', b'{"done": true}'])))
        k = self._agent()
        assert Kodiqa._stream_ollama(k, []) == "hi"
        assert "reasoning:" not in k.console.file.getvalue()
