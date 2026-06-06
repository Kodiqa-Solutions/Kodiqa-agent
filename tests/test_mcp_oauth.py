"""Tests for the MCP OAuth flow (mcp_oauth.py).

Everything except the live browser is covered: PKCE, discovery, dynamic client
registration, the localhost callback server, token exchange/refresh, the token
cache, both grants, and a deterministic end-to-end of the interactive flow
(real callback server + a fake browser that completes the redirect).
"""

import base64
import hashlib
import json
import time
import urllib.parse

import requests

import mcp_oauth
from mcp_oauth import OAuthSession, gen_pkce
from mcp import MCPHttpServer


class _Resp:
    def __init__(self, body=None, ok=True, status=200):
        self._body = body or {}
        self.ok = ok
        self.status_code = status
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class TestPKCE:
    def test_challenge_is_s256_of_verifier(self):
        v, c = gen_pkce()
        expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        assert c == expected
        assert "=" not in c  # url-safe, unpadded


class TestDiscovery:
    def test_discover_follows_protected_resource(self, monkeypatch):
        def fake_get(url, timeout=None):
            if url.endswith("/.well-known/oauth-protected-resource"):
                return _Resp({"authorization_servers": ["https://auth.example.com"]})
            if url.endswith("/.well-known/oauth-authorization-server"):
                return _Resp({"authorization_endpoint": "https://auth.example.com/authorize",
                              "token_endpoint": "https://auth.example.com/token",
                              "registration_endpoint": "https://auth.example.com/register"})
            return _Resp(ok=False, status=404)
        monkeypatch.setattr(mcp_oauth.requests, "get", fake_get)
        meta = mcp_oauth.discover("https://mcp.example.com/mcp")
        assert meta["token_endpoint"] == "https://auth.example.com/token"

    def test_discover_returns_none_when_missing(self, monkeypatch):
        monkeypatch.setattr(mcp_oauth.requests, "get", lambda *a, **k: _Resp(ok=False, status=404))
        assert mcp_oauth.discover("https://mcp.example.com/mcp") is None


class TestRegisterAndExchange:
    def test_register_client(self, monkeypatch):
        monkeypatch.setattr(mcp_oauth.requests, "post",
                            lambda *a, **k: _Resp({"client_id": "cid-123", "client_secret": "sek"}))
        assert mcp_oauth.register_client("https://a/register", "http://127.0.0.1:9/callback") == ("cid-123", "sek")

    def test_exchange_code(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, timeout=None):
            captured.update(data)
            return _Resp({"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        monkeypatch.setattr(mcp_oauth.requests, "post", fake_post)
        tok = mcp_oauth.exchange_code("https://a/token", "CODE", "VER", "cid", None, "http://cb", resource="https://mcp")
        assert tok["access_token"] == "AT"
        assert captured["grant_type"] == "authorization_code"
        assert captured["code_verifier"] == "VER"
        assert captured["resource"] == "https://mcp"


class TestCallbackServer:
    def test_captures_code_from_redirect(self):
        result = {}
        redirect_uri, httpd = mcp_oauth._start_callback_server(result)
        try:
            requests.get(redirect_uri + "?code=THECODE&state=ST", timeout=5)
            # the handler runs in a thread; give it a moment
            for _ in range(20):
                if result.get("code"):
                    break
                time.sleep(0.05)
            assert result["code"] == "THECODE"
            assert result["state"] == "ST"
        finally:
            httpd.shutdown()


class TestTokenCacheAndRefresh:
    def test_cache_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_oauth, "OAUTH_DIR", str(tmp_path))
        s = OAuthSession("https://mcp.example.com/mcp")
        s._store({"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}, "https://a/token")
        # a fresh session for the same URL loads the cached token
        s2 = OAuthSession("https://mcp.example.com/mcp")
        assert s2.has_token()
        assert s2.tokens["access_token"] == "AT"

    def test_auth_header_refreshes_when_expired(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_oauth, "OAUTH_DIR", str(tmp_path))
        s = OAuthSession("https://mcp.example.com/mcp")
        s.tokens = {"access_token": "OLD", "refresh_token": "RT", "expires_at": time.time() - 1,
                    "token_endpoint": "https://a/token"}
        monkeypatch.setattr(mcp_oauth.requests, "post",
                            lambda *a, **k: _Resp({"access_token": "NEW", "expires_in": 3600}))
        h = s.auth_header()
        assert h["Authorization"] == "Bearer NEW"
        assert s.tokens["refresh_token"] == "RT"  # preserved across refresh

    def test_refresh_returns_false_without_refresh_token(self):
        s = OAuthSession.__new__(OAuthSession)
        s.tokens = {"access_token": "x"}
        s.client_id = None
        s.client_secret = None
        assert s.refresh() is False


class TestGrants:
    def test_client_credentials(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_oauth, "OAUTH_DIR", str(tmp_path))
        monkeypatch.setattr(mcp_oauth, "discover", lambda u: {"token_endpoint": "https://a/token",
                                                              "authorization_endpoint": "https://a/auth"})
        monkeypatch.setattr(mcp_oauth.requests, "post",
                            lambda *a, **k: _Resp({"access_token": "M2M", "expires_in": 3600}))
        s = OAuthSession("https://mcp.example.com/mcp", client_id="cid", client_secret="sek")
        assert s.authenticate_client_credentials() is True
        assert s.auth_header()["Authorization"] == "Bearer M2M"

    def test_interactive_end_to_end(self, tmp_path, monkeypatch):
        """Real callback server + a fake browser that completes the redirect."""
        monkeypatch.setattr(mcp_oauth, "OAUTH_DIR", str(tmp_path))
        monkeypatch.setattr(mcp_oauth, "discover", lambda u: {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token"})
        monkeypatch.setattr(mcp_oauth, "exchange_code",
                            lambda *a, **k: {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})

        def fake_browser(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect, state = q["redirect_uri"][0], q["state"][0]
            requests.get(f"{redirect}?code=CODE&state={state}", timeout=5)
        monkeypatch.setattr(mcp_oauth.webbrowser, "open", fake_browser)

        s = OAuthSession("https://mcp.example.com/mcp", client_id="cid")
        assert s.authenticate_interactive(open_browser=True, timeout=10) is True
        assert s.has_token()


class TestHttpServer401Retry:
    def test_refresh_and_retry_on_401(self, monkeypatch):
        class FakeOAuth:
            def __init__(self):
                self.tok = "old"
                self.refreshed = False

            def auth_header(self):
                return {"Authorization": "Bearer " + self.tok}

            def refresh(self):
                self.refreshed = True
                self.tok = "new"
                return True

        calls = {"n": 0}

        class R:
            def __init__(self, status, body=None, ctype="application/json"):
                self.status_code = status
                self._body = body or {}
                self.headers = {"Content-Type": ctype}
                self.text = ""

            def json(self):
                return self._body

            def iter_lines(self, decode_unicode=False):
                return iter([])

            def close(self):
                pass

        def fake_post(url, json=None, headers=None, timeout=None, stream=None):
            calls["n"] += 1
            if calls["n"] == 1:  # first tool call → expired
                return R(401)
            return R(200, {"jsonrpc": "2.0", "id": json.get("id"),
                           "result": {"content": [{"type": "text", "text": "ok-after-refresh"}]}})

        oauth = FakeOAuth()
        s = MCPHttpServer("r", "http://t/mcp", oauth=oauth)
        monkeypatch.setattr("mcp.requests.post", fake_post)
        out = s.call_tool("echo", {})
        assert out == "ok-after-refresh"
        assert oauth.refreshed is True
