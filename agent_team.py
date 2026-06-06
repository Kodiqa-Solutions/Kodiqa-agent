"""Sub-agents + agent teams, extracted from the Kodiqa God-class (refactor STEP 3).

AgentTeam runs background sub-agents (`/agent`, optionally in an isolated git
worktree) and coordinator/worker agent teams (`/team`). The registries
(_agents, _teams, counters) live on the agent and are read/written via the
back-reference; Kodiqa keeps thin wrappers so call sites are unchanged.
"""

import os
import re
import subprocess
import threading

import requests
from rich.panel import Panel

from config import OLLAMA_URL, is_claude_model


class AgentTeam:
    def __init__(self, agent):
        self.agent = agent

    def create_agent_worktree(self, agent_id):
        """Create a git worktree for isolated agent work."""
        worktree_dir = os.path.join(self.agent.cwd, ".kodiqa_worktrees", agent_id)
        branch = f"kodiqa-{agent_id}"
        try:
            os.makedirs(os.path.dirname(worktree_dir), exist_ok=True)
            subprocess.run(
                ["git", "worktree", "add", "-b", branch, worktree_dir],
                capture_output=True, text=True, timeout=10, cwd=self.agent.cwd, check=True,
            )
            return worktree_dir
        except Exception as e:
            self.agent.console.print(f"  [red]Worktree creation failed: {e}[/]")
            return None

    def handle_agent(self, arg):
        """Spawn a sub-agent to handle a task."""
        if not arg:
            self.agent.console.print("[dim]Usage: /agent <task description>[/]")
            self.agent.console.print("[dim]  /agent --worktree <task> — run in isolated git worktree[/]")
            return
        use_worktree = False
        if arg.strip().startswith("--worktree"):
            use_worktree = True
            arg = arg.replace("--worktree", "", 1).strip()
        if not arg:
            self.agent.console.print("[dim]Provide a task after --worktree[/]")
            return
        active = sum(1 for a in self.agent._agents.values() if a.get("status") == "running")
        if active >= 3:
            self.agent.console.print("[red]Max 3 concurrent agents. Wait for one to finish.[/]")
            return
        self.agent._agent_counter += 1
        agent_id = f"agent_{self.agent._agent_counter}"
        worktree_dir = None
        if use_worktree:
            worktree_dir = self.agent._create_agent_worktree(agent_id)
            if not worktree_dir:
                self.agent.console.print("[yellow]Falling back to shared workspace.[/]")
        self.agent._agents[agent_id] = {
            "task": arg, "status": "running", "result": None,
            "worktree": worktree_dir,
        }
        wt_label = f" [dim](worktree)[/]" if worktree_dir else ""
        self.agent.console.print(f"[green]●[/] Spawned {agent_id}: {arg[:60]}{wt_label}")

        def worker():
            try:
                wt_ctx = f"\nWorking directory: {worktree_dir}" if worktree_dir else ""
                task_prompt = f"Complete this task concisely:{wt_ctx}\n{arg}"
                # Use compact non-streaming query
                if is_claude_model(self.agent.model) or self.agent._is_live_claude(self.agent.model):
                    result = self.agent._claude_nostream(
                        task_prompt,
                        [{"role": "user", "content": arg}]
                    )
                else:
                    provider = self.agent._get_provider_for_model(self.agent.model)
                    if provider:
                        result = self.agent._openai_compat_nostream(
                            task_prompt,
                            [{"role": "user", "content": arg}],
                            provider,
                        )
                    else:
                        resp = requests.post(
                            f"{OLLAMA_URL}/api/chat",
                            json={"model": self.agent.model, "messages": [
                                {"role": "system", "content": task_prompt},
                                {"role": "user", "content": arg},
                            ], "stream": False},
                            timeout=120,
                        )
                        result = resp.json().get("message", {}).get("content", "No response")
                self.agent._agents[agent_id]["result"] = result
                self.agent._agents[agent_id]["status"] = "done"
            except Exception as e:
                self.agent._agents[agent_id]["result"] = f"Error: {e}"
                self.agent._agents[agent_id]["status"] = "error"
            finally:
                if worktree_dir:
                    # Show diff from worktree
                    try:
                        diff = subprocess.run(
                            ["git", "diff", "HEAD"],
                            capture_output=True, text=True, timeout=10, cwd=worktree_dir,
                        )
                        if diff.stdout.strip():
                            self.agent._agents[agent_id]["worktree_diff"] = diff.stdout[:5000]
                    except Exception:
                        pass

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def handle_agents(self):
        """List running and completed agents."""
        if not self.agent._agents:
            self.agent.console.print("[dim]No agents. Use /agent <task> to spawn one.[/]")
            return
        for aid, info in self.agent._agents.items():
            status = info["status"]
            color = {"running": "yellow", "done": "green", "error": "red"}.get(status, "dim")
            wt = " [dim](worktree)[/]" if info.get("worktree") else ""
            self.agent.console.print(f"  [{color}]●[/] {aid} [{color}]{status}[/]{wt} — {info['task'][:50]}")
            if status in ("done", "error") and info.get("result"):
                result = info["result"]
                if len(result) > 500:
                    result = result[:500] + "..."
                self.agent.console.print(Panel(result, title=aid, border_style=color))
            if info.get("worktree_diff"):
                self.agent.console.print(f"  [dim]Worktree has changes. Use 'git merge kodiqa-{aid}' to merge.[/]")
        # Offer to inject completed results
        done = [(aid, info) for aid, info in self.agent._agents.items() if info["status"] == "done" and info.get("result")]
        if done:
            self.agent.console.print(f"\n[dim]{len(done)} completed. Results shown above.[/]")

    def handle_team(self, arg):
        """Spawn a team: coordinator breaks task into subtasks, workers execute in parallel."""
        if not arg:
            self.agent.console.print("[dim]Usage: /team <task description>[/]")
            self.agent.console.print("[dim]  Coordinator splits task → workers execute → results merged[/]")
            return
        self.agent._team_counter += 1
        team_id = f"team_{self.agent._team_counter}"
        self.agent._teams[team_id] = {
            "task": arg, "status": "planning", "subtasks": [], "final_result": None,
        }
        self.agent.console.print(f"[green]●[/] Team {team_id}: {arg[:60]}")
        self.agent.console.print(f"  [cyan]Coordinator planning...[/]")

        def team_worker():
            try:
                # Phase 1: Coordinator breaks task into subtasks
                plan_prompt = (
                    f"Break this task into 2-4 independent subtasks that can be done in parallel. "
                    f"Return ONLY a JSON array of subtask description strings, nothing else.\n\n"
                    f"Task: {arg}"
                )
                if is_claude_model(self.agent.model) or self.agent._is_live_claude(self.agent.model):
                    plan_result = self.agent._claude_nostream(plan_prompt, [{"role": "user", "content": plan_prompt}])
                else:
                    provider = self.agent._get_provider_for_model(self.agent.model)
                    if provider:
                        plan_result = self.agent._openai_compat_nostream(plan_prompt, [{"role": "user", "content": plan_prompt}], provider)
                    else:
                        resp = requests.post(f"{OLLAMA_URL}/api/chat", json={
                            "model": self.agent.model, "messages": [{"role": "user", "content": plan_prompt}], "stream": False,
                        }, timeout=120)
                        plan_result = resp.json().get("message", {}).get("content", "[]")

                # Parse JSON subtasks from response
                import json as _json
                subtasks = []
                try:
                    # Find JSON array in response
                    match = re.search(r'\[.*\]', plan_result, re.DOTALL)
                    if match:
                        subtasks = _json.loads(match.group())
                except Exception:
                    subtasks = [arg]  # Fallback: single task

                if not subtasks:
                    subtasks = [arg]
                subtasks = subtasks[:4]  # Cap at 4

                self.agent._teams[team_id]["status"] = "running"
                self.agent._teams[team_id]["subtasks"] = [
                    {"task": st, "status": "pending", "result": None} for st in subtasks
                ]
                self.agent.console.print(f"  [cyan]Team {team_id}: {len(subtasks)} subtasks planned[/]")
                for i, st in enumerate(subtasks):
                    self.agent.console.print(f"    {i+1}. {st[:60]}")

                # Phase 2: Execute subtasks in parallel via threads
                import concurrent.futures
                def run_subtask(idx, task_desc):
                    self.agent._teams[team_id]["subtasks"][idx]["status"] = "running"
                    try:
                        if is_claude_model(self.agent.model) or self.agent._is_live_claude(self.agent.model):
                            r = self.agent._claude_nostream(task_desc, [{"role": "user", "content": task_desc}])
                        else:
                            provider = self.agent._get_provider_for_model(self.agent.model)
                            if provider:
                                r = self.agent._openai_compat_nostream(task_desc, [{"role": "user", "content": task_desc}], provider)
                            else:
                                resp = requests.post(f"{OLLAMA_URL}/api/chat", json={
                                    "model": self.agent.model, "messages": [{"role": "user", "content": task_desc}], "stream": False,
                                }, timeout=120)
                                r = resp.json().get("message", {}).get("content", "No result")
                        self.agent._teams[team_id]["subtasks"][idx]["result"] = r
                        self.agent._teams[team_id]["subtasks"][idx]["status"] = "done"
                        return r
                    except Exception as e:
                        self.agent._teams[team_id]["subtasks"][idx]["result"] = f"Error: {e}"
                        self.agent._teams[team_id]["subtasks"][idx]["status"] = "error"
                        return f"Error: {e}"

                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(subtasks), 3)) as executor:
                    futures = {executor.submit(run_subtask, i, st): i for i, st in enumerate(subtasks)}
                    concurrent.futures.wait(futures)

                # Phase 3: Merge results
                self.agent._teams[team_id]["status"] = "merging"
                results_text = "\n\n".join(
                    f"Subtask {i+1}: {st['task'][:80]}\nResult: {(st['result'] or 'No result')[:2000]}"
                    for i, st in enumerate(self.agent._teams[team_id]["subtasks"])
                )
                merge_prompt = (
                    f"Merge these subtask results into a single coherent response.\n\n"
                    f"Original task: {arg}\n\n{results_text}"
                )
                if is_claude_model(self.agent.model) or self.agent._is_live_claude(self.agent.model):
                    final = self.agent._claude_nostream(merge_prompt, [{"role": "user", "content": merge_prompt}])
                else:
                    provider = self.agent._get_provider_for_model(self.agent.model)
                    if provider:
                        final = self.agent._openai_compat_nostream(merge_prompt, [{"role": "user", "content": merge_prompt}], provider)
                    else:
                        resp = requests.post(f"{OLLAMA_URL}/api/chat", json={
                            "model": self.agent.model, "messages": [{"role": "user", "content": merge_prompt}], "stream": False,
                        }, timeout=120)
                        final = resp.json().get("message", {}).get("content", "No result")

                self.agent._teams[team_id]["final_result"] = final
                self.agent._teams[team_id]["status"] = "done"
                self.agent.console.print(f"\n  [green]●[/] Team {team_id} complete!")

            except Exception as e:
                self.agent._teams[team_id]["status"] = "error"
                self.agent._teams[team_id]["final_result"] = f"Error: {e}"
                self.agent.console.print(f"\n  [red]●[/] Team {team_id} error: {e}")

        t = threading.Thread(target=team_worker, daemon=True)
        t.start()

    def handle_teams(self):
        """List all teams and their subtask status."""
        if not self.agent._teams:
            self.agent.console.print("[dim]No teams. Use /team <task> to spawn one.[/]")
            return
        for tid, info in self.agent._teams.items():
            status = info["status"]
            color = {"planning": "cyan", "running": "yellow", "merging": "cyan", "done": "green", "error": "red"}.get(status, "dim")
            self.agent.console.print(f"  [{color}]●[/] {tid} [{color}]{status}[/] — {info['task'][:50]}")
            for i, st in enumerate(info.get("subtasks", [])):
                sc = {"pending": "dim", "running": "yellow", "done": "green", "error": "red"}.get(st["status"], "dim")
                self.agent.console.print(f"    [{sc}]●[/] Subtask {i+1}: {st['task'][:50]} [{sc}]{st['status']}[/]")
            if info.get("final_result"):
                result = info["final_result"]
                if len(result) > 500:
                    result = result[:500] + "..."
                self.agent.console.print(Panel(result, title=f"{tid} result", border_style=color))
