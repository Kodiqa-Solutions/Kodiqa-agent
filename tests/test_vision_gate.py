"""Tests for the vision-capability gate (don't send images to text-only models)."""

from unittest.mock import MagicMock

from kodiqa import Kodiqa


def _agent(model, provider):
    k = MagicMock()
    k.model = model
    k._is_live_claude = lambda m: False
    k._get_provider_for_model = lambda m: provider
    return k


class TestModelSupportsVision:
    def test_deepseek_is_text_only(self):
        assert Kodiqa._model_supports_vision(_agent("deepseek-v4-pro", "deepseek")) is False
        assert Kodiqa._model_supports_vision(_agent("deepseek-chat", "deepseek")) is False

    def test_claude_supports_vision(self):
        k = _agent("claude-sonnet-4-6", None)
        # is_claude_model handles the claude case; force the branch
        import kodiqa
        assert kodiqa.is_claude_model("claude-sonnet-4-6")
        assert Kodiqa._model_supports_vision(k) is True

    def test_openai_assumed_capable(self):
        assert Kodiqa._model_supports_vision(_agent("gpt-4o", "openai")) is True

    def test_mistral_only_pixtral(self):
        assert Kodiqa._model_supports_vision(_agent("pixtral-large", "mistral")) is True
        assert Kodiqa._model_supports_vision(_agent("mistral-large-latest", "mistral")) is False

    def test_groq_only_vision_models(self):
        assert Kodiqa._model_supports_vision(_agent("llama-3.2-90b-vision", "groq")) is True
        assert Kodiqa._model_supports_vision(_agent("llama-3.3-70b-versatile", "groq")) is False


class TestImageGateInChat:
    def _agent(self, model, provider, images):
        k = MagicMock()
        k.model = model
        k.history = []
        k._pending_files = []
        k._pending_images = images
        k._is_live_claude = lambda m: False
        k._get_provider_for_model = lambda m: provider
        k._append_files_to_text = lambda msg, files: msg
        k._model_supports_vision = lambda: Kodiqa._model_supports_vision(k)
        k._build_system_prompt = lambda t: "SYS"
        k._assistant_msg = lambda kind, text, tcs: {"role": "assistant", "content": text}
        # stop the loop immediately after the user message is appended
        k._stream_native_with_failover = lambda kind, prov, sp: (
            {"text": "ok", "tool_calls": []}, kind, prov)
        k._stream_interrupted = False
        k.config = {"max_iterations": 40}
        return k

    def test_text_only_model_drops_image_content(self):
        img = {"media_type": "image/png", "data": "QQ==", "path": "/x.png"}
        k = self._agent("deepseek-v4-pro", "deepseek", [img])
        Kodiqa._run_native_chat(k, "check this", "openai")
        user_msg = k.history[0]
        # text-only model → content is a plain string, NOT a list with image_url
        assert isinstance(user_msg["content"], str)
        assert "can't view images" in user_msg["content"]

    def test_vision_model_keeps_image_block(self):
        img = {"media_type": "image/png", "data": "QQ==", "path": "/x.png"}
        k = self._agent("gpt-4o", "openai", [img])
        Kodiqa._run_native_chat(k, "check this", "openai")
        user_msg = k.history[0]
        assert isinstance(user_msg["content"], list)
        assert any(b.get("type") == "image_url" for b in user_msg["content"])


class TestBuilderStripsImagesForTextOnly:
    """Defensive: an image already in history must not poison a text-only model
    (the 'can't chat anymore' bug) — _build_openai_messages strips it to a note."""

    def test_strips_image_for_deepseek(self):
        k = MagicMock()
        k.history = [{"role": "user", "content": [
            {"type": "text", "text": "check this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}}]}]
        k._model_supports_vision = lambda: False
        msgs = Kodiqa._build_openai_messages(k, "SYS")
        user = [m for m in msgs if m["role"] == "user"][0]
        assert isinstance(user["content"], str)
        assert "image omitted" in user["content"] and "check this" in user["content"]

    def test_keeps_image_for_vision_model(self):
        k = MagicMock()
        k.history = [{"role": "user", "content": [
            {"type": "text", "text": "check this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}}]}]
        k._model_supports_vision = lambda: True
        msgs = Kodiqa._build_openai_messages(k, "SYS")
        user = [m for m in msgs if m["role"] == "user"][0]
        assert isinstance(user["content"], list)
        assert any(b.get("type") == "image_url" for b in user["content"])
