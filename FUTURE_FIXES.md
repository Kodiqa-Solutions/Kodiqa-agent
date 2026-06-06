# Future Fixes & Roadmap

Findings from the full-codebase audit on **2026-06-06** (Kodiqa v3.4.x). The
**Critical** tier was fixed and shipped in **v3.4.0**; everything below is
**deferred** — verified real, not yet done. Line numbers are from the v3.4.x
tree and may drift; re-grep before editing.

> Audit method: 4 parallel review agents (security/leaks, correctness,
> code-quality, features). Every critical claim was verified against the code
> before acting — zero false positives.

---

## ✅ Already fixed (v3.4.0–v3.4.3, for reference)

- Auto-compact death spiral (`_estimate_tokens` used cumulative tokens)
- `git_diff` command injection (now argv via `do_git_diff` + `shlex`)
- `/team` `NameError` (top-level `import re`)
- Unbounded chat loops (now enforce `max_iterations`)
- `settings.json` written `0600` (API keys)
- Throttled→every-launch startup model check (`--no-update`, `/update`, config)
- `/model` picker now lists installed models, not static aliases
- Added `/pull`, `/delete`/`/rm`, `/update`; ruff + CI lint step

---

## 🟠 High — fix next

### 1. Workspace boundary bypassed on multi-tool turns
- **Where:** `kodiqa.py` `_chat_claude` (~3492), `_chat_openai_compat` (~3943); `actions.py:execute_tools_parallel`
- **Issue:** the multi-tool branch calls `execute_tools_parallel` → `_dispatch` directly, skipping `_check_workspace_boundary` (only the single-tool path enforces it). A model emitting 2+ tool calls in one turn can `write_file`/`delete_file`/`move_file` outside cwd with no prompt. Also bypassable via symlinks (`abspath` not `realpath`), and `run_command` is never boundary-checked.
- **Fix:** pre-filter calls through `_check_workspace_boundary` before `execute_tools_parallel`, and use `os.path.realpath` for the prefix comparison.

### 2. Cost tracking reports $0 for current models
- **Where:** `kodiqa.py:113` `COST_TABLE`; used at `_display_token_usage` (~4447)
- **Issue:** table only has legacy IDs; the current `claude-sonnet-4-6` / `claude-opus-4-6` alias targets aren't present → `$0.00` cost, silently breaking `/budget`.
- **Fix:** key the cost table by the alias targets; ideally source pricing from `config.py` (single source of truth with CLAUDE.md).

### 3. Sessions don't restore for Claude / OpenAI
- **Where:** `kodiqa.py:_save_session` (~1021)
- **Issue:** keeps only `str` content. Claude assistant turns are content-block *lists* → all assistant messages silently dropped on save. OpenAI tool-call messages have `content=None` → orphaned `role:"tool"` messages on restore → API 400.
- **Fix:** serialize list/None content too (it's JSON-serializable) instead of filtering.

### 4. SSRF in `web_fetch`
- **Where:** `web.py:fetch_page` (~161); `actions.py:do_web_fetch` (~677); auto-approved
- **Issue:** fetches any model-supplied URL with no validation — cloud metadata (`169.254.169.254`), localhost admin endpoints, unbounded redirects.
- **Fix:** allow only http/https; resolve host and reject private/loopback/link-local ranges (re-validate each redirect hop); cap redirects.

### 5. MCP stdio deadlock
- **Where:** `mcp.py:28` (`stderr=PIPE` never drained), `mcp.py:80` (`readline()` no timeout)
- **Issue:** stderr pipe fills at ~64KB → child blocks; a hung server hangs Kodiqa indefinitely.
- **Fix:** `stderr=DEVNULL` (or drain on a thread); add a read timeout / `select` around `readline()`.

### 6. Batch edit queue clobbers same-file edits
- **Where:** `actions.py` `do_edit_file` (~454, snapshots disk at queue time), `apply_queued_edit` (~96)
- **Issue:** two queued edits to one file both snapshot original content; apply overwrites whole file each time → only the last edit survives.
- **Fix:** base each queued edit on the latest queued state for that path, or apply as diffs rather than full-file overwrites.

---

## 🟡 Medium

- **OpenAI o3/o4 models error** — `_build_openai_request_body` (~4084) always sends `max_tokens`; o-series require `max_completion_tokens` → 400. Detect o-series and switch.
- **`OLLAMA_BIN` hardcoded macOS path** — `config.py:7`. Ollama mgmt (`/pull`, `/delete`, updates) is non-functional on Linux/Windows despite the Linux classifier. Fix: `os.environ.get("OLLAMA_BIN") or shutil.which("ollama") or <mac path>`.
- **`multi_edit` broken in Ollama text mode** — `edits` arrives as a string; handler requires a list → always fails. Fix: `json.loads` the `edits` string in the dispatch lambda.
- **`_stop_ollama` uses `pkill -f ollama`** — kills *all* Ollama processes, not just ones we spawned. Track our PID. No `atexit`/signal cleanup → crashes leak MCP/LSP/Ollama children. Add an `atexit` handler.
- **SQLite cross-thread** — `memory.py:13` opens with `check_same_thread=True`, but `memory_search` runs in the parallel thread pool → silently fails. Use `check_same_thread=False` or run sequentially.
- **Streaming response not closed on mid-stream exception** — Claude/OpenAI/Ollama loops only `resp.close()` on interrupt, not in `finally` → leaked pooled connection on error.
- **Hook command injection** — `actions.py:_run_hook` (~34) does `cmd.replace("{k}", v)` then `shell=True` with model-controlled `v`. Substitute via argv / `shlex.quote`.
- **`BLOCKED_COMMANDS` trivially bypassable** — substring match (`rm  -rf /`, `rm -fr /`, etc. pass). Rely on confirmation + sandbox; at minimum tokenize argv instead of raw substrings.
- **~56 silent `except Exception:`** in `kodiqa.py` — a `_logger` exists but is used in ~17 places. Standardize: log at WARNING in generic handlers.

---

## 🟢 Quick wins (high value, low effort)

- **`kodiqa -c` / `--resume`** — wire the existing `/history` resume path into an argparse flag.
- **`/redo`** — undo exists (`do_undo_edit`); add a redo stack.
- **Live cost/token ticker** in `StreamWriter` during generation (reuses token accounting).
- **End-of-turn diffstat** — roll-up `N files changed, +x −y` from the edit queue.
- **Provider picker on first run** — `_first_run_setup` hardcodes Claude despite 7 providers; generalize via the provider registry.
- **Better error messages** — on 401 → "run /key", on connection error → "Ollama not running…".

---

## 🔵 Bigger bets (differentiators)

- **Cross-provider failover / smart routing** — `_retry_api_call` only retries the same model. Auto-failover across the 7 providers when one is down/rate-limited; route cheap-drafts / strong-reviews. No competitor has this; aligns with Kodiqa's multi-provider identity.
- **Git-snapshot "rewind a whole turn"** — today only per-file undo + conversation checkpoints; no "revert all file changes from this run." Shadow-branch snapshot before each turn.
- **Custom prompt-template commands** (`.kodiqa/commands/*.md`) — `/alias` only remaps commands, not reusable prompts.
- **Editor/IDE bridge** — LSP exists (`lsp.py`); surface diagnostics inline + a thin VS Code/Zed bridge (the one box competitors win).

---

## 🏗️ Architecture (longer-term refactor)

`kodiqa.py` is ~5,800 lines, one `Kodiqa` class, ~140 methods. This is *why*
bugs like the `/team` NameError and $0 cost slipped through — the highest-churn
code (3 chat loops, provider routing, message builders, `_handle_slash`) has
**zero test coverage** because it's untestable as written.

Suggested order (each unlocks the next):
1. **Command registry** — replace the 814-line/164-branch `_handle_slash` if/elif with `{"/cmd": handler}`; derive `_SLASH_COMMANDS` and `/help` from it (kills 3-way drift).
2. **`providers/` package** — one module per provider with a common `ChatProvider` interface (`build_messages`, `stream`, `wrap_results`); collapse the 3 duplicated chat loops into one driver sharing `_build_system_prompt`, `_run_tool_calls`, `_post_tools` (~250 lines removed).
3. **Split the God-class** — `OllamaManager`, `ModelRegistry`/discovery, `SessionStore`, `ContextBuilder`, agent/team subsystem; keep `Kodiqa` a thin orchestrator.
4. **Backfill tests** on the extracted pure-logic units (message builders, system-prompt assembly, command resolution, cost calc).

Also: move the 11 flat top-level modules into a `kodiqa/` package (`pyproject.toml:py-modules`) to avoid site-packages name collisions (`config`, `web`, `tools`), and add optional-dependency extras for voice / RAG / repomap / LSP.
