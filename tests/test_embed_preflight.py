"""Tests for /embed + /rag embedding-model preflight."""

from unittest.mock import MagicMock

from kodiqa import Kodiqa


class TestEnsureEmbedModel:
    def test_true_when_installed(self):
        k = MagicMock()
        k._installed_ollama_models.return_value = [("nomic-embed-text:latest", "270MB")]
        assert Kodiqa._ensure_embed_model(k) is True
        k._pull_model.assert_not_called()

    def test_false_when_ollama_unreachable(self):
        k = MagicMock()
        k._installed_ollama_models.return_value = None
        assert Kodiqa._ensure_embed_model(k) is False

    def test_offers_pull_and_succeeds(self, monkeypatch):
        monkeypatch.setattr("kodiqa.Prompt.ask", lambda *a, **kw: "y")
        k = MagicMock()
        # not installed at first; appears after the pull
        k._installed_ollama_models.side_effect = [[], [("nomic-embed-text", "270MB")]]
        assert Kodiqa._ensure_embed_model(k) is True
        k._pull_model.assert_called_once_with("nomic-embed-text")

    def test_decline_pull_returns_false(self, monkeypatch):
        monkeypatch.setattr("kodiqa.Prompt.ask", lambda *a, **kw: "n")
        k = MagicMock()
        k._installed_ollama_models.return_value = []
        assert Kodiqa._ensure_embed_model(k) is False
        k._pull_model.assert_not_called()
