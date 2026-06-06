"""Wiring + behavior tests for the subsystems split out of the Kodiqa God-class
(refactor STEP 3): ContextBuilder, OllamaManager, ModelRegistry, AgentTeam.

These confirm (a) Kodiqa's thin wrappers delegate to the right manager, and
(b) a few pure-logic methods still behave correctly after the move.
"""

from unittest.mock import MagicMock

from kodiqa import Kodiqa


class TestWrapperDelegation:
    """Each Kodiqa wrapper must forward to its manager attribute."""

    def test_context_wrappers(self):
        k = MagicMock()
        Kodiqa._git_context(k); k.context.git_context.assert_called_once()
        Kodiqa._load_context_file(k); k.context.load_context_file.assert_called_once()
        Kodiqa._build_system_prompt(k, "T"); k.context.build_system_prompt.assert_called_once_with("T")

    def test_ollama_wrappers(self):
        k = MagicMock()
        Kodiqa._ensure_ollama(k); k.ollama.ensure_ollama.assert_called_once()
        Kodiqa._stop_ollama(k); k.ollama.stop_ollama.assert_called_once()
        Kodiqa._pull_model(k, "qwen"); k.ollama.pull_model.assert_called_once_with("qwen")

    def test_model_registry_wrappers(self):
        k = MagicMock()
        Kodiqa._resolve_model_name(k, "opus"); k.models.resolve_model_name.assert_called_once_with("opus")
        Kodiqa._get_provider_for_model(k, "x"); k.models.get_provider_for_model.assert_called_once_with("x")
        Kodiqa._list_models(k); k.models.list_models.assert_called_once()

    def test_agent_team_wrappers(self):
        k = MagicMock()
        Kodiqa._handle_agent(k, "task"); k.agent_team.handle_agent.assert_called_once_with("task")
        Kodiqa._handle_team(k, "task"); k.agent_team.handle_team.assert_called_once_with("task")


class TestContextBuilderPure:
    def test_git_context_empty(self):
        from context_builder import ContextBuilder
        agent = MagicMock()
        agent.git_info = None
        assert ContextBuilder(agent).git_context() == ""

    def test_git_context_formats(self):
        from context_builder import ContextBuilder
        agent = MagicMock()
        agent.git_info = {"branch": "main", "changed_files": 0, "recent_commits": ""}
        out = ContextBuilder(agent).git_context()
        assert "## Git Repository" in out and "main" in out

    def test_shell_env_context_empty(self):
        from context_builder import ContextBuilder
        agent = MagicMock()
        agent.shell_env = {}
        assert ContextBuilder(agent).shell_env_context() == ""

    def test_get_project_context_path(self):
        from context_builder import ContextBuilder
        agent = MagicMock()
        agent.cwd = "/Users/x/proj"
        path = ContextBuilder(agent).get_project_context_path()
        assert path.endswith("Users-x-proj.md")


class TestModelRegistryPure:
    def test_resolve_alias(self):
        from model_registry import ModelRegistry
        assert ModelRegistry(MagicMock()).resolve_model_name("opus")  # resolves to a real id
        assert ModelRegistry(MagicMock()).resolve_model_name("xyz-unknown") == "xyz-unknown"

    def test_get_provider_unknown_returns_none(self):
        from model_registry import ModelRegistry
        agent = MagicMock()
        agent._cached_api_models = {}
        assert ModelRegistry(agent).get_provider_for_model("totally-made-up-model") is None
