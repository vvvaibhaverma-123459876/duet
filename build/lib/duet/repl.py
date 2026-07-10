from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import TextIO

from . import __version__
from .broker import SessionResult, run_session
from .config import DuetConfig
from .doctor import available_agent_names
from .transcript import Transcript
from .ui import UI
from .verifiers import AlwaysUnknown, PytestVerifier
from .workspace import create_workspace, git_log_summary, release_lock, seed_demo, workspace_state


class Repl:
    def __init__(
        self,
        config: DuetConfig,
        available_agents: set[str],
        workspace: Path | None = None,
        no_color: bool = False,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        commit_mode: str = "default",
    ) -> None:
        self.config = config
        self.available_agents = available_agents
        self.agents = {name: agent for name, agent in config.agents.items() if name in available_agents}
        self.workspace = workspace or create_workspace()
        self.commit_mode = commit_mode
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.ui = UI(no_color=no_color, stream=self.stdout)
        self.last_result: SessionResult | None = None
        self.force_next: str | None = None
        self.running = True
        self.paused = False

    def run(self) -> int:
        solo = next(iter(self.agents)) if len(self.agents) == 1 else None
        self.ui.banner(__version__, str(self.workspace), {name: name in self.available_agents for name in self.config.agents}, solo)
        while self.running:
            try:
                print("duet › ", end="", file=self.stdout, flush=True)
                line = self.stdin.readline()
            except KeyboardInterrupt:
                print("\nInterrupted. Type /quit to exit or enter another task.", file=self.stdout)
                continue
            if not line:
                break
            line = line.strip()
            if not line:
                print("Enter a task or /help.", file=self.stdout)
                continue
            if line.startswith("/"):
                self._command(line)
            else:
                if self.paused:
                    print("Paused. Type /resume before starting another task.", file=self.stdout)
                    continue
                self._task(line)
        release_lock(self.workspace)
        return 0

    def _task(self, task: str) -> None:
        start = self.force_next or self.config.session.start_with
        if start not in self.agents:
            start = next(iter(self.agents))
        self.force_next = None
        roles = _roles(start)
        result = run_session(
            task,
            self.workspace,
            self.agents,
            start,
            self.config.session.max_turns,
            self.config.session.wallclock_seconds,
            self.config.session.loop_threshold,
            PytestVerifier() if (self.workspace / "test_roman.py").exists() else AlwaysUnknown(),
            roles=roles,
            on_turn=self.ui.turn,
            require_all_agents_for_success=len(self.agents) > 1,
            commit_mode=self.commit_mode,
        )
        self.last_result = result
        print(f"Outcome: {result.outcome} · stop: {result.stop_condition}", file=self.stdout)
        print(f"Transcript: {result.transcript_path}", file=self.stdout)

    def _command(self, line: str) -> None:
        parts = shlex.split(line)
        cmd = parts[0]
        if cmd in {"/quit", "/exit"}:
            self.running = False
        elif cmd == "/help":
            print("/help /status /transcript /diff /log /handoff <agent> /ask <agent> <question> /pause /resume /stop /budget <n> /workspace /save [path] /new /config /quit", file=self.stdout)
        elif cmd == "/status":
            next_agent = self.force_next or self.config.session.start_with
            turn = len(self.last_result.session.transcript.messages) if self.last_result else 0
            print(self.ui.status(str(self.workspace), turn, self.config.session.max_turns, next_agent), file=self.stdout)
        elif cmd == "/transcript":
            if self.last_result:
                print(self.last_result.session.transcript.render_markdown(), file=self.stdout)
            else:
                print("No transcript yet.", file=self.stdout)
        elif cmd == "/diff":
            print(workspace_state(self.workspace), file=self.stdout)
        elif cmd == "/log":
            print(git_log_summary(self.workspace) or "(no commits)", file=self.stdout)
        elif cmd == "/handoff" and len(parts) == 2 and parts[1] in self.agents:
            self.force_next = parts[1]
            print(f"Next speaker forced to {parts[1]}.", file=self.stdout)
        elif cmd == "/ask" and len(parts) >= 3 and parts[1] in self.agents:
            agent = self.agents[parts[1]]
            result = agent.send(" ".join(parts[2:]), self.workspace)
            print(result.text, file=self.stdout)
        elif cmd == "/stop":
            print("Stopped current task; workspace kept.", file=self.stdout)
        elif cmd == "/pause":
            self.paused = True
            print("Paused. Type /resume to continue.", file=self.stdout)
        elif cmd == "/resume":
            self.paused = False
            print("Resumed.", file=self.stdout)
        elif cmd == "/budget" and len(parts) == 2 and parts[1].isdigit():
            self.config.session.max_turns = int(parts[1])
            print(f"max_turns set to {parts[1]}", file=self.stdout)
        elif cmd == "/workspace":
            print(self.workspace, file=self.stdout)
        elif cmd == "/save":
            if not self.last_result:
                print("No transcript yet.", file=self.stdout)
            else:
                target = Path(parts[1]).expanduser() if len(parts) > 1 else self.workspace / ".duet" / "latest.md"
                target.write_text(self.last_result.session.transcript.render_markdown(), encoding="utf-8")
                print(f"Saved {target}", file=self.stdout)
        elif cmd == "/new":
            self.workspace = create_workspace()
            print(f"New workspace: {self.workspace}", file=self.stdout)
        elif cmd == "/config":
            print(f"start_with={self.config.session.start_with} max_turns={self.config.session.max_turns}", file=self.stdout)
        else:
            print(f"Unknown command {cmd}. Type /help for commands.", file=self.stdout)


def run_seeded_repl_task(repl: Repl, task: str) -> None:
    seed_demo(repl.workspace)
    repl._task(task)


def _roles(start: str) -> dict[str, str]:
    other = "codex" if start == "claude" else "claude"
    return {
        start: "You are the Implementer. Edit source files to implement the requested behavior, run tests when useful, then hand off.",
        other: "You are the Verifier. Add edge-case tests when useful, review for bugs, run tests, and report issues or completion.",
    }
