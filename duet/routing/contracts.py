"""Routing contracts (D09): what the router reads and what it decides.

Pure data. `duet.routing.policy.route()` maps a `RoutingRequest` to a
`RoutingDecision` deterministically; the runtime persists the decision,
applies it at the next supported boundary (a managed turn) or offers it as
advice (a native session), and records what the provider accepted and
observed afterwards.

Vocabulary:
- A logical profile (routine < standard < deep < critical_review) is what a
  task needs. It is mapped per provider to a (model, effort) candidate at
  run time, from that provider's own discovered or configured settings. No
  model names are built in, and effort labels are never compared across
  providers.
- A floor is the least profile the assessed risk allows. Budget pressure can
  lower a target towards the floor, never below it.
- Coverage says whether DUET can apply the choice: "enforced" (a managed
  turn with the setting controllable), "partial" (only some of it), or
  "advisory" (a native session, or a setting the provider does not expose)."""
from __future__ import annotations

from dataclasses import dataclass, field

PROFILES = ("routine", "standard", "deep", "critical_review")
RISKS = ("low", "medium", "high", "critical")
UNCERTAINTIES = ("low", "medium", "high")
ROLES = ("writer", "reviewer")
PURPOSES = ("implement", "repair", "review", "investigate", "discussion")
FAILURE_CLASSES = ("environment", "requirements", "hypothesis", "provider", "timeout", "unknown")
COVERAGE = ("enforced", "partial", "advisory")
PRESSURE = ("none", "pressure", "critical")  # from admission (D08): quota/budget state


def profile_rank(profile: str) -> int:
    return PROFILES.index(profile)


def risk_rank(risk: str) -> int:
    return RISKS.index(risk)


@dataclass(frozen=True)
class Reason:
    """One piece of evidence behind an assessment or decision."""

    code: str  # e.g. "path:auth", "keyword:migration", "failures:2", "agent_raised", "pin", "pressure"
    detail: str
    weight: str = "info"  # info | raises | lowers | blocks

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail, "weight": self.weight}


@dataclass(frozen=True)
class FailureRecord:
    """A failed attempt on the task, as the router sees it."""

    source: str  # check | turn | review
    signature: str  # stable identity (see taskgraph failure signatures)
    text: str = ""  # bounded excerpt: check output tail, error message, review summary
    kind: str | None = None  # provider error kind when source == "turn" (quota, auth, timeout, ...)


@dataclass(frozen=True)
class TaskFacts:
    """What the controller knows about a task. Built from the store, never
    from the agent's prose alone."""

    task_id: str
    revision: int
    kind: str  # code | investigate | test_design | review
    description: str
    changed_paths: tuple[str, ...] = ()  # the latest snapshot's changes (or the diff under review)
    changed_lines: int | None = None
    protected_paths: tuple[str, ...] = ()  # user constraints
    has_checks: bool = True  # the acceptance contract names at least one required check
    dependencies: int = 0
    failures: tuple[FailureRecord, ...] = ()  # newest last
    repo_file_count: int | None = None


@dataclass(frozen=True)
class AgentAssessment:
    """What a participant proposed (duet_request_profile or a plan note). It
    can raise scrutiny; it can never lower it below the controller's view."""

    risk: str | None = None
    uncertainty: str | None = None
    profile: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class Assessment:
    risk: str
    uncertainty: str
    floor: str  # least profile allowed for this task
    reasons: tuple[Reason, ...]

    def to_dict(self) -> dict:
        return {"risk": self.risk, "uncertainty": self.uncertainty, "floor": self.floor, "reasons": [r.to_dict() for r in self.reasons]}


@dataclass(frozen=True)
class ProviderControls:
    """What DUET can set on this provider, from D04 capability discovery."""

    provider: str
    model_control: str  # supported | unsupported | unknown (D04 Control values)
    effort_control: str
    efforts: tuple[str, ...] | None  # discovered effort labels, None if not discoverable
    models: tuple[str, ...] | None  # discovered model ids, None if not discoverable


@dataclass(frozen=True)
class Candidate:
    """One provider setting for a profile. model/effort None: the provider's
    (or the user's own) default, untouched."""

    provider: str
    profile: str
    model: str | None
    effort: str | None
    source: str  # "user_map" | "effort_order" | "default"

    def to_dict(self) -> dict:
        return {"provider": self.provider, "profile": self.profile, "model": self.model, "effort": self.effort, "source": self.source}


@dataclass(frozen=True)
class Pin:
    """A user's bound for one provider. model/effort pin the exact setting;
    min/max bound the profiles DUET may choose. Only the user sets pins, and
    DUET never writes them to the provider's own configuration."""

    provider: str
    model: str | None = None
    effort: str | None = None
    min_profile: str | None = None
    max_profile: str | None = None


@dataclass(frozen=True)
class PriorDecision:
    """A previous decision for the same participant, with its outcome."""

    task_id: str
    profile: str
    model: str | None
    effort: str | None
    turn_index: int  # the participant's turn number when it applied
    outcome: str | None  # succeeded | failed | None (not yet known)
    escalated: bool = False


@dataclass(frozen=True)
class RoutingRequest:
    run_id: str
    participant_id: str
    provider: str
    origin: str  # native_original | managed
    role: str  # writer | reviewer
    purpose: str
    task: TaskFacts
    controls: ProviderControls
    turn_index: int  # the turn this decision is for (0 for the first)
    agent: AgentAssessment | None = None
    pins: tuple[Pin, ...] = ()
    user_map: tuple[Candidate, ...] = ()  # user-configured profile -> setting mappings
    history: tuple[PriorDecision, ...] = ()
    pressure: str = "none"
    estimates: dict = field(default_factory=dict)  # metric -> Estimate.to_dict(), from admission


@dataclass(frozen=True)
class Excluded:
    candidate: Candidate
    reason: str

    def to_dict(self) -> dict:
        return {"candidate": self.candidate.to_dict(), "reason": self.reason}


@dataclass(frozen=True)
class RoutingDecision:
    action: str  # run | diagnose_environment | clarify | replan | stop
    profile: str
    model: str | None
    effort: str | None
    coverage: str
    assessment: Assessment
    candidates: tuple[Candidate, ...]  # eligible, best first
    excluded: tuple[Excluded, ...]
    floor_met: bool
    switch: bool  # the setting differs from the participant's previous turn
    switch_cost: str  # none | low | high, with the reason in `reasons`
    escalated: bool
    reasons: tuple[Reason, ...]
    explanation: str  # one paragraph a person can read

    def to_dict(self) -> dict:
        return {
            "action": self.action, "profile": self.profile, "model": self.model, "effort": self.effort,
            "coverage": self.coverage, "assessment": self.assessment.to_dict(),
            "candidates": [c.to_dict() for c in self.candidates], "excluded": [e.to_dict() for e in self.excluded],
            "floor_met": self.floor_met, "switch": self.switch, "switch_cost": self.switch_cost, "escalated": self.escalated,
            "reasons": [r.to_dict() for r in self.reasons], "explanation": self.explanation,
        }


@dataclass(frozen=True)
class RoutingPolicy:
    """Configuration, not performance facts (spec 8.4)."""

    floor_for_risk: tuple[tuple[str, str], ...] = (("low", "routine"), ("medium", "standard"), ("high", "deep"), ("critical", "critical_review"))
    review_floor: str = "standard"  # least profile for any review of a code change
    risky_review_floor: str = "deep"  # for a review when the risk is high or critical
    repairs_per_approach: int = 2  # failed repairs under one approach before a re-plan
    max_escalations_per_task: int = 1
    switch_hysteresis_turns: int = 2  # turns at a setting before a non-forced change
    deescalate_after_successes: int = 2
