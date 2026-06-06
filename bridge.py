"""Editor/IDE bridge — a small authenticated localhost HTTP server that exposes
Kodiqa to editor extensions (VS Code / Zed / Neovim, etc.).

It's the backend an extension talks to; the protocol is intentionally tiny:

  GET  /health                       -> {status, model, version}        (no auth)
  POST /ask     {prompt, context?}   -> {response}                      (auth)
  GET  /diagnostics?file=PATH        -> {file, diagnostics:[...]}       (auth)

Auth is a per-session bearer token (printed when the bridge starts), and it binds
to 127.0.0.1 only, so other local processes can't reach it. /ask is a one-shot,
non-streaming model query that does NOT touch conversation history or edit files —
safe to call while the CLI is in use.
"""

import json
import logging
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

_logger = logging.getLogger("kodiqa")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        tok = self.server.token
        return (self.headers.get("Authorization", "") == f"Bearer {tok}"
                or self.headers.get("X-Kodiqa-Token", "") == tok)

    def do_GET(self):
        u = urlparse(self.path)
        agent = self.server.agent
        if u.path == "/health":
            return self._send(200, {"status": "ok", "model": agent.model, "version": self.server.version})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if u.path == "/diagnostics":
            f = (parse_qs(u.query).get("file") or [""])[0]
            diags = []
            client = getattr(agent, "_lsp_client", None)
            if client and f:
                try:
                    diags = client.diagnostics(f)
                except Exception:
                    _logger.debug("ignored error in bridge diagnostics", exc_info=True)
            return self._send(200, {"file": f, "diagnostics": diags})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if urlparse(self.path).path != "/ask":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or "{}")
        except Exception:
            return self._send(400, {"error": "invalid JSON"})
        prompt = (req.get("prompt") or "").strip()
        if not prompt:
            return self._send(400, {"error": "prompt is required"})
        ctx = req.get("context") or ""
        full = f"Context:\n```\n{ctx}\n```\n\n{prompt}" if ctx else prompt
        with self.server.lock:  # serialize model calls
            try:
                answer = self.server.agent._ask_oneshot(full)
            except Exception as e:
                return self._send(500, {"error": str(e)})
        return self._send(200, {"response": answer})


class KodiqaBridge:
    """Runs the bridge HTTP server in a daemon thread."""

    def __init__(self, agent, port=0, token=None):
        self.agent = agent
        self.port = port
        # Stable token via KODIQA_BRIDGE_TOKEN (handy for editor configs / demos),
        # else a fresh random one each session.
        self.token = token or os.environ.get("KODIQA_BRIDGE_TOKEN") or secrets.token_urlsafe(24)
        self.httpd = None

    def start(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        self.httpd.agent = self.agent
        self.httpd.token = self.token
        self.httpd.lock = threading.Lock()
        try:
            from config import CHANGELOG
            self.httpd.version = CHANGELOG[0]["version"]
        except Exception:
            self.httpd.version = "?"
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.port

    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                _logger.debug("ignored error stopping bridge", exc_info=True)
            self.httpd = None
