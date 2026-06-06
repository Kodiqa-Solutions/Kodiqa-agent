"""Tests for remote MCP over Streamable HTTP transport (+ auth parsing).

Includes a real in-process HTTP MCP server to prove the transport works on the
wire, plus mocked unit tests for the SSE path and the auth-header parsing.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import mcp
from mcp import MCPHttpServer, MCPManager
from kodiqa import Kodiqa


# ── A minimal real Streamable-HTTP MCP server (JSON responses) ──
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or "{}")
        method, rid = req.get("method"), req.get("id")
        if method == "notifications/initialized":
            self.send_response(202); self.end_headers(); return
        if method == "initialize":
            body = {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-03-26"}}
            extra = {"Mcp-Session-Id": "sess-abc"}
        elif method == "tools/list":
            body = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "echo", "description": "echo text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
            ]}}
            extra = {}
        elif method == "tools/call":
            text = req["params"]["arguments"].get("text", "")
            # echo back the session id the client sent, to prove it's reused
            sid = self.headers.get("Mcp-Session-Id", "")
            body = {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": f"{text}|{sid}"}]}}
            extra = {}
        else:
            body = {"jsonrpc": "2.0", "id": rid, "error": {"message": "unknown"}}
            extra = {}
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        for k, v in extra.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class TestHttpTransportE2E:
    def _serve(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}/mcp"

    def test_start_lists_tools_and_captures_session(self):
        srv, url = self._serve()
        try:
            s = MCPHttpServer("remote", url)
            assert s.start() is True
            assert [t["name"] for t in s.tools] == ["echo"]
            assert s.session_id == "sess-abc"
            assert s.get_tool_schemas()[0]["name"] == "mcp_remote_echo"
        finally:
            srv.shutdown()

    def test_call_tool_over_http_reuses_session(self):
        srv, url = self._serve()
        try:
            s = MCPHttpServer("remote", url)
            s.start()
            out = s.call_tool("echo", {"text": "hi"})
            # server echoes "text|session-id" — proves the Mcp-Session-Id was sent back
            assert out == "hi|sess-abc"
        finally:
            srv.shutdown()

    def test_manager_add_http_server(self):
        srv, url = self._serve()
        try:
            m = MCPManager()
            tools = m.add_http_server("remote", url)
            assert tools and m.tool_count() == 1
            assert "remote [http]" in m.list_servers()
            # routing works through the manager
            assert m.call_tool("mcp_remote_echo", {"text": "x"}).startswith("x|")
        finally:
            srv.shutdown()


# ── SSE response path (mocked, deterministic) ──
class _FakeResp:
    def __init__(self, *, json_body=None, sse=None, status=200, headers=None):
        self._json = json_body
        self._sse = sse or []
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.text = json.dumps(json_body) if json_body else ""

    def json(self):
        return self._json

    def iter_lines(self, decode_unicode=False):
        yield from self._sse

    def close(self):
        pass


class TestSSEAndErrors:
    def test_sse_response_parsed(self, monkeypatch):
        s = MCPHttpServer("r", "http://t/mcp")
        s._id = 7  # next request id will be 8

        def fake_post(url, json=None, headers=None, timeout=None, stream=None):
            rid = json.get("id")
            evt = "data: " + __import__("json").dumps(
                {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "streamed"}]}})
            return _FakeResp(sse=[evt, ""], headers={"Content-Type": "text/event-stream"})

        monkeypatch.setattr(mcp.requests, "post", fake_post)
        assert s.call_tool("echo", {}) == "streamed"

    def test_http_error_surfaces(self, monkeypatch):
        s = MCPHttpServer("r", "http://t/mcp")
        monkeypatch.setattr(mcp.requests, "post",
                            lambda *a, **k: _FakeResp(status=401, headers={}, json_body=None))
        assert "MCP error" in s.call_tool("echo", {}) or "HTTP 401" in s.call_tool("echo", {})

    def test_auth_header_sent(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None, stream=None):
            captured.update(headers or {})
            return _FakeResp(json_body={"jsonrpc": "2.0", "id": json.get("id"), "result": {"content": []}})

        monkeypatch.setattr(mcp.requests, "post", fake_post)
        MCPHttpServer("r", "http://t/mcp", headers={"Authorization": "Bearer XYZ"}).call_tool("echo", {})
        assert captured.get("Authorization") == "Bearer XYZ"


class TestAuthParsing:
    def _agent(self):
        k = MagicMock()
        k._resolve_secret = Kodiqa._resolve_secret.__get__(k)
        return k

    def test_bearer_and_header(self):
        k = self._agent()
        h = Kodiqa._parse_mcp_auth(k, ["--bearer", "TOK", "--header", "X-Api-Key:secret"])
        assert h["Authorization"] == "Bearer TOK"
        assert h["X-Api-Key"] == "secret"

    def test_env_secret(self, monkeypatch):
        monkeypatch.setenv("MY_MCP_TOKEN", "from-env")
        k = self._agent()
        h = Kodiqa._parse_mcp_auth(k, ["--bearer", "env:MY_MCP_TOKEN"])
        assert h["Authorization"] == "Bearer from-env"

    def test_file_secret(self, tmp_path):
        f = tmp_path / "tok.txt"
        f.write_text("file-token\n")
        k = self._agent()
        h = Kodiqa._parse_mcp_auth(k, ["--token", f"file:{f}"])
        assert h["Authorization"] == "Bearer file-token"

    def test_no_auth(self):
        assert Kodiqa._parse_mcp_auth(self._agent(), []) == {}
