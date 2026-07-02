from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from duet.adapters import AgentResult
from duet.broker import run_session
from duet.verifiers import AlwaysUnknown, VerificationResult
from duet.workspace import create_workspace


@dataclass
class MockAgent:
    name: str
    display_name: str
    replies: list[str]

    def send(self, prompt: str, workspace: Path) -> AgentResult:
        text = self.replies.pop(0)
        (workspace / f"{self.name}.txt").write_text(text, encoding="utf-8")
        return AgentResult(text=text, exit_code=0, duration_s=0.01, raw_stdout=text, raw_stderr="")


class PassOnSecond:
    name = "pytest"

    def __init__(self) -> None:
        self.calls = 0

    def verify(self, workspace: Path) -> VerificationResult:
        self.calls += 1
        return VerificationResult("passed" if self.calls >= 2 else "failed", self.calls >= 2, "")


def test_ping_pong_order_and_handoff_stripping(tmp_path):
    workspace = create_workspace(str(tmp_path / "w"))
    session = run_session(
        "task",
        workspace,
        {
            "claude": MockAgent("claude", "Claude", ["first [[HANDOFF]]"]),
            "codex": MockAgent("codex", "Codex", ["second [[DONE]]"]),
        },
        "claude",
        6,
        900,
        0.99,
        AlwaysUnknown(),
    )
    assert [m.agent for m in session.transcript.messages] == ["claude", "codex"]
    assert "[[HANDOFF]]" not in session.transcript.messages[0].content
    assert session.outcome == "success"
    assert session.stop_condition == "ControlToken([[DONE]])"


def test_verifier_stops_after_second_turn(tmp_path):
    workspace = create_workspace(str(tmp_path / "w"))
    session = run_session(
        "task",
        workspace,
        {
            "claude": MockAgent("claude", "Claude", ["first"]),
            "codex": MockAgent("codex", "Codex", ["second"]),
        },
        "claude",
        6,
        900,
        0.99,
        PassOnSecond(),
    )
    assert session.outcome == "success"
    assert session.stop_condition == "VerifierStop(pytest)"
