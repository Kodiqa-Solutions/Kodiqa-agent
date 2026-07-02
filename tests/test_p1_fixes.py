"""Regression tests for the P1 audit fixes."""
import json
import os
from unittest.mock import MagicMock

import kodiqa
from kodiqa import Kodiqa


# ── P1.1: Ollama history is flattened to plain role+string messages ──────────
class TestOllamaHistoryFlatten:
    def test_flattens_mixed_format_history(self):
        k = MagicMock()
        k.history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "name": "read_file", "id": "t1", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file body"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "openai tool result"},
        ]
        msgs = Kodiqa._build_ollama_messages(k, "SYS")
        assert msgs[0] == {"role": "system", "content": "SYS"}
        for m in msgs[1:]:
            assert isinstance(m["content"], str)           # never a list Ollama can't parse
            assert m["role"] in ("user", "assistant")      # no bare role:"tool"
        blob = " ".join(m["content"] for m in msgs)
        assert "ok" in blob and "file body" in blob and "openai tool result" in blob
        assert "called tool read_file" in blob

    def test_preserves_images_on_user_turn(self):
        k = MagicMock()
        k.history = [{"role": "user", "content": "see this", "images": ["b64data"]}]
        msgs = Kodiqa._build_ollama_messages(k, "SYS")
        assert msgs[1]["images"] == ["b64data"]

    def test_plain_history_passthrough(self):
        k = MagicMock()
        k.history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        msgs = Kodiqa._build_ollama_messages(k, "SYS")
        assert [m["content"] for m in msgs] == ["SYS", "a", "b"]


# ── P1.2: /compact nostream drops orphan tool messages (no 400) ──────────────
class TestNostreamDropsToolMessages:
    def test_no_tool_role_in_request(self, monkeypatch):
        captured = {}

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"choices": [{"message": {"content": "summary"}}]}

        monkeypatch.setattr(kodiqa.requests, "post",
                            lambda url, headers=None, json=None, timeout=None:
                            (captured.__setitem__("body", json) or FakeResp()))
        k = MagicMock()
        k._get_provider_for_model = lambda m: "deepseek"
        k.api_keys = {"deepseek": "key"}
        k.model = "deepseek-chat"
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},  # no str content
            {"role": "tool", "tool_call_id": "c1", "content": "result"},           # must be dropped
            {"role": "assistant", "content": "done"},
        ]
        out = Kodiqa._openai_compat_nostream(k, "SYS", messages, "deepseek")
        roles = [m["role"] for m in captured["body"]["messages"]]
        assert "tool" not in roles          # the orphan tool message is gone
        assert roles[0] == "system"
        assert out == "summary"


# ── P1.3: atomic JSON writes ─────────────────────────────────────────────────
class TestAtomicWrite:
    def test_writes_valid_json_no_tmp_left(self, tmp_path):
        from session_store import SessionStore
        p = str(tmp_path / "s.json")
        SessionStore._atomic_write_json(p, {"a": 1, "b": [2, 3]})
        assert json.load(open(p)) == {"a": 1, "b": [2, 3]}
        assert not os.path.exists(p + ".tmp")   # replaced into place, temp cleaned up


# ── P1.4: history-index IDs are monotonic (no collision after 100) ───────────
class TestMonotonicHistoryId:
    def _agent(self, tmp_path):
        a = MagicMock()
        a.history = [{"role": "user", "content": "a"},
                     {"role": "assistant", "content": "b"},
                     {"role": "user", "content": "c"}]
        a.model = "m"
        a.cwd = str(tmp_path)
        a.session_tokens = {"cost": 0}
        a._session_stats = {"tools_used": {}}
        return a

    def test_no_collision_after_trim(self, tmp_path, monkeypatch):
        import session_store
        monkeypatch.setattr(session_store, "KODIQA_DIR", str(tmp_path))
        hist = tmp_path / "history"
        hist.mkdir()
        (hist / "index.json").write_text(json.dumps(
            [{"id": i, "messages": 2, "user_messages": 2} for i in range(1, 101)]))
        st = session_store.SessionStore(self._agent(tmp_path))

        st.archive()
        idx = json.loads((hist / "index.json").read_text())
        assert idx[-1]["id"] == 101 and (hist / "session_101.json").exists()

        st.archive()   # must NOT reuse 101
        idx = json.loads((hist / "index.json").read_text())
        assert idx[-1]["id"] == 102 and (hist / "session_102.json").exists()
        assert (hist / "session_101.json").exists()  # earlier session not clobbered
