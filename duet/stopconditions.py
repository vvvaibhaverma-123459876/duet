from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .transcript import Message, Transcript
from .verifiers import VerificationResult, Verifier

TOKEN_RE = re.compile(r"\[\[(DONE|HANDOFF)\]\]")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


@dataclass(frozen=True)
class StopDecision:
    should_stop: bool
    condition: str = ""
    # success: verified completion; unverified: completion claimed but nothing
    # verified it; halted: stopped before completion.
    outcome: str = "halted"


def parse_control_token(text: str) -> str | None:
    """Return DONE/HANDOFF only when the token sits on the agent's final line.

    Prose that merely mentions a token ("I'll emit [[DONE]] once tests pass"),
    tokens inside code fences or inline code, and Duet's own appended warning
    lines are ignored. A final line carrying both tokens is ambiguous and is
    treated as HANDOFF: completion must be claimed unambiguously."""
    in_fence = False
    last = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped.startswith("[Duet warning:") or stripped.startswith("[Duet:"):
            continue
        last = stripped
    tokens = set(TOKEN_RE.findall(_INLINE_CODE_RE.sub("", last)))
    if tokens == {"DONE"}:
        return "DONE"
    if "HANDOFF" in tokens:
        return "HANDOFF"
    return None


def strip_control_tokens(text: str) -> tuple[str, str | None]:
    """Remove every control token from the displayed text and return the token
    that governs the turn (see parse_control_token)."""
    token = parse_control_token(text)
    cleaned = TOKEN_RE.sub("", text).strip()
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
    """A bare [[DONE]] is a completion *claim*. On its own it can only ever
    yield an unverified outcome; StopPolicy upgrades it to success when a
    configured verifier passes on the same state."""

    def check(self, control_token: str | None, **kwargs) -> StopDecision:
        if control_token == "DONE":
            return StopDecision(True, "ControlToken([[DONE]])", "unverified")
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
    """Runs the verifier and decides whether its result establishes completion.

    A pass establishes completion only together with a completion claim, or
    when the verifier was *failing at baseline* (the checks encode the missing
    behaviour and now pass: red to green). A suite that was already green
    before any turn proves nothing about the task."""

    def __init__(self, verifier: Verifier, baseline_status: str | None = None) -> None:
        self.verifier = verifier
        self.baseline_status = baseline_status
        self.last_result: VerificationResult | None = None

    def check(self, workspace, control_token: str | None = None, **kwargs) -> StopDecision:
        self.last_result = self.verifier.verify(workspace)
        if self.last_result.status == "passed" and (control_token == "DONE" or self.baseline_status == "failed"):
            return StopDecision(True, f"VerifierStop({self.verifier.name})", "success")
        return StopDecision(False)


class StopPolicy:
    def __init__(
        self,
        max_turns: int,
        wallclock_seconds: int,
        loop_threshold: float,
        verifier: Verifier,
        baseline_status: str | None = None,
    ) -> None:
        self.verifier_stop = VerifierStop(verifier, baseline_status)
        self.limits = [MaxTurns(max_turns), WallClockBudget(wallclock_seconds), LoopDetector(loop_threshold)]
        # Set when the last check refused a [[DONE]] because verification
        # failed, so the next prompt can say so instead of ignoring it silently.
        self.done_rejected = False

    @property
    def last_result(self) -> VerificationResult | None:
        return self.verifier_stop.last_result

    def check(self, **kwargs) -> StopDecision:
        self.done_rejected = False
        verified = self.verifier_stop.check(**kwargs)
        if verified.should_stop:
            return verified
        if kwargs.get("control_token") == "DONE":
            result = self.verifier_stop.last_result
            if result is not None and result.status == "failed":
                self.done_rejected = True  # a failing gate outranks the claim
            elif result is None or result.status != "passed":
                return ControlToken().check(control_token="DONE")
        for condition in self.limits:
            decision = condition.check(**kwargs)
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
