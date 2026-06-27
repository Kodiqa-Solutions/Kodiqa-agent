"""Tests for config.py functions and constants."""

import json

from config import (
    is_claude_model, is_qwen_api_model, get_openai_provider, is_openai_compat_model,
    CONFIRM_ACTIONS, BLOCKED_COMMANDS, OPENAI_COMPAT_PROVIDERS,
    DEFAULTS, load_config,
)


class TestIsClaudeModel:
    def test_alias_key(self):
        assert is_claude_model("claude") is True
        assert is_claude_model("sonnet") is True
        assert is_claude_model("haiku") is True
        assert is_claude_model("opus") is True

    def test_full_model_name(self):
        assert is_claude_model("claude-sonnet-4-20250514") is True
        assert is_claude_model("claude-haiku-4-5-20251001") is True

    def test_negative(self):
        assert is_claude_model("qwen3:14b") is False
        assert is_claude_model("gpt-4") is False
        assert is_claude_model("qwen-api") is False


class TestGetOpenaiProvider:
    def test_qwen_aliases(self):
        assert get_openai_provider("qwen-api") == "qwen"
        assert get_openai_provider("qwen-max") == "qwen"
        assert get_openai_provider("qwen-coder") == "qwen"

    def test_openai_aliases(self):
        assert get_openai_provider("gpt") == "openai"
        assert get_openai_provider("gpt-mini") == "openai"
        assert get_openai_provider("o3") == "openai"
        assert get_openai_provider("o4-mini") == "openai"

    def test_deepseek_aliases(self):
        assert get_openai_provider("deepseek") == "deepseek"
        assert get_openai_provider("deepseek-r1") == "deepseek"

    def test_groq_aliases(self):
        assert get_openai_provider("llama") == "groq"
        assert get_openai_provider("mixtral") == "groq"

    def test_mistral_aliases(self):
        assert get_openai_provider("mistral") == "mistral"
        assert get_openai_provider("codestral") == "mistral"

    def test_model_values(self):
        assert get_openai_provider("gpt-4o") == "openai"
        assert get_openai_provider("deepseek-chat") == "deepseek"
        assert get_openai_provider("llama-3.3-70b-versatile") == "groq"
        assert get_openai_provider("mistral-large-latest") == "mistral"

    def test_local_models_return_none(self):
        assert get_openai_provider("qwen3:14b") is None
        assert get_openai_provider("qwen3-coder:latest") is None

    def test_claude_returns_none(self):
        assert get_openai_provider("claude-sonnet-4-20250514") is None

    def test_is_openai_compat(self):
        assert is_openai_compat_model("gpt") is True
        assert is_openai_compat_model("deepseek") is True
        assert is_openai_compat_model("llama") is True
        assert is_openai_compat_model("mistral") is True
        assert is_openai_compat_model("qwen-api") is True
        assert is_openai_compat_model("qwen3:14b") is False

    def test_backward_compat_is_qwen(self):
        assert is_qwen_api_model("qwen-api") is True
        assert is_qwen_api_model("qwen-max") is True
        assert is_qwen_api_model("qwen3:14b") is False
        assert is_qwen_api_model("gpt") is False


class TestProviderRegistry:
    def test_all_providers_have_required_keys(self):
        required = {"url", "key_setting", "color", "label", "aliases"}
        for name, prov in OPENAI_COMPAT_PROVIDERS.items():
            for key in required:
                assert key in prov, f"{name} missing {key}"

    def test_no_alias_collisions(self):
        """Ensure no alias appears in two different providers."""
        seen = {}
        for prov_name, prov in OPENAI_COMPAT_PROVIDERS.items():
            for alias in prov["aliases"]:
                assert alias not in seen, f"Alias '{alias}' in both {seen[alias]} and {prov_name}"
                seen[alias] = prov_name

    def test_five_providers(self):
        assert len(OPENAI_COMPAT_PROVIDERS) == 5
        assert "openai" in OPENAI_COMPAT_PROVIDERS
        assert "deepseek" in OPENAI_COMPAT_PROVIDERS
        assert "groq" in OPENAI_COMPAT_PROVIDERS
        assert "mistral" in OPENAI_COMPAT_PROVIDERS
        assert "qwen" in OPENAI_COMPAT_PROVIDERS


class TestConfirmActions:
    def test_write_tools_need_confirmation(self):
        for tool in ["write_file", "edit_file", "run_command", "git_commit",
                      "delete_file", "move_file", "multi_edit", "diff_apply"]:
            assert tool in CONFIRM_ACTIONS, f"{tool} should require confirmation"

    def test_read_tools_auto_approved(self):
        for tool in ["read_file", "list_dir", "tree", "glob", "grep",
                      "git_status", "git_diff", "web_search", "memory_search"]:
            assert tool not in CONFIRM_ACTIONS, f"{tool} should be auto-approved"


class TestBlockedCommands:
    def test_dangerous_patterns(self):
        assert any("rm -rf /" in cmd for cmd in BLOCKED_COMMANDS)
        assert any("sudo rm" in cmd for cmd in BLOCKED_COMMANDS)
        assert any("mkfs" in cmd for cmd in BLOCKED_COMMANDS)


class TestLoadConfig:
    def test_defaults_without_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.CONFIG_FILE", str(tmp_path / "nonexistent.json"))
        cfg = load_config()
        assert cfg["max_iterations"] == DEFAULTS["max_iterations"]
        assert cfg["max_file_size"] == DEFAULTS["max_file_size"]
        assert cfg["command_timeout"] == DEFAULTS["command_timeout"]

    def test_merges_user_overrides(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"max_iterations": 50, "command_timeout": 300}))
        monkeypatch.setattr("config.CONFIG_FILE", str(config_file))
        cfg = load_config()
        assert cfg["max_iterations"] == 50
        assert cfg["command_timeout"] == 300
        # Unset keys should still have defaults
        assert cfg["max_file_size"] == DEFAULTS["max_file_size"]

    def test_invalid_json_returns_defaults(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        config_file.write_text("not valid json {{{")
        monkeypatch.setattr("config.CONFIG_FILE", str(config_file))
        cfg = load_config()
        assert cfg["max_iterations"] == DEFAULTS["max_iterations"]


class TestSaveSettingsSafety:
    """save_settings must not silently drop API keys on a partial save, and keeps a .bak."""

    def _point_at(self, tmp_path, monkeypatch):
        import config
        sf = tmp_path / "settings.json"
        monkeypatch.setattr(config, "SETTINGS_FILE", str(sf))
        monkeypatch.setattr(config, "KODIQA_DIR", str(tmp_path))
        return config, sf

    def test_partial_save_preserves_api_keys(self, tmp_path, monkeypatch):
        config, sf = self._point_at(tmp_path, monkeypatch)
        sf.write_text(json.dumps({"qwen_api_key": "sk-secret", "default_model": "x"}))
        config.save_settings({"kv_cache_type": "q4_0"})  # partial dict, missing the key
        out = json.loads(sf.read_text())
        assert out["qwen_api_key"] == "sk-secret"  # preserved
        assert out["kv_cache_type"] == "q4_0"

    def test_explicit_clear_is_honored(self, tmp_path, monkeypatch):
        config, sf = self._point_at(tmp_path, monkeypatch)
        sf.write_text(json.dumps({"qwen_api_key": "sk-secret"}))
        config.save_settings({"qwen_api_key": ""})  # explicit clear
        assert json.loads(sf.read_text())["qwen_api_key"] == ""

    def test_writes_backup(self, tmp_path, monkeypatch):
        config, sf = self._point_at(tmp_path, monkeypatch)
        sf.write_text(json.dumps({"a": 1}))
        config.save_settings({"b": 2})
        assert (tmp_path / "settings.json.bak").exists()
        assert json.loads((tmp_path / "settings.json.bak").read_text()) == {"a": 1}


class TestVersionHelpers:
    def test_version_is_newer_numeric(self):
        from config import version_is_newer
        assert version_is_newer("3.19.0", "3.18.0")
        assert version_is_newer("v3.18.1", "3.18.0")
        assert not version_is_newer("3.18.0", "3.18.0")
        assert not version_is_newer("3.9.0", "3.18.0")  # numeric, not string compare

    def test_installed_version_matches_changelog(self):
        from config import installed_version, CHANGELOG
        assert installed_version() == CHANGELOG[0]["version"].lstrip("vV")
