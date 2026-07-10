from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .sessions import SessionInfo, list_claude_sessions, list_codex_sessions

LIVE_THRESHOLD_SECONDS = 180


@dataclass(frozen=True)
class DetectedSession:
    agent: str
    info: SessionInfo
    live: bool


@dataclass(frozen=True)
class Activity:
    repo: Path
    sessions: list[DetectedSession]
    processes: dict[str, int]

    def candidates(self, agent: str) -> list[DetectedSession]:
        return [detected for detected in self.sessions if detected.agent == agent]

    def best(self, agent: str) -> DetectedSession | None:
        found = self.candidates(agent)
        return found[0] if found else None


def detect_activity(
    repo: Path,
    claude_home: Path | None = None,
    codex_home: Path | None = None,
    now: float | None = None,
    processes: dict[str, int] | None = None,
) -> Activity:
    """Discover agent sessions relevant to a repo and whether they look live.

    A session counts as live when its transcript was written within
    LIVE_THRESHOLD_SECONDS — the direct evidence an agent is mid-task. Running
    claude/codex processes are reported alongside as corroboration; they cannot
    be tied to a specific session from the outside."""
    current = time.time() if now is None else now
    sessions: list[DetectedSession] = []
    for info in list_claude_sessions(repo, claude_home=claude_home):
        sessions.append(DetectedSession("claude", info, _is_live(info, current)))
    for info in list_codex_sessions(codex_home=codex_home, limit=25):
        if _codex_matches_repo(info, repo):
            sessions.append(DetectedSession("codex", info, _is_live(info, current)))
    return Activity(
        repo=repo,
        sessions=sessions,
        processes=agent_processes() if processes is None else processes,
    )


def format_activity(activity: Activity) -> str:
    lines = [f"Agent activity for {activity.repo.resolve()}:"]
    running = ", ".join(f"{name} x{count}" for name, count in sorted(activity.processes.items()) if count)
    lines.append(f"  Running agent processes: {running or 'none detected'}")
    for agent in ("claude", "codex"):
        found = activity.candidates(agent)
        if not found:
            lines.append(f"  {agent}: no sessions found (connect will cold-start it)")
            continue
        for detected in found[:5]:
            info = detected.info
            stamp = info.modified.strftime("%Y-%m-%d %H:%M")
            state = "LIVE" if detected.live else "idle"
            preview = info.preview or ""
            lines.append(f"  {agent}: [{state}] {info.session_id}  {stamp}  {preview}")
    lines.append("")
    lives = [d for d in activity.sessions if d.live]
    if lives:
        lines.append("Live sessions are best observed read-only: duet peek <agent> <session-id>.")
        lines.append("Connecting to them forks their conversation state (requires --fork-live).")
    lines.append("Connect idle sessions into a duet: duet connect --repo <repo> \"<task>\"")
    return "\n".join(lines)


def agent_processes() -> dict[str, int]:
    try:
        out = subprocess.run(
            ["ps", "-axo", "command"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    counts = {"claude": 0, "codex": 0}
    for line in out.splitlines():
        head = line.strip().split(" ")[0].rsplit("/", 1)[-1]
        if head in counts:
            counts[head] += 1
    return counts


def _is_live(info: SessionInfo, now: float) -> bool:
    return (now - info.modified.timestamp()) <= LIVE_THRESHOLD_SECONDS


def _codex_matches_repo(info: SessionInfo, repo: Path) -> bool:
    """Codex records the cwd it started in, which may be the repo itself or an
    ancestor (an operator who cd'd into the repo mid-session still matches)."""
    if not info.cwd:
        return False
    try:
        cwd = Path(info.cwd).resolve()
        target = repo.resolve()
    except OSError:
        return False
    return cwd == target or cwd in target.parents or target in cwd.parents
