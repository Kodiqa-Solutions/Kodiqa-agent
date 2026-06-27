"""Ollama lifecycle + model library, extracted from the Kodiqa God-class (refactor STEP 3).

OllamaManager owns starting/stopping the Ollama server process (tracking only the
process WE spawned), the startup update check, and the local-model operations
(/pull, /delete, discovery, library browse). Owned process state stays on the agent
(_ollama_proc, _ollama_started_by_us) so external setters are untouched; this class
reads it via the back-reference. Kodiqa keeps thin wrappers so call sites are unchanged.
"""

import os
import re
import signal
import subprocess
import sys
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
            self.agent.console.print(f"[green]●[/] Ollama started{self._serve_opts_note()}")
            return True
        self.agent.console.print("[yellow]●[/] Could not start Ollama [dim](start manually: ollama serve)[/]")
        return False

    def _serve_env(self):
        """Environment for a server WE spawn. Ollama ships flash attention and
        KV-cache quantization OFF; we turn them on by default because they cut RAM
        (KV cache → ½ at q8_0, ¼ at q4_0) and speed up long contexts with
        negligible quality loss. User-set env vars always win (setdefault), and
        config keys `flash_attention` / `kv_cache_type` (f16 = off) tune it."""
        env = os.environ.copy()
        cfg = getattr(self.agent, "config", {}) or {}
        kv = cfg.get("kv_cache_type", "q8_0")
        want_flash = cfg.get("flash_attention", True) or (kv and kv != "f16")
        if want_flash:  # KV-cache quant only takes effect with flash attention on
            env.setdefault("OLLAMA_FLASH_ATTENTION", "1")
        if kv and kv != "f16":
            env.setdefault("OLLAMA_KV_CACHE_TYPE", kv)
        return env

    def _serve_opts_note(self):
        """A dim ' (flash attn, q8_0 KV cache)' suffix describing the speed/memory
        options we enabled, or '' if none — for the startup message."""
        env = self._serve_env()
        bits = []
        if env.get("OLLAMA_FLASH_ATTENTION") == "1":
            bits.append("flash attn")
        if env.get("OLLAMA_KV_CACHE_TYPE"):
            bits.append(f"{env['OLLAMA_KV_CACHE_TYPE']} KV cache")
        return f" [dim]({', '.join(bits)})[/]" if bits else ""

    def _spawn_serve(self):
        """Start `OLLAMA_BIN serve` and wait (up to 10s) until it answers. Tracks
        the process we spawned for clean shutdown. Returns True on success."""
        try:
            proc = subprocess.Popen(
                [OLLAMA_BIN, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._serve_env(),
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
                self._write_server_pid(proc.pid)  # so a later session can stop it too
                return True
            except Exception:
                continue
        return False

    def _server_pid_file(self):
        return os.path.join(os.path.expanduser("~/.kodiqa"), "ollama_server.pid")

    def _write_server_pid(self, pid):
        """Record the PID of an Ollama server WE spawned, so any later Kodiqa
        session can clean it up (the in-memory _ollama_started_by_us flag doesn't
        survive a restart, which is why a Kodiqa-started server used to linger)."""
        try:
            with open(self._server_pid_file(), "w") as f:
                f.write(str(pid))
        except Exception:
            _logger.debug("ignored error writing ollama pid file", exc_info=True)

    def _stop_orphaned_server(self):
        """Stop an Ollama server a PREVIOUS Kodiqa session spawned (per the pid
        file) but didn't get to stop. Only touches that exact PID if it's still an
        `ollama serve` — never a GUI-app or user-started server."""
        path = self._server_pid_file()
        try:
            with open(path) as f:
                pid = int(f.read().strip())
        except Exception:
            return  # no pid file / unreadable
        try:
            args = subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                                  capture_output=True, text=True, timeout=5).stdout
            if "ollama" in args and "serve" in args:
                os.kill(pid, signal.SIGTERM)
                self.agent.console.print("[green]●[/] Ollama stopped")
        except ProcessLookupError:
            pass  # already gone
        except Exception:
            _logger.debug("ignored error stopping orphaned ollama", exc_info=True)
        finally:
            try:
                os.remove(path)
            except Exception:
                _logger.debug("ignored error removing ollama pid file", exc_info=True)

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
            self.agent.console.print(f"[green]●[/] Ollama (MLX) ready{self._serve_opts_note()}")
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

    def _manifest_layers(self, repo, tag, timeout):
        """Return a registry manifest's layers list (`[]` when the manifest exists
        but has no layers, e.g. a cloud pointer), or None when it doesn't exist."""
        try:
            resp = requests.get(
                f"https://registry.ollama.ai/v2/{repo}/manifests/{tag}",
                headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
                timeout=timeout,
            )
            if resp.status_code != 200:
                return None
            return resp.json().get("layers") or []  # null/missing layers → [] (exists)
        except Exception:
            _logger.debug("ignored error fetching manifest", exc_info=True)
            return None

    def _tags_page_tags(self, name, timeout=8):
        """Scrape ollama.com/library/<name>/tags → ordered list of tag strings
        (without the 'name:' prefix). The library page lists the recommended tag
        first. [] on failure. Used to resolve models whose registry has no
        `latest`/`cloud` manifest (e.g. only `:8b` or a `:675b-cloud` tag)."""
        try:
            resp = requests.get(f"https://ollama.com/library/{name}/tags", timeout=timeout)
            if resp.status_code != 200:
                return []
            found = re.findall(rf"{re.escape(name)}:([A-Za-z0-9._\-]+)", resp.text)
        except Exception:
            _logger.debug("ignored error fetching tags page", exc_info=True)
            return []
        seen, out = set(), []
        for t in found:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def _registry_info(self, name, timeout=8):
        """Inspect an Ollama model without downloading. Returns
        (size_bytes, is_cloud, pull_name):
          - a real GB size for normal local-weights models;
          - (None, True, …) for cloud-hosted models (run on Ollama's servers);
          - (None, False, name) when genuinely unknown.
        `pull_name` is the name to actually `ollama pull` — usually the original,
        but a resolved `<name>:<tag>` for models with no default `latest` tag."""
        repo, tag = (name.rsplit(":", 1) + ["latest"])[:2] if ":" in name else (name, "latest")
        if "/" not in repo:
            repo = f"library/{repo}"
        layers = self._manifest_layers(repo, tag, timeout)
        if layers:  # real local weights
            total = sum(layer.get("size", 0) for layer in layers)
            return (total or None), False, name
        if layers is not None:  # manifest exists but has no layers → cloud pointer
            return None, True, name
        if tag != "latest":
            return None, False, name  # an explicit tag that genuinely doesn't exist
        # Bare name with no `latest`. Cheap check: a plain `:cloud` tag in the registry.
        if self._manifest_layers(repo, "cloud", timeout) is not None:
            return None, True, name  # _pull_with_fallbacks handles the :cloud retry
        # Fall back to the library tags page for non-standard tags (e.g. `675b-cloud`,
        # or sized-only models like `granite4.1-guardian:8b`).
        tags = self._tags_page_tags(name, timeout)
        local = [t for t in tags if "cloud" not in t.lower()]
        cloud = [t for t in tags if "cloud" in t.lower()]
        if local:
            chosen = local[0]  # the page lists the recommended/default tag first
            sz_layers = self._manifest_layers(repo, chosen, timeout)
            size = sum(layer.get("size", 0) for layer in sz_layers) if sz_layers else None
            return (size or None), False, f"{name}:{chosen}"
        if cloud:
            return None, True, f"{name}:{cloud[0]}"  # non-standard cloud tag → resolve it
        return None, False, name

    def _registry_size(self, name, timeout=8):
        """Convenience: just the download size in bytes (None if unknown/cloud)."""
        return self._registry_info(name, timeout)[0]

    def _registry_infos(self, names):
        """Concurrently inspect many models (no downloads). Returns
        {name: (size_bytes, is_cloud, pull_name)}. Used to annotate the model list."""
        from concurrent.futures import ThreadPoolExecutor
        out = {}
        if not names:
            return out
        try:
            with ThreadPoolExecutor(max_workers=min(24, len(names))) as ex:
                for name, info in zip(names, ex.map(lambda n: self._registry_info(n, timeout=4), names)):
                    out[name] = info
        except Exception:
            _logger.debug("ignored error in _registry_infos", exc_info=True)
        for n in names:
            out.setdefault(n, (None, False, n))
        return out

    @staticmethod
    def _info_pull_name(info, fallback):
        """The resolved pull target from a (size, is_cloud, pull_name) tuple."""
        return info[2] if info and len(info) > 2 and info[2] else fallback

    def _size_tag(self, info):
        """Render the size/cloud annotation for a (size, is_cloud[, pull_name]) tuple."""
        if not info:
            return "[dim]size ?[/]"
        size, is_cloud = info[0], info[1]
        if size:
            return f"[yellow]~{self._fmt_size(size)}[/]"
        if is_cloud:
            return "[magenta]☁ cloud[/]"
        return "[dim]size ?[/]"

    @staticmethod
    def _fmt_size(nbytes):
        """Human-readable size, or '?' when unknown."""
        if not nbytes:
            return "?"
        if nbytes >= 1e9:
            return f"{nbytes / 1e9:.1f} GB"
        return f"{nbytes / 1e6:.0f} MB"

    def _run_pull(self, target):
        """Run `ollama pull target`, streaming Ollama's live progress to the
        console while capturing the text (so callers can inspect failures).

        Press Esc (confirm) or Ctrl+C (immediate) to cancel — Ollama caches the
        partial download, so re-pulling resumes. Returns (returncode, output,
        cancelled)."""
        try:
            proc = subprocess.Popen(
                [OLLAMA_BIN, "pull", target],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True,  # isolate from terminal signals; we drive cancel
            )
        except Exception as e:
            _logger.debug("ignored error launching pull", exc_info=True)
            return 1, str(e), False

        chunks = []
        cancelled = False
        pfd = proc.stdout.fileno()
        infd = sys.stdin.fileno()
        old = None
        is_tty = False
        try:
            is_tty = sys.stdin.isatty()
        except Exception:
            is_tty = False
        if is_tty:
            try:
                import termios
                import tty
                old = termios.tcgetattr(infd)
                tty.setcbreak(infd)
            except Exception:
                old = None

        try:
            import select
            self.agent.console.print("  [dim](press Esc to cancel)[/]")
            while True:
                watch = [pfd] + ([infd] if old is not None else [])
                r, _, _ = select.select(watch, [], [], 0.3)
                if pfd in r:
                    data = os.read(pfd, 4096)
                    if not data:
                        break
                    chunks.append(data)
                    sys.stdout.write(data.decode("utf-8", "replace"))
                    sys.stdout.flush()
                    continue
                if old is not None and infd in r:
                    ch = os.read(infd, 1)
                    if ch in (b"\x1b", b"q"):  # Esc / q → confirm cancel
                        sys.stdout.write("\r\n  Cancel this download? [y/N] ")
                        sys.stdout.flush()
                        ans = os.read(infd, 1)
                        if ans in (b"y", b"Y"):
                            cancelled = True
                            break
                        sys.stdout.write("resuming…\r\n")
                        sys.stdout.flush()
                if pfd not in r and proc.poll() is not None:
                    rest = os.read(pfd, 1 << 16)
                    if rest:
                        chunks.append(rest)
                        sys.stdout.write(rest.decode("utf-8", "replace"))
                        sys.stdout.flush()
                    break
        except KeyboardInterrupt:
            cancelled = True  # Ctrl+C → immediate cancel
        finally:
            if old is not None:
                try:
                    import termios
                    termios.tcsetattr(infd, termios.TCSADRAIN, old)
                except Exception:
                    _logger.debug("ignored error restoring tty", exc_info=True)
            if cancelled and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        _logger.debug("ignored error killing pull", exc_info=True)
            elif proc.poll() is None:
                proc.wait()
            try:
                proc.stdout.close()
            except Exception:
                _logger.debug("ignored error closing pull stdout", exc_info=True)

        out = b"".join(chunks).decode("utf-8", "replace")
        return (proc.returncode or 0), out, cancelled

    def _pull_one(self, name):
        """Pull a model, with a transparent cloud fallback.

        Many newer models (glm-5.1, glm-5.2, …) are published cloud-hosted: they
        have no local-weights tag, only `<name>:cloud`, so a bare `ollama pull
        <name>` fails with "file does not exist". When a tagless pull fails that
        way, retry as `<name>:cloud` so it installs like any other model.

        Returns (ok, installed_name, detail, via_cloud). On cancellation, detail
        is "__cancelled__".
        """
        rc, out, cancelled = self._run_pull(name)
        if cancelled:
            return False, name, "__cancelled__", False
        if rc == 0:
            return True, name, "", False
        # Retry cloud-only models (no explicit tag + a missing-manifest / MLX error).
        low = out.lower()
        if ":" not in name and ("does not exist" in low or "manifest" in low or "mlx" in low):
            cloud = f"{name}:cloud"
            self.agent.console.print(f"  [dim]No local weights for {name} — trying cloud model [cyan]{cloud}[/][dim]...[/]")
            rc2, out2, canc2 = self._run_pull(cloud)
            if canc2:
                return False, cloud, "__cancelled__", False
            if rc2 == 0:
                return True, cloud, "", True
            out = out2 or out
        return False, name, out.strip(), False

    def _pull_with_fallbacks(self, name):
        """Pull `name`, escalating through every source so a model from the list
        installs whichever way it's published:
          1. Ollama registry (`ollama pull <name>`)
          2. Ollama cloud (`<name>:cloud`) for cloud-hosted models
          3. HuggingFace GGUF (`ollama pull hf.co/<repo>:<quant>`) when Ollama
             doesn't host it but a community GGUF build exists.
        Prints progress/results. Returns (ok, installed_name, cancelled)."""
        ok, installed, detail, via_cloud = self._pull_one(name)
        if ok:
            self.agent.console.print(f"  [green]●[/] [cyan]{installed}[/] installed!")
            if via_cloud or "cloud" in installed.lower():
                self.agent.console.print("  [dim]Cloud model — run [/][cyan]ollama signin[/][dim] once to use it.[/]")
            return True, installed, False
        if detail == "__cancelled__":
            self.agent.console.print("  [yellow]⏹ Pull cancelled.[/] [dim](partial data cached — re-pull to resume)[/]")
            return False, name, True
        self.agent.console.print(f"  [yellow]●[/] Not in Ollama's registry [dim]({detail[:80]})[/]")
        # Escalate to a community GGUF build on HuggingFace.
        ok2, installed2, canc2 = self._pull_from_huggingface(name)
        if ok2:
            return True, installed2, False
        if not canc2:
            self._maybe_mlx_hint(detail)
        return False, name, canc2

    def _pull_from_huggingface(self, name):
        """Find `name` as a GGUF on HuggingFace and pull it via
        `ollama pull hf.co/<repo>:<quant>`. Lists the available quants (with sizes)
        and lets the user choose. Returns (ok, installed_name, cancelled)."""
        self.agent.console.print(f"  [dim]Searching HuggingFace for a GGUF build of [cyan]{name}[/][dim]...[/]")
        repos = self._hf_search_gguf(name)
        if not repos:
            self.agent.console.print(f"  [yellow]No GGUF build found on HuggingFace for {name}.[/]")
            return False, name, False
        repo_id, likes = repos[0]
        self.agent.console.print(f"  [dim]Found [cyan]{repo_id}[/][dim] ({likes}★). Listing quants...[/]")
        quants = self._hf_quants(repo_id)
        if not quants:
            self.agent.console.print(f"  [yellow]Couldn't list quant files for {repo_id}.[/]")
            return False, name, False
        self.agent.console.print(f"\n  [bold]Available quants for[/] [cyan]{repo_id}[/]:")
        for i, (q, sz) in enumerate(quants, 1):
            self.agent.console.print(f"    [cyan bold]{i}.[/] {q} [dim]({sz / 1e9:.1f} GB)[/]")
        try:
            ans = Prompt.ask("\n  [bold]Pull which quant?[/] [dim](number, or 'cancel')[/]", default="cancel")
        except (EOFError, KeyboardInterrupt):
            return False, name, True
        ans = ans.strip().lower()
        if not ans.isdigit() or not (1 <= int(ans) <= len(quants)):
            self.agent.console.print("  [dim]Cancelled.[/]")
            return False, name, False
        quant, size = quants[int(ans) - 1]
        target = f"hf.co/{repo_id}:{quant}"
        self.agent.console.print(f"  [yellow]●[/] Pulling [cyan]{target}[/] [dim](~{size / 1e9:.1f} GB)[/]...")
        rc, _out, cancelled = self._run_pull(target)
        if cancelled:
            self.agent.console.print("  [yellow]⏹ Pull cancelled.[/] [dim](partial data cached — re-pull to resume)[/]")
            return False, name, True
        if rc == 0:
            self.agent.console.print(f"  [green]●[/] [cyan]{target}[/] installed from HuggingFace!")
            return True, target, False
        self.agent.console.print(f"  [red]●[/] HuggingFace pull failed (exit {rc}).")
        return False, name, False

    def _hf_search_gguf(self, name, limit=30):
        """Search HuggingFace for GGUF repos matching `name`; return
        [(repo_id, likes), ...] sorted by likes (most popular first)."""
        try:
            from urllib.parse import quote
            resp = requests.get(
                f"https://huggingface.co/api/models?search={quote(name)}&limit={limit}",
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            _logger.debug("ignored error in _hf_search_gguf", exc_info=True)
            return []
        repos = [(m.get("id", ""), m.get("likes", 0) or 0)
                 for m in data if "gguf" in m.get("id", "").lower()]
        repos.sort(key=lambda r: -r[1])
        return repos

    def _hf_quants(self, repo_id):
        """Return [(quant_label, total_size_bytes), ...] for a GGUF repo, smallest
        first. Sharded quants are summed into one entry per quant."""
        try:
            resp = requests.get(f"https://huggingface.co/api/models/{repo_id}?blobs=true", timeout=20)
            resp.raise_for_status()
            sibs = resp.json().get("siblings", [])
        except Exception:
            _logger.debug("ignored error in _hf_quants", exc_info=True)
            return []
        agg = {}
        for s in sibs:
            f = s.get("rfilename", "")
            if not f.lower().endswith(".gguf"):
                continue
            label = self._quant_label(f)
            agg[label] = agg.get(label, 0) + (s.get("size") or 0)
        return sorted(agg.items(), key=lambda kv: kv[1])

    @staticmethod
    def _quant_label(path):
        """Derive the Ollama HF tag (quantization) from a GGUF file path. Prefers a
        Q…/IQ…/BF16/F16 token; for shard subfolders fall back to the folder name."""
        base = os.path.basename(path)
        base = re.sub(r"-\d{4,5}-of-\d{4,5}", "", base)  # strip shard suffix
        stem = base[:-5] if base.lower().endswith(".gguf") else base
        m = re.search(r"((?:UD-)?(?:I?Q\d[\w.]*|BF16|F16|F32))", stem, re.I)
        if m:
            return m.group(1)
        parts = path.split("/")
        return parts[-2] if len(parts) > 1 else stem

    def unload_models(self):
        """Free RAM on exit: Ollama keeps a model resident for `keep_alive` minutes
        (default 5) after use, so a 10GB+ model can linger long after you quit
        Kodiqa. This asks the server to unload every loaded model now (keep_alive=0).
        Works regardless of who started the server; safe no-op if it's not running."""
        try:
            resp = requests.get(f"{OLLAMA_URL}/api/ps", timeout=2)
            loaded = resp.json().get("models", [])
        except Exception:
            return  # server not running / unreachable
        freed = 0
        for m in loaded:
            name = m.get("name") or m.get("model")
            if not name:
                continue
            try:
                requests.post(f"{OLLAMA_URL}/api/generate", json={"model": name, "keep_alive": 0}, timeout=5)
                freed += 1
            except Exception:
                _logger.debug("ignored error unloading model", exc_info=True)
        if freed:
            self.agent.console.print(f"[dim]Freed {freed} loaded model(s) from RAM.[/]")

    def stop_ollama(self):
        """Stop an Ollama server Kodiqa spawned (never a GUI-app/user-started one).
        Handles both the server this session started and one a prior session left
        running (via the pid file)."""
        if not self.agent._ollama_started_by_us:
            # We didn't start one this session — but a previous session might have
            # (its in-memory flag is gone). Clean that up via the recorded pid.
            self._stop_orphaned_server()
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
            try:
                os.remove(self._server_pid_file())
            except Exception:
                _logger.debug("ignored error removing ollama pid file", exc_info=True)
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

        # Return all available (page is already sorted by pulls); the caller paginates.
        return models[:300]

    def _choose_models(self, new_models, page_size=100):
        """Paginated picker for the 'new models' list. Shows a page at a time with
        each model's size (or ☁ cloud), supports 'next'/'prev', numbers (global,
        across pages), names, and 'all'; then a y/n/skip confirm where 'n' returns
        to the picker. Sizes are fetched lazily per page and cached (no downloads).
        Returns the confirmed list of resolved pull-names (which may include a
        resolved `:tag` for sized/cloud models), or None to skip/cancel."""
        info_cache = {}
        last_page = (len(new_models) - 1) // page_size

        def _ensure_sizes(names):
            todo = [n for n in names if n not in info_cache]
            if todo:
                with Status("[dim]Fetching model sizes...[/]", console=self.agent.console, spinner="dots"):
                    info_cache.update(self._registry_infos(todo))

        page = 0
        while True:
            start = page * page_size
            page_models = new_models[start:start + page_size]
            _ensure_sizes([m for m, _, _ in page_models])

            page_label = f" [dim](page {page + 1}/{last_page + 1})[/]" if last_page else ""
            self.agent.console.print(
                f"\n[bold yellow]New models {start + 1}–{start + len(page_models)} of {len(new_models)}[/]"
                f"{page_label} [dim](most popular on ollama.com/library)[/]:")
            for offset, (model, desc, pulls) in enumerate(page_models):
                gidx = start + offset + 1
                pulls_str = f" [dim]({pulls} pulls)[/]" if pulls else ""
                self.agent.console.print(f"  [cyan bold]{gidx}.[/] [cyan]{model}[/] — {desc[:60]}{pulls_str} {self._size_tag(info_cache.get(model))}")
            self.agent.console.print("  [dim]☁ cloud = runs on Ollama's servers (no local download; needs `ollama signin`)[/]")

            nav = (["'next'"] if page < last_page else []) + (["'prev'"] if page > 0 else [])
            nav_str = (", " + ", ".join(nav)) if nav else ""
            try:
                answer = Prompt.ask(
                    f"\n[bold]Pull new models?[/] [dim](enter numbers, 'all'{nav_str}, or 'skip')[/]",
                    default="skip",
                )
            except (EOFError, KeyboardInterrupt):
                return None
            a = answer.strip().lower()
            if a in ("skip", ""):
                return None
            if a in ("next", ">"):
                if page < last_page:
                    page += 1
                else:
                    self.agent.console.print("[dim]Already on the last page.[/]")
                continue
            if a in ("prev", "<"):
                page = max(0, page - 1)
                continue

            to_pull = []
            if a == "all":
                to_pull = [m for m, _, _ in new_models]
            else:
                for part in answer.replace(",", " ").split():
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
            to_pull = list(dict.fromkeys(to_pull))  # dedupe, keep order
            if not to_pull:
                self.agent.console.print("[dim]Didn't recognize that — use numbers from the list, 'all', 'next', or 'skip'.[/]")
                continue

            # Show the selection with sizes/cloud + total (fetch any not yet cached,
            # e.g. numbers typed referencing a page you didn't open). Display and
            # pull the *resolved* name (may carry a :tag for sized/cloud models).
            _ensure_sizes(to_pull)
            resolved = [self._info_pull_name(info_cache.get(m), m) for m in to_pull]
            total = sum((info_cache.get(m) or (None, False, m))[0] or 0 for m in to_pull)
            self.agent.console.print("\n[bold]Selected:[/]")
            for m, pull_name in zip(to_pull, resolved):
                self.agent.console.print(f"  [cyan]{pull_name}[/] {self._size_tag(info_cache.get(m))}")
            if total:
                self.agent.console.print(f"  [bold]Total: ~{self._fmt_size(total)}[/]")
            try:
                confirm = Prompt.ask(
                    "\n[bold]Download these?[/] [dim](y = download, n = pick again, skip = cancel)[/]",
                    choices=["y", "n", "skip"], default="y",
                )
            except (EOFError, KeyboardInterrupt):
                return None
            if confirm == "skip":
                return None
            if confirm == "n":
                continue  # back to the pick prompt
            return resolved  # y → proceed (resolved pull-names)

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

        to_pull = self._choose_models(new_models)
        if not to_pull:
            return

        installed_names = []
        for model in to_pull:
            self.agent.console.print(f"\n  [yellow]●[/] Pulling [cyan]{model}[/]...")
            try:
                ok, name, cancelled = self._pull_with_fallbacks(model)
            except Exception as e:
                self.agent.console.print(f"  [red]●[/] Error: {e}")
                continue
            if ok:
                installed_names.append(name)
            if cancelled:
                self.agent.console.print("[dim]Stopped remaining downloads.[/]")
                break

        self.agent.console.print(f"\n[green]Done. Use /multi all for multi-model mode.[/]")
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
            info = self._registry_info(name)
            target = self._info_pull_name(info, name)  # resolve :tag for sized/cloud models
            self.agent.console.print(f"\n  [yellow]●[/] Pulling [cyan]{target}[/] {self._size_tag(info)}...")
            try:
                ok, _installed, cancelled = self._pull_with_fallbacks(target)
            except Exception as e:
                self.agent.console.print(f"  [red]●[/] Error: {e}")
                continue
            if ok:
                self.agent._invalidate_model_cache()
            if cancelled:
                break

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
