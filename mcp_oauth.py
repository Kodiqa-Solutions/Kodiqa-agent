"""OAuth 2.1 for remote MCP servers (refactor STEP / Remote MCP Phase 2).

Implements the MCP authorization flow:
  discovery → dynamic client registration → authorization-code + PKCE (browser)
  OR client-credentials (machine-to-machine) → token exchange → cache + refresh.

The network/crypto pieces are plain functions so they're unit-testable; only the
final browser round-trip (webbrowser.open + the user logging in) is interactive.
Tokens are cached under ~/.kodiqa/oauth/ keyed by server URL and refreshed
automatically when they near expiry or a request returns 401.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from config import KODIQA_DIR

_logger = logging.getLogger("kodiqa")
OAUTH_DIR = os.path.join(KODIQA_DIR, "oauth")


# ── small helpers ──

def _origin(url):
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def gen_pkce():
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _cache_path(server_url):
    h = hashlib.sha256(server_url.encode()).hexdigest()[:16]
    return os.path.join(OAUTH_DIR, f"{h}.json")


def discover(server_url):
    """Discover the authorization server + its endpoints for an MCP server URL.
    Returns the AS metadata dict (authorization_endpoint/token_endpoint/…) or None."""
    base = _origin(server_url)
    as_url = base
    try:
        r = requests.get(base + "/.well-known/oauth-protected-resource", timeout=10)
        if r.ok:
            servers = r.json().get("authorization_servers") or []
            if servers:
                as_url = servers[0]
    except Exception:
        _logger.debug("ignored error in discover (protected-resource)", exc_info=True)
    for suffix in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        try:
            r = requests.get(as_url.rstrip("/") + suffix, timeout=10)
            if r.ok:
                meta = r.json()
                if meta.get("authorization_endpoint") and meta.get("token_endpoint"):
                    return meta
        except Exception:
            _logger.debug("ignored error in discover (as-metadata)", exc_info=True)
    return None


def register_client(registration_endpoint, redirect_uri, client_name="Kodiqa"):
    """Dynamic Client Registration (RFC 7591). Returns (client_id, client_secret)."""
    body = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    try:
        r = requests.post(registration_endpoint, json=body, timeout=15)
        if r.ok:
            d = r.json()
            return d.get("client_id"), d.get("client_secret")
    except Exception:
        _logger.debug("ignored error in register_client", exc_info=True)
    return None, None


def _start_callback_server(result):
    """Start a localhost callback server; returns (redirect_uri, httpd). The first
    GET fills `result` with the code/state/error from the query string."""
    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.setdefault("code", q.get("code", [None])[0])
            result.setdefault("state", q.get("state", [None])[0])
            result.setdefault("error", q.get("error", [None])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body style='font-family:sans-serif'>"
                             b"<h2>Kodiqa &mdash; authentication complete.</h2>"
                             b"You can close this tab and return to the terminal.</body></html>")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}/callback", httpd


def exchange_code(token_endpoint, code, verifier, client_id, client_secret, redirect_uri, resource=None):
    """Exchange an authorization code for tokens. Returns the token dict or None."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret
    if resource:
        data["resource"] = resource
    try:
        r = requests.post(token_endpoint, data=data, timeout=15)
        if r.ok:
            return r.json()
        _logger.warning(f"OAuth token exchange failed: {r.status_code} {r.text[:200]}")
    except Exception:
        _logger.debug("ignored error in exchange_code", exc_info=True)
    return None


class OAuthSession:
    """Holds tokens for one remote MCP server and produces an auth header, handling
    discovery, registration, the chosen grant, caching, and refresh."""

    def __init__(self, server_url, client_id=None, client_secret=None, scope=None):
        self.server_url = server_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.tokens = {}     # access_token / refresh_token / expires_at / token_endpoint
        self._load()

    # ---- persistence ----
    def _load(self):
        try:
            p = _cache_path(self.server_url)
            if os.path.isfile(p):
                d = json.load(open(p))
                self.tokens = d.get("tokens", {})
                self.client_id = self.client_id or d.get("client_id")
                self.client_secret = self.client_secret or d.get("client_secret")
        except Exception:
            _logger.debug("ignored error in OAuthSession._load", exc_info=True)

    def _save(self):
        try:
            os.makedirs(OAUTH_DIR, exist_ok=True)
            p = _cache_path(self.server_url)
            with open(p, "w") as f:
                json.dump({"tokens": self.tokens, "client_id": self.client_id,
                           "client_secret": self.client_secret}, f)
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
        except Exception:
            _logger.debug("ignored error in OAuthSession._save", exc_info=True)

    def _store(self, tok, token_endpoint):
        self.tokens = {
            "access_token": tok.get("access_token"),
            "refresh_token": tok.get("refresh_token"),
            "expires_at": time.time() + tok.get("expires_in", 3600),
            "token_endpoint": token_endpoint,
        }
        self._save()

    # ---- token use ----
    def has_token(self):
        return bool(self.tokens.get("access_token"))

    def auth_header(self):
        if not self.has_token():
            return {}
        self._refresh_if_needed()
        return {"Authorization": "Bearer " + self.tokens["access_token"]}

    def _refresh_if_needed(self):
        exp = self.tokens.get("expires_at", 0)
        if exp and time.time() > exp - 30:
            self.refresh()

    def refresh(self):
        """Refresh the access token. Returns True on success."""
        rt = self.tokens.get("refresh_token")
        te = self.tokens.get("token_endpoint")
        if not rt or not te:
            return False
        data = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": self.client_id}
        if self.client_secret:
            data["client_secret"] = self.client_secret
        try:
            r = requests.post(te, data=data, timeout=15)
            if r.ok:
                tok = r.json()
                # keep the old refresh token if the server doesn't return a new one
                tok.setdefault("refresh_token", rt)
                self._store(tok, te)
                return True
            _logger.warning(f"OAuth refresh failed: {r.status_code}")
        except Exception:
            _logger.debug("ignored error in refresh", exc_info=True)
        return False

    # ---- grants ----
    def authenticate_client_credentials(self):
        """Machine-to-machine grant (no browser). Needs client_id + client_secret."""
        meta = discover(self.server_url)
        if not meta:
            return False
        data = {"grant_type": "client_credentials", "client_id": self.client_id,
                "client_secret": self.client_secret}
        if self.scope:
            data["scope"] = self.scope
        try:
            r = requests.post(meta["token_endpoint"], data=data, timeout=15)
            if r.ok:
                self._store(r.json(), meta["token_endpoint"])
                return self.has_token()
            _logger.warning(f"OAuth client-credentials failed: {r.status_code} {r.text[:200]}")
        except Exception:
            _logger.debug("ignored error in authenticate_client_credentials", exc_info=True)
        return False

    def authenticate_interactive(self, log=None, open_browser=True, timeout=300):
        """Authorization-code + PKCE browser flow. `log` is an optional printer."""
        log = log or (lambda *a, **k: None)
        meta = discover(self.server_url)
        if not meta:
            log("Could not discover the server's OAuth configuration.")
            return False
        result = {}
        redirect_uri, httpd = _start_callback_server(result)
        try:
            if not self.client_id and meta.get("registration_endpoint"):
                self.client_id, self.client_secret = register_client(meta["registration_endpoint"], redirect_uri)
            if not self.client_id:
                log("No OAuth client id (server doesn't support dynamic registration — pass --oauth-client-id).")
                return False
            verifier, challenge = gen_pkce()
            state = secrets.token_urlsafe(16)
            params = {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "resource": self.server_url,
            }
            if self.scope:
                params["scope"] = self.scope
            auth_url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)
            log("Opening your browser to authorize… (complete the login there)")
            log(auth_url)
            if open_browser:
                try:
                    webbrowser.open(auth_url)
                except Exception:
                    _logger.debug("ignored error opening browser", exc_info=True)
            deadline = time.time() + timeout
            while time.time() < deadline and not result.get("code") and not result.get("error"):
                time.sleep(0.3)
        finally:
            httpd.shutdown()
        if result.get("error"):
            log(f"Authorization failed: {result['error']}")
            return False
        if not result.get("code"):
            log("Timed out waiting for authorization.")
            return False
        if result.get("state") != state:
            log("Authorization state mismatch — aborting for safety.")
            return False
        tok = exchange_code(meta["token_endpoint"], result["code"], verifier,
                            self.client_id, self.client_secret, redirect_uri, resource=self.server_url)
        if not tok or not tok.get("access_token"):
            log("Token exchange failed.")
            return False
        self._store(tok, meta["token_endpoint"])
        return True
