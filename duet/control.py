from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StopTarget:
    kind: str  # "duet" | "claude" | "codex"
    pid: int
    detail: str

    def describe(self) -> str:
        return f"{self.kind} (pid {self.pid}) {self.detail}"


def running_targets(repo: Path, ps_output: str | None = None, self_pid: int | None = None) -> list[StopTarget]:
    """Everything stoppable: a duet run holding this repo's lock, plus any
    claude/codex CLI processes on the machine. Agent processes cannot be tied
    to a session file from the outside, so they are identified by tty/runtime
    and the operator chooses."""
    me = os.getpid() if self_pid is None else self_pid
    targets: list[StopTarget] = []
    lock_pid = _lock_holder(repo)
    if lock_pid is not None and _pid_alive(lock_pid):
        targets.append(StopTarget("duet", lock_pid, f"holds {repo / '.duet' / 'session.lock'}"))
    out = _ps() if ps_output is None else ps_output
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_str, tty, etime, command = parts
        head = command.strip().split(" ")[0].rsplit("/", 1)[-1]
        if head not in ("claude", "codex"):
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid in (me, lock_pid):
            continue
        targets.append(StopTarget(head, pid, f"tty={tty} up={etime}"))
    return targets


def stop_target(target: StopTarget, force: bool = False) -> str:
    """SIGINT is what Ctrl-C in the agent's own terminal would send, giving it
    a chance to save state; --force escalates to SIGTERM."""
    sig = signal.SIGTERM if force else signal.SIGINT
    try:
        os.kill(target.pid, sig)
    except ProcessLookupError:
        return f"{target.describe()} — already gone"
    except PermissionError:
        return f"{target.describe()} — permission denied"
    return f"{target.describe()} — sent {sig.name}"


def _ps() -> str:
    try:
        return subprocess.run(
            ["ps", "-axo", "pid=,tty=,etime=,command="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _lock_holder(repo: Path) -> int | None:
    lock = repo / ".duet" / "session.lock"
    try:
        for line in lock.read_text(encoding="utf-8").splitlines():
            if line.startswith("pid="):
                return int(line.split("=", 1)[1])
    except (OSError, ValueError):
        return None
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
