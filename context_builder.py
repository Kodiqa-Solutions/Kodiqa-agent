"""Prompt-context assembly, extracted from the Kodiqa God-class (refactor STEP 3).

ContextBuilder gathers everything that goes into the system prompt — the base
template, persona, global/project context files, git status, shell environment,
and pinned files — plus the git/shell detection helpers. It holds a back-reference
to the agent for shared state; Kodiqa keeps thin wrappers so call sites are unchanged.
"""

import os
import subprocess
import sys

from config import PERSONAS, CONTEXT_FILE, KODIQA_DIR

import logging
_logger = logging.getLogger("kodiqa")


class ContextBuilder:
    def __init__(self, agent):
        self.agent = agent

    def build_system_prompt(self, template):
        """Assemble the full system prompt: base template + persona + context +
        git + shell env + pinned files. Shared by every provider's chat loop."""
        memories_ctx = self.agent.memory.get_context()
        context_file_ctx = self.agent._load_context_file()
        system_prompt = template.format(cwd=self.agent.cwd, model=self.agent.model, memories=memories_ctx)
        if self.agent._persona and self.agent._persona in PERSONAS:
            system_prompt = PERSONAS[self.agent._persona]["prompt"] + "\n\n" + system_prompt
        if context_file_ctx:
            system_prompt += "\n\n" + context_file_ctx
        repo_ctx = self.agent._load_repo_instructions()
        if repo_ctx:
            system_prompt += "\n\n" + repo_ctx
        git_ctx = self.agent._git_context()
        if git_ctx:
            system_prompt += "\n\n" + git_ctx
        env_ctx = self.agent._shell_env_context()
        if env_ctx:
            system_prompt += "\n\n" + env_ctx
        pinned_ctx = self.agent._build_pinned_context()
        if pinned_ctx:
            system_prompt += "\n\n" + pinned_ctx
        return system_prompt

    def build_pinned_context(self):
        """Read all pinned files and format as context block."""
        if not self.agent._pinned_files:
            return ""
        parts = ["## Pinned Files"]
        for path in self.agent._pinned_files:
            try:
                with open(path, "r", errors="replace") as f:
                    content = f.read()
                if len(content) > 10000:
                    content = content[:10000] + "\n... (truncated)"
                rel = os.path.relpath(path, self.agent.cwd) if path.startswith(self.agent.cwd) else path
                parts.append(f"### {rel}\n```\n{content}\n```")
            except Exception:
                _logger.debug("ignored error in build_pinned_context", exc_info=True)
        return "\n\n".join(parts) if len(parts) > 1 else ""

    def shell_env_context(self):
        """Format shell environment for system prompt."""
        if not self.agent.shell_env:
            return ""
        parts = [f"- {k}: {v}" for k, v in self.agent.shell_env.items() if k not in ("cwd",)]
        if parts:
            return "## Shell Environment\n" + "\n".join(parts)
        return ""

    def detect_shell_env(self):
        """Detect shell environment, OS, and dev tools."""
        env = {
            "os": os.uname().sysname,
            "arch": os.uname().machine,
            "shell": os.environ.get("SHELL", "unknown"),
            "python": sys.version.split()[0],
            "cwd": self.agent.cwd,
        }
        # Detect common dev tools
        for tool in ["git", "node", "npm", "cargo", "go", "java", "docker"]:
            try:
                result = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    version = result.stdout.strip().split("\n")[0][:50]
                    env[tool] = version
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        return env

    def detect_git(self):
        """Detect git repo info for current directory."""
        import subprocess
        try:
            # Check if in a git repo
            subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True, check=True, cwd=self.agent.cwd)
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.agent.git_info = None
            return
        info = {}
        try:
            r = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, cwd=self.agent.cwd)
            info["branch"] = r.stdout.strip() or "detached"
        except Exception:
            info["branch"] = "unknown"
        try:
            r = subprocess.run(["git", "log", "--oneline", "-5"], capture_output=True, text=True, cwd=self.agent.cwd)
            info["recent_commits"] = r.stdout.strip()
        except Exception:
            info["recent_commits"] = ""
        try:
            r = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, cwd=self.agent.cwd)
            changes = r.stdout.strip()
            info["changed_files"] = len(changes.splitlines()) if changes else 0
            info["status_short"] = changes
        except Exception:
            info["changed_files"] = 0
            info["status_short"] = ""
        # Capture short diff stat for context
        try:
            r = subprocess.run(["git", "diff", "--stat", "--no-color"], capture_output=True, text=True, cwd=self.agent.cwd, timeout=5)
            info["diff_stat"] = r.stdout.strip()[:500] if r.stdout.strip() else ""
        except Exception:
            info["diff_stat"] = ""
        # Capture staged diff stat
        try:
            r = subprocess.run(["git", "diff", "--staged", "--stat", "--no-color"], capture_output=True, text=True, cwd=self.agent.cwd, timeout=5)
            info["staged_stat"] = r.stdout.strip()[:500] if r.stdout.strip() else ""
        except Exception:
            info["staged_stat"] = ""
        self.agent.git_info = info

    def git_context(self):
        """Format git info for system prompt."""
        if not self.agent.git_info:
            return ""
        g = self.agent.git_info
        lines = ["## Git Repository"]
        lines.append(f"- Branch: {g['branch']}")
        if g["changed_files"]:
            lines.append(f"- Uncommitted changes: {g['changed_files']} files")
            if g.get("status_short"):
                lines.append(f"```\n{g['status_short']}\n```")
        if g.get("diff_stat"):
            lines.append(f"- Unstaged diff:\n```\n{g['diff_stat']}\n```")
        if g.get("staged_stat"):
            lines.append(f"- Staged diff:\n```\n{g['staged_stat']}\n```")
        if g["recent_commits"]:
            lines.append(f"- Recent commits:\n```\n{g['recent_commits']}\n```")
        return "\n".join(lines)

    def _read_capped(self, path, cap=12000):
        try:
            with open(path, "r", errors="replace") as f:
                content = f.read().strip()
            if len(content) > cap:
                content = content[:cap] + "\n... (truncated)"
            return content
        except Exception:
            _logger.debug("ignored error reading %s", path, exc_info=True)
            return ""

    def load_repo_instructions(self):
        """Read in-repo agent instruction files: the cross-tool AGENTS.md standard,
        with CLAUDE.md as an interop fallback. Walks from cwd up to the git/repo root
        so a root-level AGENTS.md is found even when run from a subdirectory; when
        several AGENTS.md exist along the path they're concatenated root-first so the
        nearest (most specific) appears last, per the AGENTS.md spec."""
        cwd = self.agent.cwd
        chain = []
        d = os.path.abspath(cwd)
        for _ in range(25):
            chain.append(d)
            if os.path.isdir(os.path.join(d, ".git")):
                break
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        chain.reverse()  # root-most first
        agents_files = [os.path.join(d, "AGENTS.md") for d in chain
                        if os.path.isfile(os.path.join(d, "AGENTS.md"))]
        parts = []
        if agents_files:
            for p in agents_files:
                content = self._read_capped(p)
                if content:
                    rel = os.path.relpath(p, cwd)
                    parts.append(f"## Project Instructions (AGENTS.md: {rel})\n{content}")
        else:
            # Interop fallback: a CLAUDE.md in the working directory.
            p = os.path.join(cwd, "CLAUDE.md")
            if os.path.isfile(p):
                content = self._read_capped(p)
                if content:
                    parts.append(f"## Project Instructions (CLAUDE.md)\n{content}")
        return "\n\n".join(parts)

    def get_project_context_path(self):
        safe_name = self.agent.cwd.strip("/").replace("/", "-")
        return os.path.join(KODIQA_DIR, "projects", f"{safe_name}.md")

    def load_context_file(self):
        parts = []
        if os.path.isfile(CONTEXT_FILE):
            try:
                with open(CONTEXT_FILE, "r") as f:
                    content = f.read().strip()
                if content:
                    parts.append(f"## Global Context (from ~/.kodiqa/KODIQA.md)\n{content}")
            except Exception:
                _logger.debug("ignored error in load_context_file", exc_info=True)
        project_ctx = self.agent._get_project_context_path()
        if os.path.isfile(project_ctx):
            try:
                with open(project_ctx, "r") as f:
                    content = f.read().strip()
                if content:
                    parts.append(f"## Project Context ({self.agent.cwd})\n{content}")
            except Exception:
                _logger.debug("ignored error in load_context_file", exc_info=True)
        return "\n\n".join(parts)
