from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Message:
    turn_index: int
    agent: str
    content: str
    exit_code: int
    duration_s: float
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    raw_stdout: str = ""
    raw_stderr: str = ""
    cost_usd: float = 0.0


@dataclass
class Transcript:
    task: str
    messages: list[Message] = field(default_factory=list)
    outcome: str = "unknown"
    stop_condition: str = ""
    workspace: str = ""
    error: str = ""
    notes: list[str] = field(default_factory=list)
    total_cost_usd: float = 0.0

    def add(self, message: Message) -> None:
        self.messages.append(message)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "messages": [asdict(m) for m in self.messages],
            "outcome": self.outcome,
            "stop_condition": self.stop_condition,
            "workspace": self.workspace,
            "error": self.error,
            "notes": list(self.notes),
            "total_cost_usd": self.total_cost_usd,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        transcript = cls(
            task=data["task"],
            outcome=data.get("outcome", "unknown"),
            stop_condition=data.get("stop_condition", ""),
            workspace=data.get("workspace", ""),
            error=data.get("error", ""),
            notes=list(data.get("notes", [])),
            total_cost_usd=float(data.get("total_cost_usd", 0.0)),
        )
        transcript.messages = [Message(**item) for item in data.get("messages", [])]
        return transcript

    def save_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load_json(cls, path: Path) -> "Transcript":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def render_markdown(self) -> str:
        lines = [
            "# Duet Session",
            "",
            f"**Outcome:** {self.outcome}",
            f"**Stop condition:** {self.stop_condition or 'none'}",
            f"**Workspace:** `{self.workspace}`",
            *([""] + [f"> {note}" for note in self.notes] if self.notes else []),
            "",
            "## Task",
            "",
            self.task,
            "",
            "## Turns",
            "",
        ]
        for message in self.messages:
            lines.extend(
                [
                    f"### Turn {message.turn_index}: {message.agent}",
                    "",
                    f"- Timestamp: `{message.timestamp}`",
                    f"- Exit code: `{message.exit_code}`",
                    f"- Duration: `{message.duration_s:.2f}s`",
                    "",
                    message.content,
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"
