"""Regression tests for the P3 polish fixes."""
import json
from unittest.mock import MagicMock

from kodiqa import Kodiqa, _KEYWORD_ARGS


class TestSaveSettingsNoMutation:
    def test_preserves_disk_keys_without_mutating_caller(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "KODIQA_DIR", str(tmp_path))
        monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.json"))
        (tmp_path / "settings.json").write_text(json.dumps({"deepseek_api_key": "secret"}))
        caller = {"model": "m"}
        config.save_settings(caller)
        assert caller == {"model": "m"}                     # caller dict not mutated
        written = json.load(open(tmp_path / "settings.json"))
        assert written["deepseek_api_key"] == "secret"      # key still preserved on disk
        assert written["model"] == "m"


class TestUndoBufferCap:
    def test_distinct_paths_capped(self):
        import actions
        actions._undo_buffer.clear()
        actions._redo_buffer.clear()
        actions._turn_snapshot.clear()
        for i in range(400):
            actions._push_undo(f"/f/{i}.txt", "x")
        assert len(actions._undo_buffer) <= 300          # doesn't grow unbounded
        assert "/f/399.txt" in actions._undo_buffer      # most recent path kept


class TestVoicePreflight:
    def test_requires_rec_binary(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda c: None)  # rec not installed
        k = MagicMock()
        k.api_keys = {"openai": "key"}
        k.console = MagicMock()
        Kodiqa._handle_voice(k, "")
        printed = " ".join(str(c) for c in k.console.print.call_args_list)
        assert "rec" in printed.lower()


class TestCompleterKeywords:
    def test_option_word_commands_have_keywords(self):
        for c in ("/approve", "/effort", "/failover", "/tune", "/toon", "/sandbox", "/mcp"):
            assert c in _KEYWORD_ARGS and _KEYWORD_ARGS[c]
        assert set(_KEYWORD_ARGS["/effort"]) == {"off", "low", "medium", "high"}
