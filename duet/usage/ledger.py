"""A pure, deterministic usage ledger (D07).

The ledger keeps the raw, deduplicated observations and derives everything
else on demand, so the answer depends only on the set of observations (and
explicit session bindings), never on arrival order.

Accounting rules:
- Dedupe: an observation is identified by (provider, session, source,
  source_event_id, metric, key, scope). A replay is a no-op; the same
  identity with a different value is kept once and reported as a conflict.
- Primary and validators: for each (provider, session, metric) the policy
  names an ordered list of sources; the first one present is primary and is
  the only one counted. Every other source validates it (agree, lagging,
  ahead, disagree, incomparable) and never adds to it. A metric seen only
  through undesignated sources is unknown, not counted.
- Counters: `call`/`turn` observations are deltas. Cumulative observations
  become deltas only within one epoch, per key, in time order. The first
  value of an epoch counts from its baseline (zero, the parent's level for a
  fork, or unknown: then it is an unknown bounded by the value). A decrease
  is a reset: it is recorded, starts a new derived epoch and makes that
  interval unknown (work before the reset may be lost); it is never a
  negative or absolute delta.
- Unknown stays unknown: an UNKNOWN observation of a cumulative counter is
  resolved only by a later value of the same epoch that covers its time;
  otherwise it is an unbounded unknown segment. Totals then report a lower
  bound, an upper bound when one exists, and no exact value.
- Gauges (context occupancy, quota windows) are latest-value readings with
  freshness. They are never summed, never enter consumption, and quota
  percentages are never turned into tokens or combined across providers.
- Pools: a delta is attributed to the pool of its observation. When a
  counter's pool changes between observations, or the pool is unknown, the
  delta is unattributed (reported by `unattributed`), not guessed."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from fnmatch import fnmatchcase
from types import MappingProxyType
from typing import Iterable, Mapping

from .observations import (
    CLAUDE_COST_SOURCE,
    CLAUDE_MODEL_USAGE_SOURCE,
    CODEX_LAST_SOURCE,
    CODEX_TOTAL_SOURCE,
    METRICS,
    TURN_SOURCE,
    Baseline,
    Dimension,
    Freshness,
    Observation,
    ObservationError,
    STATUSLINE_SOURCE,
    Quality,
    Semantics,
)

Number = Decimal | int


class Ingest(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"  # same identity and value: a replay
    CONFLICT = "conflict"  # same identity, different value: first kept, conflict recorded


@dataclass(frozen=True)
class SourceRule:
    provider: str  # fnmatch pattern
    metric: str  # fnmatch pattern
    sources: tuple[str, ...]  # preference order: the first present is primary


@dataclass(frozen=True)
class SourcePolicy:
    rules: tuple[SourceRule, ...]

    def preference(self, provider: str, metric: str) -> tuple[str, ...]:
        for rule in self.rules:
            if fnmatchcase(provider, rule.provider) and fnmatchcase(metric, rule.metric):
                return rule.sources
        return ()


DEFAULT_SOURCE_POLICY = SourcePolicy((
    # The CLI result is the session's total; the status line is the fallback
    # for native sessions without a DUET-run CLI result. Per-model costUSD
    # is a breakdown of the same total, so it validates only.
    SourceRule("claude", "cost.estimated_usd", (CLAUDE_COST_SOURCE, STATUSLINE_SOURCE)),
    # modelUsage covers the whole agent tree; per-message usage is main-loop
    # input/cache only and validates.
    SourceRule("claude", "tokens.*", (CLAUDE_MODEL_USAGE_SOURCE,)),
    SourceRule("claude", "time.session_ms", (STATUSLINE_SOURCE,)),
    SourceRule("claude", "time.api_ms", (STATUSLINE_SOURCE,)),
    SourceRule("codex", "tokens.*", (CODEX_TOTAL_SOURCE, CODEX_LAST_SOURCE)),
    SourceRule("*", "attempts.turns", (TURN_SOURCE,)),
    SourceRule("*", "time.active_ms", (TURN_SOURCE,)),
))


@dataclass(frozen=True)
class FreshnessPolicy:
    """Freshness thresholds. These are DUET policy defaults, not provider
    facts: an observation older than its threshold is flagged stale."""

    default_ms: int = 15 * 60_000
    overrides: Mapping[Dimension, int] = field(default_factory=lambda: MappingProxyType({
        Dimension.QUOTA: 10 * 60_000,
        Dimension.CONTEXT: 5 * 60_000,
    }))

    def max_age_ms(self, dimension: Dimension) -> int:
        return self.overrides.get(dimension, self.default_ms)


@dataclass(frozen=True)
class Delta:
    provider: str
    session_id: str | None
    run_id: str | None
    pool_id: str | None  # attributed pool; None = unattributed
    source: str
    metric: str
    key: str
    value: Number | None  # None = unknown
    upper_bound: Number | None  # for an unknown value: its bound, when one exists
    reason: str
    end_ms: int
    start_ms: int | None = None  # previous observation of the same counter
    cumulative: bool = False
    quality: Quality = Quality.OBSERVED
    event_id: str = ""

    @property
    def known(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class Reset:
    provider: str
    session_id: str | None
    source: str
    metric: str
    key: str
    epoch: str
    before: Number
    after: Number
    before_at_ms: int
    after_at_ms: int


@dataclass(frozen=True)
class PoolChange:
    provider: str
    session_id: str | None
    source: str
    metric: str
    key: str
    before: str | None
    after: str | None
    at_ms: int


@dataclass(frozen=True)
class Conflict:
    kept: Observation
    rejected: Observation


@dataclass(frozen=True)
class Validation:
    provider: str
    session_id: str | None
    metric: str
    primary: str
    source: str
    basis: str  # level (both cumulative: latest running totals) | consumption (derived deltas)
    primary_value: Number | None
    value: Number | None
    status: str  # agree | lagging | ahead | disagree | incomparable
    primary_at_ms: int
    at_ms: int


@dataclass(frozen=True)
class MetricConsumption:
    provider: str
    metric: str
    dimension: Dimension
    unit: str
    lower_bound: Number | None  # sum of known deltas; None when none is known
    upper_bound: Number | None  # None when an unknown segment is unbounded
    unknown_segments: int
    quality: Quality  # weakest quality among known deltas; UNKNOWN when none is known
    by_key: Mapping[str, Number]  # known part per key (model)
    sessions: tuple[str | None, ...]
    primary: Mapping[str | None, str | None]  # session -> primary source
    validations: tuple[Validation, ...]
    resets: tuple[Reset, ...]
    latest_observed_at_ms: int
    stale: bool | None  # None when no `now_ms` was given
    deltas: tuple[Delta, ...] = ()

    @property
    def exact(self) -> bool:
        return self.unknown_segments == 0 and self.lower_bound is not None

    @property
    def value(self) -> Number | None:
        """The total when it is fully known; otherwise None (see the bounds)."""
        return self.lower_bound if self.exact else None

    @property
    def disputed(self) -> bool:
        return any(v.status == "disagree" for v in self.validations)


@dataclass(frozen=True)
class TokenTotal:
    provider: str
    value: int | None
    lower_bound: int | None
    upper_bound: int | None
    derivation: str
    excluded: tuple[str, ...]  # subset or unverified metrics deliberately not added


@dataclass(frozen=True)
class TokenAlgebra:
    """How a provider's token metrics relate. `addends` are disjoint and sum
    to the total; `subsets` are already included in another metric."""

    reported_total: str | None
    addends: tuple[str, ...]
    subsets: Mapping[str, str]
    unverified: tuple[str, ...] = ()


TOKEN_ALGEBRA: Mapping[str, TokenAlgebra] = MappingProxyType({
    # codex-rs: non_cached_input = input - cached; reasoning is shown as part
    # of output; total is reported. cacheWriteInputTokens' relation to
    # inputTokens is not established, so it is never added.
    "codex": TokenAlgebra("tokens.total", ("tokens.input", "tokens.output"),
                          MappingProxyType({"tokens.cache_read": "tokens.input", "tokens.reasoning_output": "tokens.output"}),
                          ("tokens.cache_write",)),
    # Anthropic usage: input excludes cache reads and writes; thinking is
    # inside output. No total is reported.
    "claude": TokenAlgebra(None, ("tokens.input", "tokens.cache_read", "tokens.cache_write", "tokens.output"), MappingProxyType({})),
})


@dataclass(frozen=True)
class ConsumptionReport:
    metrics: Mapping[tuple[str, str], MetricConsumption]

    def get(self, provider: str, metric: str) -> MetricConsumption | None:
        return self.metrics.get((provider, metric))

    def providers(self) -> tuple[str, ...]:
        return tuple(sorted({provider for provider, _ in self.metrics}))

    def total_tokens(self, provider: str) -> TokenTotal:
        """Total tokens without double counting subsets (cached input inside
        Codex input, reasoning inside output). Per provider only: token
        counts of different providers do not mean the same thing."""
        algebra = TOKEN_ALGEBRA.get(provider)
        if algebra is None:
            return TokenTotal(provider, None, None, None, "no token algebra is known for this provider", ())
        excluded = tuple(sorted(algebra.subsets)) + algebra.unverified
        if algebra.reported_total and (reported := self.get(provider, algebra.reported_total)) is not None:
            return TokenTotal(provider, reported.value, reported.lower_bound, reported.upper_bound, f"reported {algebra.reported_total}", excluded)
        parts = {metric: self.get(provider, metric) for metric in algebra.addends}
        missing = [metric for metric, part in parts.items() if part is None]
        present = [part for part in parts.values() if part is not None]
        lowers = [p.lower_bound for p in present if p.lower_bound is not None]
        lower = sum(lowers) if lowers else None
        derivation = " + ".join(algebra.addends)
        if missing:
            return TokenTotal(provider, None, lower, None, f"{derivation}; not observed: {', '.join(missing)}", excluded)
        exact = all(p.exact for p in present)
        upper = sum(p.upper_bound for p in present) if all(p.upper_bound is not None for p in present) else None
        return TokenTotal(provider, lower if exact else None, lower, upper, derivation, excluded)


@dataclass(frozen=True)
class GaugeReading:
    provider: str
    session_id: str | None
    pool_id: str | None
    metric: str
    key: str
    value: Number | None
    unit: str
    source: str
    quality: Quality
    observed_at_ms: int
    freshness: Freshness


@dataclass(frozen=True)
class QuotaWindow:
    """One quota window's latest observed level. A percentage of a window,
    nothing more: not tokens, not comparable across providers."""

    provider: str
    pool_id: str | None
    key: str  # e.g. codex:primary, five_hour
    used_percent: Number | None
    window_minutes: int | None
    resets_at_ms: int | None
    observed_at_ms: int
    source: str
    quality: Quality
    freshness: Freshness

    @property
    def current_used_percent(self) -> Number | None:
        """The level only while it still describes the current window."""
        return self.used_percent if self.freshness is Freshness.FRESH else None


@dataclass
class _Series:
    deltas: list[Delta] = field(default_factory=list)
    resets: list[Reset] = field(default_factory=list)
    pool_changes: list[PoolChange] = field(default_factory=list)
    level: Number | None = None
    level_complete: bool = True
    cumulative_only: bool = False
    latest_at: int = 0

    @property
    def known_sum(self) -> Number | None:
        known = [d.value for d in self.deltas if d.value is not None]
        return sum(known) if known else None

    @property
    def all_known(self) -> bool:
        return all(d.value is not None for d in self.deltas)


@dataclass(frozen=True)
class _SessionMetric:
    provider: str
    session_id: str | None
    metric: str
    primary: str | None
    deltas: tuple[Delta, ...]
    validations: tuple[Validation, ...]
    resets: tuple[Reset, ...]


@dataclass(frozen=True)
class _Derived:
    session_metrics: tuple[_SessionMetric, ...]
    resets: tuple[Reset, ...]
    pool_changes: tuple[PoolChange, ...]


_QUALITY_RANK = {Quality.OBSERVED: 2, Quality.ESTIMATED: 1, Quality.UNKNOWN: 0}


def _order(o: Observation) -> tuple:
    # Time first; equal timestamps order a monotone counter by value, then by
    # identity, so the result never depends on arrival order.
    return (o.observed_at_ms, o.value is not None, o.value if o.value is not None else 0, o.source_event_id)


def _zero_like(value: Number) -> Number:
    return Decimal(0) if isinstance(value, Decimal) else 0


def _delta(o: Observation, value: Number | None, upper: Number | None, reason: str, *,
           start_ms: int | None = None, cumulative: bool = False, pool_id: str | None = None) -> Delta:
    return Delta(
        provider=o.provider, session_id=o.session_id, run_id=o.run_id, pool_id=pool_id if cumulative else o.pool_id,
        source=o.source, metric=o.metric, key=o.key, value=value, upper_bound=upper, reason=reason, end_ms=o.observed_at_ms,
        start_ms=start_ms, cumulative=cumulative, quality=o.quality if value is not None else Quality.UNKNOWN,
        event_id=o.source_event_id,
    )


def _bounded(o: Observation, bound: Number, reason: str, **kwargs) -> Delta:
    """An unknown delta known to lie in [0, bound]; a zero bound is exact."""
    if bound == 0:
        return _delta(o, _zero_like(bound), None, f"{reason}:bounded_zero", **kwargs)
    return _delta(o, None, bound, reason, **kwargs)


class Ledger:
    def __init__(
        self,
        policy: SourcePolicy | None = None,
        *,
        freshness: FreshnessPolicy | None = None,
        tolerance: Mapping[str, Number] | None = None,
    ) -> None:
        self.policy = policy or DEFAULT_SOURCE_POLICY
        self.freshness = freshness or FreshnessPolicy()
        # Validation tolerance per unit. Money: one micro-dollar.
        self.tolerance: dict[str, Number] = dict(tolerance) if tolerance is not None else {"USD": Decimal("0.000001")}
        self._obs: dict[tuple, Observation] = {}
        self._conflicts: list[Conflict] = []
        self._bindings: dict[str, list[tuple[str, int | None, int | None]]] = {}
        self._derived: _Derived | None = None

    # -- input -----------------------------------------------------------------------------

    @staticmethod
    def identity(o: Observation) -> tuple:
        return (o.provider, o.session_id, o.source, o.source_event_id, o.metric, o.key, o.scope.value)

    def ingest(self, observation: Observation) -> Ingest:
        if not isinstance(observation, Observation):
            raise ObservationError("the ledger only accepts Observation instances")
        identity = self.identity(observation)
        existing = self._obs.get(identity)
        if existing is not None:
            if existing.value == observation.value and existing.quality is observation.quality:
                if observation.observed_at_ms < existing.observed_at_ms:
                    # A replay never predates the original: keep the earliest
                    # time, so the stored state does not depend on arrival order.
                    self._obs[identity] = observation
                    self._derived = None
                return Ingest.DUPLICATE
            self._conflicts.append(Conflict(existing, observation))
            return Ingest.CONFLICT
        self._obs[identity] = observation
        self._derived = None
        return Ingest.ACCEPTED

    def ingest_all(self, observations: Iterable[Observation]) -> tuple[Ingest, ...]:
        return tuple(self.ingest(o) for o in observations)

    def attribute_session(self, session_id: str, run_id: str, *, since_ms: int | None = None, until_ms: int | None = None) -> None:
        """Count a session's run-less observations (for example a native
        session's status line) towards `run_id` between `since_ms` and
        `until_ms` (None: unbounded). A session can join several runs in
        turn. A cumulative delta whose interval crosses either boundary
        includes work outside the run, so for the run it is unknown, bounded
        by the delta."""
        if since_ms is not None and until_ms is not None and until_ms <= since_ms:
            raise ObservationError("until_ms must be later than since_ms")
        self._bindings.setdefault(session_id, []).append((run_id, since_ms, until_ms))
        self._derived = None

    # -- raw views ---------------------------------------------------------------------------

    @property
    def observations(self) -> tuple[Observation, ...]:
        return tuple(sorted(self._obs.values(), key=lambda o: (_order(o), tuple("" if part is None else part for part in self.identity(o)))))

    @property
    def conflicts(self) -> tuple[Conflict, ...]:
        return tuple(self._conflicts)

    def resets(self) -> tuple[Reset, ...]:
        return self._derive().resets

    def pool_changes(self) -> tuple[PoolChange, ...]:
        return self._derive().pool_changes

    # -- consumption ---------------------------------------------------------------------------

    def consumption(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        pool_id: str | None = None,
        provider: str | None = None,
        now_ms: int | None = None,
    ) -> ConsumptionReport:
        """Consumption by (provider, metric) for the matching deltas. Filters
        combine; None means no filter. Pool consumption includes only deltas
        attributed to that pool (see `unattributed`)."""
        return self._report(session_id=session_id, run_id=run_id, pool_id=pool_id, provider=provider, now_ms=now_ms, unattributed=False)

    def session_consumption(self, session_id: str, *, now_ms: int | None = None) -> ConsumptionReport:
        return self.consumption(session_id=session_id, now_ms=now_ms)

    def run_consumption(self, run_id: str, *, now_ms: int | None = None) -> ConsumptionReport:
        return self.consumption(run_id=run_id, now_ms=now_ms)

    def pool_consumption(self, pool_id: str, *, now_ms: int | None = None) -> ConsumptionReport:
        return self.consumption(pool_id=pool_id, now_ms=now_ms)

    def unattributed(self, *, provider: str | None = None, now_ms: int | None = None) -> ConsumptionReport:
        """Deltas no pool can be charged with: unknown account, or the
        account changed between two observations of the counter."""
        return self._report(session_id=None, run_id=None, pool_id=None, provider=provider, now_ms=now_ms, unattributed=True)

    def _report(self, *, session_id, run_id, pool_id, provider, now_ms, unattributed) -> ConsumptionReport:
        grouped: dict[tuple[str, str], list[tuple[_SessionMetric, list[Delta]]]] = defaultdict(list)
        for sm in self._derive().session_metrics:
            if provider is not None and sm.provider != provider:
                continue
            if session_id is not None and sm.session_id != session_id:
                continue
            deltas = list(sm.deltas)
            if run_id is not None:
                deltas = [view for view in (self._run_view(d, run_id) for d in deltas) if view is not None]
            if pool_id is not None:
                deltas = [d for d in deltas if d.pool_id == pool_id]
            if unattributed:
                deltas = [d for d in deltas if d.pool_id is None]
            if deltas:
                grouped[(sm.provider, sm.metric)].append((sm, deltas))
        metrics = {key: self._aggregate(key, items, now_ms) for key, items in sorted(grouped.items())}
        return ConsumptionReport(MappingProxyType(metrics))

    def _run_view(self, d: Delta, run_id: str) -> Delta | None:
        if d.run_id is not None:
            return d if d.run_id == run_id else None
        bindings = self._bindings.get(d.session_id, ()) if d.session_id is not None else ()
        for bound_run, since, until in bindings:
            if bound_run != run_id:
                continue
            if not d.cumulative:  # a point in time
                if (since is None or d.end_ms >= since) and (until is None or d.end_ms < until):
                    return d
                continue
            if (since is not None and d.end_ms <= since) or (until is not None and d.start_ms is not None and d.start_ms >= until):
                continue  # entirely outside the run
            if (since is None or (d.start_ms is not None and d.start_ms >= since)) and (until is None or d.end_ms <= until):
                return d
            bound = d.value if d.value is not None else d.upper_bound
            if bound is not None and bound == 0:
                return d
            return replace(d, value=None, upper_bound=bound, reason="straddles_run_boundary", quality=Quality.UNKNOWN)
        return None

    def _aggregate(self, key: tuple[str, str], items, now_ms: int | None) -> MetricConsumption:
        provider, metric = key
        spec = METRICS[metric]
        deltas = [d for _, ds in items for d in ds]
        known = [d for d in deltas if d.value is not None]
        unknown = [d for d in deltas if d.value is None]
        lower = sum(d.value for d in known) if known else None
        if not unknown:
            upper = lower
        elif all(d.upper_bound is not None for d in unknown):
            upper = (lower or 0) + sum(d.upper_bound for d in unknown)
        else:
            upper = None
        by_key: dict[str, Number] = {}
        for d in known:
            by_key[d.key] = by_key.get(d.key, 0) + d.value
        quality = min((d.quality for d in known), key=_QUALITY_RANK.__getitem__) if known else Quality.UNKNOWN
        latest = max(d.end_ms for d in deltas)
        stale = None if now_ms is None else (now_ms - latest) > self.freshness.max_age_ms(spec.dimension)
        sessions = tuple(sorted({sm.session_id for sm, _ in items}, key=lambda s: (s is None, s or "")))
        return MetricConsumption(
            provider=provider, metric=metric, dimension=spec.dimension, unit=spec.unit, lower_bound=lower, upper_bound=upper,
            unknown_segments=len(unknown), quality=quality, by_key=MappingProxyType(dict(sorted(by_key.items()))),
            sessions=sessions, primary=MappingProxyType({sm.session_id: sm.primary for sm, _ in items}),
            validations=tuple(v for sm, _ in items for v in sm.validations),
            resets=tuple(r for sm, _ in items for r in sm.resets), latest_observed_at_ms=latest, stale=stale,
            deltas=tuple(deltas),
        )

    # -- gauges ------------------------------------------------------------------------------------

    def context(self, session_id: str, *, now_ms: int) -> Mapping[str, GaugeReading]:
        """Latest context-occupancy readings for a session, by metric. A
        level, not usage: a compaction lowers it and nothing is reset."""
        latest: dict[str, Observation] = {}
        for o in self._obs.values():
            if o.dimension is Dimension.CONTEXT and o.session_id == session_id:
                current = latest.get(o.metric)
                if current is None or _order(o) > _order(current):
                    latest[o.metric] = o
        max_age = self.freshness.max_age_ms(Dimension.CONTEXT)
        return MappingProxyType({
            metric: GaugeReading(o.provider, o.session_id, o.pool_id, o.metric, o.key, o.value, o.unit, o.source, o.quality,
                                 o.observed_at_ms, o.freshness(now_ms, max_age))
            for metric, o in sorted(latest.items())
        })

    def capacity(self, *, now_ms: int, pool_id: str | None = None, provider: str | None = None) -> tuple[QuotaWindow, ...]:
        """The latest level of every quota window, per (pool, provider,
        window). Sessions and runs sharing a pool see the same windows; the
        latest observation wins, nothing is added or averaged."""
        latest: dict[tuple, Observation] = {}
        for o in self._obs.values():
            if o.dimension is not Dimension.QUOTA:
                continue
            if (pool_id is not None and o.pool_id != pool_id) or (provider is not None and o.provider != provider):
                continue
            # A known pool is shared by every session on it; with an unknown
            # pool, windows seen by different sessions are kept apart.
            window = (o.pool_id or "", o.provider, o.key, "" if o.pool_id else (o.session_id or ""))
            current = latest.get(window)
            if current is None or _order(o) > _order(current):
                latest[window] = o
        max_age = self.freshness.max_age_ms(Dimension.QUOTA)
        return tuple(
            QuotaWindow(o.provider, o.pool_id, o.key, o.value, o.window_minutes, o.resets_at_ms, o.observed_at_ms, o.source,
                        o.quality, o.freshness(now_ms, max_age))
            for _, o in sorted(latest.items())
        )

    def binding_window(self, pool_id: str, *, now_ms: int) -> QuotaWindow | None:
        """The pool's most-used fresh window: the constraint that binds first.
        A pool belongs to one provider; percentages of different providers
        are not comparable and are never combined."""
        windows = self.capacity(now_ms=now_ms, pool_id=pool_id)
        if len({w.provider for w in windows}) > 1:
            raise ObservationError(f"pool {pool_id!r} holds windows of several providers; their percentages are not comparable")
        fresh = [w for w in windows if w.current_used_percent is not None]
        return max(fresh, key=lambda w: (w.used_percent, w.key)) if fresh else None

    # -- derivation ----------------------------------------------------------------------------------

    def _derive(self) -> _Derived:
        if self._derived is not None:
            return self._derived
        counters = sorted((o for o in self._obs.values() if o.semantics is Semantics.COUNTER), key=_order)
        index: dict[tuple, list[Observation]] = defaultdict(list)
        groups: dict[tuple, dict[str, list[Observation]]] = defaultdict(lambda: defaultdict(list))
        for o in counters:
            groups[(o.provider, o.session_id, o.metric)][o.source].append(o)
            if o.scope.is_cumulative and o.value is not None:
                index[(o.provider, o.session_id, o.source, o.metric, o.key)].append(o)
        session_metrics, resets, pool_changes = [], [], []
        for (provider, session, metric), by_source in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "", kv[0][2])):
            series = {source: self._series(obs, index) for source, obs in sorted(by_source.items())}
            for s in series.values():
                resets += s.resets
                pool_changes += s.pool_changes
            preference = self.policy.preference(provider, metric)
            primary = next((source for source in preference if source in series), None)
            if primary is None:
                last = max((o for obs in by_source.values() for o in obs), key=_order)
                deltas = (Delta(provider, session, last.run_id, None, "", metric, "", None, None, "no_designated_primary", last.observed_at_ms,
                                quality=Quality.UNKNOWN),)
                validations: tuple[Validation, ...] = ()
                primary_resets: tuple[Reset, ...] = ()
            else:
                deltas = tuple(series[primary].deltas)
                validations = tuple(self._validate(provider, session, metric, primary, series[primary], source, s)
                                    for source, s in series.items() if source != primary)
                primary_resets = tuple(series[primary].resets)
            session_metrics.append(_SessionMetric(provider, session, metric, primary, deltas, validations, primary_resets))
        self._derived = _Derived(tuple(session_metrics), tuple(resets), tuple(pool_changes))
        return self._derived

    def _series(self, observations: list[Observation], index) -> _Series:
        """Deltas of one (provider, session, metric, source)."""
        out = _Series(latest_at=max(o.observed_at_ms for o in observations))
        direct = [o for o in observations if o.scope.is_direct]
        cumulative = [o for o in observations if o.scope.is_cumulative]
        out.cumulative_only = bool(cumulative) and not direct
        for o in direct:
            out.deltas.append(_delta(o, o.value, None, "direct") if o.value is not None else _delta(o, None, None, "unknown"))
        epochs: dict[str, list[Observation]] = defaultdict(list)
        for o in cumulative:
            epochs[o.epoch].append(o)
        if len(epochs) > 1:
            out.level_complete = False
        latest_values: dict[str, Observation] = {}
        for epoch, members in sorted(epochs.items()):
            members.sort(key=_order)
            first = members[0]
            by_key: dict[str, list[Observation]] = defaultdict(list)
            for o in members:
                if o.value is not None:
                    by_key[o.key].append(o)
            epoch_deltas: list[Delta] = []
            for key, values in sorted(by_key.items()):
                prev: Observation | None = None
                for o in values:
                    if prev is None:
                        delta = self._first_delta(o, first.baseline, first.parent_session_id, index)
                    else:
                        pool = o.pool_id if o.pool_id == prev.pool_id else None
                        if o.pool_id != prev.pool_id:
                            out.pool_changes.append(PoolChange(o.provider, o.session_id, o.source, o.metric, key, prev.pool_id, o.pool_id, o.observed_at_ms))
                        if o.value >= prev.value:
                            delta = _delta(o, o.value - prev.value, None, "delta", start_ms=prev.observed_at_ms, cumulative=True, pool_id=pool)
                        else:
                            out.resets.append(Reset(o.provider, o.session_id, o.source, o.metric, key, epoch, prev.value, o.value,
                                                    prev.observed_at_ms, o.observed_at_ms))
                            out.level_complete = False
                            delta = _delta(o, None, None, "reset", start_ms=prev.observed_at_ms, cumulative=True, pool_id=pool)
                    epoch_deltas.append(delta)
                    prev = o
                current = latest_values.get(key)
                if current is None or _order(values[-1]) > _order(current):
                    latest_values[key] = values[-1]
            for marker in (o for o in members if o.value is None):
                at = marker.observed_at_ms
                covered = any(d.reason != "reset" and (d.start_ms is None or d.start_ms < at) and at <= d.end_ms for d in epoch_deltas)
                if not covered:
                    epoch_deltas.append(_delta(marker, None, None, "unknown", cumulative=True, pool_id=marker.pool_id))
            out.deltas += epoch_deltas
        levels = [o.value for o in latest_values.values()]
        direct_known = [o.value for o in direct if o.value is not None]
        if levels or direct_known:
            out.level = sum(levels) + sum(direct_known)
        return out

    def _first_delta(self, o: Observation, baseline: Baseline, parent: str | None, index) -> Delta:
        common = dict(cumulative=True, pool_id=o.pool_id)
        if baseline is Baseline.ZERO:
            return _delta(o, o.value, None, "baseline_zero", **common)
        if baseline is Baseline.INHERITED:
            level = self._level_at(index, o, parent)
            if level is None:
                return _bounded(o, o.value, "inherited_unknown", **common)
            if o.value >= level:
                return _delta(o, o.value - level, None, "inherited", **common)
            return _bounded(o, o.value, "inherited_mismatch", **common)
        return _bounded(o, o.value, "baseline_unknown", **common)

    @staticmethod
    def _level_at(index, o: Observation, parent: str | None) -> Number | None:
        earlier = [p for p in index.get((o.provider, parent, o.source, o.metric, o.key), ()) if p.observed_at_ms <= o.observed_at_ms]
        return earlier[-1].value if earlier else None

    def _validate(self, provider, session, metric, primary: str, p: _Series, source: str, v: _Series) -> Validation:
        if p.cumulative_only and v.cumulative_only:
            basis, pv, vv, comparable = "level", p.level, v.level, p.level_complete and v.level_complete
        else:
            basis, pv, vv, comparable = "consumption", p.known_sum, v.known_sum, p.all_known and v.all_known
        tolerance = self.tolerance.get(METRICS[metric].unit, 0)
        if pv is None or vv is None:
            status = "incomparable"
        elif abs(pv - vv) <= tolerance:
            status = "agree"
        elif not comparable:
            status = "incomparable"
        elif vv < pv:
            status = "lagging" if v.latest_at < p.latest_at else "disagree"
        else:
            status = "ahead" if v.latest_at > p.latest_at else "disagree"
        return Validation(provider, session, metric, primary, source, basis, pv, vv, status, p.latest_at, v.latest_at)


__all__ = [
    "ConsumptionReport", "Conflict", "DEFAULT_SOURCE_POLICY", "Delta", "FreshnessPolicy", "GaugeReading", "Ingest", "Ledger",
    "MetricConsumption", "PoolChange", "QuotaWindow", "Reset", "STATUSLINE_SOURCE", "SourcePolicy", "SourceRule",
    "TOKEN_ALGEBRA", "TokenAlgebra", "TokenTotal", "Validation",
]
