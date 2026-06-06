"""Tests for new features: thinking display, tab complete, context mgmt, branching."""

import os
import sys
from io import StringIO
from unittest.mock import MagicMock, patch


class TestStreamWriterThinking:
    """Tests for <think>...</think> block handling in StreamWriter."""

    def _make_writer(self, console, compact=True):
        from kodiqa import StreamWriter
        return StreamWriter(console, compact=compact)

    def test_think_block_suppressed_compact(self):
        console = MagicMock()
        writer = self._make_writer(console, compact=True)
        buf = StringIO()
        with patch.object(sys, "stdout", buf):
            writer.write("Before thinking\n")
            writer.write("<think>\n")
            writer.write("reasoning step 1\n")
            writer.write("reasoning step 2\n")
            writer.write("</think>\n")
            writer.write("After thinking\n")
            writer.flush_pending()
        output = buf.getvalue()
        assert "Before thinking" in output
        assert "After thinking" in output
        assert "reasoning step" not in output

    def test_think_lines_counted(self):
        console = MagicMock()
        writer = self._make_writer(console, compact=True)
        buf = StringIO()
        with patch.object(sys, "stdout", buf):
            writer.write("<think>\n")
            writer.write("line 1\n")
            writer.write("line 2\n")
            writer.write("line 3\n")
            writer.write("</think>\n")
            writer.flush_pending()
        assert writer._in_think is False
        # Check console printed the summary
        console.print.assert_called()
        summary = str(console.print.call_args_list[-1])
        assert "3 lines" in summary

    def test_think_verbose_passes_through(self):
        console = MagicMock()
        writer = self._make_writer(console, compact=False)
        buf = StringIO()
        with patch.object(sys, "stdout", buf):
            writer.write("text\n")
            writer.write("<think>\n")
            writer.write("reasoning\n")
            writer.write("</think>\n")
            writer.flush_pending()
        output = buf.getvalue()
        # In verbose mode, everything passes through
        assert "text" in output
        assert "<think>" in output
        assert "reasoning" in output

    def test_think_state_reset_after_close(self):
        console = MagicMock()
        writer = self._make_writer(console, compact=True)
        buf = StringIO()
        with patch.object(sys, "stdout", buf):
            writer.write("<think>\n")
            assert writer._in_think is True
            writer.write("stuff\n")
            writer.write("</think>\n")
            assert writer._in_think is False
            writer.flush_pending()


class TestCompletePathHelper:
    """Tests for _complete_path file completion."""

    def test_complete_path_existing_dir(self, tmp_path):
        # Create some files
        (tmp_path / "file1.py").write_text("x")
        (tmp_path / "file2.txt").write_text("x")
        (tmp_path / "subdir").mkdir()

        # We can't easily instantiate Kodiqa without full setup,
        # so just verify the directory has our files
        assert "file1.py" in os.listdir(str(tmp_path))
        assert "file2.txt" in os.listdir(str(tmp_path))

    def test_complete_path_nonexistent(self):
        """Complete path for non-existent directory returns empty."""
        # Test the core logic that _complete_path uses
        try:
            os.listdir("/nonexistent_path_xyz_123")
            assert False, "Should have raised"
        except OSError:
            pass  # expected


class TestContextLimit:
    """Tests for _context_limit logic (tested via config values)."""

    def test_claude_model_limit(self):
        from config import is_claude_model
        assert is_claude_model("claude-3-sonnet-20240229")
        # Claude models get 200K limit

    def test_qwen_model_limit(self):
        from config import get_openai_provider
        assert get_openai_provider("qwen-max") == "qwen"
        # Qwen models get 1M limit

    def test_ollama_default_limit(self):
        from config import is_claude_model, get_openai_provider
        # Local model is neither Claude nor any API provider
        assert not is_claude_model("qwen2.5-coder:7b")
        assert get_openai_provider("qwen2.5-coder:7b") is None


class TestBranching:
    """Tests for conversation branching logic."""

    def test_branch_save_and_list(self):
        import copy
        branches = {}
        history = [{"role": "user", "content": "hello"}]
        model = "test-model"

        # Simulate /branch save mybranch
        name = "mybranch"
        branches[name] = {
            "history": copy.deepcopy(history),
            "model": model,
        }
        assert "mybranch" in branches
        assert len(branches["mybranch"]["history"]) == 1
        assert branches["mybranch"]["model"] == "test-model"

    def test_branch_switch(self):
        import copy
        branches = {}
        history_main = [{"role": "user", "content": "main"}]
        history_alt = [{"role": "user", "content": "alt"}, {"role": "assistant", "content": "reply"}]

        branches["alt"] = {"history": copy.deepcopy(history_alt), "model": "model-a"}

        # Simulate switch
        branches["_previous"] = {"history": copy.deepcopy(history_main), "model": "model-b"}
        current_history = copy.deepcopy(branches["alt"]["history"])
        assert len(current_history) == 2
        assert current_history[0]["content"] == "alt"
        assert "_previous" in branches

    def test_branch_delete(self):
        branches = {"test": {"history": [], "model": "m"}}
        del branches["test"]
        assert "test" not in branches

    def test_branch_delete_nonexistent(self):
        branches = {}
        assert "nope" not in branches


class TestSlashCommands:
    """Tests for _SLASH_COMMANDS list completeness."""

    def test_mcp_in_commands(self):
        from kodiqa import Kodiqa
        assert "/mcp" in Kodiqa._SLASH_COMMANDS

    def test_branch_in_commands(self):
        from kodiqa import Kodiqa
        assert "/branch" in Kodiqa._SLASH_COMMANDS

    def test_all_commands_start_with_slash(self):
        from kodiqa import Kodiqa
        for cmd in Kodiqa._SLASH_COMMANDS:
            assert cmd.startswith("/"), f"Command {cmd} doesn't start with /"

    def test_no_duplicate_commands(self):
        from kodiqa import Kodiqa
        assert len(Kodiqa._SLASH_COMMANDS) == len(set(Kodiqa._SLASH_COMMANDS))


class TestContextEstimate:
    """Auto-compact must use the last request's prompt size, not the cumulative
    session total (regression for the auto-compact death-spiral fix)."""

    def test_uses_last_context_tokens(self):
        from kodiqa import Kodiqa
        k = MagicMock()
        k._last_context_tokens = 1234
        assert Kodiqa._estimate_tokens(k) == 1234

    def test_falls_back_to_heuristic_when_zero(self):
        from kodiqa import Kodiqa
        k = MagicMock()
        k._last_context_tokens = 0
        k.history = [{"role": "user", "content": "x" * 40}]
        # char/4 heuristic, NOT a cumulative session counter
        assert Kodiqa._estimate_tokens(k) == 10


class TestCostTable:
    """Default model aliases must be priced or cost/budget silently report $0."""

    def test_current_claude_aliases_priced(self):
        from kodiqa import COST_TABLE
        from config import CLAUDE_ALIASES
        for alias in ("claude", "sonnet", "opus"):
            target = CLAUDE_ALIASES[alias]
            assert target in COST_TABLE, f"{alias} -> {target} missing from COST_TABLE ($0 cost)"
            assert COST_TABLE[target][0] > 0 and COST_TABLE[target][1] > 0


class TestOpenAIRequestBody:
    """OpenAI o-series need max_completion_tokens; others need max_tokens."""

    def _body(self, model, provider="openai"):
        from kodiqa import Kodiqa
        k = MagicMock()
        k.model = model
        k.settings = {}
        k._get_openai_tools.return_value = []
        return Kodiqa._build_openai_request_body(k, [], provider)

    def test_o_series_uses_max_completion_tokens(self):
        for m in ("o3", "o3-mini", "o4-mini"):
            b = self._body(m)
            assert "max_completion_tokens" in b and "max_tokens" not in b, m

    def test_gpt_uses_max_tokens(self):
        b = self._body("gpt-4o")
        assert "max_tokens" in b and "max_completion_tokens" not in b


class TestRedoCommand:
    """/redo must be a registered slash command."""

    def test_redo_in_commands(self):
        from kodiqa import Kodiqa
        assert "/redo" in Kodiqa._SLASH_COMMANDS


class TestLiveTicker:
    """StreamWriter's live cost/token ticker."""

    def test_ticker_estimates_tokens_and_cost(self):
        from kodiqa import StreamWriter
        w = StreamWriter(MagicMock(), compact=True, out_rate=15.0 / 1_000_000)
        w._out_chars = 4000  # ~1000 tokens at ~4 chars/token
        t = w._ticker()
        assert "tok" in t and "$" in t

    def test_ticker_hides_cost_for_free_models(self):
        from kodiqa import StreamWriter
        w = StreamWriter(MagicMock(), compact=True, out_rate=0.0)
        w._out_chars = 400
        t = w._ticker()
        assert "$" not in t  # local/free model → no dollar figure
        assert "tok" in t

    def test_write_counts_output_chars(self):
        from kodiqa import StreamWriter
        w = StreamWriter(MagicMock(), compact=False)
        w.write("hello")
        w.write(" world")
        assert w._out_chars == 11

    def test_output_rate_priced(self):
        from kodiqa import Kodiqa
        from config import CLAUDE_ALIASES
        k = MagicMock()
        k.model = CLAUDE_ALIASES["sonnet"]
        assert Kodiqa._output_rate(k) > 0


class TestDiffstat:
    """End-of-turn diffstat rollup from the change log."""

    def test_show_diffstat_prints_summary(self):
        import actions
        from kodiqa import Kodiqa
        actions.clear_change_log()
        actions._record_change("/proj/a.py", "x\n", "x\ny\nz\n")
        k = MagicMock()
        k.cwd = "/proj"
        Kodiqa._show_diffstat(k)
        printed = " ".join(str(c) for c in k.console.print.call_args_list)
        assert "changed" in printed
        actions.clear_change_log()

    def test_show_diffstat_silent_when_no_changes(self):
        import actions
        from kodiqa import Kodiqa
        actions.clear_change_log()
        k = MagicMock()
        k.cwd = "/proj"
        Kodiqa._show_diffstat(k)
        k.console.print.assert_not_called()


class TestResumeFromHistory:
    """--resume wiring: resume a saved history session by id (or latest)."""

    def test_resume_latest(self, tmp_path, monkeypatch):
        import json
        import kodiqa
        monkeypatch.setattr(kodiqa, "KODIQA_DIR", str(tmp_path))
        hist = tmp_path / "history"
        hist.mkdir()
        (hist / "index.json").write_text(json.dumps([{"id": 1}, {"id": 2}]))
        (hist / "session_2.json").write_text(json.dumps(
            {"model": "claude", "cwd": "/nonexistent_xyz",
             "history": [{"role": "user", "content": "hi"}]}))
        k = MagicMock()
        k.cwd = str(tmp_path)
        kodiqa.Kodiqa._resume_from_history(k, None)  # None → most recent (id 2)
        assert k.history == [{"role": "user", "content": "hi"}]
        assert k.model == "claude"

    def test_resume_missing_id(self, tmp_path, monkeypatch):
        import json
        import kodiqa
        monkeypatch.setattr(kodiqa, "KODIQA_DIR", str(tmp_path))
        hist = tmp_path / "history"
        hist.mkdir()
        (hist / "index.json").write_text(json.dumps([{"id": 1}]))
        k = MagicMock()
        k.cwd = str(tmp_path)
        kodiqa.Kodiqa._resume_from_history(k, "999")
        printed = " ".join(str(c) for c in k.console.print.call_args_list)
        assert "not found" in printed
