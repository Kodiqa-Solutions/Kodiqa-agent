"""OpenAPI + GraphQL tool sources — turn any REST or GraphQL API into callable
tools at runtime (no codegen), inspired by mcp2cli.

Both classes mirror the MCP server interface (name / tools / start / call_tool /
get_tool_schemas / stop) so MCPManager treats them like any other tool source —
they get the same lazy-discovery, routing, and TOON handling for free.
"""

import json
import logging
import os
import re

import requests

from mcp import _claude_tool_schemas

_logger = logging.getLogger("kodiqa")

_HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _sanitize(name):
    """Make a safe, readable tool name from an operationId / path."""
    name = re.sub(r"[^0-9a-zA-Z]+", "_", str(name)).strip("_")
    return name or "op"


# ── OpenAPI ──

class OpenAPIServer:
    """Builds one tool per OpenAPI operation; call_tool issues the HTTP request."""

    def __init__(self, name, spec_src, base_url=None, headers=None):
        self.name = name
        self.spec_src = spec_src
        self.base_url = base_url
        self.headers = headers or {}
        self.tools = []
        self._ops = {}  # tool_name -> {method, path, path_params, query_params, body_params}

    def _load_spec(self):
        try:
            if self.spec_src.startswith(("http://", "https://")):
                r = requests.get(self.spec_src, headers=self.headers, timeout=20)
                r.raise_for_status()
                text = r.text
            else:
                with open(os.path.expanduser(self.spec_src)) as f:
                    text = f.read()
        except Exception as e:
            _logger.warning(f"OpenAPI '{self.name}': could not load spec: {e}")
            return None
        try:
            return json.loads(text)
        except ValueError:
            try:
                import yaml  # optional — only needed for YAML specs
                return yaml.safe_load(text)
            except Exception:
                _logger.warning(f"OpenAPI '{self.name}': spec is not JSON (YAML needs PyYAML installed)")
                return None

    def start(self):
        spec = self._load_spec()
        if not spec:
            return False
        if not self.base_url:
            servers = spec.get("servers") or []
            if servers:
                self.base_url = servers[0].get("url")
            elif spec.get("host"):  # Swagger 2.0
                scheme = (spec.get("schemes") or ["https"])[0]
                self.base_url = f"{scheme}://{spec['host']}{spec.get('basePath', '')}"
        self._build_tools(spec)
        return bool(self.tools)

    def _build_tools(self, spec):
        for path, item in (spec.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            shared = item.get("parameters", [])
            for method, op in item.items():
                if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                    continue
                name = _sanitize(op.get("operationId") or f"{method}_{path}")
                props, required = {}, []
                path_params, query_params, body_params = set(), set(), set()
                for p in shared + op.get("parameters", []):
                    pname, loc = p.get("name"), p.get("in")
                    if not pname or loc not in ("path", "query"):
                        continue
                    props[pname] = p.get("schema", {"type": "string"})
                    if p.get("required"):
                        required.append(pname)
                    (path_params if loc == "path" else query_params).add(pname)
                body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {}
                for bn, bs in (body.get("properties") or {}).items():
                    props[bn] = bs
                    body_params.add(bn)
                for rq in body.get("required", []):
                    if rq not in required:
                        required.append(rq)
                desc = op.get("summary") or op.get("description") or f"{method.upper()} {path}"
                self.tools.append({"name": name, "description": desc[:200],
                                   "inputSchema": {"type": "object", "properties": props, "required": required}})
                self._ops[name] = {"method": method.upper(), "path": path,
                                   "path_params": path_params, "query_params": query_params,
                                   "body_params": body_params}

    def call_tool(self, tool_name, arguments):
        op = self._ops.get(tool_name)
        if not op:
            return f"Unknown operation: {tool_name}"
        if not self.base_url:
            return "No base URL for this API — pass --base-url."
        args = dict(arguments or {})
        path = op["path"]
        for pp in op["path_params"]:
            if pp in args:
                path = path.replace("{" + pp + "}", str(args.pop(pp)))
        query = {k: args.pop(k) for k in list(args) if k in op["query_params"]}
        body = {k: args.pop(k) for k in list(args) if k in op["body_params"]}
        url = self.base_url.rstrip("/") + "/" + path.lstrip("/")
        try:
            r = requests.request(op["method"], url, params=query or None,
                                 json=body or None, headers=self.headers, timeout=60)
            return f"HTTP {r.status_code}\n{r.text}"
        except Exception as e:
            return f"API request error: {e}"

    def get_tool_schemas(self):
        return _claude_tool_schemas(self.name, self.tools)

    def stop(self):
        pass


# ── GraphQL ──

_GQL_INTROSPECTION = """
query {
  __schema {
    queryType { name }
    mutationType { name }
    types {
      kind name
      fields {
        name description
        args { name type { kind name ofType { kind name ofType { kind name } } } }
        type { kind name ofType { kind name ofType { kind name } } }
      }
    }
  }
}
"""


def _gql_named(t):
    while t and t.get("ofType"):
        t = t["ofType"]
    return t or {}


def _gql_typestr(t):
    if not t:
        return "String"
    if t.get("kind") == "NON_NULL":
        return _gql_typestr(t["ofType"]) + "!"
    if t.get("kind") == "LIST":
        return "[" + _gql_typestr(t["ofType"]) + "]"
    return t.get("name") or "String"


def _gql_arg_schema(t):
    named = _gql_named(t)
    m = {"Int": "integer", "Float": "number", "String": "string", "Boolean": "boolean", "ID": "string"}
    return {"type": m.get(named.get("name"), "string")}


class GraphQLServer:
    """Introspects a GraphQL endpoint; each query/mutation becomes a tool, with an
    auto-generated selection set of the return type's scalar fields."""

    def __init__(self, name, url, headers=None):
        self.name = name
        self.url = url
        self.headers = headers or {}
        self.tools = []
        self._fields = {}   # tool_name -> {kind, type, args}
        self._types = {}

    def start(self):
        schema = self._introspect()
        if not schema:
            return False
        self._types = {t["name"]: t for t in schema.get("types", []) if t.get("name")}
        for root_key, kind in (("queryType", "query"), ("mutationType", "mutation")):
            root = schema.get(root_key)
            if not root:
                continue
            rt = self._types.get(root["name"], {})
            for f in rt.get("fields") or []:
                props, required = {}, []
                for a in f.get("args") or []:
                    props[a["name"]] = _gql_arg_schema(a["type"])
                    if a["type"].get("kind") == "NON_NULL":
                        required.append(a["name"])
                self.tools.append({
                    "name": f["name"],
                    "description": (f.get("description") or f"GraphQL {kind} {f['name']}")[:200],
                    "inputSchema": {"type": "object", "properties": props, "required": required},
                })
                self._fields[f["name"]] = {"kind": kind, "type": f["type"], "args": f.get("args") or []}
        return bool(self.tools)

    def _introspect(self):
        try:
            r = requests.post(self.url, json={"query": _GQL_INTROSPECTION},
                              headers=self.headers, timeout=30)
            r.raise_for_status()
            return r.json().get("data", {}).get("__schema")
        except Exception as e:
            _logger.warning(f"GraphQL '{self.name}': introspection failed: {e}")
            return None

    def _selection(self, type_ref):
        named = self._types.get(_gql_named(type_ref).get("name"), {})
        if named.get("kind") in ("OBJECT", "INTERFACE"):
            scalars = [fl["name"] for fl in (named.get("fields") or [])
                       if _gql_named(fl["type"]).get("kind") in ("SCALAR", "ENUM")]
            return " { " + " ".join(scalars) + " }" if scalars else " { __typename }"
        return ""  # scalar return type — no selection set

    def call_tool(self, tool_name, arguments):
        meta = self._fields.get(tool_name)
        if not meta:
            return f"Unknown field: {tool_name}"
        args = dict(arguments or {})
        argtypes = {a["name"]: a["type"] for a in meta["args"]}
        used = [k for k in args if k in argtypes]
        vardefs = ("(" + ", ".join(f"${k}: {_gql_typestr(argtypes[k])}" for k in used) + ")") if used else ""
        callargs = ("(" + ", ".join(f"{k}: ${k}" for k in used) + ")") if used else ""
        sel = self._selection(meta["type"])
        query = f"{meta['kind']} op{vardefs} {{ {tool_name}{callargs}{sel} }}"
        try:
            r = requests.post(self.url, json={"query": query, "variables": {k: args[k] for k in used}},
                              headers=self.headers, timeout=60)
            return r.text
        except Exception as e:
            return f"GraphQL request error: {e}"

    def get_tool_schemas(self):
        return _claude_tool_schemas(self.name, self.tools)

    def stop(self):
        pass
