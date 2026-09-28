from __future__ import annotations
import contextlib
import json
import os
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import oscompat

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
    cost_usd: float = 0.0  # sum of reported costs only
    cost_unknown_turns: int = 0

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
    with _locked():
        entries = _load()
        entries.append(entry)
        _save(entries[-MAX_ENTRIES:])
    return run_id


def finish_run(run_id: str, outcome: str, cost_usd: float = 0.0, cost_unknown_turns: int = 0) -> None:
    with _locked():
        entries = _load()
        for entry in entries:
            if entry.run_id == run_id:
                entry.outcome = outcome
                entry.ended = time.time()
                entry.cost_usd = cost_usd
                entry.cost_unknown_turns = cost_unknown_turns
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
        if entry.cost_unknown_turns:
            cost += "+?" if cost else " $?"
        lines.append(
            f"  [{entry.status():>14}] {started}  pid={entry.pid}{cost}  {entry.workspace}"
            f"  ({entry.branch or 'scratch'})  {entry.task_head}"
        )
    return "\n".join(lines)


@contextlib.contextmanager
def _locked():
    """Serialise read-modify-write across concurrent duet processes so parallel
    runs cannot drop each other's entries."""
    path = registry_path()
    handle = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path.with_suffix(".lock"), "a+", encoding="utf-8")
        oscompat.lock_file(handle)
    except OSError:
        handle = None  # advisory registry: never break a run over it
    try:
        yield
    finally:
        if handle is not None:
            try:
                oscompat.unlock_file(handle)
            finally:
                handle.close()


def _load() -> list[RunEntry]:
    path = registry_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    known = {f.name for f in fields(RunEntry)}
    entries = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            entries.append(RunEntry(**{k: v for k, v in item.items() if k in known}))
        except TypeError:
            continue  # one malformed entry must not hide the others
    return entries


def _save(entries: list[RunEntry]) -> None:
    path = registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps([asdict(entry) for entry in entries], indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # registry is advisory; never break a run over it


def _pid_alive(pid: int) -> bool:
    return oscompat.pid_exists(pid)
