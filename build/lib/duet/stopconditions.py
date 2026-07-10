from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .transcript import Message, Transcript
from .verifiers import VerificationResult, Verifier


@dataclass(frozen=True)
class StopDecision:
    should_stop: bool
    condition: str = ""
    outcome: str = "halted"


def strip_control_tokens(text: str) -> tuple[str, str | None]:
    token = None
    if "[[DONE]]" in text:
        token = "DONE"
    elif "[[HANDOFF]]" in text:
        token = "HANDOFF"
    cleaned = text.replace("[[DONE]]", "").replace("[[HANDOFF]]", "").strip()
    return cleaned, token


class MaxTurns:
    def __init__(self, n: int) -> None:
        self.n = n

    def check(self, transcript: Transcript, **kwargs) -> StopDecision:
        if len(transcript.messages) >= self.n:
            return StopDecision(True, f"MaxTurns({self.n})", "halted")
        return StopDecision(False)


class WallClockBudget:
    def __init__(self, seconds: int) -> None:
        self.seconds = seconds

    def check(self, started_at: float, **kwargs) -> StopDecision:
        if time.monotonic() - started_at >= self.seconds:
            return StopDecision(True, f"WallClockBudget({self.seconds})", "halted")
        return StopDecision(False)


class ControlToken:
    def check(self, control_token: str | None, **kwargs) -> StopDecision:
        if control_token == "DONE":
            return StopDecision(True, "ControlToken([[DONE]])", "success")
        return StopDecision(False)


class LoopDetector:
    def __init__(self, threshold: float) -> None:
        self.threshold = threshold

    def check(self, transcript: Transcript, current: Message, **kwargs) -> StopDecision:
        previous = [m for m in transcript.messages[:-1] if m.agent == current.agent]
        if not previous:
            return StopDecision(False)
        score = jaccard_similarity(current.content, previous[-1].content)
        if score >= self.threshold:
            return StopDecision(True, f"LoopDetector({score:.2f}>={self.threshold})", "halted")
        return StopDecision(False)


class VerifierStop:
    def __init__(self, verifier: Verifier) -> None:
        self.verifier = verifier
        self.last_result: VerificationResult | None = None

    def check(self, workspace, **kwargs) -> StopDecision:
        self.last_result = self.verifier.verify(workspace)
        if self.last_result.success:
            return StopDecision(True, f"VerifierStop({self.verifier.name})", "success")
        return StopDecision(False)


class StopPolicy:
    def __init__(self, max_turns: int, wallclock_seconds: int, loop_threshold: float, verifier: Verifier) -> None:
        self.verifier_stop = VerifierStop(verifier)
        self.conditions = [
            self.verifier_stop,
            ControlToken(),
            MaxTurns(max_turns),
            WallClockBudget(wallclock_seconds),
            LoopDetector(loop_threshold),
        ]

    def check(self, **kwargs) -> StopDecision:
        for condition in self.conditions:
            decision = condition.check(**kwargs)
            if isinstance(condition, ControlToken) and decision.should_stop:
                verifier_result = self.verifier_stop.last_result
                if verifier_result is not None and verifier_result.status == "failed":
                    return StopDecision(False)
            if decision.should_stop:
                return decision
        return StopDecision(False)


def jaccard_similarity(left: str, right: str) -> float:
    a = set(_tokens(left))
    b = set(_tokens(right))
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())
