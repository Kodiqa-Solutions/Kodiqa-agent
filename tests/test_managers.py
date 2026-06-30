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

    def test_repo_instructions_reads_agents_md(self, tmp_path):
        from context_builder import ContextBuilder
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("Use tabs, not spaces.")
        agent = MagicMock()
        agent.cwd = str(tmp_path)
        out = ContextBuilder(agent).load_repo_instructions()
        assert "AGENTS.md" in out and "Use tabs, not spaces." in out

    def test_repo_instructions_walks_up_to_root(self, tmp_path):
        from context_builder import ContextBuilder
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("root rule")
        sub = tmp_path / "pkg" / "mod"
        sub.mkdir(parents=True)
        agent = MagicMock()
        agent.cwd = str(sub)
        out = ContextBuilder(agent).load_repo_instructions()
        assert "root rule" in out  # found by walking up to the repo root

    def test_repo_instructions_nearest_wins_ordering(self, tmp_path):
        from context_builder import ContextBuilder
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("ROOT")
        sub = tmp_path / "pkg"
        sub.mkdir()
        (sub / "AGENTS.md").write_text("NEAREST")
        agent = MagicMock()
        agent.cwd = str(sub)
        out = ContextBuilder(agent).load_repo_instructions()
        assert out.index("ROOT") < out.index("NEAREST")  # root first, nearest last

    def test_repo_instructions_claude_md_fallback(self, tmp_path):
        from context_builder import ContextBuilder
        (tmp_path / ".git").mkdir()
        (tmp_path / "CLAUDE.md").write_text("claude rules")
        agent = MagicMock()
        agent.cwd = str(tmp_path)
        out = ContextBuilder(agent).load_repo_instructions()
        assert "CLAUDE.md" in out and "claude rules" in out

    def test_repo_instructions_agents_preferred_over_claude(self, tmp_path):
        from context_builder import ContextBuilder
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("agents wins")
        (tmp_path / "CLAUDE.md").write_text("claude ignored")
        agent = MagicMock()
        agent.cwd = str(tmp_path)
        out = ContextBuilder(agent).load_repo_instructions()
        assert "agents wins" in out and "claude ignored" not in out

    def test_repo_instructions_empty_when_none(self, tmp_path):
        from context_builder import ContextBuilder
        (tmp_path / ".git").mkdir()
        agent = MagicMock()
        agent.cwd = str(tmp_path)
        assert ContextBuilder(agent).load_repo_instructions() == ""


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


class TestAgentStateAccess:
    """Regression for the STEP 3 extraction: state the managers READ (via
    getattr/hasattr) lives on the agent, not the manager. A bare `self` in those
    calls silently broke --no-update, Ollama shutdown, and the live-model cache."""

    def test_check_updates_honors_skip_flag(self):
        # _skip_updates lives on the agent (set by --no-update / _cmd_update).
        from ollama_manager import OllamaManager
        agent = MagicMock()
        agent._skip_updates = True
        agent.config = {"check_updates": True}
        OllamaManager(agent).check_updates()
        agent._ensure_ollama.assert_not_called()  # returned before doing any work

    def test_stop_ollama_uses_agent_proc(self):
        # _ollama_proc lives on the agent; stop must actually terminate it.
        from ollama_manager import OllamaManager
        agent = MagicMock()
        agent._ollama_started_by_us = True
        proc = MagicMock()
        proc.poll.return_value = None  # still running
        agent._ollama_proc = proc
        OllamaManager(agent).stop_ollama()
        proc.terminate.assert_called_once()

    def test_get_provider_reads_live_cache(self):
        from model_registry import ModelRegistry
        agent = MagicMock()
        agent._cached_api_models = {"deepseek": ["some-live-model-id"]}
        assert ModelRegistry(agent).get_provider_for_model("some-live-model-id") == "deepseek"

    def test_is_live_claude_reads_cache(self):
        from model_registry import ModelRegistry
        agent = MagicMock()
        agent._cached_api_models = {"claude": ["claude-live-xyz"]}
        assert ModelRegistry(agent).is_live_claude("claude-live-xyz") is True
        assert ModelRegistry(agent).is_live_claude("not-cached") is False
