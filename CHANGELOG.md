# Changelog

All notable changes to Kodiqa are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

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
