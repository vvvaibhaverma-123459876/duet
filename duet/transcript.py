from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

# 1 (implicit, pre-v2): costs were floats and 0.0 also meant "unknown".
# 2: costs are nullable; verification evidence is recorded.
SCHEMA_VERSION = 2


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
    cost_usd: float | None = None  # None: the agent reported no usable cost
    changed: bool = False  # this turn changed the workspace (commit or dirty tree)


@dataclass
class Transcript:
    task: str
    messages: list[Message] = field(default_factory=list)
    outcome: str = "unknown"
    stop_condition: str = ""
    workspace: str = ""
    error: str = ""
    notes: list[str] = field(default_factory=list)
    # Sum of *reported* costs only; see cost_unknown_turns for completeness.
    total_cost_usd: float = 0.0
    cost_unknown_turns: int = 0
    # {"verifier", "status", "output"} measured before turn 1 and after the last turn.
    baseline_verification: dict | None = None
    final_verification: dict | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.messages:
            # Totals are derived from messages, never trusted from the caller.
            self.total_cost_usd = sum(m.cost_usd for m in self.messages if m.cost_usd is not None)
            self.cost_unknown_turns = sum(1 for m in self.messages if m.cost_usd is None)

    def add(self, message: Message) -> None:
        self.messages.append(message)
        if message.cost_usd is None:
            self.cost_unknown_turns += 1
        else:
            self.total_cost_usd += message.cost_usd

    def note(self, text: str) -> None:
        self.notes.append(text)

    @property
    def cost_complete(self) -> bool:
        return self.cost_unknown_turns == 0

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "task": self.task,
            "messages": [asdict(m) for m in self.messages],
            "outcome": self.outcome,
            "stop_condition": self.stop_condition,
            "workspace": self.workspace,
            "error": self.error,
            "notes": list(self.notes),
            "total_cost_usd": self.total_cost_usd,
            "cost_unknown_turns": self.cost_unknown_turns,
            "baseline_verification": self.baseline_verification,
            "final_verification": self.final_verification,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        version = int(data.get("schema_version", 1))
        known = {f.name for f in fields(Message)}
        messages = []
        for item in data.get("messages", []):
            item = {k: v for k, v in item.items() if k in known}
            if version < 2 and item.get("cost_usd") == 0.0:
                # Pre-v2 writers used 0.0 for "not reported"; that cannot be told
                # apart from a real zero, so it is migrated to unknown.
                item["cost_usd"] = None
            messages.append(Message(**item))
        transcript = cls(
            task=data["task"],
            outcome=data.get("outcome", "unknown"),
            stop_condition=data.get("stop_condition", ""),
            workspace=data.get("workspace", ""),
            error=data.get("error", ""),
            notes=list(data.get("notes", [])),
            baseline_verification=data.get("baseline_verification"),
            final_verification=data.get("final_verification"),
        )
        for message in messages:
            transcript.add(message)
        return transcript

    def save_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load_json(cls, path: Path) -> "Transcript":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def cost_summary(self) -> str:
        known = f"${self.total_cost_usd:.4f} reported"
        if self.cost_unknown_turns:
            return f"{known}; {self.cost_unknown_turns} turn(s) with unknown cost (not counted, not zero)"
        return known

    def render_markdown(self) -> str:
        lines = [
            "# Duet Session",
            "",
            f"**Outcome:** {self.outcome}",
            f"**Stop condition:** {self.stop_condition or 'none'}",
            f"**Workspace:** `{self.workspace}`",
            f"**Model cost:** {self.cost_summary()}",
        ]
        for label, record in (("Baseline verification", self.baseline_verification), ("Final verification", self.final_verification)):
            if record:
                lines.append(f"**{label}:** {record.get('status', 'unknown')} ({record.get('verifier', '?')})")
        lines.extend(
            [
                *([""] + [f"> {note}" for note in self.notes] if self.notes else []),
                "",
                "## Task",
                "",
                self.task,
                "",
                "## Turns",
                "",
            ]
        )
        for message in self.messages:
            cost = "unknown" if message.cost_usd is None else f"${message.cost_usd:.4f}"
            lines.extend(
                [
                    f"### Turn {message.turn_index}: {message.agent}",
                    "",
                    f"- Timestamp: `{message.timestamp}`",
                    f"- Exit code: `{message.exit_code}`",
                    f"- Duration: `{message.duration_s:.2f}s`",
                    f"- Cost: {cost}",
                    "",
                    message.content,
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"
