"""A failed registry lookup must never be read as "this model has no local weights".

The bug this locks down: `_manifest_layers` returned None both for a real 404 and
for a timeout. `_registry_info` then fell through to the `:cloud` probe — which
succeeds for most popular models, since they publish a `:cloud` tag *alongside*
their local weights. Result: gemma4 (9.6 GB of real GGUF layers) was shown as
"☁ cloud", i.e. "only online, not downloadable", and a pull could switch to
`gemma4:cloud`, which then 401s without `ollama signin`.

Under 24-way concurrency a short per-request deadline is hit routinely, so this
was not a rare edge — it was "some models can't be downloaded locally today".
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import ollama_manager
from ollama_manager import _LOOKUP_FAILED, OllamaManager

# gemma4's real shape: local weights on :latest AND a :cloud pointer next to them.
LOCAL_MANIFEST = {"layers": [
    {"mediaType": "application/vnd.ollama.image.model", "size": 9_608_338_848},
    {"mediaType": "application/vnd.ollama.image.license", "size": 11_355},
]}


def _registry(by_tag, fail_tags=(), status_for=None):
    """requests.get stand-in. `fail_tags` raise (timeout/DNS); `status_for` maps a
    tag to an HTTP status to return instead of a body."""
    def get(url, *a, **k):
        if "ollama.com/library/" in url:
            return SimpleNamespace(status_code=200, text="")
        tag = url.rsplit("/", 1)[-1]
        if tag in fail_tags:
            raise OSError("timed out")
        if status_for and tag in status_for:
            return SimpleNamespace(status_code=status_for[tag], json=lambda: {})
        body = by_tag.get(tag)
        if body is None:
            return SimpleNamespace(status_code=404, json=lambda: {})
        return SimpleNamespace(status_code=200, json=lambda: body)
    return get


class TestManifestLayersSeparatesAbsentFromUnknown:
    def test_404_means_absent(self, monkeypatch):
        monkeypatch.setattr(ollama_manager.requests, "get", _registry({}))
        assert OllamaManager(MagicMock())._manifest_layers("library/x", "latest", 4) is None

    def test_timeout_means_unknown(self, monkeypatch):
        monkeypatch.setattr(ollama_manager.requests, "get", _registry({}, fail_tags=("latest",)))
        got = OllamaManager(MagicMock())._manifest_layers("library/x", "latest", 4)
        assert got is _LOOKUP_FAILED

    def test_server_error_means_unknown(self, monkeypatch):
        monkeypatch.setattr(ollama_manager.requests, "get",
                            _registry({}, status_for={"latest": 503}))
        got = OllamaManager(MagicMock())._manifest_layers("library/x", "latest", 4)
        assert got is _LOOKUP_FAILED

    def test_rate_limit_means_unknown(self, monkeypatch):
        monkeypatch.setattr(ollama_manager.requests, "get",
                            _registry({}, status_for={"latest": 429}))
        assert OllamaManager(MagicMock())._manifest_layers("library/x", "latest", 4) is _LOOKUP_FAILED


class TestRegistryInfoDoesNotGuessCloud:
    def test_timed_out_latest_is_not_reported_as_cloud(self, monkeypatch):
        """The gemma4 regression: :latest lookup times out, :cloud answers → the
        model must come back unknown, never "cloud"."""
        monkeypatch.setattr(ollama_manager.requests, "get",
                            _registry({"latest": LOCAL_MANIFEST, "cloud": {"layers": None}},
                                      fail_tags=("latest",)))
        m = OllamaManager(MagicMock())
        size, is_cloud, pull = m._registry_info("gemma4")
        assert is_cloud is False, "a timed-out lookup was reported as a cloud-only model"
        assert size is None and pull == "gemma4"
        assert "?" in m._size_tag((size, is_cloud, pull))

    def test_a_clean_lookup_still_sees_the_local_weights(self, monkeypatch):
        monkeypatch.setattr(ollama_manager.requests, "get",
                            _registry({"latest": LOCAL_MANIFEST, "cloud": {"layers": None}}))
        size, is_cloud, _pull = OllamaManager(MagicMock())._registry_info("gemma4")
        assert size == 9_608_350_203 and is_cloud is False

    def test_a_genuine_cloud_only_model_is_still_cloud(self, monkeypatch):
        # No :latest at all (real 404), only :cloud → cloud, as before.
        monkeypatch.setattr(ollama_manager.requests, "get", _registry({"cloud": {"layers": None}}))
        size, is_cloud, pull = OllamaManager(MagicMock())._registry_info("glm-5.1")
        assert size is None and is_cloud is True and pull == "glm-5.1"

    def test_failed_cloud_probe_is_unknown_not_cloud(self, monkeypatch):
        # :latest is a real 404, but the :cloud probe itself fails → unknown.
        monkeypatch.setattr(ollama_manager.requests, "get", _registry({}, fail_tags=("cloud",)))
        size, is_cloud, _pull = OllamaManager(MagicMock())._registry_info("mystery")
        assert size is None and is_cloud is False

    def test_failed_sized_tag_lookup_does_not_crash(self, monkeypatch):
        """The tags page names an `8b` tag but fetching its manifest fails —
        summing _LOOKUP_FAILED would raise."""
        def get(url, *a, **k):
            if "ollama.com/library/" in url:
                return SimpleNamespace(status_code=200, text="granite:8b")
            tag = url.rsplit("/", 1)[-1]
            if tag == "8b":
                raise OSError("timed out")
            return SimpleNamespace(status_code=404, json=lambda: {})
        monkeypatch.setattr(ollama_manager.requests, "get", get)
        size, is_cloud, pull = OllamaManager(MagicMock())._registry_info("granite")
        assert size is None and is_cloud is False and pull == "granite:8b"


class TestBulkLookupRetriesUnknowns:
    def test_a_model_that_only_answers_on_the_retry_gets_its_size(self):
        m = OllamaManager(MagicMock())
        seen = []

        def info(name, timeout=8):
            seen.append((name, timeout))
            if name == "gemma4" and timeout <= 6:
                return None, False, name          # first sweep: timed out
            return 9_608_338_848, False, name

        m._registry_info = info
        out = m._registry_infos(["gemma4", "llama3.2"])
        assert out["gemma4"][0] == 9_608_338_848
        assert ("gemma4", 15) in seen                     # retried with room to breathe
        assert ("llama3.2", 15) not in seen               # a model that answered isn't redone

    def test_cloud_models_are_not_retried(self):
        m = OllamaManager(MagicMock())
        seen = []

        def info(name, timeout=8):
            seen.append((name, timeout))
            return None, True, name

        m._registry_info = info
        assert m._registry_infos(["glm-5.1"])["glm-5.1"][1] is True
        assert len(seen) == 1


class TestPullKeepsLocalModelsLocal:
    def _mgr(self, registry_size):
        m = OllamaManager(MagicMock())
        m._registry_info = lambda name, timeout=8: (registry_size, registry_size is None, name)
        return m

    def test_a_failed_pull_of_a_local_model_does_not_become_cloud(self):
        m = self._mgr(9_608_338_848)
        pulled = []

        def run_pull(target):
            pulled.append(target)
            return 1, "Error: pull model manifest: file does not exist", False

        m._run_pull = run_pull
        ok, name, detail, via_cloud = m._pull_one("gemma4")
        assert ok is False and via_cloud is False and name == "gemma4"
        assert pulled == ["gemma4"], "silently retried a local model as :cloud"
        assert "file does not exist" in detail

    def test_a_genuinely_cloud_only_model_still_falls_back(self):
        m = self._mgr(None)  # no local weights in the registry

        def run_pull(target):
            return (0, "", False) if target.endswith(":cloud") else (1, "file does not exist", False)

        m._run_pull = run_pull
        ok, name, _detail, via_cloud = m._pull_one("glm-5.1")
        assert ok is True and via_cloud is True and name == "glm-5.1:cloud"
