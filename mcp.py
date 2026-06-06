"""Kodiqa MCP (Model Context Protocol) client — connects to external tool servers."""

import json
import subprocess
import threading
import logging

import requests

_logger = logging.getLogger("kodiqa")


def _claude_tool_schemas(server_name, tools):
    """Convert raw MCP tool defs to Claude-compatible tool schemas (shared by all transports)."""
    return [{
        "name": f"mcp_{server_name}_{t['name']}",
        "description": f"[MCP:{server_name}] {t.get('description', t['name'])}",
        "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
    } for t in tools]


def _extract_tool_result(resp):
    """Flatten a JSON-RPC tools/call response into text (shared by all transports)."""
    if resp and "result" in resp:
        content = resp["result"].get("content", [])
        texts = [b.get("text", "") for b in content if b.get("type") == "text"]
        return "\n".join(texts) if texts else str(resp["result"])
    if resp and "error" in resp:
        return f"MCP error: {resp['error'].get('message', str(resp['error']))}"
    return "MCP: no response"


class MCPServer:
    """A connection to an MCP server process (stdio transport)."""

    def __init__(self, name, command, args=None, env=None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env
        self.process = None
        self.tools = []
        self._id = 0
        self._lock = threading.Lock()

    def start(self):
        """Start the MCP server process."""
        try:
            cmd = [self.command] + self.args
            self.process = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                # DEVNULL, not PIPE: an undrained stderr pipe fills at ~64KB and
                # deadlocks the server.
                stderr=subprocess.DEVNULL, text=True, env=self.env,
            )
            # Initialize with MCP protocol
            resp = self._send({"jsonrpc": "2.0", "method": "initialize", "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "kodiqa", "version": "1.0"},
            }})
            if resp and "result" in resp:
                # List tools
                tools_resp = self._send({"jsonrpc": "2.0", "method": "tools/list", "params": {}})
                if tools_resp and "result" in tools_resp:
                    self.tools = tools_resp["result"].get("tools", [])
                # Send initialized notification
                self._notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
                return True
        except FileNotFoundError:
            _logger.warning(f"MCP server '{self.name}': command not found: {self.command}")
        except Exception as e:
            _logger.warning(f"MCP server '{self.name}' failed to start: {e}")
        return False

    def call_tool(self, tool_name, arguments):
        """Call a tool on this MCP server."""
        resp = self._send({"jsonrpc": "2.0", "method": "tools/call", "params": {
            "name": tool_name,
            "arguments": arguments,
        }})
        return _extract_tool_result(resp)

    def _readline_timeout(self, timeout=30):
        """Read one line from stdout, returning None if it takes longer than `timeout`
        seconds — so a hung/misbehaving server can't block Kodiqa indefinitely."""
        result = {}

        def _read():
            try:
                result["line"] = self.process.stdout.readline()
            except Exception as e:
                result["err"] = e

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            return None  # timed out
        if "err" in result:
            raise result["err"]
        return result.get("line")

    def _send(self, message):
        """Send a JSON-RPC message and read the response."""
        if not self.process or self.process.poll() is not None:
            return None
        with self._lock:
            self._id += 1
            message["id"] = self._id
            try:
                line = json.dumps(message) + "\n"
                self.process.stdin.write(line)
                self.process.stdin.flush()
                resp_line = self._readline_timeout(30)
                if resp_line is None:
                    _logger.warning(f"MCP '{self.name}' timed out waiting for response")
                    return None
                if resp_line:
                    return json.loads(resp_line)
            except Exception as e:
                _logger.warning(f"MCP '{self.name}' communication error: {e}")
        return None

    def _notify(self, message):
        """Send a notification (no response expected)."""
        if not self.process or self.process.poll() is not None:
            return
        try:
            line = json.dumps(message) + "\n"
            self.process.stdin.write(line)
            self.process.stdin.flush()
        except Exception:
            _logger.debug("ignored error in _notify", exc_info=True)

    def stop(self):
        """Stop the MCP server process."""
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
                try:
                    self.process.wait(timeout=5)  # reap, don't leave a zombie
                except Exception:
                    _logger.debug("ignored error in stop", exc_info=True)

    def get_tool_schemas(self):
        """Convert MCP tools to Claude-compatible tool schemas."""
        return _claude_tool_schemas(self.name, self.tools)


class MCPHttpServer:
    """A connection to a remote MCP server over Streamable HTTP transport
    (the current MCP standard). Mirrors MCPServer's interface (start / call_tool /
    get_tool_schemas / stop / .tools / .name) so MCPManager treats both the same.

    Auth is via static request headers (e.g. {"Authorization": "Bearer …"}); the
    interactive OAuth flow plugs in here by populating those headers (Phase 2)."""

    def __init__(self, name, url, headers=None, oauth=None):
        self.name = name
        self.url = url
        self.headers = headers or {}
        self.oauth = oauth  # optional OAuthSession; supplies/refreshes the bearer token
        self.tools = []
        self.session_id = None
        self._id = 0
        self._lock = threading.Lock()

    def start(self):
        try:
            resp = self._request("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "kodiqa", "version": "1.0"},
            })
            if not resp or "result" not in resp:
                if resp and "error" in resp:
                    _logger.warning(f"MCP '{self.name}' initialize error: {resp['error']}")
                return False
            self._notify("notifications/initialized")
            tools_resp = self._request("tools/list", {})
            if tools_resp and "result" in tools_resp:
                self.tools = tools_resp["result"].get("tools", [])
            return True
        except Exception as e:
            _logger.warning(f"MCP '{self.name}' (HTTP) failed to start: {e}")
            return False

    def call_tool(self, tool_name, arguments):
        resp = self._request("tools/call", {"name": tool_name, "arguments": arguments})
        return _extract_tool_result(resp)

    def get_tool_schemas(self):
        return _claude_tool_schemas(self.name, self.tools)

    def _headers(self):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        h.update(self.headers)
        if self.oauth:  # a fresh (auto-refreshed) bearer token wins over static headers
            h.update(self.oauth.auth_header())
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _request(self, method, params=None):
        """Send a JSON-RPC request and return the parsed response (or None)."""
        with self._lock:
            self._id += 1
            msg = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
            return self._post(msg, want_id=self._id)

    def _notify(self, method, params=None):
        """Send a JSON-RPC notification (no id, no response expected)."""
        try:
            self._post({"jsonrpc": "2.0", "method": method, "params": params or {}}, want_id=None)
        except Exception:
            _logger.debug("ignored error in _notify", exc_info=True)

    def _post(self, message, want_id, _retried=False):
        try:
            resp = requests.post(self.url, json=message, headers=self._headers(),
                                 timeout=60, stream=True)
        except requests.RequestException as e:
            _logger.warning(f"MCP '{self.name}' (HTTP) request error: {e}")
            return None
        # The server assigns a session id on initialize; reuse it on every later call.
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        try:
            if resp.status_code == 202:   # accepted (notifications) — no body
                return None
            # Token expired? Refresh once via OAuth and retry the same request.
            if resp.status_code == 401 and self.oauth and not _retried:
                resp.close()
                if self.oauth.refresh():
                    return self._post(message, want_id, _retried=True)
            if resp.status_code >= 400:
                _logger.warning(f"MCP '{self.name}' (HTTP) {resp.status_code}: {resp.text[:200]}")
                return {"error": {"message": f"HTTP {resp.status_code}"}}
            ctype = resp.headers.get("Content-Type", "")
            if "text/event-stream" in ctype:
                return self._read_sse(resp, want_id)
            return resp.json()
        finally:
            resp.close()

    def _read_sse(self, resp, want_id):
        """Read a Streamable-HTTP SSE response and return the JSON-RPC message whose
        id matches `want_id` (servers may interleave notifications/other messages)."""
        data_lines = []
        last = None
        for raw in resp.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            if raw == "":  # blank line dispatches the accumulated event
                if data_lines:
                    try:
                        msg = json.loads("\n".join(data_lines))
                        last = msg
                        if isinstance(msg, dict) and (want_id is None or msg.get("id") == want_id):
                            return msg
                    except Exception:
                        _logger.debug("ignored error in _read_sse", exc_info=True)
                    data_lines = []
                continue
            if raw.startswith("data:"):
                data_lines.append(raw[5:].lstrip())
            # event:/id:/retry: lines are ignored
        return last

    def stop(self):
        """Best-effort: ask the server to end the session."""
        if not self.session_id:
            return
        try:
            requests.delete(self.url, headers=self._headers(), timeout=10).close()
        except Exception:
            _logger.debug("ignored error in stop", exc_info=True)


class MCPManager:
    """Manages multiple MCP server connections."""

    def __init__(self):
        self.servers = {}  # {name: MCPServer}

    def add_server(self, name, command, args=None, env=None):
        """Add and start a local (stdio) MCP server."""
        if name in self.servers:
            self.servers[name].stop()
        server = MCPServer(name, command, args, env)
        if server.start():
            self.servers[name] = server
            return server.tools
        return None

    def add_http_server(self, name, url, headers=None, oauth=None):
        """Add and start a remote MCP server over Streamable HTTP (token/header or OAuth)."""
        if name in self.servers:
            self.servers[name].stop()
        server = MCPHttpServer(name, url, headers, oauth=oauth)
        if server.start():
            self.servers[name] = server
            return server.tools
        return None

    def add_source(self, name, server):
        """Add and start any tool source implementing the server interface
        (e.g. OpenAPIServer / GraphQLServer). Returns its tools, or None."""
        if name in self.servers:
            self.servers[name].stop()
        if server.start():
            self.servers[name] = server
            return server.tools
        return None

    def remove_server(self, name):
        """Stop and remove an MCP server."""
        if name in self.servers:
            self.servers[name].stop()
            del self.servers[name]
            return True
        return False

    def call_tool(self, full_tool_name, arguments):
        """Call a tool by its full name (mcp_servername_toolname)."""
        # Parse: mcp_servername_toolname
        parts = full_tool_name.split("_", 2)
        if len(parts) < 3 or parts[0] != "mcp":
            return f"Invalid MCP tool name: {full_tool_name}"
        server_name = parts[1]
        tool_name = parts[2]
        if server_name not in self.servers:
            return f"MCP server '{server_name}' not connected"
        return self.servers[server_name].call_tool(tool_name, arguments)

    def get_all_tools(self):
        """Get all tool schemas from all connected servers."""
        tools = []
        for server in self.servers.values():
            tools.extend(server.get_tool_schemas())
        return tools

    def tool_count(self):
        """Total number of tools across all connected servers."""
        return sum(len(s.tools) for s in self.servers.values())

    def search_tools(self, query=""):
        """Lazy-discovery: return [{name, description}] for matching tools WITHOUT
        their (token-heavy) input schemas. `query` is a case-insensitive substring
        match over the full name + description; empty returns everything."""
        q = (query or "").lower().strip()
        out = []
        for schema in self.get_all_tools():
            name = schema["name"]
            desc = schema.get("description", "")
            if not q or q in name.lower() or q in desc.lower():
                out.append({"name": name, "description": desc})
        return out

    def get_tool_schema(self, full_name):
        """Return the full Claude tool schema (incl. input_schema) for one tool,
        or None. Used to fetch a single schema on demand instead of all of them."""
        for schema in self.get_all_tools():
            if schema["name"] == full_name:
                return schema
        return None

    def list_servers(self):
        """List connected servers with their tools."""
        if not self.servers:
            return "No MCP servers connected."
        lines = []
        for name, server in self.servers.items():
            tool_names = [t["name"] for t in server.tools]
            kind = {"MCPServer": "stdio", "MCPHttpServer": "http",
                    "OpenAPIServer": "openapi", "GraphQLServer": "graphql"}.get(
                type(server).__name__, "tool")
            lines.append(f"  {name} [{kind}]: {len(server.tools)} tools ({', '.join(tool_names[:5])})")
        return "\n".join(lines)

    def stop_all(self):
        """Stop all servers."""
        for server in self.servers.values():
            server.stop()
        self.servers.clear()
