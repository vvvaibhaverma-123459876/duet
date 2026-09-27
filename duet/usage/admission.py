"""Completion-aware admission (D08): pure decisions.

Before DUET dispatches a provider turn it asks `decide()`: may this action
run now, given the pools the user authorised, the provider's quota gauges and
the capacity held back to finish the run (the peer's review, repairs)? The
answer is admit, defer or pause. It is never a silent overspend and never a
paid fallback: nothing here can choose another provider, account or billing
mode. All inputs are plain data; `duet.usage.reservations` gathers them
inside the reserving transaction and turns the decision into reservations,
holds and events.

Rules (spec 7.3-7.5):
- Per local_bound pool: estimate + outstanding reservations + remaining
  finishing reserves + uncertainty margin <= allowance - used, in the pool's
  own provider, metric and unit. Outstanding reservations and finishing
  reserves are both HELD rows, so `pool.held` carries them together.
- A finishing action (the review, a repair) draws from its run's earmarked
  reserve on the same pool instead of reserving again; only the part of its
  estimate the reserve does not cover is checked against free capacity.
- Pools are provider scoped: a Claude review is funded only from Claude
  pools and reserves. `decide` refuses a pool of another provider outright.
- Optional work never draws on a finishing reserve, and is the first thing
  deferred under quota pressure or with unknown sizes.
- Unknown quantities get an explicit bounded policy, never a fictional
  inequality: an unknown-size turn is admitted one at a time per pool while
  the pool still has room (optional work waits for a measured turn); unknown
  provider quota allows a bounded number of optional turns per run.
- Quota percentages are compared with thresholds, never converted into
  turns or tokens, never averaged across windows or providers. At or above
  the stop threshold the provider pauses until the window's reset; after
  the reset, stale telemetry is not trusted: one real turn goes ahead as
  the probe, the rest wait for its outcome (no probe storm).
- Enforcement is labelled per pool: provider_cap only when the provider
  itself enforces the limit (Claude's per-invocation `--max-budget-usd`
  for money), local_bound for work DUET schedules, best_effort otherwise. A
  pool that demands a provider cap the provider cannot enforce pauses the
  run for the user before any paid work (no false cap)."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .estimation import Estimate

ACTION_CLASSES = ("finishing", "required", "optional")
VERDICTS = ("admit", "defer", "pause")
PAUSE_KINDS = ("quota", "budget", "approval")
ENFORCEMENT_LABELS = ("provider_cap", "local_bound", "best_effort")

MINUTE_MS = 60_000


@dataclass(frozen=True)
class PoolView:
    """A local pool as seen inside the reserving transaction."""

    pool_id: str
    provider: str
    metric: str
    unit: str
    allowance: Decimal | None  # None: tracked only, no bound
    used: Decimal  # known usage in the window
    held: Decimal  # every HELD reservation: in-flight actions and finishing reserves
    enforcement: str  # local_bound | best_effort | provider_cap, as the user set it
    unknown_records: int = 0  # usage records of unknown size in the window

    @property
    def money(self) -> bool:
        return self.unit.upper() == "USD" or self.metric.startswith("cost")


@dataclass(frozen=True)
class ReserveView:
    """This run's finishing reserve on one pool."""

    reservation_id: str
    purpose: str  # review | repair
    pool_id: str
    quantity: Decimal  # still held
    units_left: int


@dataclass(frozen=True)
class Gauge:
    """One provider quota window as last observed."""

    provider: str
    window: str
    used_percent: Decimal
    resets_at_ms: int | None
    observed_at_ms: int


@dataclass(frozen=True)
class Hold:
    """A provider paused for quota."""

    provider: str
    state: str  # HELD | PROBING
    reason: str
    resume_at_ms: int | None
    placed_at_ms: int
    attempts: int = 0


@dataclass(frozen=True)
class AdmissionPolicy:
    quota_defer_percent: Decimal = Decimal("90")  # at or above: no optional work
    quota_stop_percent: Decimal = Decimal("100")  # at or above: pause the provider until reset
    stale_after_ms: int = 15 * MINUTE_MS
    reset_grace_ms: int = MINUTE_MS  # look again this long after a window's reset time
    backoff_base_ms: int = 5 * MINUTE_MS  # unknown reset time: 5, 10, 20, 40, 60, 60 ... minutes
    backoff_max_ms: int = 60 * MINUTE_MS
    unknown_quota_optional_limit: int = 12  # optional turns per provider per run without quota telemetry
    review_rounds: int = 2  # finishing reserve: the review and one re-review
    repair_turns: int = 2  # finishing reserve: repairs after changes are requested


@dataclass(frozen=True)
class ActionRequest:
    provider: str
    action_class: str  # finishing | required | optional
    purpose: str  # review | repair | implement | discussion | investigate
    estimates: dict = field(default_factory=dict)  # metric -> Estimate
    per_call_budget_cap: bool = False  # the provider enforces a per-invocation money cap
    optional_so_far: int = 0  # optional turns this provider already took in this run
    unknown_in_flight: dict = field(default_factory=dict)  # pool_id -> unknown-size actions still running


@dataclass(frozen=True)
class Line:
    """One reservation to make for an admitted action."""

    pool_id: str
    metric: str
    quantity: Decimal
    draw_from: str | None = None  # a finishing reservation this line is drawn from
    unknown_size: bool = False

    def to_dict(self) -> dict:
        return {"pool": self.pool_id, "metric": self.metric, "quantity": str(self.quantity), "draw_from": self.draw_from, "unknown_size": self.unknown_size}


@dataclass(frozen=True)
class Decision:
    verdict: str  # admit | defer | pause
    reason: str
    pause_kind: str | None = None  # quota | budget | approval, for pause
    lines: tuple[Line, ...] = ()
    enforcement: tuple[tuple[str, str], ...] = ()  # (pool_id, label)
    quota: str = "unknown"  # observed | pressure | stale | unknown | held | probe
    per_call_budget: Decimal | None = None  # pass to the provider, when it enforces one
    resume_at_ms: int | None = None
    hold: str | None = None  # place | probe | release: what the caller does with the provider hold
    warnings: tuple[str, ...] = ()
    transient: bool = False  # defer: retry the same action shortly (another turn is settling something)

    @property
    def admitted(self) -> bool:
        return self.verdict == "admit"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict, "reason": self.reason, "pause_kind": self.pause_kind,
            "lines": [line.to_dict() for line in self.lines], "enforcement": dict(self.enforcement), "quota": self.quota,
            "per_call_budget": None if self.per_call_budget is None else str(self.per_call_budget),
            "resume_at_ms": self.resume_at_ms, "hold": self.hold, "warnings": list(self.warnings), "transient": self.transient,
        }


def backoff_ms(attempts: int, policy: AdmissionPolicy) -> int:
    return min(policy.backoff_base_ms * (2 ** max(attempts, 0)), policy.backoff_max_ms)


def classify_enforcement(pool: PoolView, per_call_budget_cap: bool) -> str:
    """How firmly `pool` binds work DUET schedules for this provider."""
    if pool.allowance is None or pool.enforcement == "best_effort":
        return "best_effort"
    if pool.enforcement == "provider_cap":
        return "provider_cap" if (pool.money and per_call_budget_cap) else "unenforceable"
    return "local_bound"


def fresh_gauges(gauges: list[Gauge], now_ms: int, policy: AdmissionPolicy) -> tuple[list[Gauge], list[Gauge]]:
    """(fresh, stale): a reading is stale when old, or when its window has
    reset since it was taken (its percentage then describes a past window)."""
    fresh, stale = [], []
    for g in gauges:
        expired = g.resets_at_ms is not None and now_ms >= g.resets_at_ms
        (stale if expired or now_ms - g.observed_at_ms > policy.stale_after_ms else fresh).append(g)
    return fresh, stale


def _quota(request: ActionRequest, gauges: list[Gauge], hold: Hold | None, now_ms: int, policy: AdmissionPolicy) -> Decision | None:
    """The provider-quota part. Returns a refusal, or None to go on (with
    the quota label and hold action carried in a partial decision)."""
    fresh, stale = fresh_gauges([g for g in gauges if g.provider == request.provider], now_ms, policy)
    over = [g for g in fresh if g.used_percent >= policy.quota_stop_percent]
    if over:
        binding = max(over, key=lambda g: (g.used_percent, g.resets_at_ms or 0))
        resume = (binding.resets_at_ms + policy.reset_grace_ms) if binding.resets_at_ms else now_ms + backoff_ms(hold.attempts if hold else 0, policy)
        return Decision("pause", f"{request.provider} quota window {binding.window} is at {binding.used_percent}%", "quota",
                        quota="held", resume_at_ms=resume, hold="place")
    if hold is not None:
        newer = [g for g in fresh if g.observed_at_ms > hold.placed_at_ms]
        if newer:
            return None  # fresh telemetry after the hold, below the stop threshold: the caller releases it
        if hold.state == "PROBING":
            return Decision("defer", f"{request.provider} is paused for quota; one turn is checking whether the reset happened", quota="probe", transient=True)
        if hold.resume_at_ms is not None and now_ms < hold.resume_at_ms:
            return Decision("pause", f"{request.provider} is paused for quota: {hold.reason}", "quota", quota="held", resume_at_ms=hold.resume_at_ms)
    if fresh and max(g.used_percent for g in fresh) >= policy.quota_defer_percent and request.action_class == "optional":
        top = max(fresh, key=lambda g: g.used_percent)
        return Decision("defer", f"{request.provider} quota window {top.window} is at {top.used_percent}%: optional work waits, the critical path continues", quota="pressure")
    if not fresh and request.action_class == "optional" and request.optional_so_far >= policy.unknown_quota_optional_limit:
        return Decision("defer", f"no current {request.provider} quota reading: optional work is bounded to {policy.unknown_quota_optional_limit} turns per run", quota="stale" if stale else "unknown")
    return None


def decide(
    request: ActionRequest,
    pools: list[PoolView],
    reserves: list[ReserveView],
    gauges: list[Gauge],
    hold: Hold | None,
    *,
    now_ms: int,
    policy: AdmissionPolicy = AdmissionPolicy(),
) -> Decision:
    """Admit, defer or pause one action. `pools` are the pools that fund the
    request's provider; `reserves` this run's finishing reserves."""
    if request.action_class not in ACTION_CLASSES:
        raise ValueError(f"action_class must be one of {ACTION_CLASSES}")
    for pool in pools:
        if pool.provider != request.provider:
            raise ValueError(f"pool {pool.pool_id} belongs to {pool.provider}; {request.provider} work cannot be funded from it")
    refusal = _quota(request, gauges, hold, now_ms, policy)
    if refusal is not None:
        return refusal
    fresh, stale = fresh_gauges([g for g in gauges if g.provider == request.provider], now_ms, policy)
    warnings: list[str] = []
    hold_action = None
    if hold is not None:
        newer = [g for g in fresh if g.observed_at_ms > hold.placed_at_ms]
        hold_action = "release" if newer else "probe"
    if fresh:
        quota = "pressure" if max(g.used_percent for g in fresh) >= policy.quota_defer_percent else "observed"
    else:
        quota = "stale" if stale else "unknown"
        warnings.append(f"no current {request.provider} quota reading: quota protection is best effort")
    if hold_action == "probe":
        quota = "probe"
        warnings.append(f"{request.provider}'s quota pause ended without a fresh reading: this turn is the one probe")

    lines: list[Line] = []
    labels: list[tuple[str, str]] = []
    budgets: list[Decimal] = []
    for pool in pools:
        label = classify_enforcement(pool, request.per_call_budget_cap)
        if label == "unenforceable":
            return Decision(
                "pause",
                f"pool {pool.pool_id} asks for a provider-enforced cap, which {request.provider} cannot enforce"
                f"{'' if pool.money else ' for this metric'}. DUET will not start work under a cap that does not exist:"
                " set the pool to local_bound (DUET refuses the work it schedules beyond the allowance) or best_effort.",
                "approval", quota=quota, enforcement=((pool.pool_id, "unenforceable"),),
            )
        labels.append((pool.pool_id, label))
        estimate: Estimate | None = request.estimates.get(pool.metric)
        high = estimate.high if estimate is not None else None
        reserve = next((r for r in reserves if r.pool_id == pool.pool_id and r.purpose == request.purpose), None) if request.action_class == "finishing" else None
        if label == "best_effort":
            lines.append(Line(pool.pool_id, pool.metric, high if high is not None else Decimal(0), unknown_size=high is None))
            continue
        available = pool.allowance - pool.used - pool.held
        if high is None:
            # Bounded unknown-size policy: one at a time, while there is room.
            if request.action_class == "optional":
                return Decision("defer", f"the size of a {request.provider} turn in {pool.metric} is not known yet: optional work waits for a measured turn",
                                quota=quota, enforcement=tuple(labels))
            if request.unknown_in_flight.get(pool.pool_id, 0) > 0:
                return Decision("defer", f"another {request.provider} turn of unknown size is still running against {pool.pool_id}", quota=quota,
                                enforcement=tuple(labels), transient=True)
            room = available + (reserve.quantity if reserve else Decimal(0))
            if room <= 0:
                return _short(request, pool, Decimal(0), available, quota, labels)
            warnings.append(f"{pool.pool_id}: turn size unknown, admitted alone while {room} {pool.unit} remain; it can overshoot")
            lines.append(Line(pool.pool_id, pool.metric, Decimal(0), draw_from=reserve.reservation_id if reserve else None, unknown_size=True))
            if label == "provider_cap":
                budgets.append(room)
            continue
        # Each usage record of unknown size is assumed to have been as large as this estimate.
        margin = high * pool.unknown_records
        drawn = min(high, reserve.quantity) if reserve and reserve.units_left > 0 else Decimal(0)
        remainder = high - drawn
        if remainder > 0 and remainder > available - margin:
            return _short(request, pool, high, available - margin, quota, labels, drawn=drawn)
        if drawn and available - margin < 0:
            warnings.append(f"{pool.pool_id} is overdrawn by {margin - available} {pool.unit}; this {request.purpose} is funded from its reserve")
        if drawn:
            lines.append(Line(pool.pool_id, pool.metric, drawn, draw_from=reserve.reservation_id))
        if remainder or not drawn:
            lines.append(Line(pool.pool_id, pool.metric, remainder))
        if label == "provider_cap":
            budgets.append(drawn + max(available - margin, Decimal(0)))
    per_call = min(budgets) if budgets else None
    if per_call is not None and per_call <= 0:
        return Decision("pause", f"no {request.provider} money left under its provider-capped pool", "budget", quota=quota, enforcement=tuple(labels))
    what = {"finishing": f"finishing {request.purpose}", "required": request.purpose, "optional": f"optional {request.purpose}"}[request.action_class]
    return Decision("admit", f"{request.provider} {what} admitted", lines=tuple(lines), enforcement=tuple(labels), quota=quota,
                    per_call_budget=per_call, hold=hold_action, warnings=tuple(warnings))


def _short(request: ActionRequest, pool: PoolView, need: Decimal, free: Decimal, quota: str, labels: list, *, drawn: Decimal = Decimal(0)) -> Decision:
    detail = f"pool {pool.pool_id} cannot cover {need} {pool.unit}"
    if drawn:
        detail += f" ({drawn} from this run's {request.purpose} reserve)"
    detail += f": {max(free, Decimal(0))} free after {pool.used} used and {pool.held} held (including finishing reserves)"
    if request.action_class == "optional":
        return Decision("defer", f"optional work deferred: {detail}", quota=quota, enforcement=tuple(labels))
    return Decision("pause", detail, "budget", quota=quota, enforcement=tuple(labels))


@dataclass(frozen=True)
class FinishingLine:
    provider: str
    pool_id: str
    metric: str
    purpose: str  # review | repair
    units: int
    per_unit: Decimal | None  # None: turn size not known yet

    @property
    def quantity(self) -> Decimal:
        return Decimal(0) if self.per_unit is None else self.per_unit * self.units


def plan_finishing(
    *,
    writer_provider: str | None,
    reviewer_provider: str | None,
    pools: list[PoolView],
    estimates: dict,
    policy: AdmissionPolicy = AdmissionPolicy(),
) -> list[FinishingLine]:
    """What must stay reserved to finish a run: review turns on the
    reviewer's provider, repair turns on the writer's, in each bounded pool
    of that provider (best-effort pools protect nothing). Pass None for a
    participant DUET does not schedule (a native session): its turns are not
    DUET's to reserve. `estimates` maps (provider, metric) -> Estimate."""
    out = []
    for purpose, provider, units in (("review", reviewer_provider, policy.review_rounds), ("repair", writer_provider, policy.repair_turns)):
        if provider is None or units <= 0:
            continue
        for pool in pools:
            if pool.provider != provider or pool.allowance is None or pool.enforcement == "best_effort":
                continue
            estimate: Estimate | None = estimates.get((provider, pool.metric))
            out.append(FinishingLine(provider, pool.pool_id, pool.metric, purpose, units, None if estimate is None else estimate.high))
    return out
