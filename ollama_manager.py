"""Ollama lifecycle + model library, extracted from the Kodiqa God-class (refactor STEP 3).

OllamaManager owns starting/stopping the Ollama server process (tracking only the
process WE spawned), the startup update check, and the local-model operations
(/pull, /delete, discovery, library browse). Owned process state stays on the agent
(_ollama_proc, _ollama_started_by_us) so external setters are untouched; this class
reads it via the back-reference. Kodiqa keeps thin wrappers so call sites are unchanged.
"""

import os
import re
import subprocess
import time

import requests
from bs4 import BeautifulSoup
from rich.prompt import Prompt
from rich.status import Status

from config import OLLAMA_URL, OLLAMA_BIN, OLLAMA_APP_BIN, ollama_bin_has_mlx, save_settings

import logging
_logger = logging.getLogger("kodiqa")


class OllamaManager:
    def __init__(self, agent):
        self.agent = agent

    def ensure_ollama(self):
        """Make sure Ollama is running, start it if not.

        If a server is already up but can't load MLX (e.g. a Homebrew `ollama`),
        and we have an MLX-capable build available, transparently restart it with
        that build so MLX-format models (glm-5.1, …) pull and run like any other.
        """
        try:
            requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
            self._ensure_mlx_server()  # upgrade a non-MLX server in place if we can
            return True  # Already running
        except Exception:
            _logger.debug("ignored error in ensure_ollama", exc_info=True)
        # Try to start Ollama (OLLAMA_BIN already prefers the MLX-capable build)
        self.agent.console.print("[dim]Starting Ollama...[/]")
        if self._spawn_serve():
            self.agent.console.print("[green]●[/] Ollama started")
            return True
        self.agent.console.print("[yellow]●[/] Could not start Ollama [dim](start manually: ollama serve)[/]")
        return False

    def _spawn_serve(self):
        """Start `OLLAMA_BIN serve` and wait (up to 10s) until it answers. Tracks
        the process we spawned for clean shutdown. Returns True on success."""
        try:
            proc = subprocess.Popen(
                [OLLAMA_BIN, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            _logger.debug("ignored error in _spawn_serve", exc_info=True)
            return False
        for _ in range(20):
            time.sleep(0.5)
            try:
                requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
                self.agent._ollama_started_by_us = True
                self.agent._ollama_proc = proc  # track OUR process for clean shutdown
                return True
            except Exception:
                continue
        return False

    def _running_serve_bin(self):
        """Path of the `ollama serve` process currently bound to the port, or None."""
        try:
            out = subprocess.run(["ps", "-Ao", "args="], capture_output=True, text=True, timeout=5).stdout
        except Exception:
            _logger.debug("ignored error in _running_serve_bin", exc_info=True)
            return None
        for line in out.splitlines():
            line = line.strip()
            if not line or " serve" not in line:
                continue
            first = line.split()[0]
            if os.path.basename(first) == "ollama" or "/ollama" in first:
                return first
        return None

    def _ensure_mlx_server(self):
        """If the running server can't load MLX but we have an MLX-capable build,
        restart it with that build. macOS-only in practice (libmlx ships only with
        the Mac app); a no-op when already MLX-capable or no MLX build exists."""
        if not ollama_bin_has_mlx(OLLAMA_BIN):
            return  # nothing better to offer (e.g. Linux, or only Homebrew installed)
        running = self._running_serve_bin()
        if running and ollama_bin_has_mlx(running):
            return  # already serving from an MLX-capable build
        self.agent.console.print(
            "[dim]Switching Ollama to the MLX-capable build "
            "(so models like glm-5.1 install like any other)...[/]")
        # Stop whatever is on the port, then start the MLX build in its place.
        try:
            subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True, timeout=5)
        except Exception:
            _logger.debug("ignored error stopping non-MLX ollama", exc_info=True)
        for _ in range(12):  # wait for the port to free (up to ~3.6s)
            time.sleep(0.3)
            try:
                requests.get(f"{OLLAMA_URL}/api/tags", timeout=1)
            except Exception:
                break
        if self._spawn_serve():
            self.agent.console.print("[green]●[/] Ollama (MLX) ready")
        else:
            self.agent.console.print("[yellow]●[/] Could not start the MLX build [dim](models needing MLX may fail)[/]")

    def _maybe_mlx_hint(self, stderr):
        """When a pull fails because the server can't load MLX and we have no
        MLX-capable build to switch to, point the user at the fix (the Mac app)."""
        if not stderr or "mlx" not in stderr.lower():
            return
        if ollama_bin_has_mlx(OLLAMA_BIN) or os.path.exists(OLLAMA_APP_BIN):
            # We have (or just used) an MLX build — restart should have handled it;
            # don't muddy the output with install advice.
            return
        self.agent.console.print(
            "  [dim]This is an MLX-format model. The Homebrew `ollama` lacks the MLX "
            "runtime — install the official app from [/][cyan]https://ollama.com/download[/]"
            "[dim] (it bundles MLX) and Kodiqa will use it automatically.[/]")

    def _pull_one(self, name):
        """Pull a model, with a transparent cloud fallback.

        Many newer models (glm-5.1, glm-5.2, …) are published cloud-hosted: they
        have no local-weights tag, only `<name>:cloud`, so a bare `ollama pull
        <name>` fails with "file does not exist". When a tagless pull fails that
        way, retry as `<name>:cloud` so it installs like any other model.

        Returns (ok, installed_name, detail, via_cloud).
        """
        def _try(target):
            try:
                r = subprocess.run([OLLAMA_BIN, "pull", target], capture_output=True, text=True, timeout=600)
                return r.returncode == 0, (r.stderr or "").strip()
            except subprocess.TimeoutExpired:
                return False, "__timeout__"

        ok, err = _try(name)
        if ok:
            return True, name, "", False
        # Retry cloud-only models (no explicit tag + a missing-manifest / MLX error).
        low = err.lower()
        if ":" not in name and ("does not exist" in low or "manifest" in low or "mlx" in low):
            cloud = f"{name}:cloud"
            self.agent.console.print(f"  [dim]No local weights for {name} — trying cloud model [cyan]{cloud}[/][dim]...[/]")
            ok2, err2 = _try(cloud)
            if ok2:
                return True, cloud, "", True
            err = err2 if err2 and err2 != "__timeout__" else err
        return False, name, err, False

    def stop_ollama(self):
        """Stop only the Ollama process WE started (don't pkill unrelated ones)."""
        if not self.agent._ollama_started_by_us:
            return
        proc = getattr(self.agent, "_ollama_proc", None)
        try:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        _logger.debug("ignored error in stop_ollama", exc_info=True)
                self.agent.console.print("[green]●[/] Ollama stopped")
            self.agent._ollama_started_by_us = False
            self.agent._ollama_proc = None
        except Exception:
            _logger.debug("ignored error in stop_ollama", exc_info=True)

    def fetch_ollama_library(self, installed):
        """Fetch available models from ollama.com/library, filter out already installed."""
        try:
            with Status("[dim]Fetching available models from ollama.com...[/]", console=self.agent.console, spinner="dots"):
                resp = requests.get("https://ollama.com/library", timeout=10)
                resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception:
            return []

        models = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("/library/"):
                continue
            name = href.replace("/library/", "")
            if "/" in name or not name:
                continue
            # Skip embedding models — not useful for chat
            text = a.get_text(" ", strip=True).lower()
            if "embed" in name or "embedding" in text:
                continue
            # Description
            p = a.find("p")
            desc = p.get_text(strip=True) if p else ""
            # Pull count
            pulls_match = re.search(r"([\d.]+[KMB]?)\s*Pulls", a.get_text(" ", strip=True))
            pulls = pulls_match.group(1) if pulls_match else ""
            # Skip if already installed
            already_have = any(
                inst.startswith(name.split(":")[0]) for inst in installed.keys()
            )
            if not already_have:
                models.append((name, desc, pulls))

        # Return top 100 by popularity (page is already sorted by pulls)
        return models[:100]

    def check_updates(self, show_welcome=True):
        """Check for model updates and new models on startup.

        Opt-out via --no-update / config check_updates=false, and throttled to run
        at most once per update_check_interval_hours (default 24) so the per-model
        `ollama pull` sweep + ollama.com scrape don't block every launch.
        Called with show_welcome=False from /update (mid-session, banner already shown).
        """

        if getattr(self.agent, "_skip_updates", False) or not self.agent.config.get("check_updates", True):
            return
        interval_h = self.agent.config.get("update_check_interval_hours", 24)
        last = self.agent.settings.get("last_update_check", 0)
        if interval_h > 0 and (time.time() - last) < interval_h * 3600:
            return

        if not self.agent._ensure_ollama():
            return
        # Record now so a skipped/failed check still throttles the next launch.
        self.agent.settings["last_update_check"] = time.time()
        try:
            save_settings(self.agent.settings)
        except Exception:
            _logger.debug("ignored error in check_updates", exc_info=True)

        try:
            # Get installed models
            resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            resp.raise_for_status()
            installed = {m["name"]: m for m in resp.json().get("models", [])}
        except Exception:
            return

        # 1. Check installed models for updates
        if not installed:
            self.agent.console.print("\n[yellow]No local models installed.[/]")
        else:
            self.agent.console.print(f"\n[dim]Checking {len(installed)} installed models for updates...[/]")
            updated_count = 0
            for model_name in list(installed.keys()):
                try:
                    # Record the model's digest before pulling so we can tell whether
                    # anything actually changed ("ollama pull" prints "success" either way).
                    old_digest = installed[model_name].get("digest")
                    with Status(f"  [dim]Checking {model_name}...[/]", console=self.agent.console, spinner="dots"):
                        result = subprocess.run(
                            [OLLAMA_BIN, "pull", model_name],
                            capture_output=True, text=True, timeout=120,
                        )
                    if result.returncode != 0:
                        self.agent.console.print(f"  [yellow]●[/] {model_name} [dim]check failed[/]")
                        continue
                    # Compare digest after the pull to detect a real update.
                    new_digest = old_digest
                    try:
                        tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5).json().get("models", [])
                        for m in tags:
                            if m["name"] == model_name:
                                new_digest = m.get("digest")
                                break
                    except Exception:
                        _logger.debug("ignored error in check_updates", exc_info=True)
                    if old_digest and new_digest and new_digest != old_digest:
                        self.agent.console.print(f"  [green]●[/] {model_name} [bold green]updated![/]")
                        updated_count += 1
                    else:
                        self.agent.console.print(f"  [green]●[/] {model_name} [dim]up to date[/]")
                except subprocess.TimeoutExpired:
                    self.agent.console.print(f"  [yellow]●[/] {model_name} [dim]timeout[/]")
                except Exception:
                    continue

            if updated_count > 0:
                self.agent.console.print(f"\n[green]{updated_count} model(s) updated![/]")

        # Show welcome before new models list (skipped when invoked mid-session via /update)
        if show_welcome:
            self.agent._welcome()
            self.agent._welcome_shown = True

        # 2. Fetch available models from Ollama library
        new_models = self.agent._fetch_ollama_library(installed)
        if not new_models:
            return

        # Show new models available
        self.agent.console.print(f"\n[bold yellow]Top {len(new_models)} new models available[/] [dim](most popular on ollama.com/library)[/]:")
        for i, (model, desc, pulls) in enumerate(new_models, 1):
            pulls_str = f" [dim]({pulls} pulls)[/]" if pulls else ""
            self.agent.console.print(f"  [cyan bold]{i}.[/] [cyan]{model}[/] — {desc[:70]}{pulls_str}")

        try:
            answer = Prompt.ask(
                "\n[bold]Pull new models?[/] [dim](enter numbers, 'all', or 'skip')[/]",
                default="skip"
            )
        except (EOFError, KeyboardInterrupt):
            return

        if answer.strip().lower() == "skip":
            return

        to_pull = []
        if answer.strip().lower() == "all":
            to_pull = [m for m, _, _ in new_models]
        else:
            # Parse numbers or model names
            parts = answer.replace(",", " ").split()
            for part in parts:
                try:
                    idx = int(part) - 1
                    if 0 <= idx < len(new_models):
                        to_pull.append(new_models[idx][0])
                except ValueError:
                    # Maybe they typed a model name (require 3+ chars to avoid accidental matches)
                    if len(part) >= 3:
                        for model, _, _ in new_models:
                            if part.lower() in model.lower():
                                to_pull.append(model)
                                break

        if not to_pull:
            return

        installed_names = []
        for model in to_pull:
            self.agent.console.print(f"\n  [yellow]●[/] Pulling [cyan]{model}[/]...")
            try:
                ok, name, detail, via_cloud = self._pull_one(model)
            except Exception as e:
                self.agent.console.print(f"  [red]●[/] Error: {e}")
                continue
            if ok:
                installed_names.append(name)
                self.agent.console.print(f"  [green]●[/] [cyan]{name}[/] installed!")
                if via_cloud:
                    self.agent.console.print("  [dim]Cloud model — run [/][cyan]ollama signin[/][dim] once to use it.[/]")
            elif detail == "__timeout__":
                self.agent.console.print(f"  [red]●[/] Timeout pulling {model}")
            else:
                self.agent.console.print(f"  [red]●[/] Failed to pull {model}: {detail[:100]}")
                self._maybe_mlx_hint(detail)

        self.agent.console.print(f"\n[green]Models pulled! Use /multi all for multi-model mode.[/]")
        # Auto-set model if current one wasn't installed (use the name that actually
        # installed — may be the :cloud variant).
        if not installed and installed_names:
            self.agent.model = installed_names[0]
            self.agent.console.print(f"Model set to [cyan]{self.agent.model}[/]")

    def installed_ollama_models(self):
        """Return [(name, size_str), ...] for locally installed Ollama models."""
        try:
            resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            resp.raise_for_status()
            out = []
            for m in resp.json().get("models", []):
                size = m.get("size", 0)
                size_str = f"{size / 1e9:.1f}GB" if size > 1e9 else f"{size / 1e6:.0f}MB"
                out.append((m["name"], size_str))
            return out
        except Exception:
            return None

    def pull_model(self, arg):
        """Pull (download) an Ollama model by name on demand."""
        if not arg:
            self.agent.console.print("[yellow]Usage: /pull <model>[/] [dim](e.g. /pull llama3.2)[/]")
            return
        for name in arg.replace(",", " ").split():
            self.agent.console.print(f"\n  [yellow]●[/] Pulling [cyan]{name}[/]...")
            try:
                ok, installed_name, detail, via_cloud = self._pull_one(name)
            except Exception as e:
                self.agent.console.print(f"  [red]●[/] Error: {e}")
                continue
            if ok:
                self.agent.console.print(f"  [green]●[/] [cyan]{installed_name}[/] installed!")
                if via_cloud:
                    self.agent.console.print("  [dim]Cloud model — run [/][cyan]ollama signin[/][dim] once to use it.[/]")
                self.agent._invalidate_model_cache()
            elif detail == "__timeout__":
                self.agent.console.print(f"  [red]●[/] Timeout pulling {name}")
            else:
                self.agent.console.print(f"  [red]●[/] Failed to pull {name}: {detail[:120]}")
                self._maybe_mlx_hint(detail)

    def delete_model(self, arg):
        """Delete one or more locally downloaded Ollama models (frees disk)."""
        models = self.agent._installed_ollama_models()
        if models is None:
            self.agent.console.print("[red]Can't reach Ollama (not running?)[/]")
            return
        if not models:
            self.agent.console.print("[yellow]No local models installed.[/]")
            return

        # Resolve targets: explicit arg(s), or interactive pick.
        targets = []
        if arg:
            wanted = arg.replace(",", " ").split()
            for w in wanted:
                match = next((n for n, _ in models if n == w), None) \
                    or next((n for n, _ in models if w.lower() in n.lower()), None)
                if match:
                    targets.append(match)
                else:
                    self.agent.console.print(f"[yellow]No installed model matches '{w}'.[/]")
        else:
            self.agent.console.print("[bold]Local models:[/]")
            for i, (name, size_str) in enumerate(models, 1):
                marker = " [cyan]◀ (current)[/]" if name == self.agent.model else ""
                self.agent.console.print(f"  [cyan bold]{i}.[/] [cyan]{name}[/] [dim]({size_str})[/]{marker}")
            try:
                answer = Prompt.ask("\n[bold red]Delete which?[/] [dim](numbers, 'all', or 'cancel')[/]", default="cancel")
            except (EOFError, KeyboardInterrupt):
                return
            answer = answer.strip().lower()
            if answer in ("cancel", "skip", ""):
                return
            if answer == "all":
                targets = [n for n, _ in models]
            else:
                for part in answer.replace(",", " ").split():
                    if part.isdigit() and 1 <= int(part) <= len(models):
                        targets.append(models[int(part) - 1][0])

        targets = list(dict.fromkeys(targets))  # dedupe, keep order
        if not targets:
            return

        # Confirm — deletion is destructive and irreversible (must re-download).
        self.agent.console.print(f"\n[yellow]Will delete:[/] {', '.join(targets)}")
        try:
            confirm = Prompt.ask("[bold red]Confirm delete?[/]", choices=["y", "n"], default="n")
        except (EOFError, KeyboardInterrupt):
            return
        if confirm != "y":
            self.agent.console.print("[dim]Cancelled.[/]")
            return

        for name in targets:
            try:
                with Status(f"  [dim]Deleting {name}...[/]", console=self.agent.console, spinner="dots"):
                    result = subprocess.run([OLLAMA_BIN, "rm", name], capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    self.agent.console.print(f"  [green]●[/] Deleted [cyan]{name}[/]")
                    self.agent._invalidate_model_cache()
                    if name == self.agent.model:
                        self.agent.console.print(f"  [yellow]Note: {name} was the active model. Use /model to switch.[/]")
                else:
                    self.agent.console.print(f"  [red]●[/] Failed to delete {name}: {(result.stderr or '').strip()[:100]}")
            except subprocess.TimeoutExpired:
                self.agent.console.print(f"  [red]●[/] Timeout deleting {name}")
            except Exception as e:
                self.agent.console.print(f"  [red]●[/] Error: {e}")

    def discover_models(self):
        """Auto-discover all installed Ollama models for multi-mode default."""
        try:
            resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            return models if models else []
        except Exception:
            return []
