"""Tests for TOON encoding (toon.py) + the /toon toggle."""

import json
from unittest.mock import MagicMock

import kodiqa
from kodiqa import Kodiqa
from toon import to_toon, maybe_toon


class TestEncode:
    def test_uniform_array_is_tabular(self):
        obj = {"users": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]}
        assert to_toon(obj) == "users[2]{id,name}:\n  1,Alice\n  2,Bob"

    def test_scalar_array_inline(self):
        assert to_toon({"tags": ["a", "b", "c"]}) == "tags[3]: a,b,c"

    def test_nested_object(self):
        assert to_toon({"meta": {"x": 1, "y": 2}}) == "meta:\n  x: 1\n  y: 2"

    def test_scalar_members(self):
        assert to_toon({"n": 5, "s": "hi", "ok": True, "z": None}) == "n: 5\ns: hi\nok: true\nz: null"

    def test_root_array_tabular(self):
        assert to_toon([{"a": 1}, {"a": 2}]) == "[2]{a}:\n  1\n  2"

    def test_non_uniform_array_falls_back_to_json(self):
        out = to_toon({"items": [1, {"a": 2}]})
        assert out == 'items: [1,{"a":2}]'

    def test_quoting_values_with_commas(self):
        # a value containing the delimiter must be quoted
        assert to_toon({"rows": [{"v": "a,b"}]}) == 'rows[1]{v}:\n  "a,b"'

    def test_numeric_looking_string_quoted(self):
        # preserve string type (don't let "01" read as the number 1)
        assert to_toon({"rows": [{"zip": "01234"}]}) == 'rows[1]{zip}:\n  "01234"'

    def test_accepts_json_string(self):
        assert to_toon('{"tags":["x","y"]}') == "tags[2]: x,y"

    def test_non_json_string_unchanged(self):
        assert to_toon("just text") == "just text"


class TestMaybeToon:
    def test_compacts_large_uniform_array_and_is_shorter(self):
        data = {"rows": [{"id": i, "name": f"user{i}", "active": True} for i in range(25)]}
        js = json.dumps(data)
        out = maybe_toon(js)
        assert out.startswith("# TOON")
        assert len(out) < len(js)          # genuine savings
        assert "{id,name,active}" in out   # header written once

    def test_passes_through_non_json(self):
        assert maybe_toon("hello world") == "hello world"

    def test_never_longer_than_input(self):
        # tiny JSON: TOON + hint would be longer → must return the original untouched
        assert maybe_toon('{"a":1}') == '{"a":1}'

    def test_passes_through_non_string(self):
        assert maybe_toon(None) is None


class TestToonCommand:
    def test_toggle_persists(self, monkeypatch):
        monkeypatch.setattr(kodiqa, "save_settings", lambda s: None)
        k = MagicMock()
        k.settings = {}
        k.toon_enabled = False
        Kodiqa._cmd_toon(k, "on")
        assert k.toon_enabled is True and k.settings["toon"] is True
        Kodiqa._cmd_toon(k, "off")
        assert k.toon_enabled is False and k.settings["toon"] is False

    def test_registered(self):
        assert "/toon" in Kodiqa._COMMAND_HANDLERS
