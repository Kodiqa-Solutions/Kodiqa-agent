"""Tests for the Ollama MLX-server upgrade and the cloud-model pull fallback.

Two real-world failures these cover:
  1. Homebrew `ollama` lacks the MLX runtime, so MLX-format models fail to pull.
     Kodiqa now prefers / switches to the MLX-capable macOS app build.
  2. Cloud-hosted models (glm-5.1, …) expose only a `:cloud` tag, so a bare
     `ollama pull glm-5.1` fails — Kodiqa retries `<name>:cloud` transparently.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import config
from ollama_manager import OllamaManager


def _bin_with_mlx(tmp_path, mlx=True):
    """Create a fake ollama binary dir; optionally drop libmlx.dylib next to it."""
    d = tmp_path / "Resources"
    d.mkdir()
    binp = d / "ollama"
    binp.write_text("#!/bin/sh\n")
    if mlx:
        (d / "libmlx.dylib").write_text("")
    return str(binp)


class TestMlxDetection:
    def test_has_mlx_true_when_dylib_present(self, tmp_path):
        assert config.ollama_bin_has_mlx(_bin_with_mlx(tmp_path, mlx=True)) is True

    def test_has_mlx_false_when_absent(self, tmp_path):
        assert config.ollama_bin_has_mlx(_bin_with_mlx(tmp_path, mlx=False)) is False

    def test_has_mlx_false_on_none(self):
        assert config.ollama_bin_has_mlx(None) is False


class TestResolveOllamaBin:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OLLAMA_BIN", "/custom/ollama")
        assert config.resolve_ollama_bin() == "/custom/ollama"

    def test_prefers_app_build_when_it_has_mlx(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OLLAMA_BIN", raising=False)
        app = _bin_with_mlx(tmp_path, mlx=True)
        monkeypatch.setattr(config, "OLLAMA_APP_BIN", app)
        # Even if a (homebrew) ollama is on PATH, the MLX app build wins.
        monkeypatch.setattr(config.shutil, "which", lambda _: "/opt/homebrew/bin/ollama")
        assert config.resolve_ollama_bin() == app

    def test_falls_back_to_path_without_mlx_app(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OLLAMA_BIN", raising=False)
        monkeypatch.setattr(config, "OLLAMA_APP_BIN", str(tmp_path / "nope" / "ollama"))
        monkeypatch.setattr(config.shutil, "which", lambda _: "/opt/homebrew/bin/ollama")
        assert config.resolve_ollama_bin() == "/opt/homebrew/bin/ollama"


def _fake_run(behavior):
    """Build a subprocess.run stand-in. `behavior` maps the pull target (argv[2])
    to (returncode, stderr)."""
    calls = []

    def run(argv, **kw):
        target = argv[2]
        calls.append(target)
        rc, err = behavior.get(target, (1, "file does not exist"))
        return SimpleNamespace(returncode=rc, stderr=err, stdout="")

    return run, calls


class TestPullCloudFallback:
    def test_plain_model_pulls_directly(self, monkeypatch):
        run, calls = _fake_run({"llama3.2": (0, "")})
        monkeypatch.setattr("ollama_manager.subprocess.run", run)
        m = OllamaManager(MagicMock())
        ok, name, _detail, via_cloud = m._pull_one("llama3.2")
        assert ok and name == "llama3.2" and via_cloud is False
        assert calls == ["llama3.2"]  # no cloud retry needed

    def test_cloud_only_model_falls_back(self, monkeypatch):
        run, calls = _fake_run({
            "glm-5.1": (1, "pull model manifest: file does not exist"),
            "glm-5.1:cloud": (0, ""),
        })
        monkeypatch.setattr("ollama_manager.subprocess.run", run)
        m = OllamaManager(MagicMock())
        ok, name, _detail, via_cloud = m._pull_one("glm-5.1")
        assert ok and name == "glm-5.1:cloud" and via_cloud is True
        assert calls == ["glm-5.1", "glm-5.1:cloud"]

    def test_mlx_error_triggers_cloud_retry(self, monkeypatch):
        run, calls = _fake_run({
            "glm-5.1": (1, 'WARN MLX dynamic library not available error="failed to load MLX"'),
            "glm-5.1:cloud": (0, ""),
        })
        monkeypatch.setattr("ollama_manager.subprocess.run", run)
        ok, name, _d, via_cloud = OllamaManager(MagicMock())._pull_one("glm-5.1")
        assert ok and name == "glm-5.1:cloud" and via_cloud

    def test_explicit_tag_does_not_cloud_retry(self, monkeypatch):
        run, calls = _fake_run({"glm4:9b": (1, "file does not exist")})
        monkeypatch.setattr("ollama_manager.subprocess.run", run)
        ok, _name, _d, via_cloud = OllamaManager(MagicMock())._pull_one("glm4:9b")
        assert ok is False and via_cloud is False
        assert calls == ["glm4:9b"]  # tagged name → no :cloud retry

    def test_genuine_failure_returns_detail(self, monkeypatch):
        run, _calls = _fake_run({
            "bogus": (1, "some network error"),
            "bogus:cloud": (1, "still broken"),
        })
        monkeypatch.setattr("ollama_manager.subprocess.run", run)
        ok, _name, detail, _vc = OllamaManager(MagicMock())._pull_one("bogus")
        # "some network error" isn't a missing-manifest/MLX signal → no cloud retry.
        assert ok is False and detail == "some network error"


class TestEnsureMlxServer:
    def test_noop_when_no_mlx_build(self, monkeypatch):
        # OLLAMA_BIN has no MLX → nothing better to offer, leave the server alone.
        monkeypatch.setattr("ollama_manager.ollama_bin_has_mlx", lambda _b: False)
        m = OllamaManager(MagicMock())
        m._running_serve_bin = lambda: "/opt/homebrew/bin/ollama"
        m._spawn_serve = MagicMock()
        m._ensure_mlx_server()
        m._spawn_serve.assert_not_called()

    def test_noop_when_running_server_already_mlx(self, monkeypatch):
        monkeypatch.setattr("ollama_manager.ollama_bin_has_mlx", lambda _b: True)
        m = OllamaManager(MagicMock())
        m._running_serve_bin = lambda: "/Applications/Ollama.app/Contents/Resources/ollama"
        m._spawn_serve = MagicMock()
        m._ensure_mlx_server()
        m._spawn_serve.assert_not_called()

    def test_restarts_when_running_server_lacks_mlx(self, monkeypatch):
        # OLLAMA_BIN is MLX-capable but the running serve is not → restart it.
        monkeypatch.setattr("ollama_manager.ollama_bin_has_mlx",
                            lambda b: "Ollama.app" in (b or ""))
        monkeypatch.setattr("ollama_manager.OLLAMA_BIN",
                            "/Applications/Ollama.app/Contents/Resources/ollama")
        monkeypatch.setattr("ollama_manager.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0))
        monkeypatch.setattr("ollama_manager.requests.get",
                            lambda *a, **k: (_ for _ in ()).throw(Exception("down")))
        monkeypatch.setattr("ollama_manager.time.sleep", lambda _s: None)
        m = OllamaManager(MagicMock())
        m._running_serve_bin = lambda: "/opt/homebrew/bin/ollama"
        m._spawn_serve = MagicMock(return_value=True)
        m._ensure_mlx_server()
        m._spawn_serve.assert_called_once()
