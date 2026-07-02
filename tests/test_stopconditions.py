from __future__ import annotations

import time
from pathlib import Path

from duet.stopconditions import (
    ControlToken,
    LoopDetector,
    MaxTurns,
    StopPolicy,
    WallClockBudget,
    jaccard_similarity,
    strip_control_tokens,
)
from duet.transcript import Message, Transcript
from duet.verifiers import VerificationResult


class PassingVerifier:
    name = "passing"

    def verify(self, workspace: Path) -> VerificationResult:
        return VerificationResult("passed", True, "ok")


class FailingVerifier:
    name = "failing"

    def verify(self, workspace: Path) -> VerificationResult:
        return VerificationResult("failed", False, "no")


def test_control_token_strips_done_and_handoff():
    assert strip_control_tokens("done [[DONE]]")[0] == "done"
    assert strip_control_tokens("yield [[HANDOFF]]") == ("yield", "HANDOFF")


def test_max_turns_fires():
    transcript = Transcript("task", [Message(1, "a", "x", 0, 0.1)])
    decision = MaxTurns(1).check(transcript=transcript)
    assert decision.should_stop
    assert decision.condition == "MaxTurns(1)"


def test_wallclock_fires():
    decision = WallClockBudget(1).check(started_at=time.monotonic() - 2)
    assert decision.should_stop


def test_done_control_token_fires_success():
    decision = ControlToken().check(control_token="DONE")
    assert decision.should_stop
    assert decision.outcome == "success"


def test_loop_detector_similarity_math_and_fire():
    assert jaccard_similarity("alpha beta", "alpha beta gamma") == 2 / 3
    transcript = Transcript(
        "task",
        [
            Message(1, "claude", "same words repeat", 0, 0.1),
            Message(2, "codex", "other", 0, 0.1),
            Message(3, "claude", "same words repeat", 0, 0.1),
        ],
    )
    decision = LoopDetector(0.9).check(transcript=transcript, current=transcript.messages[-1])
    assert decision.should_stop


def test_verifier_precedence_before_done_and_max_turns(tmp_path):
    transcript = Transcript("task", [Message(1, "a", "[[DONE]]", 0, 0.1)])
    decision = StopPolicy(1, 1, 0.9, PassingVerifier()).check(
        transcript=transcript,
        current=transcript.messages[-1],
        control_token="DONE",
        started_at=time.monotonic() - 10,
        workspace=tmp_path,
    )
    assert decision.condition == "VerifierStop(passing)"
    assert decision.outcome == "success"


def test_policy_reaches_cap_when_verifier_fails(tmp_path):
    transcript = Transcript("task", [Message(1, "a", "x", 0, 0.1)])
    decision = StopPolicy(1, 100, 0.9, FailingVerifier()).check(
        transcript=transcript,
        current=transcript.messages[-1],
        control_token=None,
        started_at=time.monotonic(),
        workspace=tmp_path,
    )
    assert decision.condition == "MaxTurns(1)"
