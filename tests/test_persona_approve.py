"""Tests for 1.6: sticky model per persona + per-category auto-approve."""

from unittest.mock import MagicMock

import kodiqa
from kodiqa import Kodiqa


class TestPerCategoryAutoApprove:
    def _agent(self, approved=()):
        k = MagicMock()
        k.permission_mode = "default"
        k.auto_approve = set(approved)
        k._APPROVE_CATEGORIES = Kodiqa._APPROVE_CATEGORIES
        k._action_category = lambda at: Kodiqa._action_category(k, at)
        return k

    def test_category_mapping(self):
        k = self._agent()
        cat = lambda d: Kodiqa._action_category(k, d)
        assert cat("run command") == "command"
        assert cat("git commit") == "command"
        assert cat("delete file") == "delete"
        assert cat("copy to clipboard") == "clipboard"
        assert cat("write file") == "write"
        assert cat("edit file") == "write"

    def test_auto_approves_enabled_category(self):
        k = self._agent(approved={"write"})
        assert Kodiqa._confirm(k, "Write file: /tmp/x") is True
        # a category that's NOT enabled still needs confirmation (arrow_select)
        k._arrow_select = MagicMock(return_value=2)  # "No"
        assert Kodiqa._confirm(k, "Run command: rm x") is False

    def test_disabled_category_not_auto_approved(self):
        k = self._agent(approved=set())
        k._arrow_select = MagicMock(return_value=0)  # "Yes"
        assert Kodiqa._confirm(k, "Write file: /tmp/x") is True
        k._arrow_select.assert_called_once()  # it had to prompt

    def test_command_set_persists(self, monkeypatch):
        monkeypatch.setattr(kodiqa, "save_settings", lambda s: None)
        k = MagicMock()
        k._APPROVE_CATEGORIES = Kodiqa._APPROVE_CATEGORIES
        k.auto_approve = set()
        k.settings = {}
        Kodiqa._cmd_approve(k, "write on")
        assert "write" in k.auto_approve and k.settings["auto_approve"] == ["write"]
        Kodiqa._cmd_approve(k, "write off")
        assert "write" not in k.auto_approve

    def test_registered(self):
        assert "/approve" in Kodiqa._COMMAND_HANDLERS


class TestStickyPersonaModel:
    def _agent(self, settings=None, model="claude-sonnet-4-6"):
        k = MagicMock()
        k.settings = settings if settings is not None else {}
        k.model = model
        k._persona = None
        k._resolve_model_name = lambda a: {"opus": "claude-opus-4-6", "gpt": "gpt-4o"}.get(a, a)
        k._cmd_model = MagicMock()
        return k

    def test_switch_persona_no_binding_does_not_switch_model(self, monkeypatch):
        monkeypatch.setattr(kodiqa, "save_settings", lambda s: None)
        k = self._agent()
        Kodiqa._cmd_persona(k, "architect")
        assert k._persona == "architect"
        k._cmd_model.assert_not_called()

    def test_bind_model_persists_and_switches(self, monkeypatch):
        monkeypatch.setattr(kodiqa, "save_settings", lambda s: None)
        k = self._agent(model="gpt-4o")
        Kodiqa._cmd_persona(k, "architect opus")
        assert k.settings["persona_models"]["architect"] == "opus"
        k._cmd_model.assert_called_once_with("opus")

    def test_existing_binding_auto_switches_on_plain_switch(self, monkeypatch):
        monkeypatch.setattr(kodiqa, "save_settings", lambda s: None)
        k = self._agent(settings={"persona_models": {"architect": "opus"}}, model="gpt-4o")
        Kodiqa._cmd_persona(k, "architect")
        k._cmd_model.assert_called_once_with("opus")

    def test_no_switch_when_already_on_bound_model(self, monkeypatch):
        monkeypatch.setattr(kodiqa, "save_settings", lambda s: None)
        k = self._agent(settings={"persona_models": {"architect": "opus"}},
                        model="claude-opus-4-6")  # already the bound model
        Kodiqa._cmd_persona(k, "architect")
        k._cmd_model.assert_not_called()
