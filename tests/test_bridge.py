"""Tests for the editor/IDE bridge HTTP server (bridge.py)."""

from unittest.mock import MagicMock

import requests

from bridge import KodiqaBridge


def _bridge(agent):
    b = KodiqaBridge(agent, port=0)
    b.start()
    return b


def _agent():
    a = MagicMock()
    a.model = "claude-sonnet-4-6"
    a._ask_oneshot.return_value = "ANSWER"
    a._lsp_client = None
    return a


class TestHealth:
    def test_health_no_auth(self):
        b = _bridge(_agent())
        try:
            r = requests.get(b.url() + "/health", timeout=5)
            assert r.status_code == 200
            d = r.json()
            assert d["status"] == "ok" and d["model"] == "claude-sonnet-4-6" and "version" in d
        finally:
            b.stop()


class TestAuth:
    def test_ask_requires_auth(self):
        b = _bridge(_agent())
        try:
            r = requests.post(b.url() + "/ask", json={"prompt": "hi"}, timeout=5)
            assert r.status_code == 401
        finally:
            b.stop()

    def test_diagnostics_requires_auth(self):
        b = _bridge(_agent())
        try:
            r = requests.get(b.url() + "/diagnostics?file=x.py", timeout=5)
            assert r.status_code == 401
        finally:
            b.stop()


class TestAsk:
    def _hdr(self, b):
        return {"Authorization": f"Bearer {b.token}"}

    def test_ask_returns_response(self):
        agent = _agent()
        b = _bridge(agent)
        try:
            r = requests.post(b.url() + "/ask", json={"prompt": "explain this"},
                              headers=self._hdr(b), timeout=5)
            assert r.status_code == 200 and r.json()["response"] == "ANSWER"
            agent._ask_oneshot.assert_called_once()
        finally:
            b.stop()

    def test_ask_includes_context(self):
        agent = _agent()
        b = _bridge(agent)
        try:
            requests.post(b.url() + "/ask", json={"prompt": "fix", "context": "def f(): pass"},
                          headers=self._hdr(b), timeout=5)
            sent = agent._ask_oneshot.call_args[0][0]
            assert "def f(): pass" in sent and "fix" in sent
        finally:
            b.stop()

    def test_ask_missing_prompt(self):
        b = _bridge(_agent())
        try:
            r = requests.post(b.url() + "/ask", json={}, headers=self._hdr(b), timeout=5)
            assert r.status_code == 400
        finally:
            b.stop()


class TestDiagnostics:
    def test_returns_lsp_diagnostics(self):
        agent = _agent()
        agent._lsp_client = MagicMock()
        agent._lsp_client.diagnostics.return_value = [{"line": 3, "message": "unused var"}]
        b = _bridge(agent)
        try:
            r = requests.get(b.url() + "/diagnostics?file=app.py",
                             headers={"Authorization": f"Bearer {b.token}"}, timeout=5)
            assert r.status_code == 200
            assert r.json()["diagnostics"] == [{"line": 3, "message": "unused var"}]
        finally:
            b.stop()

    def test_no_lsp_returns_empty(self):
        b = _bridge(_agent())  # _lsp_client is None
        try:
            r = requests.get(b.url() + "/diagnostics?file=app.py",
                             headers={"Authorization": f"Bearer {b.token}"}, timeout=5)
            assert r.status_code == 200 and r.json()["diagnostics"] == []
        finally:
            b.stop()


class TestUnknown:
    def test_unknown_path(self):
        b = _bridge(_agent())
        try:
            r = requests.get(b.url() + "/nope", headers={"Authorization": f"Bearer {b.token}"}, timeout=5)
            assert r.status_code == 404
        finally:
            b.stop()
