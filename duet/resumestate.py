from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

RESUME_FILE = "resume.json"


class ResumeError(RuntimeError):
    """Raised when no usable resume manifest exists."""


@dataclass
class ResumeState:
    task: str
    outcome: str
    stop_condition: str
    mode: str  # "live" | "scratch"
    workspace: str
    sessions: dict[str, str] = field(default_factory=dict)  # agent -> last session id
    branch: str = ""
    saved_at: float = field(default_factory=time.time)

    def attach_specs(self) -> list[str]:
        return [f"{agent}={session_id}" for agent, session_id in self.sessions.items() if session_id]


def resume_path(workspace: Path) -> Path:
    return workspace / ".duet" / RESUME_FILE


def save_resume_state(workspace: Path, state: ResumeState) -> Path:
    path = resume_path(workspace)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    return path


def load_resume_state(workspace: Path) -> ResumeState:
    path = resume_path(workspace)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ResumeError(
            f"no resume manifest at {path}; only sessions run since this feature exist there — "
            f"use `duet connect` with explicit --attach ids instead"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ResumeError(f"cannot read resume manifest {path}: {exc}") from exc
    try:
        return ResumeState(
            task=data["task"],
            outcome=data.get("outcome", "unknown"),
            stop_condition=data.get("stop_condition", ""),
            mode=data.get("mode", "scratch"),
            workspace=data.get("workspace", str(workspace)),
            sessions={str(k): str(v) for k, v in data.get("sessions", {}).items()},
            branch=data.get("branch", ""),
            saved_at=float(data.get("saved_at", 0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ResumeError(f"malformed resume manifest {path}: {exc}") from exc
