"""Regression tests for the P2 resilience fixes."""
import json
import threading
import time
from unittest.mock import MagicMock

from kodiqa import Kodiqa
from mcp import MCPServer


# ── P2.1: quit-time summary is best-effort (gated + hard timeout) ────────────
class TestExitSummary:
    def test_skipped_when_disabled(self):
        k = MagicMock()
        k.config = {"summarize_on_exit": False}
        k.history = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
        Kodiqa._save_session_summary(k)
        k._generate_exit_summary.assert_not_called()

    def test_timeout_returns_empty_without_blocking(self):
        k = MagicMock()
        k.model = "deepseek-chat"          # non-Claude → uses the openai-compat path
        k.history = [{"role": "user", "content": "a"}]
        k._get_provider_for_model = lambda m: "deepseek"
        k._openai_compat_nostream = lambda prompt, hist: (time.sleep(3), "late")[1]
        start = time.time()
        out = Kodiqa._generate_exit_summary(k, timeout=0.5)
        assert out == "" and time.time() - start < 2  # didn't wait for the 3s worker


# ── P2.2: MCP reader times out cleanly (persistent reader, no thread leak) ────
class TestMcpReaderTimeout:
    def test_readline_timeout_returns_none(self):
        s = MCPServer("t", "cmd")
        s.process = MagicMock()
        gate = threading.Event()
        s.process.stdout.readline = lambda: (gate.wait(), "")[1]  # blocks until released
        try:
            assert s._readline_timeout(timeout=0.3) is None
        finally:
            gate.set()  # let the single reader unblock and exit


# ── P2.3: MemoryStore tolerates concurrent access ───────────────────────────
class TestMemoryConcurrency:
    def test_parallel_store_and_search(self, memory_store):
        errors = []

        def work(i):
            try:
                for _ in range(25):
                    memory_store.store(f"note {i}", "tag")
                    memory_store.search("note")
            except Exception as e:  # a shared unlocked sqlite conn would raise here
                errors.append(e)

        ts = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors


# ── P2.4: image base64 is not dumped as text on provider switch ──────────────
class TestImageBase64Strip:
    def test_image_tool_result_stripped(self):
        k = MagicMock()
        k._model_supports_vision = lambda: True
        big = "A" * 100_000
        k.history = [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c0", "type": "function",
                             "function": {"name": "read_image", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c0",
             "content": [{"type": "image", "source": {"data": big}}]},
        ]
        blob = json.dumps(Kodiqa._build_openai_messages(k, "SYS"))
        assert big not in blob            # base64 not smuggled into the request
        assert "image omitted" in blob


# ── P2.6: token estimate counts tool_calls args ─────────────────────────────
class TestTokenEstimate:
    def test_counts_openai_tool_call_args(self):
        k = MagicMock()
        k._last_context_tokens = 0
        args = "x" * 4000
        k.history = [{"role": "assistant", "content": None,
                      "tool_calls": [{"function": {"name": "edit", "arguments": args}}]}]
        # 4000 chars/4 = 1000 for the args + ~2000 baseline
        assert Kodiqa._estimate_tokens(k) >= 1000 + 2000

    def test_uses_last_context_when_set(self):
        k = MagicMock()
        k._last_context_tokens = 5000
        assert Kodiqa._estimate_tokens(k) == 5000
