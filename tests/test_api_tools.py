"""Tests for OpenAPI + GraphQL tool sources (api_tools.py).

Includes real in-process HTTP servers so the request/introspection paths are
exercised on the wire, plus unit tests for spec parsing and helpers.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api_tools import (
    OpenAPIServer, GraphQLServer, _sanitize, _gql_typestr, _gql_arg_schema,
)


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


_SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "http://placeholder"}],
    "paths": {
        "/pets/{id}": {"get": {"operationId": "getPet", "parameters": [
            {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}]}},
        "/pets": {
            "get": {"operationId": "listPets", "parameters": [
                {"name": "limit", "in": "query", "schema": {"type": "integer"}}]},
            "post": {"operationId": "createPet", "requestBody": {"content": {"application/json": {
                "schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}}}},
        },
    },
}


class _RestHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _echo(self, method):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode() if n else ""
        payload = json.dumps({"method": method, "path": self.path, "body": body}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._echo("GET")

    def do_POST(self):
        self._echo("POST")


class TestOpenAPI:
    def _server(self, tmp_path, base_url):
        f = tmp_path / "spec.json"
        f.write_text(json.dumps(_SPEC))
        return OpenAPIServer("petstore", str(f), base_url=base_url)

    def test_builds_tools_from_spec(self, tmp_path):
        s = self._server(tmp_path, "http://x")
        assert s.start() is True
        names = {t["name"] for t in s.tools}
        assert names == {"getPet", "listPets", "createPet"}
        get_pet = next(t for t in s.tools if t["name"] == "getPet")
        assert get_pet["inputSchema"]["required"] == ["id"]
        create = next(t for t in s.tools if t["name"] == "createPet")
        assert "name" in create["inputSchema"]["properties"]
        assert s.get_tool_schemas()[0]["name"].startswith("mcp_petstore_")

    def test_call_get_with_path_param(self, tmp_path):
        srv, url = _serve(_RestHandler)
        try:
            s = self._server(tmp_path, url)
            s.start()
            out = s.call_tool("getPet", {"id": 5})
            assert '"method": "GET"' in out and "/pets/5" in out
        finally:
            srv.shutdown()

    def test_call_get_with_query(self, tmp_path):
        srv, url = _serve(_RestHandler)
        try:
            s = self._server(tmp_path, url)
            s.start()
            out = s.call_tool("listPets", {"limit": 10})
            assert "/pets?limit=10" in out
        finally:
            srv.shutdown()

    def test_call_post_with_body(self, tmp_path):
        srv, url = _serve(_RestHandler)
        try:
            s = self._server(tmp_path, url)
            s.start()
            out = s.call_tool("createPet", {"name": "Fido"})
            assert '"method": "POST"' in out and '\\"name\\": \\"Fido\\"' in out
        finally:
            srv.shutdown()


# ── GraphQL ──
_GQL_SCHEMA = {
    "queryType": {"name": "Query"},
    "mutationType": None,
    "types": [
        {"kind": "OBJECT", "name": "Query", "fields": [
            {"name": "users", "description": "list users",
             "args": [{"name": "limit", "type": {"kind": "SCALAR", "name": "Int", "ofType": None}}],
             "type": {"kind": "LIST", "name": None, "ofType": {"kind": "OBJECT", "name": "User", "ofType": None}}}]},
        {"kind": "OBJECT", "name": "User", "fields": [
            {"name": "id", "args": [], "type": {"kind": "SCALAR", "name": "ID", "ofType": None}},
            {"name": "name", "args": [], "type": {"kind": "SCALAR", "name": "String", "ofType": None}}]},
    ],
}


class _GqlHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or "{}")
        if "__schema" in req.get("query", ""):
            body = {"data": {"__schema": _GQL_SCHEMA}}
        else:
            body = {"echo_query": req.get("query"), "variables": req.get("variables")}
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class TestGraphQL:
    def test_introspect_builds_tools(self):
        srv, url = _serve(_GqlHandler)
        try:
            s = GraphQLServer("gql", url)
            assert s.start() is True
            assert [t["name"] for t in s.tools] == ["users"]
            assert "limit" in s.tools[0]["inputSchema"]["properties"]
        finally:
            srv.shutdown()

    def test_call_builds_query_with_selection(self):
        srv, url = _serve(_GqlHandler)
        try:
            s = GraphQLServer("gql", url)
            s.start()
            out = s.call_tool("users", {"limit": 2})
            assert "users(limit: $limit)" in out          # variable wired
            assert "id name" in out                         # auto selection set
            assert '"limit": 2' in out                      # variables sent
        finally:
            srv.shutdown()


class TestHelpers:
    def test_sanitize(self):
        assert _sanitize("get /pets/{id}") == "get_pets_id"

    def test_gql_typestr(self):
        t = {"kind": "NON_NULL", "ofType": {"kind": "LIST", "ofType": {"kind": "SCALAR", "name": "Int"}}}
        assert _gql_typestr(t) == "[Int]!"

    def test_gql_arg_schema(self):
        assert _gql_arg_schema({"kind": "SCALAR", "name": "Int", "ofType": None}) == {"type": "integer"}
        assert _gql_arg_schema({"kind": "SCALAR", "name": "Whatever", "ofType": None}) == {"type": "string"}
