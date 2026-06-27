# Changelog

All notable changes to Kodiqa are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [3.18.0] - 2026-06-27

Local-model speed & memory — Phase 1 of the model-optimization roadmap.

### Added
- **Flash attention + KV-cache quantization on by default** for Ollama servers Kodiqa starts. Ollama ships both off; Kodiqa now spawns its server with `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0`, which roughly **halves KV-cache RAM** and speeds up long contexts with negligible quality loss — for every local model, for free. The startup line shows what's active (e.g. `Ollama started (flash attn, q8_0 KV cache)`).
  - Tunable via `~/.kodiqa/config.json`: `flash_attention` (default true) and `kv_cache_type` (`q8_0` default, `q4_0` for ¼ KV RAM, `f16` to disable). KV-cache quant forces flash attention on (it requires it). Any `OLLAMA_*` env var you set yourself always wins.
  - Applies only to servers Kodiqa spawns (not a separately-running GUI app), and the KV-cache setting is global to that server.

### Tests
- `test_ollama_pull.py` — `_serve_env` defaults, user-env precedence, `f16` disable, KV-quant-forces-flash, env passed to the spawned process, and the startup note. 533 total.

## [3.17.0] - 2026-06-27

Pull any model — Kodiqa now falls back to HuggingFace when Ollama doesn't host it.

### Added
- **HuggingFace GGUF fallback for `/pull` and the startup model list.** When a model isn't in Ollama's registry (and isn't a `:cloud` model), Kodiqa searches HuggingFace for a community GGUF build, shows the available quant levels with their download sizes, lets you pick one, and installs it via `ollama pull hf.co/<repo>:<quant>`. So picking a model that Ollama doesn't host (e.g. many GLM/Qwen/Llama variants) now Just Works instead of erroring.
  - The full escalation order per model is: Ollama registry → `<name>:cloud` → HuggingFace GGUF.
  - Quants are listed smallest-first with sizes, so you can avoid the 200GB+ monsters.
- **Live download progress.** Pulls now stream Ollama's real progress bar (per-layer %, MB, speed) instead of a static "Pulling…" line.
- **Download sizes shown up front.** The startup "new models" list now shows each model's download size next to its pull count (fetched concurrently from the registry, no downloads), `/pull` shows the size inline, and the confirm step shows the selection's total. Cloud-hosted models (no local weights) are labelled **☁ cloud** instead of a size, and genuinely-unknown ones show **size ?** — so a blank size is never ambiguous. Models without a default `latest` tag are resolved via the library tags page, so non-standard cloud tags (e.g. `mistral-large-3:675b-cloud`) are correctly labelled cloud and sized-only models (e.g. `granite4.1-guardian:8b`) show a real size — and they pull using that resolved tag.
- **Paginated model list.** The startup list now shows up to ~234 available models (was capped at 100), 100 per page — type **next**/**prev** to page through them. Numbers are global (so `130` works from any page), and sizes are fetched lazily per page so you only pay for pages you actually open.
- **Pick-again loop.** Answering **n** at the "Download these?" confirm now returns you to the model picker so you can choose a different number (and compare sizes) until you settle on one — instead of bailing out. **skip** cancels.
- **Cancel a running pull.** Press **Esc** during a download to get a "Cancel this download? [y/N]" prompt, or **Ctrl+C** to cancel immediately. The partial download is cached by Ollama, so re-pulling resumes where it left off.

### Fixed
- **Ollama no longer leaves a model resident in RAM after you quit.** Ollama keeps the last model loaded for `keep_alive` minutes (default 5), so a 10GB+ model could sit in RAM long after exiting Kodiqa. On quit Kodiqa now unloads loaded models (`keep_alive=0`) to free that RAM immediately. Disable with `unload_ollama_on_exit: false` in `~/.kodiqa/config.json`.
- **A Kodiqa-started Ollama server is now stopped even across sessions.** The "did we start it?" flag didn't survive a restart, so a server Kodiqa spawned in an earlier session lingered forever. Kodiqa now records the server's PID and stops it on a later exit — while still never touching a GUI-app or user-started server.

### Tests
- `test_ollama_pull.py` extended: HF GGUF search/filtering, quant parsing (flat, sharded, subfolder), the registry→cloud→HF escalation, the interactive quant picker, registry-size/cloud lookup incl. tags-page tag resolution (non-standard cloud + sized-only), the paginated picker, pull cancellation propagation, and model-unload/orphaned-server shutdown. 527 total.

## [3.16.3] - 2026-06-27

Pull any model from the list — including MLX and cloud-hosted models.

### Fixed
- **MLX models now pull like any other.** Models published in Apple's MLX format (e.g. GLM) failed with `failed to load MLX dynamic library` when the running Ollama server was a Homebrew build (which doesn't ship the MLX runtime). Kodiqa now prefers the official macOS app build (which bundles MLX) for `OLLAMA_BIN`, and if a non-MLX server is already running it transparently restarts it with the MLX-capable build.
- **Cloud-hosted models now install instead of erroring.** Newer models like `glm-5.1` / `glm-5.2` are published cloud-only (they expose just a `:cloud` tag, no local weights), so a bare `ollama pull glm-5.1` failed with "file does not exist". Kodiqa now detects this and transparently retries as `<name>:cloud`, then reminds you to run `ollama signin` once to use it.

### Tests
- `test_ollama_pull.py` — MLX detection, binary resolution preferring the MLX build, the `:cloud` pull fallback, and the in-place MLX server restart decision. 487 total.

## [3.16.2] - 2026-06-06

### Fixed
- **A failed request no longer crashes the whole CLI.** When a model request failed early (e.g. a 404 for an unavailable model, or a 401), the failover path checked an uninitialized `_stream_interrupted` flag and raised `AttributeError`, which bubbled all the way up and dropped you back to the shell. The flag is now initialized at startup.
- **The REPL is crash-resistant in general.** Any unexpected error while handling one input is now logged to `~/.kodiqa/error.log`, shown as a friendly message, and returns you to the prompt with your session intact — instead of exiting. (Ctrl+C still quits.)

### Tests
- Regression tests for the failover early-failure path and `_stream_interrupted` initialization. 473 total.

## [3.16.1] - 2026-06-06

### Added
- **Stable bridge token** — set `KODIQA_BRIDGE_TOKEN` to use a fixed auth token for the editor bridge (so you can put it in your editor config once) instead of a new random one each session.
- A demo GIF of the editor bridge on the landing page and README.

## [3.16.0] - 2026-06-06

Editor / IDE bridge — connect Kodiqa to your editor.

### Added
- **Bridge server** — `kodiqa --serve` (or `/serve` in a session) runs a small authenticated localhost HTTP server that editor extensions (VS Code, Zed, Neovim, …) can call:
  - `GET /health` (no auth) → `{status, model, version}`
  - `POST /ask {prompt, context?}` → `{response}` — a one-shot, non-streaming model answer that doesn't touch conversation history or edit files (safe to call while the CLI is in use)
  - `GET /diagnostics?file=PATH` → LSP diagnostics for a file (start an LSP with `/lsp`)
  - Binds to `127.0.0.1` only and requires a per-session bearer token (printed on start). `--port` sets a fixed port.
- A minimal reference client in `examples/bridge_client.py`; editor extensions are thin clients over this protocol.

### Tests
- `test_bridge.py` — real in-process server covering health, auth enforcement, `/ask` (with/without context, missing prompt), `/diagnostics` (with/without LSP), and unknown paths. 471 total.

## [3.15.0] - 2026-06-06

Custom prompt-template commands — your own reusable slash commands.

### Added
- **Custom commands from Markdown files.** Drop a `.kodiqa/commands/<name>.md` file (project) or `~/.kodiqa/commands/<name>.md` (global) and invoke it as `/<name>`. The file's text becomes the prompt, with `$ARGUMENTS` (all args) and `$1`, `$2`, … (positional) substitution; if there's no placeholder, the args are appended. Optional `--- description: … ---` frontmatter shows up in the listing. Project commands override global ones.
- **`/commands`** — list your custom commands (with source + description). They also tab-complete like built-ins.

Example: `echo 'Review $ARGUMENTS for bugs and suggest fixes' > .kodiqa/commands/review.md`, then `/review src/app.py`.

### Tests
- `test_custom_commands.py` — template rendering ($ARGUMENTS/$N/append/frontmatter), discovery (project-over-global, path-traversal rejection), and dispatch. 462 total.

## [3.14.0] - 2026-06-06

OpenAPI + GraphQL tool sources — turn any REST or GraphQL API into tools, no codegen.

### Added
- **OpenAPI specs as tools** — `/mcp add <name> --spec <url|file> [--base-url U]`. Kodiqa reads the spec and exposes one tool per operation (path/query/body params become the tool's arguments); calling a tool issues the HTTP request. JSON specs work out of the box; YAML if `PyYAML` is installed.
- **GraphQL endpoints as tools** — `/mcp add <name> --graphql <url>`. Kodiqa introspects the schema, exposes each query/mutation as a tool, auto-generates a selection set of the return type's scalar fields, and sends parameterized queries with variables.
- Both reuse the existing auth flags (`--bearer`, `--header`, `env:`/`file:` secrets), show up in `/mcp list` with their kind (`[openapi]`/`[graphql]`), and work with **lazy mode** and **TOON** like any other tool source.

### Tests
- `test_api_tools.py` — spec/introspection parsing and real in-process HTTP servers exercising OpenAPI requests (path/query/body) and the GraphQL introspect→query flow. 452 total.

## [3.13.0] - 2026-06-06

TOON output — token-efficient tool results.

### Added
- **`/toon` (opt-in)** — re-encode JSON tool results into TOON (Token-Oriented Object Notation): an array's field names are written once like a CSV header instead of repeated on every row, and scalar arrays are inlined. On large uniform data this is **~60% fewer tokens** than JSON (e.g. a 50-row array: 788 → 281 tokens). It only swaps in TOON when the result is genuinely shorter, so it never makes output larger; non-JSON results pass through untouched. Great alongside MCP servers that return big JSON.

### Tests
- `test_toon.py` — encoder (tabular arrays, scalar arrays, nesting, JSON fallback, quoting, numeric-string preservation) and `maybe_toon` (savings, never-longer, pass-through). 443 total.

## [3.12.0] - 2026-06-06

Rewind a whole turn — an "undo everything the AI just did" safety net.

### Added
- **`/rewind [n]`** — revert **all** file changes the AI made over the last `n` turns (default 1), not just one file at a time. It shows exactly which files will be restored or deleted (newly-created files are removed), asks for confirmation, then reverts. Works in any directory (it's content-based, no git required). Each turn's pre-edit file states are snapshotted automatically (last 10 turns kept).

### Tests
- `test_rewind.py` — turn-snapshot capture (new files → delete, edits → restore, first-touch wins), `do_rewind`, and the command's merge/confirm/pop logic. 427 total.

## [3.11.0] - 2026-06-06

Cross-provider failover — your turn finishes even when a provider doesn't.

### Added
- **Automatic cross-provider failover** (on by default). When an API call fails hard — provider down, rate-limited (429), 5xx, timeout, or bad key (401) — Kodiqa transparently retries the *same* request on the next configured provider (rebuilding the messages in that provider's format) and continues the turn there, instead of erroring out. It notifies loudly when it switches (`⚠ Failing over to <model>`), and never fails over on a user interrupt.
- **`/failover`** — `on`/`off`, `auto` (use every provider with a key set), or an explicit order: `/failover claude deepseek qwen`. `/failover` with no args shows the current order. Persisted in settings.

Single-provider users are unaffected (nothing to fail over to). Failover only crosses cloud providers that have keys configured.

### Tests
- `test_failover.py` — candidate selection (auto + explicit chain), the stream-with-failover logic (switch on failure, stay on success, never on interrupt, respect the off switch), and the `/failover` command. 419 total.

## [3.10.0] - 2026-06-06

OAuth for remote MCP servers — completes the "Remote MCP + OAuth" work.

### Added
- **OAuth 2.1 login for remote MCP servers** (`mcp_oauth.py`). Two flows:
  - **Interactive** (`--oauth`): server discovery → dynamic client registration → authorization-code + PKCE in your browser → token exchange. Just run:
    ```
    /mcp add linear https://mcp.linear.app/mcp --oauth
    ```
    Kodiqa opens your browser, you log in, done.
  - **Client credentials** (machine-to-machine, no browser):
    ```
    /mcp add api https://example.com/mcp --oauth-client-id env:CID --oauth-client-secret env:CSEC
    ```
  - Optional `--scope "read write"`.
- **Token caching + auto-refresh** — tokens are cached under `~/.kodiqa/oauth/` (0600) keyed by server URL, reused across sessions, refreshed automatically before expiry and on a `401` (the request is transparently retried once).

### Tests
- `test_mcp_oauth.py` — PKCE, discovery, dynamic registration, the localhost callback server, token exchange/refresh, the cache, both grants, a deterministic end-to-end of the interactive flow (real callback server + fake browser), and HTTP 401 refresh-and-retry. 409 total.

## [3.9.0] - 2026-06-06

Remote MCP servers — connect hosted MCP endpoints, not just local ones.

### Added
- **Remote MCP over Streamable HTTP** (the current MCP transport standard). Point `/mcp add` at a URL instead of a command:
  ```
  /mcp add linear https://mcp.linear.app/mcp --bearer env:LINEAR_TOKEN
  /mcp add api https://example.com/mcp --header "X-Api-Key:abc123"
  ```
  Handles JSON and SSE responses and reuses the server's `Mcp-Session-Id` across calls.
- **Token / header auth** for remote servers: `--bearer <token>` / `--token <token>` (sets `Authorization: Bearer`) and repeatable `--header K:V`. Values support `env:VAR` and `file:PATH` so secrets aren't typed inline or stored in history.
- `/mcp list` now shows each server's transport (`[http]` / `[stdio]`).

Remote tools work exactly like local ones — including **lazy mode** (discovered on demand via `mcp_search`).

### Notes
- Interactive **OAuth login** (browser PKCE flow) is the next increment; this release covers the static-token auth that most hosted MCP servers use today.

### Tests
- `test_mcp_http.py` — a real in-process HTTP MCP server (start, tool call, session reuse, manager routing) plus SSE-path, error, and auth-parsing tests. 397 total.

## [3.8.1] - 2026-06-06

### Fixed
- **`--no-update` was silently ignored** (regression since 3.7.3). The God-class split left a bare `self` inside `getattr(self, "_skip_updates", …)` in the extracted `OllamaManager`, so the flag — which lives on the agent — was never read and the startup model check ran anyway. Same bug class fixed in two more spots from that refactor: Ollama shutdown (`_ollama_proc` lookup → the spawned server is now actually stopped) and the live-model discovery cache (`_cached_api_models` reads → live API models are recognized again). Added regression tests so these can't silently break again.

### Added
- Demo GIF of lazy MCP tools (toggle on/off, tools discovered on demand) on the landing page and in the README.

## [3.8.0] - 2026-06-06

Lazy MCP tools — big token savings for MCP servers (inspired by mcp2cli / Anthropic Tool Search).

### Added
- **Lazy MCP tool loading (on by default).** Instead of injecting every connected MCP tool's schema into every request, Kodiqa now exposes **3 fixed meta-tools** and lets the model discover tools on demand:
  - `mcp_search` — find MCP tools by keyword (names + descriptions, ranked by usage); nothing pre-loaded
  - `mcp_tool_schema` — fetch one tool's full input schema only when needed
  - `mcp_call` — execute a tool by name
  For a 50-tool MCP server this cuts the per-turn tool-schema cost from ~5,300 to ~310 tokens (**~94%**), and the cost stays flat no matter how many MCP tools are connected.
- **Usage-aware ranking** — `mcp_search` orders results by how often you've called each tool (persisted in `~/.kodiqa/mcp_usage.json`).
- **`/mcp lazy [on|off]`** — toggle the behavior (`/mcp list` shows current mode + the token trade-off). Set `mcp_lazy: false` in settings to default to the old always-inject behavior.

### Tests
- `test_mcp_lazy.py` — manager search/schema/count primitives, lazy vs. non-lazy `_get_all_tools`, meta-tool handlers (usage ranking, schema lookup, call routing, stringified args), and routing. 383 total.

## [3.7.4] - 2026-06-06

Observability — clears the last item from the 2026-06-06 audit (silent error handlers).

### Changed
- **No more truly-silent error swallowing.** The 44 broad `except Exception: pass` catch-alls across the codebase now log via the `kodiqa` logger at DEBUG with a traceback and the enclosing function name. Behavior is unchanged (errors are still swallowed), and the default log level stays WARNING so nothing new is written to `~/.kodiqa/error.log` during normal use. (Narrow, intentional handlers like `except FileNotFoundError` were left quiet.)

### Added
- **`--debug` flag** (and `KODIQA_DEBUG=1` env var) — lowers the log level to DEBUG so the swallowed-error traces show up in `~/.kodiqa/error.log` when you're chasing a problem.
- A `NullHandler` on the package logger so pre-startup log calls never leak to stderr.

### Tests
- `test_logging.py` — swallowed errors are logged at DEBUG with exc_info, stay silent at WARNING, behavior unchanged, and the file handler is set up idempotently. 366 total.

## [3.7.3] - 2026-06-06

Internal refactor — no user-facing behavior change. Finishes splitting the `Kodiqa` God-class into focused modules.

### Changed
- **God-class split.** Five cohesive subsystems moved out of `kodiqa.py` into their own modules, each holding a back-reference to the agent while `Kodiqa` keeps thin delegating wrappers (so behavior and every call site are unchanged):
  - `session_store.py` — conversation persistence (already extracted in 3.7.2)
  - `context_builder.py` — system-prompt assembly + git/shell/pinned context
  - `ollama_manager.py` — Ollama server lifecycle, update check, model pull/delete/discovery
  - `model_registry.py` — API model discovery, `/models` listing, alias/provider resolution
  - `agent_team.py` — background sub-agents and agent teams
- `kodiqa.py` shrank from ~6250 to ~5140 lines.

### Tests
- Added `test_managers.py` (wrapper delegation + pure-method behavior for the extracted classes) and retargeted the moved tests. 361 total.

## [3.7.2] - 2026-06-06

Internal refactor — no user-facing behavior change.

### Changed
- **Unified chat loop.** The two near-identical native tool-calling loops (Claude and all OpenAI-compatible providers, ~180 lines each, ~90% shared) are now one `_run_native_chat` driver; only the provider-specific *formats* differ (message build, assistant message, tool-result message), via small per-kind seams. Shared machinery (`_build_system_prompt`, `_run_tool_calls`, `_maybe_lint_fix`) is now defined once and also reused by the Ollama loop.
- **Session persistence extracted.** Crash-recovery and the session archive moved from the `Kodiqa` God-class into a dedicated `SessionStore` (`session_store.py`); `Kodiqa` keeps thin wrappers, so behavior is unchanged. First step of splitting the God-class into focused units.

### Tests
- Added characterization tests for the chat-loop seams (`test_chat_loop.py`) and `SessionStore` round-trips (`test_session_store.py`). 350 total.

## [3.7.1] - 2026-06-06

Internal refactor — no user-facing behavior change (only `/help` has a nicer grouped layout).

### Changed
- **Command dispatch is now a registry.** The ~850-line `if/elif` chain in `_handle_slash` is replaced by an 18-line dispatcher over a `_COMMAND_HANDLERS` table; each of the 73 commands is its own `_cmd_*` method. A single `_COMMAND_SPECS` registry (name, aliases, handler, group, args, description) is the source of truth — the dispatch table, the tab-completion list (`_SLASH_COMMANDS`), and `/help` all derive from it, so they can no longer drift out of sync. Adding a command is now two steps (a method + one registry row).

### Tests
- Added `TestCommandRegistry` integrity tests (every handler resolves to a real method, lists derive correctly, dispatch routes and passes args, user-alias expansion). 326 total.

## [3.7.0] - 2026-06-06

Quick-win batch — session resume, redo, live cost ticker, diffstat, and a friendlier first run.

### Added
- **`kodiqa -c` / `--continue`** — resume the most recent session immediately, skipping the y/n prompt.
- **`--resume [ID]`** — resume a specific saved history session by id (most recent when no id is given), mirroring `/history resume`.
- **`/redo`** — re-apply the most recently undone edit. A per-file redo stack sits alongside `/undo`; any fresh edit invalidates the redo stack (standard undo/redo semantics).
- **Live cost/token ticker** — while a response streams, the code/thinking status line now shows an estimated `~N tok ~$X` (output cost reconciled from real usage at end of turn). Hidden for local/free models.
- **End-of-turn diffstat** — after each turn, a `✎ N files changed, +x −y` rollup (per-file breakdown when more than one file changed), tracked across all permission modes.
- **First-run provider picker** — first launch now offers all 7 providers (Claude + OpenAI/DeepSeek/Groq/Mistral/Qwen + local-only) via the arrow-key selector, instead of hardcoding Claude. Sets the chosen provider's default model.

### Changed
- **Friendlier API errors** — connection failures and timeouts now print an actionable hint (check connection / `/model` to switch) instead of a raw exception; the Ollama connection error suggests `ollama serve`.

### Tests
- Added regressions for redo (round-trip, redo-clears-on-fresh-edit, new-file re-delete), the change-log/diffstat rollup, the live ticker, and `--resume` history loading (317 total).

## [3.6.0] - 2026-06-06

Medium-tier audit fixes (cross-platform, correctness, stability, security).

### Fixed
- **OpenAI o-series (o3/o3-mini/o4-mini) errored** — now send `max_completion_tokens` instead of `max_tokens` (the o-series reject the latter with a 400).
- **`OLLAMA_BIN` was macOS-only** — now resolved via `$OLLAMA_BIN` → `PATH` (`shutil.which`) → the macOS app bundle, so `/pull`, `/delete`, and update checks work on Linux/Windows.
- **`multi_edit` failed in Ollama text mode** — `edits` arriving as a JSON string is now parsed. Also no longer consumes an undo slot on a no-op.
- **`_stop_ollama` killed all Ollama processes** (`pkill -f ollama`) — now stops only the process Kodiqa started, and reaps it. Added an `atexit` handler that cleans up spawned MCP/LSP/Ollama children even on crash.
- **`memory_search` could fail under parallel dispatch** — SQLite connection now opened with `check_same_thread=False` (it's used from the tool thread pool).
- **Streaming connection leak** — all three chat loops now `resp.close()` in `finally`, not just on interrupt.

### Security
- **Hook command injection** — hook `{param}` values are now `shlex.quote`d before substitution, so a model-controlled value can't inject shell commands.
- **Blocklist hardening** — `run_command` now normalizes whitespace before matching `BLOCKED_COMMANDS`, so `rm  -rf  /` (extra spaces) is caught.

### Tests
- Added regressions for o-series body, multi_edit JSON-string input, and blocklist normalization (300 total).

## [3.5.0] - 2026-06-06

High-severity fixes from the 2026-06-06 audit (security + data-loss).

### Fixed
- **Workspace boundary bypass on multi-tool turns** — `execute_tools_parallel` skipped `_check_workspace_boundary`, so a model emitting 2+ tool calls could write/delete outside the workspace unprompted. The boundary check now runs on every tool (single and parallel), and uses `realpath` so symlinks can't escape.
- **Cost/budget reported $0 for current models** — `COST_TABLE` lacked the current alias targets (`claude-sonnet-4-6`, `claude-opus-4-6`, …). Added them; budget now tracks real spend.
- **Sessions lost assistant messages on save/restore** — `_save_session` kept only string-content messages, dropping Claude content-block turns and orphaning OpenAI tool messages (400 on restore). Now saves full history, trimming only a trailing unresolved tool call.
- **SSRF in `web_fetch`** — now validates URLs (http/https only) and rejects internal/loopback/link-local/cloud-metadata hosts, re-checking every redirect hop.
- **MCP stdio deadlock** — server stderr is now `DEVNULL` (an undrained pipe deadlocked at ~64KB) and reads have a 30s timeout so a hung server can't block Kodiqa; killed servers are now reaped.
- **Batch-edit queue clobbered same-file edits** — two queued edits to one file now compose on the latest queued state instead of both snapshotting the original (only the last edit used to survive).

### Tests
- Added regression tests for SSRF, the batch-edit clobber, and the cost table (295 total).

## [3.4.3] - 2026-06-06

### Changed
- The startup model update + new-model check now runs on **every launch by default** again (`update_check_interval_hours` default 24 → 0), restoring the pre-3.4.0 behavior — while keeping all the 3.4.0 fixes. The throttle is still available (set `update_check_interval_hours` > 0), `--no-update` still skips, and `/update` still forces a check.

## [3.4.2] - 2026-06-06

### Added
- `/update` — force a model update + new-model discovery check on demand, bypassing the 24h startup throttle added in 3.4.0 (does not reprint the welcome banner).

## [3.4.1] - 2026-06-06

### Fixed
- `/model` picker now lists the models actually installed in Ollama (with their friendly alias, e.g. `/coder`) instead of the static `MODEL_ALIASES` list — so deleted models no longer appear. Falls back to the alias list if Ollama is unreachable, and shows a hint when no local models are installed. Now consistent with `/models`.

## [3.4.0] - 2026-06-06

### Fixed
- **Auto-compact death spiral**: `_estimate_tokens` used the cumulative lifetime input-token sum, so after enough turns auto-compact fired on *every* message, repeatedly nuking the conversation. Now tracks the most recent request's prompt size and resets after compaction.
- **Command injection in `git_diff`**: the auto-approved `git_diff` tool interpolated args into a `shell=True` string (RCE via prompt injection). Now runs `git diff` via argv with `shlex`, no shell.
- **`/team` crashed with `NameError`**: `re` was used in `team_worker` but never imported at module level. Added top-level `import re`.
- **Unbounded agentic loops**: `max_iterations` was defined but never enforced; all three chat loops were `while True:`. Now capped (default 15, configurable).
- **API keys world-readable**: `~/.kodiqa/settings.json` is now written with `0600` perms (dir `0700`).

### Changed
- **Startup is no longer blocking**: the model update/discovery check (which ran `ollama pull` on every installed model + scraped ollama.com on every launch) is now throttled to once per `update_check_interval_hours` (default 24) and skippable via `--no-update` or `check_updates: false` config.

### Added
- `--no-update` CLI flag; `check_updates` / `update_check_interval_hours` config options.
- `ruff` linting (pyflakes rules) + a CI lint step; removed dead code and unused imports across the codebase.
- Regression tests for the git_diff-injection and auto-compact fixes (290 tests total).

## [3.3.9] - 2026-06-06

### Added
- `/pull <model>` — download an Ollama model on demand (previously only possible at startup)
- `/delete [model]` (alias `/rm`) — delete locally downloaded Ollama models to free disk; interactive picker with sizes when no arg, exact/fuzzy match or `all` otherwise, always confirms before removing, warns when deleting the active model, tab-completes installed model names

## [3.3.8] - 2026-06-06

### Fixed
- Model discovery cache now invalidated on API key/region change (`/key`) — `/models` re-fetches live instead of serving a stale 10-minute cache, so newly-available provider models appear immediately
- Ollama update check no longer reports false "updated!" — compares model digest before/after `ollama pull` instead of matching an "up to date" string modern Ollama never prints (it prints "success" either way)

### Changed
- New-models list relabeled to "Top N new models available (most popular on ollama.com/library)" so the count isn't mistaken for the full catalog
- Raised the new-models display cap from 20 to 100

## [3.3.4] - 2026-03-05

### Fixed
- ask_user input visibility: switched from raw `input()` to Rich `Prompt.ask` so typed text is visible
- Batch edit review: added "Accept all, don't ask again" option to disable review for the session

## [3.3.1] - 2026-03-03

### Fixed
- Qwen model list: removed duplicates, removed models not in Coding Plan (qwq-plus, qwen3.5-flash, qwen-turbo, qwen-math-plus)
- Added missing Coding Plan models: glm-4.7, MiniMax-M2.5
- Model pull prompt: single-letter input no longer accidentally matches model names

### Added
- GitHub Pages landing page with particle animation, glassmorphism, animated counters
- GitHub Actions workflow for automatic Pages deployment
- Open source projects section on kodiqa.com (live GitHub star counts)

## [3.3.0] - 2026-03-02

### Changed
- License switched from GPL-3.0 to AGPL-3.0 (closes SaaS loophole)
- Commercial license page for enterprise use

## [3.2.0] - 2026-03-02

### Added
- Auto lint-fix loop (`/lint auto`) — AI fixes lint errors automatically (max 3 iterations)
- Auto test-fix loop (`/test-fix`) — run tests, AI fixes failures, re-run
- Hooks system — pre/post hooks for tool execution via config.json
- Watch AI triggers — `# AI:` comments in watched files trigger AI actions
- Architect mode (`/architect`) — strong model plans, cheap model implements
- Background/headless mode (`--headless`) — run tasks non-interactively
- Worktree isolation (`/agent --worktree`) — git worktree per sub-agent
- OS-level sandboxing (`/sandbox`) — sandbox-exec (macOS), firejail/bwrap (Linux)
- Repo map (`/map`) — tree-sitter or regex symbol extraction across codebase
- Agent teams (`/team`) — coordinator splits tasks, workers execute in parallel

## [3.0.0] - 2026-03-02

### Added
- `/changelog` — view version history
- `/stats` — session metrics (files, tools, time, cost)
- `/review-local` — AI reviews staged git changes
- `/test` — auto-generate unit tests for any file
- `/persona` — switch AI personality (security-expert, code-reviewer, teacher, architect, debugger)
- `/patch` — apply diff/patch from clipboard
- `/profile` — save/load config profiles
- `/refactor` — AI-powered multi-file refactoring (rename, extract)
- `/history` — browse and resume past sessions
- `/watch` — file watcher with change notifications
- `/embed` + `/rag` — RAG search with local embeddings (Ollama/OpenAI)
- `/debug` — run script, catch errors, debug with AI
- `/diagram` — generate Mermaid diagrams via AI
- Parallel tool calls for OpenAI-compatible providers

### Fixed
- README test count and missing v2 commands

## [2.0.0] - 2025-12-15

### Added
- 15 new features: plugins, sub-agents, LSP, themes, templates, voice
- 5 UI themes (dark, light, dracula, monokai, nord)
- Stream interrupt (Esc/Ctrl+C stops streaming instantly)
- GitHub PR workflow (`/pr`, `/review`, `/issue`)
- Pinned context (`/pin`, `/unpin`)
- Command aliases (`/alias`, `/unalias`)
- Desktop notifications (`/notify`)
- Cost optimizer (`/optimizer`)
- Session sharing (`/share` — styled HTML export)
- Project templates (`/init` — 5 templates)
- Custom tool plugins (`/plugins`)
- Sub-agents (`/agent`, `/agents` — threaded background tasks)
- LSP integration (`/lsp` — Python, TypeScript, Go)
- Voice input (`/voice` — sox + Whisper)

## [1.0.0] - 2025-10-01

### Added
- Initial release
- 26 tools, 7 API providers, MCP server support
- Multi-model consensus mode
- 3 permission modes, plan mode, batch edit review
- Context window management, conversation branching
- Compact streaming, thinking display, tab autocomplete
- Persistent memory (SQLite), session recovery
