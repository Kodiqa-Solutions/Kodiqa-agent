"""Model discovery + resolution, extracted from the Kodiqa God-class (refactor STEP 3).

ModelRegistry handles live model discovery across API providers (cached), the
`/models` listing, alias resolution, and provider lookup for a given model id.
The discovery cache lives on the agent (_cached_api_models) so it is shared with
other code; this class reads/writes it via the back-reference. Kodiqa keeps thin
wrappers so call sites are unchanged.
"""

import time

import requests
from rich.panel import Panel
from rich.prompt import Prompt

from config import (
    OLLAMA_URL, MODEL_ALIASES, CLAUDE_ALIASES, OPENAI_COMPAT_PROVIDERS,
    get_openai_provider,
)

import logging
_logger = logging.getLogger("kodiqa")


def _extract_context_len(m):
    """Pull a model's context window from a /models entry, if the provider reports
    one. Providers disagree on the key (Groq: context_window, Mistral:
    max_context_length, OpenRouter: context_length, vLLM: max_model_len), and some
    nest it under 'capabilities'. Returns an int or None."""
    keys = ("context_length", "context_window", "max_context_length", "max_model_len")
    sources = [m]
    caps = m.get("capabilities")
    if isinstance(caps, dict):
        sources.append(caps)
    for src in sources:
        for k in keys:
            v = src.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
    return None


class ModelRegistry:
    def __init__(self, agent):
        self.agent = agent

    def invalidate_model_cache(self):
        """Force the next _fetch_api_models() to re-fetch (e.g. after a key/region change)."""
        if hasattr(self.agent, "_cached_api_models"):
            self.agent._cached_api_models["_ts"] = 0

    def fetch_api_models(self):
        """Fetch live model lists from Claude and all OpenAI-compat APIs. Caches results."""
        if not hasattr(self.agent, "_cached_api_models"):
            self.agent._cached_api_models = {"claude": [], "_ts": 0}
            for prov_name in OPENAI_COMPAT_PROVIDERS:
                self.agent._cached_api_models[prov_name] = []
        # Cache for 10 minutes
        if time.time() - self.agent._cached_api_models.get("_ts", 0) < 600:
            return self.agent._cached_api_models
        # Fetch Claude models
        claude_models = []
        if self.agent.claude_key:
            try:
                resp = requests.get(
                    "https://api.anthropic.com/v1/models",
                    headers={"x-api-key": self.agent.claude_key, "anthropic-version": "2023-06-01"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    for m in data:
                        mid = m.get("id", "")
                        if mid.startswith("claude-") and "embed" not in mid:
                            claude_models.append(mid)
                    claude_models.sort()
            except Exception:
                _logger.debug("ignored error in fetch_api_models", exc_info=True)
        # Fetch all OpenAI-compatible provider models. "_context" maps model id ->
        # context window for providers that report it (Groq/Mistral/OpenRouter), so
        # _context_limit can use the official number instead of a hardcoded guess.
        result = {"claude": claude_models, "_ts": time.time(), "_context": {}}
        ctx_map = result["_context"]
        for prov_name, prov in OPENAI_COMPAT_PROVIDERS.items():
            key = self.agent.api_keys.get(prov_name, "")
            if not key:
                result[prov_name] = []
                continue
            models = []
            try:
                resp = requests.get(
                    prov.get("models_url", prov["url"].replace("/chat/completions", "/models")),
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    for m in data:
                        mid = m.get("id", "")
                        if mid:
                            models.append(mid)
                            ctx = _extract_context_len(m)
                            if ctx:
                                ctx_map[mid] = ctx
                    models.sort()
            except Exception:
                _logger.debug("ignored error in fetch_api_models", exc_info=True)
            result[prov_name] = models
        self.agent._cached_api_models = result
        return self.agent._cached_api_models

    def get_api_model_choices(self):
        """Get models from APIs that aren't already in aliases."""
        live = self.agent._fetch_api_models()
        extras = {}
        extras["claude"] = [m for m in live.get("claude", []) if m not in CLAUDE_ALIASES.values()]
        for prov_name, prov in OPENAI_COMPAT_PROVIDERS.items():
            extras[prov_name] = [m for m in live.get(prov_name, []) if m not in prov["aliases"].values()]
        return extras

    def list_models(self):
        choices = []  # list of (model_name, provider)
        n = 0
        lines = []
        if self.agent.claude_key:
            lines.append("[bold yellow]Claude API:[/]")
            for alias, model in CLAUDE_ALIASES.items():
                n += 1
                choices.append((model, "claude"))
                marker = " [cyan]◀[/]" if model == self.agent.model else ""
                lines.append(f"  [dim]{n:>3}.[/] [cyan]{model}[/] [dim](/{alias})[/]{marker}")
            extras = self.agent._get_api_model_choices()
            extra_claude = extras.get("claude", [])
            if extra_claude:
                lines.append("  [dim]── additional (from API) ──[/]")
                for m in extra_claude:
                    n += 1
                    choices.append((m, "claude"))
                    marker = " [cyan]◀[/]" if m == self.agent.model else ""
                    lines.append(f"  [dim]{n:>3}.[/] [cyan]{m}[/]{marker}")
            lines.append("")
        # All OpenAI-compatible providers
        extras = self.agent._get_api_model_choices()
        for prov_name, prov in OPENAI_COMPAT_PROVIDERS.items():
            key = self.agent.api_keys.get(prov_name, "")
            if not key:
                continue
            lines.append(f"[bold {prov['color']}]{prov['label']} API:[/]")
            for alias, model in prov["aliases"].items():
                n += 1
                choices.append((model, prov_name))
                marker = " [cyan]◀[/]" if model == self.agent.model else ""
                lines.append(f"  [dim]{n:>3}.[/] [cyan]{model}[/] [dim](/{alias})[/]{marker}")
            extra = extras.get(prov_name, [])
            if extra:
                lines.append("  [dim]── additional (from API) ──[/]")
                for m in extra:
                    n += 1
                    choices.append((m, prov_name))
                    marker = " [cyan]◀[/]" if m == self.agent.model else ""
                    lines.append(f"  [dim]{n:>3}.[/] [cyan]{m}[/]{marker}")
            lines.append("")
        lines.append("[bold green]Local Ollama:[/]")
        try:
            resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            resp.raise_for_status()
            models = resp.json().get("models", [])
            if not models:
                lines.append("  [dim]No models found. Is Ollama running?[/]")
            else:
                for m in models:
                    name = m["name"]
                    n += 1
                    choices.append((name, "local"))
                    size = m.get("size", 0)
                    size_str = f"{size / 1e9:.1f}GB" if size > 1e9 else f"{size / 1e6:.0f}MB"
                    marker = " [cyan]◀[/]" if name == self.agent.model else ""
                    lines.append(f"  [dim]{n:>3}.[/] [cyan]{name}[/] [dim]({size_str})[/]{marker}")
        except Exception:
            lines.append("  [dim]Can't reach Ollama (not running?)[/]")
        self.agent.console.print(Panel("\n".join(lines), title="Available Models", border_style="blue"))
        # Let user pick by number
        if choices:
            try:
                pick = Prompt.ask("[bold]Pick a model[/] (number or 'skip')")
            except (EOFError, KeyboardInterrupt):
                return
            pick = pick.strip()
            if pick.lower() in ("skip", ""):
                return
            if pick.isdigit() and 1 <= int(pick) <= len(choices):
                new_model, prov_name = choices[int(pick) - 1]
                self.agent.model = new_model
                self.agent._last_context_tokens = 0  # re-measure context for the new model
                self.agent.multi_models = []
                if prov_name == "claude":
                    self.agent._stop_ollama()
                    provider_str = "[yellow]Claude API[/]"
                elif prov_name == "local":
                    provider_str = "[green]Local[/]"
                    self.agent._ensure_ollama()
                else:
                    self.agent._stop_ollama()
                    prov = OPENAI_COMPAT_PROVIDERS[prov_name]
                    provider_str = f"[{prov['color']}]{prov['label']} API[/]"
                self.agent.console.print(f"Switched to [cyan]{self.agent.model}[/] ({provider_str})")
            else:
                self.agent.console.print(f"[dim]Invalid choice.[/]")

    def resolve_model_name(self, name):
        """Resolve a model alias to full model name."""
        from config import CLAUDE_ALIASES, OPENAI_COMPAT_PROVIDERS, QWEN_EXTRA_ALIASES
        if name in CLAUDE_ALIASES:
            return CLAUDE_ALIASES[name]
        if name in MODEL_ALIASES:
            return MODEL_ALIASES[name]
        for prov_data in OPENAI_COMPAT_PROVIDERS.values():
            aliases = prov_data.get("aliases", {})
            if name in aliases:
                return aliases[name]
        if name in QWEN_EXTRA_ALIASES:
            return QWEN_EXTRA_ALIASES[name]
        return name

    def is_live_claude(self, model_name):
        """Check if model is in cached live Claude model list."""
        cached = getattr(self.agent, "_cached_api_models", None)
        return cached is not None and model_name in cached.get("claude", [])

    def get_provider_for_model(self, model_name):
        """Return provider name for a model, checking aliases + live cache."""
        prov = get_openai_provider(model_name)
        if prov:
            return prov
        cached = getattr(self.agent, "_cached_api_models", {})
        for prov_name in OPENAI_COMPAT_PROVIDERS:
            if model_name in cached.get(prov_name, []):
                return prov_name
        return None
