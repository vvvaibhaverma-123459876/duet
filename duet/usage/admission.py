"""Completion-aware admission (D08): contracts and pure decisions.

Before DUET dispatches provider work it asks `decide()`: may this action
run now, given the pools the user authorised, the provider quota gauges and
the capacity reserved for finishing the run (the other provider's review,
repairs, checks)? The answer is admit, defer or pause, never a silent
overspend and never a paid fallback. All inputs are plain data; the runtime
gathers them and turns the decision into reservations and events.

Rules (spec 7.3-7.5, D08):
- For each measurable pool: estimate + outstanding reservations + remaining
  finishing reserve + uncertainty margin <= usable authorised capacity, with
  matching scope and units. Finishing actions draw from their earmarked
  reserve instead of counting it again.
- Finishing capacity is provider-specific: a Claude review is funded from
  Claude pools, a Codex review from Codex pools, never from the other.
- Optional work can never consume reserved finishing capacity; it is
  deferred instead.
- Where quantities cannot be measured (no pool, unknown sizes, no quota
  telemetry) a bounded, explicit policy applies and the enforcement is
  labelled best effort; missing telemetry must not make every task
  permanently impossible.
- Quota percentages are never converted into turns or tokens and never
  averaged across providers. A window at or above the stop threshold pauses
  that provider until its reset; near the threshold only finishing work is
  admitted. Stale telemetry is not trusted as a reset.
- Enforcement is classified per pool: provider_cap only where the provider
  itself enforces a limit (Claude's per-invocation budget flag), local_bound
  for work DUET schedules, best_effort otherwise."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

ACTION_CLASSES = ("finishing", "required", "optional")
PURPOSES = ("review", "repair", "implement", "investigate", "discussion", "check")
VERDICTS = ("admit", "defer", "pause")
ENFORCEMENT_LABELS = ("provider_cap", "local_bound", "best_effort", "unbounded")


@dataclass(frozen=True)
class Estimate:
    """What one action is expected to use of one metric. `high` is what
    admission reserves; None means unknown."""

    metric: str
    low: Decimal | None
    high: Decimal | None
    quality: str  # observed | estimated | unknown
    basis: str = ""


@dataclass(frozen=True)
class PoolState:
    """A local pool as the runtime sees it (see runtime/pools.py). `held`
    excludes finishing reservations, which are reported separately."""

    pool_id: str
    provider: str
    metric: str
    unit: str
    allowance: Decimal | None  # None: tracked only, no bound
    used: Decimal
    held: Decimal
    finishing_held: Decimal
    enforcement: str  # local_bound | best_effort | provider_cap (as the user set it)
    uncertain: bool = False  # records of unknown size exist


@dataclass(frozen=True)
class QuotaGauge:
    """A provider quota window as observed (duet.usage ledger capacity)."""

    provider: str
    window: str  # e.g. "primary:300m"
    used_percent: Decimal | None
    resets_at_ms: int | None
    observed_at_ms: int | None
    stale: bool = False


@dataclass(frozen=True)
class ProviderControls:
    """What the provider can enforce itself for one invocation."""

    provider: str
    per_call_budget_cap: bool = False  # e.g. claude --max-budget-usd


@dataclass(frozen=True)
class ActionRequest:
    provider: str
    action_class: str  # finishing | required | optional
    purpose: str
    estimates: tuple[Estimate, ...]
    # Finishing actions name the finishing reservations they may draw from
    # (reservation ids), one per pool at most.
    finishing_available: tuple[tuple[str, str], ...] = ()  # (pool_id, reservation_id)


@dataclass(frozen=True)
class AdmissionPolicy:
    quota_defer_percent: Decimal = Decimal("90")  # at or above: finishing work only
    quota_stop_percent: Decimal = Decimal("100")  # at or above: pause the provider until reset
    stale_after_ms: int = 10 * 60 * 1000
    # Bounded useful work while telemetry is unknown: optional actions per
    # provider per run before DUET asks for a decision.
    unknown_quota_optional_limit: int = 12
    uncertainty_margin: Decimal = Decimal("0")  # added to every estimate, in the pool's unit
    review_rounds: int = 2  # finishing review turns reserved for the non-author provider
    repair_turns: int = 2  # finishing repair turns reserved for the writer's provider


@dataclass(frozen=True)
class Decision:
    verdict: str  # admit | defer | pause
    reason: str
    reservations: tuple[dict, ...] = ()  # {pool, metric, quantity(str), draw_from(res id)|None}
    enforcement: tuple[tuple[str, str], ...] = ()  # (pool_id, provider_cap|local_bound|best_effort|unbounded)
    per_call_budget: Decimal | None = None  # pass to the provider when it can enforce it
    resume_at_ms: int | None = None  # for pause: when to look again (a reset time), if known
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinishingItem:
    provider: str
    metric: str
    quantity: Decimal | None  # None: unknown size (no estimate)
    purpose: str  # review | repair
    count: int = 1


@dataclass(frozen=True)
class RunFacts:
    writer_provider: str
    reviewer_provider: str
    optional_actions_so_far: dict = field(default_factory=dict)  # provider -> count this run


def estimate_turn(provider: str, metric: str, history: list[Decimal], *, prior: Estimate | None = None) -> Estimate:
    """Conservative estimate of one provider turn's use of `metric` from this
    run's (or pool's) observed per-turn history."""
    raise NotImplementedError


def finishing_plan(facts: RunFacts, estimates: dict[tuple[str, str], Estimate], policy: AdmissionPolicy) -> list[FinishingItem]:
    """What must stay reserved to finish: review turns for the reviewer's
    provider and repair turns for the writer's, per metric that has pools."""
    raise NotImplementedError


def decide(
    request: ActionRequest,
    pools: list[PoolState],
    gauges: list[QuotaGauge],
    controls: ProviderControls,
    facts: RunFacts,
    *,
    now_ms: int,
    policy: AdmissionPolicy = AdmissionPolicy(),
) -> Decision:
    """Admit, defer or pause one action."""
    raise NotImplementedError


def classify_enforcement(pool: PoolState, controls: ProviderControls) -> str:
    """How firmly `pool` can be held for this provider."""
    raise NotImplementedError
