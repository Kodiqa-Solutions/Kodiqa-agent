"""Tests for the Ollama MLX-server upgrade and the cloud-model pull fallback.

Two real-world failures these cover:
  1. Homebrew `ollama` lacks the MLX runtime, so MLX-format models fail to pull.
     Kodiqa now prefers / switches to the MLX-capable macOS app build.
  2. Cloud-hosted models (glm-5.1, …) expose only a `:cloud` tag, so a bare
     `ollama pull glm-5.1` fails — Kodiqa retries `<name>:cloud` transparently.
"""

import signal
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

    def run_pull(target):
        calls.append(target)
        # behavior maps target -> (rc, out, cancelled); default = missing manifest
        rc, out, cancelled = behavior.get(target, (1, "file does not exist", False))
        return rc, out, cancelled

    return run_pull, calls


def _mgr_with_pull(behavior):
    """An OllamaManager whose _run_pull is stubbed from a target->(rc,out,cancel) map."""
    m = OllamaManager(MagicMock())
    run_pull, calls = _fake_run(behavior)
    m._run_pull = run_pull
    return m, calls


class TestPullCloudFallback:
    def test_plain_model_pulls_directly(self):
        m, calls = _mgr_with_pull({"llama3.2": (0, "", False)})
        ok, name, _detail, via_cloud = m._pull_one("llama3.2")
        assert ok and name == "llama3.2" and via_cloud is False
        assert calls == ["llama3.2"]  # no cloud retry needed

    def test_cloud_only_model_falls_back(self):
        m, calls = _mgr_with_pull({
            "glm-5.1": (1, "pull model manifest: file does not exist", False),
            "glm-5.1:cloud": (0, "", False),
        })
        ok, name, _detail, via_cloud = m._pull_one("glm-5.1")
        assert ok and name == "glm-5.1:cloud" and via_cloud is True
        assert calls == ["glm-5.1", "glm-5.1:cloud"]

    def test_mlx_error_triggers_cloud_retry(self):
        m, _calls = _mgr_with_pull({
            "glm-5.1": (1, 'WARN MLX dynamic library not available error="failed to load MLX"', False),
            "glm-5.1:cloud": (0, "", False),
        })
        ok, name, _d, via_cloud = m._pull_one("glm-5.1")
        assert ok and name == "glm-5.1:cloud" and via_cloud

    def test_explicit_tag_does_not_cloud_retry(self):
        m, calls = _mgr_with_pull({"glm4:9b": (1, "file does not exist", False)})
        ok, _name, _d, via_cloud = m._pull_one("glm4:9b")
        assert ok is False and via_cloud is False
        assert calls == ["glm4:9b"]  # tagged name → no :cloud retry

    def test_genuine_failure_returns_detail(self):
        m, _calls = _mgr_with_pull({
            "bogus": (1, "some network error", False),
            "bogus:cloud": (1, "still broken", False),
        })
        ok, _name, detail, _vc = m._pull_one("bogus")
        # "some network error" isn't a missing-manifest/MLX signal → no cloud retry.
        assert ok is False and detail == "some network error"

    def test_cancelled_pull_reports_and_no_retry(self):
        m, calls = _mgr_with_pull({"glm-5.1": (1, "", True)})  # user cancelled
        ok, _name, detail, _vc = m._pull_one("glm-5.1")
        assert ok is False and detail == "__cancelled__"
        assert calls == ["glm-5.1"]  # cancel → no cloud escalation


class TestQuantLabel:
    def test_flat_quant(self):
        assert OllamaManager._quant_label("model-Q4_K_M.gguf") == "Q4_K_M"

    def test_subfolder_ud_quant(self):
        assert OllamaManager._quant_label("UD-Q4_K_M/GLM-5.2-UD-Q4_K_M.gguf") == "UD-Q4_K_M"

    def test_sharded_strips_suffix(self):
        assert OllamaManager._quant_label("Qwen-Q8_0-00001-of-00003.gguf") == "Q8_0"

    def test_bf16(self):
        assert OllamaManager._quant_label("x-BF16.gguf") == "BF16"


class TestHfSearch:
    def test_filters_to_gguf_and_sorts_by_likes(self, monkeypatch):
        payload = [
            {"id": "zai-org/GLM-5.2", "likes": 2624},          # not GGUF → excluded
            {"id": "unsloth/GLM-5.2-GGUF", "likes": 420},
            {"id": "someone/GLM-5.2-GGUF-tiny", "likes": 9},
        ]
        monkeypatch.setattr("ollama_manager.requests.get",
                            lambda *a, **k: SimpleNamespace(json=lambda: payload, raise_for_status=lambda: None))
        repos = OllamaManager(MagicMock())._hf_search_gguf("GLM-5.2")
        assert [r[0] for r in repos] == ["unsloth/GLM-5.2-GGUF", "someone/GLM-5.2-GGUF-tiny"]

    def test_empty_on_error(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("network")
        monkeypatch.setattr("ollama_manager.requests.get", boom)
        assert OllamaManager(MagicMock())._hf_search_gguf("x") == []


class TestHfQuants:
    def test_aggregates_shards_and_sorts(self, monkeypatch):
        sibs = {"siblings": [
            {"rfilename": "UD-Q2_K/m-00001-of-00002.gguf", "size": 100},
            {"rfilename": "UD-Q2_K/m-00002-of-00002.gguf", "size": 100},
            {"rfilename": "m-Q4_K_M.gguf", "size": 50},
            {"rfilename": "README.md", "size": 1},  # ignored
        ]}
        monkeypatch.setattr("ollama_manager.requests.get",
                            lambda *a, **k: SimpleNamespace(json=lambda: sibs, raise_for_status=lambda: None))
        out = OllamaManager(MagicMock())._hf_quants("repo")
        assert out == [("Q4_K_M", 50), ("UD-Q2_K", 200)]  # smallest first; shards summed


class TestPullWithFallbacks:
    def _mgr(self):
        return OllamaManager(MagicMock())

    def test_registry_success_short_circuits(self):
        m = self._mgr()
        m._pull_one = lambda n: (True, n, "", False)
        m._pull_from_huggingface = MagicMock()
        ok, name, cancelled = m._pull_with_fallbacks("llama3.2")
        assert ok and name == "llama3.2" and cancelled is False
        m._pull_from_huggingface.assert_not_called()

    def test_escalates_to_huggingface_when_registry_fails(self):
        m = self._mgr()
        m._pull_one = lambda n: (False, n, "file does not exist", False)
        m._pull_from_huggingface = MagicMock(return_value=(True, "hf.co/unsloth/X-GGUF:Q4_K_M", False))
        ok, name, cancelled = m._pull_with_fallbacks("X")
        assert ok and name == "hf.co/unsloth/X-GGUF:Q4_K_M" and cancelled is False
        m._pull_from_huggingface.assert_called_once_with("X")

    def test_cancelled_does_not_escalate(self):
        m = self._mgr()
        m._pull_one = lambda n: (False, n, "__cancelled__", False)
        m._pull_from_huggingface = MagicMock()
        ok, _name, cancelled = m._pull_with_fallbacks("X")
        assert ok is False and cancelled is True
        m._pull_from_huggingface.assert_not_called()


class TestPullFromHuggingface:
    def _mgr(self):
        agent = MagicMock()
        agent._mem_budget = (0, "")  # no budget → fit markers are empty in the quant list
        return OllamaManager(agent)

    def test_user_picks_quant_and_pulls(self, monkeypatch):
        m = self._mgr()
        m._hf_search_gguf = lambda n: [("unsloth/X-GGUF", 420)]
        m._hf_quants = lambda r: [("Q4_K_M", 5_000_000_000), ("Q8_0", 9_000_000_000)]
        monkeypatch.setattr("ollama_manager.Prompt.ask", lambda *a, **k: "1")
        calls = {}
        m._run_pull = lambda target: (calls.setdefault("target", target), (0, "", False))[1]
        ok, name, cancelled = m._pull_from_huggingface("X")
        assert ok and name == "hf.co/unsloth/X-GGUF:Q4_K_M" and cancelled is False
        assert calls["target"] == "hf.co/unsloth/X-GGUF:Q4_K_M"

    def test_no_repo_found(self):
        m = self._mgr()
        m._hf_search_gguf = lambda n: []
        ok, _name, cancelled = m._pull_from_huggingface("nope")
        assert ok is False and cancelled is False

    def test_cancel_at_quant_prompt_does_not_pull(self, monkeypatch):
        m = self._mgr()
        m._hf_search_gguf = lambda n: [("unsloth/X-GGUF", 1)]
        m._hf_quants = lambda r: [("Q4_K_M", 5_000_000_000)]
        monkeypatch.setattr("ollama_manager.Prompt.ask", lambda *a, **k: "cancel")
        m._run_pull = MagicMock()
        ok, _name, _cancelled = m._pull_from_huggingface("X")
        assert ok is False
        m._run_pull.assert_not_called()

    def test_cancel_during_download_propagates(self, monkeypatch):
        m = self._mgr()
        m._hf_search_gguf = lambda n: [("unsloth/X-GGUF", 1)]
        m._hf_quants = lambda r: [("Q4_K_M", 5_000_000_000)]
        monkeypatch.setattr("ollama_manager.Prompt.ask", lambda *a, **k: "1")
        m._run_pull = lambda target: (1, "", True)  # user hit Esc mid-download
        ok, _name, cancelled = m._pull_from_huggingface("X")
        assert ok is False and cancelled is True


def _fake_registry(by_tag, page_tags=None):
    """requests.get stand-in for both registry manifests and the library tags page.
      - registry `.../manifests/<tag>` → JSON body from `by_tag`, or 404.
      - `ollama.com/library/<name>/tags` → HTML listing `page_tags` (or none)."""
    page_tags = page_tags or []
    def get(url, *a, **k):
        if "ollama.com/library/" in url and url.endswith("/tags"):
            name = url.split("/library/")[1].rsplit("/tags", 1)[0]
            html = " ".join(f"{name}:{t}" for t in page_tags)
            return SimpleNamespace(status_code=200, text=html)
        tag = url.rsplit("/", 1)[-1]
        body = by_tag.get(tag)
        if body is None:
            return SimpleNamespace(status_code=404, json=lambda: {})
        return SimpleNamespace(status_code=200, json=lambda: body)
    return get


class TestRegistryInfo:
    def test_real_model_sums_layer_sizes(self, monkeypatch):
        manifest = {"layers": [
            {"mediaType": "application/vnd.ollama.image.model", "size": 19_000_000_000},
            {"mediaType": "application/vnd.ollama.image.license", "size": 1000},
        ]}
        monkeypatch.setattr("ollama_manager.requests.get", _fake_registry({"latest": manifest}))
        size, is_cloud, pull = OllamaManager(MagicMock())._registry_info("glm-4.7-flash")
        assert size == 19_000_001_000 and is_cloud is False and pull == "glm-4.7-flash"

    def test_cloud_pointer_on_latest(self, monkeypatch):
        # latest exists but has null layers (a cloud pointer) → cloud, no size.
        monkeypatch.setattr("ollama_manager.requests.get", _fake_registry({"latest": {"layers": None}}))
        size, is_cloud, _pull = OllamaManager(MagicMock())._registry_info("gemini-3-flash-preview")
        assert size is None and is_cloud is True

    def test_cloud_only_tag_via_registry(self, monkeypatch):
        # No latest, but a plain :cloud tag exists in the registry → cloud.
        monkeypatch.setattr("ollama_manager.requests.get", _fake_registry({"cloud": {"layers": None}}))
        size, is_cloud, pull = OllamaManager(MagicMock())._registry_info("glm-5.1")
        assert size is None and is_cloud is True and pull == "glm-5.1"

    def test_nonstandard_cloud_tag_via_tags_page(self, monkeypatch):
        # No latest, no plain :cloud — but the tags page lists a `675b-cloud` tag.
        monkeypatch.setattr("ollama_manager.requests.get",
                            _fake_registry({}, page_tags=["675b-cloud"]))
        size, is_cloud, pull = OllamaManager(MagicMock())._registry_info("mistral-large-3")
        assert size is None and is_cloud is True and pull == "mistral-large-3:675b-cloud"

    def test_sized_no_latest_resolves_tag_and_size(self, monkeypatch):
        # No latest/cloud, but sized tags exist → resolve the first + its size.
        monkeypatch.setattr("ollama_manager.requests.get",
                            _fake_registry({"8b": {"layers": [{"size": 6_900_000_000}]}},
                                           page_tags=["8b", "8b-q4_K_M"]))
        size, is_cloud, pull = OllamaManager(MagicMock())._registry_info("granite4.1-guardian")
        assert size == 6_900_000_000 and is_cloud is False and pull == "granite4.1-guardian:8b"

    def test_unknown_when_nothing_found(self, monkeypatch):
        monkeypatch.setattr("ollama_manager.requests.get", _fake_registry({}))
        size, is_cloud, pull = OllamaManager(MagicMock())._registry_info("totally-fake")
        assert size is None and is_cloud is False and pull == "totally-fake"

    def test_registry_size_wrapper(self, monkeypatch):
        monkeypatch.setattr("ollama_manager.requests.get",
                            _fake_registry({"latest": {"layers": [{"size": 2_000_000_000}]}}))
        assert OllamaManager(MagicMock())._registry_size("llama3.2") == 2_000_000_000

    def test_info_pull_name(self):
        assert OllamaManager._info_pull_name((None, True, "x:cloud"), "x") == "x:cloud"
        assert OllamaManager._info_pull_name((None, False), "x") == "x"  # 2-tuple → fallback
        assert OllamaManager._info_pull_name(None, "x") == "x"

    def test_size_tag_rendering(self):
        m = OllamaManager(MagicMock())
        assert "GB" in m._size_tag((19_000_000_000, False, "x"))
        assert "cloud" in m._size_tag((None, True, "x"))
        assert "?" in m._size_tag((None, False, "x"))
        assert "GB" in m._size_tag((19_000_000_000, False))  # 2-tuple still works

    def test_fmt_size(self):
        assert OllamaManager._fmt_size(0) == "?"
        assert OllamaManager._fmt_size(19_000_000_000) == "19.0 GB"
        assert OllamaManager._fmt_size(330_000_000) == "330 MB"

    def test_registry_infos_bulk(self):
        m = OllamaManager(MagicMock())
        fake = {"llama3.2": (2_000_000_000, False, "llama3.2"),
                "glm-5.1": (None, True, "glm-5.1"), "x": (None, False, "x")}
        m._registry_info = lambda n, timeout=8: fake[n]
        out = m._registry_infos(["llama3.2", "glm-5.1", "x"])
        assert out == fake

    def test_registry_infos_empty(self):
        assert OllamaManager(MagicMock())._registry_infos([]) == {}


class TestChooseModels:
    """The paginated 'new models' picker (_choose_models)."""

    def _mgr(self, infos=None):
        agent = MagicMock()
        agent._mem_budget = (0, "")  # no budget → skip fit annotations in the picker
        m = OllamaManager(agent)
        m._registry_infos = lambda names: (infos if infos is not None
                                           else {n: (1_000_000_000, False, n) for n in names})
        return m

    def _models(self, n):
        return [(f"model{i}", f"desc{i}", "1M") for i in range(n)]

    def _answers(self, monkeypatch, seq):
        it = iter(seq)
        monkeypatch.setattr("ollama_manager.Prompt.ask", lambda *a, **k: next(it))

    def test_skip_returns_none(self, monkeypatch):
        self._answers(monkeypatch, ["skip"])
        assert self._mgr()._choose_models(self._models(10)) is None

    def test_pick_number_then_confirm(self, monkeypatch):
        self._answers(monkeypatch, ["3", "y"])
        out = self._mgr()._choose_models(self._models(10))
        assert out == ["model2"]  # 1-based index 3 → model2

    def test_global_index_across_pages(self, monkeypatch):
        # 150 models, page size 100; pick #130 (on page 2) directly by number.
        self._answers(monkeypatch, ["130", "y"])
        out = self._mgr()._choose_models(self._models(150), page_size=100)
        assert out == ["model129"]

    def test_next_advances_page(self, monkeypatch):
        # next → page 2, then pick #101 (first of page 2), confirm.
        self._answers(monkeypatch, ["next", "101", "y"])
        out = self._mgr()._choose_models(self._models(150), page_size=100)
        assert out == ["model100"]

    def test_no_at_confirm_loops_back(self, monkeypatch):
        # pick #1 → say "n" (pick again) → pick #2 → "y".
        self._answers(monkeypatch, ["1", "n", "2", "y"])
        out = self._mgr()._choose_models(self._models(10))
        assert out == ["model1"]

    def test_all_selects_everything(self, monkeypatch):
        self._answers(monkeypatch, ["all", "y"])
        out = self._mgr()._choose_models(self._models(5))
        assert out == [f"model{i}" for i in range(5)]

    def test_confirm_skip_returns_none(self, monkeypatch):
        self._answers(monkeypatch, ["1", "skip"])
        assert self._mgr()._choose_models(self._models(5)) is None

    def test_returns_resolved_pull_names(self, monkeypatch):
        # A sized-no-latest model resolves to a tagged pull-name; the picker should
        # return the resolved name (so the download uses the right tag).
        infos = {
            "granite4.1-guardian": (6_900_000_000, False, "granite4.1-guardian:8b"),
            "mistral-large-3": (None, True, "mistral-large-3:675b-cloud"),
        }
        models = [("granite4.1-guardian", "d", "1M"), ("mistral-large-3", "d", "1M")]
        self._answers(monkeypatch, ["all", "y"])
        out = self._mgr(infos)._choose_models(models)
        assert out == ["granite4.1-guardian:8b", "mistral-large-3:675b-cloud"]


class TestUnloadAndShutdown:
    def test_unload_models_posts_keep_alive_zero(self, monkeypatch):
        posts = []
        monkeypatch.setattr("ollama_manager.requests.get",
                            lambda *a, **k: SimpleNamespace(json=lambda: {"models": [
                                {"name": "gpt-oss:latest"}, {"name": "llama3.2"}]}))
        monkeypatch.setattr("ollama_manager.requests.post",
                            lambda url, json=None, **k: posts.append(json) or SimpleNamespace())
        OllamaManager(MagicMock()).unload_models()
        assert {"model": "gpt-oss:latest", "keep_alive": 0} in posts
        assert {"model": "llama3.2", "keep_alive": 0} in posts

    def test_unload_models_noop_when_server_down(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("connection refused")
        monkeypatch.setattr("ollama_manager.requests.get", boom)
        posts = []
        monkeypatch.setattr("ollama_manager.requests.post", lambda *a, **k: posts.append(1))
        OllamaManager(MagicMock()).unload_models()  # must not raise
        assert posts == []

    def test_stop_ollama_handles_orphan_when_not_owner(self):
        agent = MagicMock()
        agent._ollama_started_by_us = False
        m = OllamaManager(agent)
        m._stop_orphaned_server = MagicMock()
        m.stop_ollama()
        m._stop_orphaned_server.assert_called_once()

    def test_stop_orphaned_server_kills_recorded_pid(self, tmp_path, monkeypatch):
        pidfile = tmp_path / "ollama_server.pid"
        pidfile.write_text("4242")
        m = OllamaManager(MagicMock())
        m._server_pid_file = lambda: str(pidfile)
        monkeypatch.setattr("ollama_manager.subprocess.run",
                            lambda *a, **k: SimpleNamespace(stdout="/Applications/Ollama.app/Contents/Resources/ollama serve"))
        killed = []
        monkeypatch.setattr("ollama_manager.os.kill", lambda pid, sig: killed.append((pid, sig)))
        m._stop_orphaned_server()
        assert killed == [(4242, signal.SIGTERM)]
        assert not pidfile.exists()  # pid file cleaned up

    def test_stop_orphaned_server_skips_non_ollama_pid(self, tmp_path, monkeypatch):
        pidfile = tmp_path / "ollama_server.pid"
        pidfile.write_text("4242")
        m = OllamaManager(MagicMock())
        m._server_pid_file = lambda: str(pidfile)
        # PID belongs to some other process (not ollama serve) → must NOT kill it.
        monkeypatch.setattr("ollama_manager.subprocess.run",
                            lambda *a, **k: SimpleNamespace(stdout="/usr/bin/python3 something"))
        killed = []
        monkeypatch.setattr("ollama_manager.os.kill", lambda pid, sig: killed.append(pid))
        m._stop_orphaned_server()
        assert killed == []
        assert not pidfile.exists()


class TestQuantSourcing:
    """Phase 3: prefer imatrix/UD- uploaders + label them."""

    def test_prefers_trusted_uploaders_over_likes(self, monkeypatch):
        payload = [
            {"id": "randomguy/Qwen-GGUF", "likes": 500},
            {"id": "unsloth/Qwen-GGUF", "likes": 100},
            {"id": "bartowski/Qwen-GGUF", "likes": 50},
        ]
        monkeypatch.setattr("ollama_manager.requests.get",
                            lambda *a, **k: SimpleNamespace(json=lambda: payload, raise_for_status=lambda: None))
        repos = [r[0] for r in OllamaManager(MagicMock())._hf_search_gguf("Qwen")]
        assert repos == ["unsloth/Qwen-GGUF", "bartowski/Qwen-GGUF", "randomguy/Qwen-GGUF"]

    def test_repo_note(self):
        assert "Unsloth" in OllamaManager._gguf_repo_note("unsloth/X-GGUF")
        assert "imatrix" in OllamaManager._gguf_repo_note("bartowski/X-GGUF")
        assert OllamaManager._gguf_repo_note("randomguy/X-GGUF") == ""


class TestOptReader:
    """Phase 3: settings-over-config knob reader."""

    def test_settings_wins_over_config(self):
        agent = MagicMock()
        agent.settings = {"kv_cache_type": "q4_0"}
        agent.config = {"kv_cache_type": "q8_0"}
        assert OllamaManager(agent)._opt("kv_cache_type", "x") == "q4_0"

    def test_config_fallback(self):
        agent = MagicMock()
        agent.settings = {}
        agent.config = {"flash_attention": False}
        assert OllamaManager(agent)._opt("flash_attention", True) is False

    def test_default_when_neither(self):
        agent = MagicMock()
        agent.settings = {}
        agent.config = {}
        assert OllamaManager(agent)._opt("kv_cache_type", "q8_0") == "q8_0"


class TestQuantizeAndRecommend:
    """Phase 4: on-device /quantize + /recommend."""

    def _mgr(self):
        agent = MagicMock()
        agent._mem_budget = (0, "")
        return OllamaManager(agent)

    def test_quantize_rejects_unsupported_quant(self, monkeypatch):
        run = MagicMock()
        monkeypatch.setattr("ollama_manager.subprocess.run", run)
        self._mgr().quantize_model("mymodel iq2_m")
        run.assert_not_called()  # IQ not supported via ollama create

    def test_quantize_runs_ollama_create(self, monkeypatch):
        calls = {}
        def fake_run(argv, **kw):
            calls["argv"] = argv
            return SimpleNamespace(returncode=0)
        monkeypatch.setattr("ollama_manager.subprocess.run", fake_run)
        m = self._mgr()
        m.quantize_model("base-f16 q4_K_M myq")
        argv = calls["argv"]
        assert argv[:3] == [argv[0], "create", "myq"]
        assert "--quantize" in argv and "q4_K_M" in argv and "-f" in argv

    def test_quantize_default_name(self, monkeypatch):
        calls = {}
        monkeypatch.setattr("ollama_manager.subprocess.run",
                            lambda argv, **kw: calls.setdefault("argv", argv) or SimpleNamespace(returncode=0))
        self._mgr().quantize_model("llama3.1:8b-fp16 q4_K_M")
        assert "llama3.1-q4_k_m" in calls["argv"]  # derived from source + quant

    def test_quantize_usage_when_missing_args(self, monkeypatch):
        run = MagicMock()
        monkeypatch.setattr("ollama_manager.subprocess.run", run)
        self._mgr().quantize_model("onlyone")
        run.assert_not_called()

    def test_recommend_lists_curated(self, monkeypatch):
        m = self._mgr()
        seen = []
        m._registry_info = lambda n, timeout=8: (4_000_000_000, False, n)
        m.agent.console.print = lambda *a, **k: seen.append(" ".join(str(x) for x in a))
        m.recommend_models()
        blob = "\n".join(seen)
        assert "qwen2.5-coder:7b" in blob and "Recommended local coding models" in blob

    def test_commands_registered(self):
        from kodiqa import Kodiqa
        assert "/quantize" in Kodiqa._COMMAND_HANDLERS
        assert "/recommend" in Kodiqa._COMMAND_HANDLERS


class TestTuneCommand:
    """Phase 3: /tune command + Ollama options wiring (on Kodiqa)."""

    def test_ollama_options_from_settings(self):
        from kodiqa import Kodiqa
        k = MagicMock()
        k.settings = {"ollama_num_ctx": 8192, "ollama_num_gpu": -1}
        assert Kodiqa._ollama_options(k) == {"num_ctx": 8192, "num_gpu": -1}

    def test_ollama_options_empty_by_default(self):
        from kodiqa import Kodiqa
        k = MagicMock()
        k.settings = {}
        assert Kodiqa._ollama_options(k) == {}

    def test_cmd_tune_sets_and_persists(self, monkeypatch):
        import kodiqa as kmod
        from kodiqa import Kodiqa
        saved = {}
        monkeypatch.setattr(kmod, "save_settings", lambda s: saved.update(s))
        k = MagicMock()
        k.settings = {}
        Kodiqa._cmd_tune(k, "ctx 4096")
        assert k.settings["ollama_num_ctx"] == 4096 and saved["ollama_num_ctx"] == 4096

    def test_cmd_tune_kv_validates(self, monkeypatch):
        import kodiqa as kmod
        from kodiqa import Kodiqa
        monkeypatch.setattr(kmod, "save_settings", lambda s: None)
        k = MagicMock()
        k.settings = {}
        Kodiqa._cmd_tune(k, "kv bogus")
        assert "kv_cache_type" not in k.settings  # rejected
        Kodiqa._cmd_tune(k, "kv q4_0")
        assert k.settings["kv_cache_type"] == "q4_0"

    def test_cmd_tune_reset(self, monkeypatch):
        import kodiqa as kmod
        from kodiqa import Kodiqa
        monkeypatch.setattr(kmod, "save_settings", lambda s: None)
        k = MagicMock()
        k.settings = {"ollama_num_ctx": 8192, "kv_cache_type": "q4_0",
                      "flash_attention": False, "ollama_num_gpu": -1}
        Kodiqa._cmd_tune(k, "reset")
        assert k.settings == {}

    def test_tune_registered(self):
        from kodiqa import Kodiqa
        assert "/tune" in Kodiqa._COMMAND_HANDLERS


class TestAppUpdate:
    """Self-update: startup PyPI check (throttled) + /upgrade."""

    def _k(self):
        k = MagicMock()
        k.config = {}
        k.settings = {}
        return k

    def test_upgrade_runs_pip(self, monkeypatch):
        import kodiqa as kmod
        from kodiqa import Kodiqa
        calls = {}
        monkeypatch.setattr(kmod.subprocess, "run",
                            lambda argv, **kw: calls.setdefault("argv", argv) or SimpleNamespace(returncode=0))
        Kodiqa._cmd_upgrade(self._k(), "")
        assert calls["argv"][1:] == ["-m", "pip", "install", "-U", "kodiqa"]

    def test_check_throttled_skips_fetch(self, monkeypatch):
        import time as _t
        import kodiqa as kmod
        from kodiqa import Kodiqa
        got = {"fetched": False}
        monkeypatch.setattr(kmod.requests, "get", lambda *a, **k: got.update(fetched=True))
        k = self._k()
        k.settings = {"last_version_check": int(_t.time())}  # just checked → throttled
        Kodiqa._check_app_update(k)
        assert got["fetched"] is False

    def test_check_notifies_when_newer(self, monkeypatch):
        import kodiqa as kmod
        from kodiqa import Kodiqa
        monkeypatch.setattr(kmod, "save_settings", lambda s: None)
        monkeypatch.setattr(kmod.requests, "get",
                            lambda *a, **k: SimpleNamespace(json=lambda: {"info": {"version": "99.0.0"}}))
        prints = []
        k = self._k()
        k.settings = {"last_version_check": 0}
        k.console.print = lambda *a, **kw: prints.append(" ".join(str(x) for x in a))
        Kodiqa._check_app_update(k)
        assert any("available" in p for p in prints)

    def test_check_silent_when_current(self, monkeypatch):
        import kodiqa as kmod
        from kodiqa import Kodiqa
        from config import installed_version
        monkeypatch.setattr(kmod, "save_settings", lambda s: None)
        monkeypatch.setattr(kmod.requests, "get",
                            lambda *a, **k: SimpleNamespace(json=lambda: {"info": {"version": installed_version()}}))
        prints = []
        k = self._k()
        k.settings = {"last_version_check": 0}
        k.console.print = lambda *a, **kw: prints.append(" ".join(str(x) for x in a))
        Kodiqa._check_app_update(k)
        assert not any("available" in p for p in prints)

    def test_disabled_via_config(self, monkeypatch):
        import kodiqa as kmod
        from kodiqa import Kodiqa
        got = {"fetched": False}
        monkeypatch.setattr(kmod.requests, "get", lambda *a, **k: got.update(fetched=True))
        k = self._k()
        k.config = {"check_app_update": False}
        k.settings = {"last_version_check": 0}
        Kodiqa._check_app_update(k)
        assert got["fetched"] is False

    def test_upgrade_registered(self):
        from kodiqa import Kodiqa
        assert "/upgrade" in Kodiqa._COMMAND_HANDLERS


class TestFitRecommender:
    """Phase 2: 'fits my machine' memory budget + fit/quality markers."""

    def _mgr(self, budget_bytes):
        agent = MagicMock()
        agent._mem_budget = (budget_bytes, "RAM")  # pre-seed the cache
        return OllamaManager(agent)

    def test_fit_marker_boundaries(self):
        GB = 1024 ** 3
        m = self._mgr(20 * GB)  # reserve is 2GB → fits ≤18, tight ≤20, else too big
        assert "fits" in m._fit_marker(10 * GB)
        assert "tight" in m._fit_marker(19 * GB)
        assert "too big" in m._fit_marker(25 * GB)

    def test_fit_marker_empty_when_unknown(self):
        assert self._mgr(0)._fit_marker(5 * 1024 ** 3) == ""      # no budget
        assert self._mgr(20 * 1024 ** 3)._fit_marker(None) == ""  # no size

    def test_memory_budget_prefers_vram(self):
        agent = MagicMock()
        agent._mem_budget = None
        m = OllamaManager(agent)
        m._nvidia_vram_bytes = lambda: 24 * 1024 ** 3
        m._total_ram_bytes = lambda: 64 * 1024 ** 3
        budget, label = m._memory_budget()
        assert label == "VRAM" and budget == int(24 * 1024 ** 3 * 0.92)

    def test_memory_budget_falls_back_to_ram(self):
        agent = MagicMock()
        agent._mem_budget = None
        m = OllamaManager(agent)
        m._nvidia_vram_bytes = lambda: None
        m._total_ram_bytes = lambda: 32 * 1024 ** 3
        budget, label = m._memory_budget()
        assert budget == int(32 * 1024 ** 3 * 0.72) and label in ("RAM", "unified RAM")

    def test_coding_warn_flags_sub_4bit(self):
        m = OllamaManager(MagicMock())
        for q in ["IQ1_S", "IQ2_M", "IQ3_XXS", "Q2_K", "Q3_K_M", "UD-Q3_K_XL"]:
            assert "low-bit" in m._coding_quality_warn(q), q
        for q in ["Q4_K_M", "IQ4_XS", "Q5_K_M", "Q6_K", "Q8_0", "UD-Q4_K_XL"]:
            assert m._coding_quality_warn(q) == "", q


class TestServeEnv:
    """Phase 1: flash-attention + KV-cache-quant defaults for spawned servers."""

    def _mgr(self, config):
        agent = MagicMock()
        agent.config = config
        return OllamaManager(agent)

    def test_defaults_enable_flash_and_q8_kv(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_FLASH_ATTENTION", raising=False)
        monkeypatch.delenv("OLLAMA_KV_CACHE_TYPE", raising=False)
        env = self._mgr({})._serve_env()
        assert env["OLLAMA_FLASH_ATTENTION"] == "1"
        assert env["OLLAMA_KV_CACHE_TYPE"] == "q8_0"

    def test_user_env_wins(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_KV_CACHE_TYPE", "q4_0")
        monkeypatch.setenv("OLLAMA_FLASH_ATTENTION", "0")
        env = self._mgr({})._serve_env()
        assert env["OLLAMA_KV_CACHE_TYPE"] == "q4_0"  # not overridden
        assert env["OLLAMA_FLASH_ATTENTION"] == "0"

    def test_f16_kv_disables_kv_quant(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_FLASH_ATTENTION", raising=False)
        monkeypatch.delenv("OLLAMA_KV_CACHE_TYPE", raising=False)
        env = self._mgr({"kv_cache_type": "f16", "flash_attention": False})._serve_env()
        assert "OLLAMA_KV_CACHE_TYPE" not in env
        assert "OLLAMA_FLASH_ATTENTION" not in env

    def test_kv_quant_forces_flash_on(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_FLASH_ATTENTION", raising=False)
        monkeypatch.delenv("OLLAMA_KV_CACHE_TYPE", raising=False)
        # flash off but kv quant requested → flash forced on (kv needs it)
        env = self._mgr({"kv_cache_type": "q4_0", "flash_attention": False})._serve_env()
        assert env["OLLAMA_FLASH_ATTENTION"] == "1"
        assert env["OLLAMA_KV_CACHE_TYPE"] == "q4_0"

    def test_spawn_serve_passes_env(self, monkeypatch):
        captured = {}
        def fake_popen(argv, **kw):
            captured["env"] = kw.get("env")
            raise RuntimeError("stop after capture")  # don't actually wait
        monkeypatch.setattr("ollama_manager.subprocess.Popen", fake_popen)
        monkeypatch.delenv("OLLAMA_FLASH_ATTENTION", raising=False)
        self._mgr({})._spawn_serve()
        assert captured["env"]["OLLAMA_FLASH_ATTENTION"] == "1"

    def test_opts_note_reflects_settings(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_FLASH_ATTENTION", raising=False)
        monkeypatch.delenv("OLLAMA_KV_CACHE_TYPE", raising=False)
        note = self._mgr({})._serve_opts_note()
        assert "flash attn" in note and "q8_0 KV cache" in note


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
