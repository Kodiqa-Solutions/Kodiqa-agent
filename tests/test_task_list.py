"""Tests for the live task-list tool (todo_write)."""

import pytest

from actions import do_todo_write, get_task_list, clear_task_list, _dispatch


@pytest.fixture(autouse=True)
def _reset():
    clear_task_list()
    yield
    clear_task_list()


class TestTodoWrite:
    def test_sets_and_returns_checklist(self):
        out = do_todo_write([
            {"content": "explore", "status": "completed"},
            {"content": "fix", "status": "in_progress"},
            {"content": "test", "status": "pending"},
        ])
        assert "1/3 done" in out
        assert get_task_list() == [
            {"content": "explore", "status": "completed"},
            {"content": "fix", "status": "in_progress"},
            {"content": "test", "status": "pending"},
        ]

    def test_replaces_previous_list(self):
        do_todo_write([{"content": "a", "status": "pending"}])
        do_todo_write([{"content": "b", "status": "completed"}])
        assert get_task_list() == [{"content": "b", "status": "completed"}]

    def test_json_string_input_ollama_mode(self):
        out = do_todo_write('[{"content": "x", "status": "pending"}]')
        assert "Error" not in out
        assert get_task_list() == [{"content": "x", "status": "pending"}]

    def test_bad_status_coerced_to_pending(self):
        do_todo_write([{"content": "a", "status": "bogus"}])
        assert get_task_list()[0]["status"] == "pending"

    def test_plain_string_item_becomes_pending_task(self):
        do_todo_write(["just a string task"])
        assert get_task_list() == [{"content": "just a string task", "status": "pending"}]

    def test_empty_content_dropped(self):
        out = do_todo_write([{"content": "  ", "status": "pending"}])
        assert "Error" in out
        assert get_task_list() == []

    def test_non_list_is_error(self):
        assert "Error" in do_todo_write(42)
        assert "Error" in do_todo_write("not json")

    def test_clear_resets(self):
        do_todo_write([{"content": "a", "status": "pending"}])
        clear_task_list()
        assert get_task_list() == []

    def test_dispatch_routes_todo_write(self):
        out = _dispatch("todo_write", {"todos": [{"content": "z", "status": "pending"}]}, None)
        assert "Error" not in out
        assert get_task_list() == [{"content": "z", "status": "pending"}]


class TestRegistration:
    def test_in_tool_schema(self):
        from tools import CLAUDE_TOOLS
        tool = next((t for t in CLAUDE_TOOLS if t["name"] == "todo_write"), None)
        assert tool is not None
        assert "todos" in tool["input_schema"]["properties"]

    def test_not_a_confirm_action(self):
        # todo_write touches no files/commands — it must never prompt for confirmation.
        from config import CONFIRM_ACTIONS
        assert "todo_write" not in CONFIRM_ACTIONS

    def test_in_system_prompt_for_ollama(self):
        from config import SYSTEM_PROMPT
        assert "[ACTION: todo_write]" in SYSTEM_PROMPT

    def test_tool_label(self):
        from kodiqa import _tool_label
        label = _tool_label("todo_write", {"todos": [
            {"content": "a", "status": "completed"}, {"content": "b", "status": "pending"}]})
        assert "1" in label and "2" in label
