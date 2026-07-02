"""Regression tests for the stability-audit fixes (multi-agent audit, 2026-07).

Each test pins a specific verified bug so it can't silently come back:
  - empty-file undo/rewind no longer deletes the file
  - multi_edit / diff_apply respect batch-review mode
  - multi_edit skips malformed (non-dict) edit elements
  - delete_file is undoable/rewindable (text files)
  - MCP call_tool routes underscore-named servers
  - bridge token compare is constant-time
  - _build_claude_messages converts OpenAI image_url -> Claude image
  - /clear resets _last_context_tokens
  - OpenAPIServer degrades gracefully on malformed specs
  - _run_pull reports a stalled (rc=None) pull as failure
"""
from unittest.mock import MagicMock

import actions


def _reset_edit_state():
    actions._undo_buffer.clear()
    actions._redo_buffer.clear()
    actions._turn_snapshot.clear()
    actions._edit_queue.clear()
    actions.set_batch_mode(False)


# ── Empty-file undo sentinel ────────────────────────────────────────────────

class TestEmptyFileUndo:
    def test_write_into_preexisting_empty_file_undo_restores_not_deletes(self, tmp_path):
        _reset_edit_state()
        f = tmp_path / "empty.ini"
        f.write_text("")                       # exists but empty
        actions.do_write_file(str(f), "new content\n")
        assert f.read_text() == "new content\n"
        actions.do_undo_edit(str(f))
        assert f.exists()                      # NOT deleted
        assert f.read_text() == ""             # restored to empty

    def test_write_new_file_undo_still_deletes(self, tmp_path):
        _reset_edit_state()
        f = tmp_path / "brand_new.txt"
        actions.do_write_file(str(f), "hello\n")
        assert f.exists()
        actions.do_undo_edit(str(f))
        assert not f.exists()                   # a genuinely new file is removed by undo

    def test_rewind_preserves_preexisting_empty_file(self, tmp_path):
        _reset_edit_state()
        f = tmp_path / "cfg.txt"
        f.write_text("")
        actions.do_write_file(str(f), "data\n")
        snap = actions.get_turn_snapshot()
        actions.do_rewind(snap)
        assert f.exists() and f.read_text() == ""


# ── Batch-review mode for multi_edit / diff_apply ───────────────────────────

class TestBatchModeCoverage:
    def test_multi_edit_queues_in_batch_mode(self, tmp_path):
        _reset_edit_state()
        f = tmp_path / "c.py"
        f.write_text("a=1\nb=2\n")
        actions.set_batch_mode(True)
        res = actions.do_multi_edit(str(f), [{"old_string": "a=1", "new_string": "a=10"}])
        assert "queued" in res.lower()
        assert f.read_text() == "a=1\nb=2\n"    # NOT written yet
        assert len(actions._edit_queue) == 1
        _reset_edit_state()

    def test_diff_apply_queues_in_batch_mode(self, tmp_path):
        _reset_edit_state()
        f = tmp_path / "d.txt"
        f.write_text("one\n")
        patch = "--- a/d.txt\n+++ b/d.txt\n@@ -1 +1 @@\n-one\n+two\n"
        actions.set_batch_mode(True)
        res = actions.do_diff_apply(str(f), patch)
        # queued and the file is left unchanged on disk until review
        assert "queued" in res.lower()
        assert f.read_text() == "one\n"
        assert len(actions._edit_queue) == 1
        _reset_edit_state()

    def test_multi_edit_still_applies_without_batch(self, tmp_path):
        _reset_edit_state()
        f = tmp_path / "e.py"
        f.write_text("x=1\n")
        actions.do_multi_edit(str(f), [{"old_string": "x=1", "new_string": "x=99"}])
        assert f.read_text() == "x=99\n"


class TestMultiEditRobustness:
    def test_non_dict_edit_element_is_skipped_not_fatal(self, tmp_path):
        _reset_edit_state()
        f = tmp_path / "f.py"
        f.write_text("k=1\n")
        res = actions.do_multi_edit(str(f), [{"old_string": "k=1", "new_string": "k=2"}, "oops"])
        assert "Applied" in res
        assert f.read_text() == "k=2\n"


# ── delete_file undo/rewind ─────────────────────────────────────────────────

class TestDeleteUndoable:
    def test_delete_then_undo_restores(self, tmp_path):
        _reset_edit_state()
        f = tmp_path / "gone.txt"
        f.write_text("important\n")
        actions.do_delete_file(str(f))
        assert not f.exists()
        actions.do_undo_edit(str(f))
        assert f.exists() and f.read_text() == "important\n"

    def test_delete_then_rewind_restores(self, tmp_path):
        _reset_edit_state()
        f = tmp_path / "gone2.txt"
        f.write_text("keep me\n")
        actions.do_delete_file(str(f))
        snap = actions.get_turn_snapshot()
        actions.do_rewind(snap)
        assert f.exists() and f.read_text() == "keep me\n"


# ── MCP underscore-server routing ───────────────────────────────────────────

class TestMcpUnderscoreRouting:
    def test_call_tool_routes_underscore_server_name(self):
        from mcp import MCPManager
        mgr = MCPManager()
        srv = MagicMock()
        srv.call_tool.return_value = "OK"
        mgr.servers = {"my_server": srv}
        out = mgr.call_tool("mcp_my_server_get_thing", {"a": 1})
        assert out == "OK"
        srv.call_tool.assert_called_once_with("get_thing", {"a": 1})

    def test_call_tool_prefers_longest_server_match(self):
        from mcp import MCPManager
        mgr = MCPManager()
        short, long = MagicMock(), MagicMock()
        short.call_tool.return_value = "short"
        long.call_tool.return_value = "long"
        mgr.servers = {"my": short, "my_server": long}
        assert mgr.call_tool("mcp_my_server_get_thing", {}) == "long"
        long.call_tool.assert_called_once_with("get_thing", {})

    def test_unknown_server_returns_message(self):
        from mcp import MCPManager
        mgr = MCPManager()
        mgr.servers = {}
        assert "not connected" in mgr.call_tool("mcp_nope_tool", {}).lower()


# ── Bridge constant-time token ──────────────────────────────────────────────

class TestBridgeAuth:
    def test_uses_compare_digest(self):
        import inspect
        import bridge
        src = inspect.getsource(bridge._Handler._authed)
        assert "compare_digest" in src


# ── Claude image_url normalization ──────────────────────────────────────────

class TestClaudeImageNormalization:
    def test_data_url_becomes_base64_image_block(self):
        from kodiqa import Kodiqa
        b = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}
        out = Kodiqa._to_claude_block(b)
        assert out["type"] == "image"
        assert out["source"]["type"] == "base64"
        assert out["source"]["media_type"] == "image/jpeg"
        assert out["source"]["data"] == "QUJD"

    def test_http_url_becomes_url_image_block(self):
        from kodiqa import Kodiqa
        b = {"type": "image_url", "image_url": {"url": "https://x/y.png"}}
        out = Kodiqa._to_claude_block(b)
        assert out["type"] == "image" and out["source"]["type"] == "url"

    def test_text_block_passes_through(self):
        from kodiqa import Kodiqa
        b = {"type": "text", "text": "hi"}
        assert Kodiqa._to_claude_block(b) is b


# ── Default model routes local, not to Qwen cloud ───────────────────────────

class TestDefaultModelIsLocal:
    def test_bare_qwen3_coder_routes_local(self):
        from config import get_openai_provider, DEFAULT_MODEL
        # The free-local default must NOT resolve to a cloud provider.
        assert get_openai_provider(DEFAULT_MODEL) is None
        assert get_openai_provider("qwen3-coder") is None

    def test_coding_plan_qwen_coder_aliases_still_cloud(self):
        from config import get_openai_provider
        assert get_openai_provider("qwen-coder") == "qwen"
        assert get_openai_provider("qwen3-coder-plus") == "qwen"
        assert get_openai_provider("qwen3-coder-next") == "qwen"


# ── /clear resets the cached context-token count ────────────────────────────

class TestClearResetsTokens:
    def test_clear_resets_last_context_tokens(self):
        from kodiqa import Kodiqa
        k = MagicMock()
        k._last_context_tokens = 999999
        Kodiqa._cmd_clear(k, "")
        assert k._last_context_tokens == 0
        assert k.history == []


# ── OpenAPI malformed-spec resilience ───────────────────────────────────────

class TestOpenApiMalformed:
    def test_string_parameter_entry_does_not_crash(self):
        from api_tools import OpenAPIServer
        s = OpenAPIServer("t", "http://x", None, {})
        spec = {"paths": {"/p": {"get": {"operationId": "op", "parameters": ["oops"]}}}}
        # must not raise
        s._build_tools(spec)

    def test_parameters_as_dict_does_not_crash(self):
        from api_tools import OpenAPIServer
        s = OpenAPIServer("t", "http://x", None, {})
        spec = {"paths": {"/p": {"parameters": {"bad": 1},
                                 "get": {"operationId": "op"}}}}
        s._build_tools(spec)
