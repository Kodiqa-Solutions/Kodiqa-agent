"""Tests for SessionStore — conversation persistence extracted from Kodiqa (STEP 3)."""

import json
import os
from unittest.mock import MagicMock

import session_store
from session_store import SessionStore


def _agent(tmp_path):
    agent = MagicMock()
    agent.session_file = str(tmp_path / "session.json")
    agent.model = "m"
    agent.cwd = str(tmp_path)
    agent.history = []
    agent._auto_resume = False
    agent.session_tokens = {"cost": 0.0}
    agent._session_stats = {"tools_used": {}}
    return agent


class TestSaveLoad:
    def test_save_writes_history(self, tmp_path):
        agent = _agent(tmp_path)
        agent.history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        SessionStore(agent).save()
        data = json.loads(open(agent.session_file).read())
        assert data["model"] == "m"
        assert len(data["history"]) == 2

    def test_save_trims_trailing_unresolved_toolcall(self, tmp_path):
        agent = _agent(tmp_path)
        agent.history = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            # trailing assistant issuing a tool call with no result → must be trimmed
            {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "x", "input": {}}]},
        ]
        SessionStore(agent).save()
        data = json.loads(open(agent.session_file).read())
        assert len(data["history"]) == 2
        assert data["history"][-1]["content"] == "b"

    def test_auto_resume_restores_without_prompt(self, tmp_path):
        agent = _agent(tmp_path)
        agent._auto_resume = True
        hist = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
        open(agent.session_file, "w").write(json.dumps({"model": "claude", "cwd": "/nope", "history": hist}))
        SessionStore(agent).load()
        assert agent.history == hist
        assert agent.model == "claude"

    def test_load_ignores_too_short(self, tmp_path):
        agent = _agent(tmp_path)
        agent._auto_resume = True
        open(agent.session_file, "w").write(json.dumps({"history": [{"role": "user", "content": "x"}]}))
        SessionStore(agent).load()
        # < 2 messages → file removed, nothing restored
        assert not os.path.isfile(agent.session_file)

    def test_clear_removes_file(self, tmp_path):
        agent = _agent(tmp_path)
        open(agent.session_file, "w").write("{}")
        SessionStore(agent).clear()
        assert not os.path.isfile(agent.session_file)


class TestArchive:
    def test_archive_writes_index_and_session(self, tmp_path, monkeypatch):
        monkeypatch.setattr(session_store, "KODIQA_DIR", str(tmp_path))
        agent = _agent(tmp_path)
        agent.history = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ]
        SessionStore(agent).archive()
        index = json.loads(open(tmp_path / "history" / "index.json").read())
        assert len(index) == 1
        assert index[0]["user_messages"] == 2
        assert index[0]["topic"] == "first question"
        assert os.path.isfile(tmp_path / "history" / f"session_{index[0]['id']}.json")

    def test_archive_skips_short_sessions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(session_store, "KODIQA_DIR", str(tmp_path))
        agent = _agent(tmp_path)
        agent.history = [{"role": "user", "content": "only one"}]
        SessionStore(agent).archive()
        assert not os.path.isdir(tmp_path / "history")


class TestRoundTrip:
    def test_save_then_load_round_trip(self, tmp_path):
        agent = _agent(tmp_path)
        agent._auto_resume = True
        agent.history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
        SessionStore(agent).save()
        # fresh agent loads what we saved
        agent2 = _agent(tmp_path)
        agent2._auto_resume = True
        SessionStore(agent2).load()
        assert agent2.history == agent.history
