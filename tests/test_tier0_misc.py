"""Tests for the Tier-0 UX fixes: repomap symbol kind, search persistence, voice preflight."""

from unittest.mock import MagicMock

import kodiqa
from kodiqa import Kodiqa


class TestRepomapSymbolKind:
    def _rm(self, tmp_path):
        from repomap import RepoMap
        return RepoMap(str(tmp_path))

    def test_java_kind_is_keyword_not_modifier(self, tmp_path):
        f = tmp_path / "A.java"
        f.write_text(
            "public class Foo {\n"
            "  public static void main(String[] args) {}\n"
            "}\n"
        )
        syms = self._rm(tmp_path)._extract_symbols_regex(str(f), "java")
        by = {s["name"]: s["kind"] for s in syms}
        assert by.get("Foo") == "class"      # not "public"
        assert by.get("main") == "void"      # not "public" / "static"

    def test_python_kind(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("def foo():\n    pass\nclass Bar:\n    pass\n")
        syms = self._rm(tmp_path)._extract_symbols_regex(str(f), "python")
        by = {s["name"]: s["kind"] for s in syms}
        assert by.get("foo") == "def" and by.get("Bar") == "class"


class TestSearchEnginePersistence:
    def test_persists_and_sets(self, monkeypatch):
        applied = {}
        monkeypatch.setattr(kodiqa, "save_settings", lambda s: None)
        monkeypatch.setattr(kodiqa, "set_search_engine", lambda e: applied.__setitem__("engine", e))
        k = MagicMock()
        k.settings = {}
        Kodiqa._set_search_engine_persistent(k, "google")
        assert k.settings["search_engine"] == "google"
        assert applied["engine"] == "google"


class TestVoicePreflight:
    def test_no_openai_key_does_not_record(self, monkeypatch):
        called = {"ran": False}
        monkeypatch.setattr(kodiqa.subprocess, "run",
                            lambda *a, **kw: called.__setitem__("ran", True))
        k = MagicMock()
        k.api_keys = {}
        k.console = MagicMock()
        Kodiqa._handle_voice(k, "")
        assert called["ran"] is False  # bailed before invoking sox
