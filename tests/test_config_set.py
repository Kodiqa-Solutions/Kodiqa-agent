"""Setting any config value from the terminal, and surfacing values that are pinned
in config.json but no longer match the default.

config.json is written once as a full snapshot of DEFAULTS, so a value the user never
chose keeps overriding a later default change — that is how max_iterations stayed at
15 after the default became 40 and the agent kept stopping mid-task.
"""

import io
import json
from unittest.mock import MagicMock

import pytest
from rich.console import Console

import config as config_mod
from config import (
    DEFAULTS,
    coerce_config_value,
    load_config,
    pinned_config_values,
    reset_config_keys,
    set_config_value,
)
from kodiqa import Kodiqa


def _console():
    return Console(file=io.StringIO(), force_terminal=False, width=100)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Redirect config.json into a temp dir — never touch the real ~/.kodiqa."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(path))
    monkeypatch.setattr(config_mod, "KODIQA_DIR", str(tmp_path))
    return path


class TestCoerceValue:
    def test_int_key(self):
        assert coerce_config_value("max_iterations", "60") == 60

    def test_int_rejects_junk_and_zero(self):
        with pytest.raises(ValueError, match="whole number"):
            coerce_config_value("max_iterations", "lots")
        with pytest.raises(ValueError, match="greater than 0"):
            coerce_config_value("max_iterations", "0")

    def test_bool_key(self):
        assert coerce_config_value("check_updates", "off") is False
        assert coerce_config_value("check_updates", "true") is True
        with pytest.raises(ValueError, match="true/false"):
            coerce_config_value("check_updates", "maybe")

    def test_list_key_takes_json(self):
        assert coerce_config_value("skip_dirs", '["a","b"]') == ["a", "b"]
        with pytest.raises(ValueError, match="JSON"):
            coerce_config_value("skip_dirs", "a,b")


class TestSetAndReset:
    def test_set_persists_and_load_config_sees_it(self, cfg):
        set_config_value("max_iterations", 60)
        assert json.loads(cfg.read_text())["max_iterations"] == 60
        assert load_config()["max_iterations"] == 60

    def test_set_backs_up_an_existing_file(self, cfg):
        cfg.write_text(json.dumps({"max_iterations": 15}))
        set_config_value("max_iterations", 60)
        assert json.loads((cfg.parent / "config.json.bak").read_text())["max_iterations"] == 15

    def test_pinned_reports_only_real_drift(self, cfg):
        cfg.write_text(json.dumps({
            "max_iterations": 15,                       # differs from the default
            "command_timeout": DEFAULTS["command_timeout"],  # matches → not drift
            "my_own_key": "keep",                       # not a known key → ignored
        }))
        pinned = pinned_config_values()
        assert set(pinned) == {"max_iterations"}
        assert pinned["max_iterations"] == (15, DEFAULTS["max_iterations"])

    def test_pinned_is_empty_without_a_file(self, cfg):
        assert pinned_config_values() == {}

    def test_reset_removes_the_key_so_the_default_applies(self, cfg):
        cfg.write_text(json.dumps({"max_iterations": 15, "command_timeout": 999}))
        assert reset_config_keys(["max_iterations"]) == ["max_iterations"]
        assert "max_iterations" not in json.loads(cfg.read_text())
        assert load_config()["max_iterations"] == DEFAULTS["max_iterations"]
        assert load_config()["command_timeout"] == 999  # untouched

    def test_reset_of_an_absent_key_is_a_no_op(self, cfg):
        cfg.write_text(json.dumps({"command_timeout": 999}))
        assert reset_config_keys(["max_iterations"]) == []


class TestConfigCommand:
    def _agent(self):
        k = MagicMock()
        k.console = _console()
        k.config = dict(DEFAULTS)
        k._reload_config = lambda: k.config.update(load_config())
        return k

    def test_set_applies_immediately(self, cfg):
        k = self._agent()
        Kodiqa._set_config_value(k, ["max_iterations", "60"])
        assert k.config["max_iterations"] == 60
        assert "60" in k.console.file.getvalue()

    def test_unknown_key_is_rejected_with_a_hint(self, cfg):
        k = self._agent()
        Kodiqa._set_config_value(k, ["max_iteration", "60"])
        out = k.console.file.getvalue()
        assert "Unknown config key" in out and "max_iterations" in out
        assert not cfg.exists()  # nothing written

    def test_bad_value_is_rejected_without_writing(self, cfg):
        k = self._agent()
        Kodiqa._set_config_value(k, ["max_iterations", "banana"])
        assert "whole number" in k.console.file.getvalue()
        assert not cfg.exists()

    def test_usage_when_arguments_are_missing(self, cfg):
        k = self._agent()
        Kodiqa._set_config_value(k, ["max_iterations"])
        assert "Usage:" in k.console.file.getvalue()

    def test_add_and_remove_a_list_entry(self, cfg):
        k = self._agent()
        Kodiqa._edit_config_list(k, "add", ["skip_dirs", ".next"])
        assert ".next" in load_config()["skip_dirs"]
        Kodiqa._edit_config_list(k, "remove", ["skip_dirs", ".next"])
        assert ".next" not in load_config()["skip_dirs"]

    def test_add_rejects_a_non_list_key(self, cfg):
        k = self._agent()
        Kodiqa._edit_config_list(k, "add", ["max_iterations", "5"])
        assert "not a list setting" in k.console.file.getvalue()

    def test_reset_via_the_command(self, cfg):
        cfg.write_text(json.dumps({"max_iterations": 15}))
        k = self._agent()
        Kodiqa._reset_config_values(k, [])
        assert k.config["max_iterations"] == DEFAULTS["max_iterations"]

    def test_reset_reports_a_key_that_is_not_pinned(self, cfg):
        cfg.write_text(json.dumps({"max_iterations": 15}))
        k = self._agent()
        Kodiqa._reset_config_values(k, ["command_timeout"])
        assert "not pinned" in k.console.file.getvalue()
