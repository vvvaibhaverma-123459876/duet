from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_ENTRIES = 200


@dataclass
class RunEntry:
    run_id: str
    pid: int
    workspace: str
    branch: str
    task_head: str
    started: float
    outcome: str = ""  # empty while running
    ended: float = 0.0
    cost_usd: float = 0.0

    def status(self) -> str:
        if self.outcome:
            return self.outcome
        return "running" if _pid_alive(self.pid) else "died"


def registry_path() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "duet" / "runs.json"


def register_run(workspace: Path, branch: str, task: str) -> str:
    run_id = f"{int(time.time())}-{os.getpid()}"
    entry = RunEntry(
        run_id=run_id,
        pid=os.getpid(),
        workspace=str(workspace),
        branch=branch,
        task_head=" ".join(task.split())[:80],
        started=time.time(),
    )
    entries = _load()
    entries.append(entry)
    _save(entries[-MAX_ENTRIES:])
    return run_id


def finish_run(run_id: str, outcome: str, cost_usd: float = 0.0) -> None:
    entries = _load()
    for entry in entries:
        if entry.run_id == run_id:
            entry.outcome = outcome
            entry.ended = time.time()
            entry.cost_usd = cost_usd
    _save(entries)


def list_runs(limit: int = 15) -> list[RunEntry]:
    return list(reversed(_load()[-limit:]))


def format_runs(entries: list[RunEntry]) -> str:
    if not entries:
        return "No duet runs recorded on this machine yet."
    lines = ["Recent duet runs (newest first):"]
    for entry in entries:
        started = time.strftime("%m-%d %H:%M", time.localtime(entry.started))
        cost = f" ${entry.cost_usd:.2f}" if entry.cost_usd else ""
        lines.append(
            f"  [{entry.status():>8}] {started}  pid={entry.pid}{cost}  {entry.workspace}"
            f"  ({entry.branch or 'scratch'})  {entry.task_head}"
        )
    return "\n".join(lines)


def _load() -> list[RunEntry]:
    path = registry_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [RunEntry(**item) for item in raw]
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def _save(entries: list[RunEntry]) -> None:
    path = registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(entry) for entry in entries], indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # registry is advisory; never break a run over it


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
