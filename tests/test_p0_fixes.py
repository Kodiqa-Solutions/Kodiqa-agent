"""Regression tests for the P0 audit fixes:
  P0.1 interactive prompts pause the tool-execution spinner
  P0.2 lazy-MCP meta-tools route via _execute_tool even when batched
  P0.3 batch-edit review outcomes reach the model (not dropped fake ids)
"""
import contextlib
import io
from unittest.mock import MagicMock

import actions
import kodiqa
from kodiqa import Kodiqa


# ── P0.1: spinner is paused before any interactive prompt ────────────────────
class TestSpinnerPause:
    def test_stop_live_status_stops_and_clears(self):
        k = MagicMock()
        st = MagicMock()
        k._live_status = st
        Kodiqa._stop_live_status(k)
        st.stop.assert_called_once()
        assert k._live_status is None

    def test_stop_live_status_noop_when_none(self):
        k = MagicMock()
        k._live_status = None
        Kodiqa._stop_live_status(k)  # must not raise

    def test_confirm_pauses_spinner_first(self):
        k = MagicMock()
        k.permission_mode = "auto"       # returns True immediately, after stopping
        k.console = MagicMock()
        k._stop_live_status = MagicMock()
        assert Kodiqa._confirm(k, "Run command: ls") is True
        k._stop_live_status.assert_called_once()

    def test_ask_user_pauses_via_hook(self, monkeypatch):
        from rich.console import Console
        paused = {"hit": False}
        actions.set_console(Console(file=io.StringIO()))
        actions.set_status_pause(lambda: paused.__setitem__("hit", True))
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: "1")
        try:
            actions.do_ask_user("pick one?", options=[{"label": "A"}, {"label": "B"}])
        finally:
            actions.set_status_pause(None)
        assert paused["hit"] is True

    def test_arrow_select_is_instance_method(self):
        # Converted from staticmethod so it can pause the spinner; callers use self.
        import inspect
        sig = inspect.signature(Kodiqa._arrow_select)
        assert list(sig.parameters)[0] == "self"


# ── shared harness for _run_tool_calls ───────────────────────────────────────
def _agent(**over):
    k = MagicMock()
    k.batch_edits = over.get("batch_edits", False)
    k.toon_enabled = False
    k._MCP_META_NAMES = Kodiqa._MCP_META_NAMES
    k._run_status = lambda label: contextlib.nullcontext()
    k._execute_tool = MagicMock(side_effect=lambda name, inp: f"[{name}]")
    k._partition_workspace = lambda calls: (calls, [])
    k._run_lint_if_enabled = lambda: None
    k._review_edit_queue = lambda: over.get("review", [])
    k.console = MagicMock()
    return k


class TestMetaRouting:
    def test_meta_tool_routed_via_execute_tool_when_batched(self, monkeypatch):
        # [mcp_search, read_file] used to send mcp_search through _dispatch ("Unknown
        # tool"). Now mcp_search must route through _execute_tool (-> _mcp_meta_call).
        monkeypatch.setattr(kodiqa, "get_edit_queue", lambda: [])
        k = _agent()
        calls = [{"id": "1", "name": "mcp_search", "input": {"q": "x"}},
                 {"id": "2", "name": "read_file", "input": {"path": "a"}}]
        results, lint, note = Kodiqa._run_tool_calls(k, calls)
        routed = [c.args[0] for c in k._execute_tool.call_args_list]
        assert "mcp_search" in routed          # went through _execute_tool, not _dispatch
        assert "read_file" in routed
        by_id = dict(results)
        assert by_id["1"] == "[mcp_search]" and "Unknown tool" not in by_id["1"]


class TestReviewFeedback:
    def test_review_outcome_returned_as_note(self, monkeypatch):
        monkeypatch.setattr(kodiqa, "get_edit_queue", lambda: [{"path": "x.py"}])
        k = _agent(batch_edits=True, review=["Rejected write to x.py"])
        results, lint, note = Kodiqa._run_tool_calls(
            k, [{"id": "1", "name": "write_file", "input": {"path": "x.py"}}])
        assert "Rejected write to x.py" in note
        assert "NOT written" in note
        # the outcome is NOT smuggled in as a droppable fake-id tool result
        assert all(tid != "review" for tid, _ in results)

    def test_no_note_without_review(self, monkeypatch):
        monkeypatch.setattr(kodiqa, "get_edit_queue", lambda: [])
        k = _agent(batch_edits=True)
        results, lint, note = Kodiqa._run_tool_calls(
            k, [{"id": "1", "name": "read_file", "input": {"path": "a"}}])
        assert note == ""
