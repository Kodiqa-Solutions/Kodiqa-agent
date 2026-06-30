"""Tests for the reasoning-effort dial (/effort)."""

from unittest.mock import MagicMock

from kodiqa import Kodiqa


def _agent(effort=None, settings=None):
    k = MagicMock()
    k.reasoning_effort = effort
    k.settings = settings if settings is not None else {}
    k.model = "gpt-4o"
    k._get_openai_tools = lambda: []
    return k


class TestEffortInRequestBody:
    def _body(self, model, provider, effort):
        k = _agent(effort=effort)
        k.model = model
        return Kodiqa._build_openai_request_body(k, [], provider)

    def test_openai_reasoning_model_gets_effort(self):
        body = self._body("o3", "openai", "high")
        assert body.get("reasoning_effort") == "high"

    def test_gpt5_gets_effort(self):
        body = self._body("gpt-5", "openai", "low")
        assert body.get("reasoning_effort") == "low"

    def test_non_reasoning_openai_model_skipped(self):
        # gpt-4o would 400 on reasoning_effort — must NOT be sent.
        body = self._body("gpt-4o", "openai", "high")
        assert "reasoning_effort" not in body

    def test_openrouter_uses_unified_reasoning(self):
        body = self._body("openai/o3", "openrouter", "medium")
        assert body.get("reasoning") == {"effort": "medium"}

    def test_other_providers_skipped(self):
        for prov, model in [("deepseek", "deepseek-chat"), ("qwen", "qwen3-max"),
                            ("groq", "llama-3.3-70b-versatile"), ("mistral", "mistral-large-latest")]:
            body = self._body(model, prov, "high")
            assert "reasoning_effort" not in body and "reasoning" not in body

    def test_effort_off_sends_nothing(self):
        body = self._body("o3", "openai", None)
        assert "reasoning_effort" not in body


class TestEffortCommand:
    def test_set_levels_persist(self, monkeypatch):
        import kodiqa
        saved = {}
        monkeypatch.setattr(kodiqa, "save_settings", lambda s: saved.update(s))
        k = MagicMock()
        k.settings = {}
        for level in ("low", "medium", "high"):
            Kodiqa._cmd_effort(k, level)
            assert k.reasoning_effort == level
            assert k.settings["reasoning_effort"] == level

    def test_off_clears(self, monkeypatch):
        import kodiqa
        monkeypatch.setattr(kodiqa, "save_settings", lambda s: None)
        k = MagicMock()
        k.settings = {"reasoning_effort": "high"}
        Kodiqa._cmd_effort(k, "off")
        assert k.reasoning_effort is None
        assert "reasoning_effort" not in k.settings

    def test_registered(self):
        assert "/effort" in Kodiqa._COMMAND_HANDLERS
