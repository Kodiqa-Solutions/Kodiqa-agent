"""Conversation persistence, extracted from the Kodiqa God-class (refactor STEP 3).

SessionStore owns crash-recovery (`session.json`) and the browsable session
archive under `~/.kodiqa/history/`. It holds a back-reference to the agent for
the shared state it reads/writes (history, model, cwd, console); Kodiqa keeps
thin `_save_session`/`_load_session`/… wrappers so call sites are unchanged.
"""

import json
import os

from rich.prompt import Prompt

from config import KODIQA_DIR

import logging
_logger = logging.getLogger("kodiqa")


class SessionStore:
    def __init__(self, agent):
        self.agent = agent

    def save(self):
        """Auto-save conversation to disk for recovery."""
        agent = self.agent
        try:
            # Save full history (str AND content-block/tool_calls messages) so assistant
            # turns aren't lost on restore. Only trim a trailing *unresolved* tool call
            # (an assistant turn issuing tools with no following results) — restoring that
            # would 400, since the next request would have tool_use/tool_calls with no result.
            def _unresolved_toolcall(m):
                if m.get("role") != "assistant":
                    return False
                if m.get("tool_calls"):  # OpenAI-compat format
                    return True
                c = m.get("content")
                return isinstance(c, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_use" for b in c)
            saveable = list(agent.history)
            while saveable and _unresolved_toolcall(saveable[-1]):
                saveable.pop()
            data = {"model": agent.model, "cwd": agent.cwd, "history": saveable}
            with open(agent.session_file, "w") as f:
                json.dump(data, f)
        except Exception:
            _logger.debug("ignored error in save", exc_info=True)

    def load(self):
        """Offer to resume previous session if it exists."""
        agent = self.agent
        if not os.path.isfile(agent.session_file):
            return
        try:
            with open(agent.session_file, "r") as f:
                data = json.load(f)
            history = data.get("history", [])
            if len(history) < 2:
                os.remove(agent.session_file)
                return
            msg_count = len([m for m in history if m.get("role") == "user"])

            def _restore(d, h):
                agent.history = h
                agent.model = d.get("model", agent.model)
                saved_cwd = d.get("cwd", agent.cwd)
                if os.path.isdir(saved_cwd):
                    agent.cwd = saved_cwd
                    os.chdir(agent.cwd)

            # -c / --continue: skip the prompt and resume straight away.
            if agent._auto_resume:
                _restore(data, history)
                agent.console.print(f"[green]Resumed previous session ({msg_count} messages).[/]")
                return
            agent.console.print(f"[dim]Previous session found ({msg_count} messages). Resume? (y/n)[/]")
            try:
                answer = Prompt.ask("Resume", choices=["y", "n"], default="y")
                if answer.lower() == "y":
                    _restore(data, history)
                    agent.console.print("[green]Session restored.[/]")
                else:
                    os.remove(agent.session_file)
            except (EOFError, KeyboardInterrupt):
                os.remove(agent.session_file)
        except Exception:
            _logger.debug("ignored error in load", exc_info=True)

    def resume_from_history(self, session_id):
        """Resume a saved history session by id (or the most recent when id is None).

        Used by the `--resume [ID]` CLI flag; mirrors the `/history resume` path.
        """
        agent = self.agent
        history_dir = os.path.join(KODIQA_DIR, "history")
        index_file = os.path.join(history_dir, "index.json")
        if not os.path.isfile(index_file):
            agent.console.print("[dim]No session history to resume.[/]")
            return
        try:
            with open(index_file, "r") as f:
                index = json.load(f)
        except Exception:
            index = []
        if not index:
            agent.console.print("[dim]No session history to resume.[/]")
            return
        if not session_id:  # most recent
            session_id = str(index[-1].get("id", ""))
        session_file = os.path.join(history_dir, f"session_{session_id}.json")
        if not os.path.isfile(session_file):
            agent.console.print(f"[red]Session {session_id} not found.[/] Use [bold]/history[/] to list them.")
            return
        try:
            with open(session_file, "r") as f:
                data = json.load(f)
        except Exception:
            agent.console.print(f"[red]Could not read session {session_id}.[/]")
            return
        agent.history = data.get("history", [])
        agent.model = data.get("model", agent.model)
        saved_cwd = data.get("cwd", agent.cwd)
        if os.path.isdir(saved_cwd):
            agent.cwd = saved_cwd
            os.chdir(agent.cwd)
        agent.console.print(f"[green]Resumed session {session_id} ({len(agent.history)} messages).[/]")

    def clear(self):
        """Remove saved session file."""
        try:
            if os.path.isfile(self.agent.session_file):
                os.remove(self.agent.session_file)
        except Exception:
            _logger.debug("ignored error in clear", exc_info=True)

    def archive(self):
        """Save current session to the history index on quit."""
        agent = self.agent
        user_msgs = [m for m in agent.history if m.get("role") == "user"]
        if len(user_msgs) < 2:
            return
        try:
            import datetime
            history_dir = os.path.join(KODIQA_DIR, "history")
            os.makedirs(history_dir, exist_ok=True)
            first_user = next(
                (m["content"] for m in agent.history
                 if m.get("role") == "user" and isinstance(m.get("content"), str)),
                "",
            )
            entry = {
                "timestamp": datetime.datetime.now().isoformat(),
                "model": agent.model,
                "cwd": agent.cwd,
                "messages": len(agent.history),
                "user_messages": len(user_msgs),
                "cost": agent.session_tokens.get("cost", 0),
                "tools_used": sum(agent._session_stats.get("tools_used", {}).values()),
                "topic": first_user[:100],
            }
            index_file = os.path.join(history_dir, "index.json")
            index = []
            if os.path.isfile(index_file):
                try:
                    with open(index_file, "r") as f:
                        index = json.load(f)
                except Exception:
                    index = []
            entry["id"] = len(index) + 1
            index.append(entry)
            if len(index) > 100:
                index = index[-100:]
            with open(index_file, "w") as f:
                json.dump(index, f, indent=2)
            # Save full session
            saveable = [m for m in agent.history if isinstance(m.get("content"), str)]
            session_file = os.path.join(history_dir, f"session_{entry['id']}.json")
            with open(session_file, "w") as f:
                json.dump({"model": agent.model, "cwd": agent.cwd, "history": saveable}, f)
        except Exception:
            _logger.debug("ignored error in archive", exc_info=True)
