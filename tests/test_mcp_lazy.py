"""Tests for lazy MCP tools — on-demand discovery instead of injecting every
MCP tool schema each turn (the mcp2cli-inspired token-saving feature)."""

from unittest.mock import MagicMock

from mcp import MCPManager
from kodiqa import Kodiqa, CLAUDE_TOOLS


class _FakeServer:
    def __init__(self, name, tools):
        self.name = name
        self.tools = tools  # [{name, description, inputSchema}]

    def get_tool_schemas(self):
        return [{
            "name": f"mcp_{self.name}_{t['name']}",
            "description": f"[MCP:{self.name}] {t.get('description', '')}",
            "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
        } for t in self.tools]


def _mgr():
    m = MCPManager()
    m.servers = {"git": _FakeServer("git", [
        {"name": "commit", "description": "create a git commit", "inputSchema": {"type": "object", "properties": {"msg": {"type": "string"}}}},
        {"name": "status", "description": "show working tree status"},
        {"name": "push", "description": "push commits to remote"},
    ])}
    return m


class TestManagerPrimitives:
    def test_tool_count(self):
        assert _mgr().tool_count() == 3

    def test_search_all(self):
        names = {r["name"] for r in _mgr().search_tools("")}
        assert names == {"mcp_git_commit", "mcp_git_status", "mcp_git_push"}

    def test_search_filter(self):
        # name-substring match
        assert [r["name"] for r in _mgr().search_tools("status")] == ["mcp_git_status"]
        # description-text match
        assert [r["name"] for r in _mgr().search_tools("remote")] == ["mcp_git_push"]

    def test_search_omits_schema(self):
        res = _mgr().search_tools("")
        assert all("input_schema" not in r for r in res)  # cheap: names+desc only

    def test_get_tool_schema(self):
        s = _mgr().get_tool_schema("mcp_git_commit")
        assert s and s["input_schema"]["properties"] == {"msg": {"type": "string"}}
        assert _mgr().get_tool_schema("mcp_git_nope") is None


def _agent(lazy=True):
    k = MagicMock()
    k.mcp = _mgr()
    k.mcp_lazy = lazy
    k._mcp_usage = {}
    k._MCP_META_NAMES = Kodiqa._MCP_META_NAMES
    # bind the real methods under test
    k._mcp_meta_tools = Kodiqa._mcp_meta_tools.__get__(k)
    k._save_mcp_usage = lambda: None
    return k


class TestGetAllToolsLazy:
    def test_lazy_exposes_three_meta_tools(self):
        tools = Kodiqa._get_all_tools(_agent(lazy=True))
        names = [t["name"] for t in tools]
        assert "mcp_search" in names and "mcp_tool_schema" in names and "mcp_call" in names
        # raw MCP tools must NOT be injected in lazy mode
        assert "mcp_git_commit" not in names
        # built-ins still present, and only 3 MCP-related schemas added
        assert len(tools) == len(CLAUDE_TOOLS) + 3

    def test_non_lazy_injects_all(self):
        tools = Kodiqa._get_all_tools(_agent(lazy=False))
        names = [t["name"] for t in tools]
        assert "mcp_git_commit" in names and "mcp_git_status" in names
        assert "mcp_search" not in names

    def test_no_servers_no_meta_tools(self):
        k = _agent(lazy=True)
        k.mcp = MCPManager()  # nothing connected
        tools = Kodiqa._get_all_tools(k)
        assert [t["name"] for t in tools] == [t["name"] for t in CLAUDE_TOOLS]


class TestMetaHandlers:
    def test_search_ranks_by_usage(self):
        k = _agent()
        k._mcp_usage = {"mcp_git_push": 5, "mcp_git_commit": 1}
        out = Kodiqa._mcp_meta_call(k, "mcp_search", {})
        # most-used tool listed first
        assert out.index("mcp_git_push") < out.index("mcp_git_commit")
        assert "used 5x" in out

    def test_search_respects_query_and_limit(self):
        out = Kodiqa._mcp_meta_call(_agent(), "mcp_search", {"query": "status"})
        assert "mcp_git_status" in out and "mcp_git_commit" not in out

    def test_tool_schema_lookup(self):
        out = Kodiqa._mcp_meta_call(_agent(), "mcp_tool_schema", {"name": "mcp_git_commit"})
        assert '"msg"' in out

    def test_tool_schema_not_found(self):
        out = Kodiqa._mcp_meta_call(_agent(), "mcp_tool_schema", {"name": "mcp_git_nope"})
        assert "not found" in out.lower()

    def test_call_increments_usage_and_routes(self):
        k = _agent()
        k.mcp = MagicMock()
        k.mcp.call_tool.return_value = "OK"
        k._MCP_META_NAMES = Kodiqa._MCP_META_NAMES
        result = Kodiqa._mcp_meta_call(k, "mcp_call", {"name": "mcp_git_commit", "arguments": {"msg": "hi"}})
        assert result == "OK"
        k.mcp.call_tool.assert_called_once_with("mcp_git_commit", {"msg": "hi"})
        assert k._mcp_usage["mcp_git_commit"] == 1

    def test_call_parses_stringified_arguments(self):
        k = _agent()
        k.mcp = MagicMock()
        k.mcp.call_tool.return_value = "OK"
        Kodiqa._mcp_meta_call(k, "mcp_call", {"name": "mcp_git_commit", "arguments": '{"msg": "hi"}'})
        k.mcp.call_tool.assert_called_once_with("mcp_git_commit", {"msg": "hi"})

    def test_call_requires_name(self):
        out = Kodiqa._mcp_meta_call(_agent(), "mcp_call", {"arguments": {}})
        assert "required" in out.lower()


class TestMetaToolRouting:
    def test_execute_tool_dispatches_meta(self):
        k = MagicMock()
        k._MCP_META_NAMES = Kodiqa._MCP_META_NAMES
        k._mcp_meta_call.return_value = "meta-result"
        out = Kodiqa._execute_tool(k, "mcp_search", {"query": "x"})
        assert out == "meta-result"
        k._mcp_meta_call.assert_called_once_with("mcp_search", {"query": "x"})

    def test_execute_tool_raw_mcp_still_routes_to_manager(self):
        k = MagicMock()
        k._MCP_META_NAMES = Kodiqa._MCP_META_NAMES
        k.mcp.call_tool.return_value = "raw"
        out = Kodiqa._execute_tool(k, "mcp_git_commit", {"msg": "x"})
        assert out == "raw"
        k.mcp.call_tool.assert_called_once_with("mcp_git_commit", {"msg": "x"})
