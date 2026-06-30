"""Tests for the LSP client framing + diagnostics capture (lsp.py).

A fake language server is driven over a real OS pipe so we exercise the actual
Content-Length framing and the select/os.read message reader.
"""

import json
import os
import threading
import time

from lsp import LSPClient


def _frame(obj):
    body = json.dumps(obj).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body


class _FakeProc:
    """Stands in for subprocess.Popen: stdout is a real pipe we feed framed bytes."""
    def __init__(self):
        self._r, self._w = os.pipe()
        self.stdout = os.fdopen(self._r, "rb", buffering=0)
        self.stdin = open(os.devnull, "wb")

    def feed(self, raw):
        os.write(self._w, raw)

    def close(self):
        try:
            os.close(self._w)
        except OSError:
            pass


class TestReadMessage:
    def _client(self):
        c = LSPClient()
        c.process = _FakeProc()
        c.language = "python"
        return c

    def test_reads_one_framed_message(self):
        c = self._client()
        c.process.feed(_frame({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}))
        msg = c._read_message(time.time() + 2)
        assert msg == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        c.process.close()

    def test_timeout_returns_none_without_blocking(self):
        c = self._client()
        start = time.time()
        msg = c._read_message(time.time() + 0.3)  # nothing fed
        assert msg is None
        assert time.time() - start < 2  # honored the deadline, didn't hang
        c.process.close()

    def test_response_skips_notifications(self):
        c = self._client()
        # a notification (no id) arrives before our actual response
        c.process.feed(_frame({"jsonrpc": "2.0", "method": "window/logMessage", "params": {}}))
        c.process.feed(_frame({"jsonrpc": "2.0", "id": 5, "result": "answer"}))
        assert c._read_response(5, timeout=2) == "answer"
        c.process.close()


class TestDiagnostics:
    def test_captures_publish_diagnostics(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("x=1\n")
        uri = f"file://{os.path.abspath(str(f))}"
        c = LSPClient()
        c.process = _FakeProc()
        c.language = "python"

        # Simulate the server publishing diagnostics shortly after didOpen.
        diag = {"range": {"start": {"line": 0, "character": 0}}, "severity": 2, "message": "unused"}

        def server():
            time.sleep(0.05)
            # an unrelated notification first, then the matching publish
            c.process.feed(_frame({"jsonrpc": "2.0", "method": "window/logMessage", "params": {}}))
            c.process.feed(_frame({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                                   "params": {"uri": uri, "diagnostics": [diag]}}))
        threading.Thread(target=server, daemon=True).start()

        diags = c.diagnostics(str(f), timeout=3)
        assert diags == [diag]
        c.process.close()

    def test_returns_empty_when_none_published(self, tmp_path):
        f = tmp_path / "y.py"
        f.write_text("ok\n")
        c = LSPClient()
        c.process = _FakeProc()
        c.language = "python"
        # server stays silent → empty list, no hang
        diags = c.diagnostics(str(f), timeout=0.4)
        assert diags == []
        c.process.close()

    def test_not_running_returns_empty(self):
        c = LSPClient()
        assert c.diagnostics("/whatever.py") == []
