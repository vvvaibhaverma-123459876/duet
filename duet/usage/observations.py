"""Normalised usage observations (D07).

An `Observation` is one number with its meaning spelled out: provider,
account/pool (None = unknown), native session, optional run, metric (and so
its dimension), scope, telemetry path (`source`) and dedupe identity
(`source_event_id`), counter epoch and baseline, quality and time.

Invariants enforced here, so that no caller can break them later:
- money is `Decimal`, never float; tokens, durations and counts are `int`;
- an unknown value is `value=None` with quality UNKNOWN. Nothing unknown,
  missing or invalid becomes zero;
- gauges (quota windows, context occupancy, elapsed deadline) only take the
  point-in-time scopes `window`/`snapshot`, and counters (tokens, cost,
  active time, attempts, discussion) only take `call`/`turn`/cumulative
  scopes. A context-occupancy figure therefore cannot become consumption.

Provider semantics used by the adapters below (checked 2026-09-27):
- Claude (code.claude.com/docs/en/agent-sdk/cost-tracking): `total_cost_usd`
  and `costUSD` are client-side estimates; a resumed or forked session's
  result restores and includes the session's earlier spend (>= 2.1.277);
  assistant messages of one step share a message id and must be counted
  once; their `output_tokens` is a placeholder; a crash result may carry
  zeroed usage. Anthropic `input_tokens` excludes cache reads/writes, so the
  four token fields are disjoint.
- Codex (codex-rs `protocol.rs`, `TokenUsageInfo::append_last_usage` and
  `TokenUsage::non_cached_input`): every `thread/tokenUsage/updated` adds its
  `last` breakdown to `total`; `cachedInputTokens` is part of
  `inputTokens` and `reasoningOutputTokens` part of `outputTokens`.
  `last` is therefore one update's usage, not necessarily a whole turn.
  Rate-limit updates are sparse: an absent window is not a cleared one."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from ..runtime.contracts import ValidationError


class ObservationError(ValidationError):
    code = "invalid_observation"


class Quality(StrEnum):
    OBSERVED = "observed"  # reported by the provider (or measured by DUET) as a fact
    ESTIMATED = "estimated"  # a derived or client-side figure, e.g. Claude's cost
    UNKNOWN = "unknown"  # the value is not known; `value` is None


class Semantics(StrEnum):
    COUNTER = "counter"  # consumption: deltas of the same metric can be summed
    GAUGE = "gauge"  # a level at a point in time: never summed


class Dimension(StrEnum):
    TOKENS = "tokens"
    COST_ESTIMATED = "cost.estimated"
    COST_BILLED = "cost.billed"  # authoritative billing, only when a provider actually reports it
    QUOTA = "quota"  # account/pool capacity windows
    CONTEXT = "context"  # context-window occupancy
    ACTIVE_TIME = "time.active"
    ELAPSED = "time.elapsed"  # wall clock against a deadline
    ATTEMPTS = "attempts"
    DISCUSSION = "discussion"  # peer-discussion overhead

    @property
    def semantics(self) -> Semantics:
        return Semantics.GAUGE if self in (Dimension.QUOTA, Dimension.CONTEXT, Dimension.ELAPSED) else Semantics.COUNTER


class Scope(StrEnum):
    CALL = "call"  # one provider call/update: a delta
    TURN = "turn"  # one turn: a delta
    SESSION_CUMULATIVE = "session_cumulative"  # running total for a native session (within an epoch)
    THREAD_CUMULATIVE = "thread_cumulative"  # running total for a Codex thread (within an epoch)
    WINDOW = "window"  # a quota window's level
    SNAPSHOT = "snapshot"  # any other point-in-time level

    @property
    def is_cumulative(self) -> bool:
        return self in (Scope.SESSION_CUMULATIVE, Scope.THREAD_CUMULATIVE)

    @property
    def is_direct(self) -> bool:
        return self in (Scope.CALL, Scope.TURN)

    @property
    def counts(self) -> bool:
        return self.is_cumulative or self.is_direct


class Baseline(StrEnum):
    """Where a cumulative counter stood before the first observation of its
    epoch. Only meaningful for cumulative scopes."""

    ZERO = "zero"  # a session/thread DUET saw created: the counter started at zero
    UNKNOWN = "unknown"  # resumed/attached/unverified: earlier history may be included
    INHERITED = "inherited"  # a fork: starts at the parent session's level


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"  # older than the freshness threshold
    EXPIRED = "expired"  # the quota window has reset since it was observed


@dataclass(frozen=True)
class MetricSpec:
    dimension: Dimension
    unit: str
    description: str


METRICS: Mapping[str, MetricSpec] = MappingProxyType({
    "tokens.input": MetricSpec(Dimension.TOKENS, "tokens", "input tokens (Codex: includes cached; Claude: excludes cache reads/writes)"),
    "tokens.output": MetricSpec(Dimension.TOKENS, "tokens", "output tokens, including any reasoning/thinking tokens"),
    "tokens.cache_read": MetricSpec(Dimension.TOKENS, "tokens", "cached input tokens read (Codex: a subset of tokens.input)"),
    "tokens.cache_write": MetricSpec(Dimension.TOKENS, "tokens", "input tokens written to the cache"),
    "tokens.reasoning_output": MetricSpec(Dimension.TOKENS, "tokens", "reasoning tokens (a subset of tokens.output)"),
    "tokens.total": MetricSpec(Dimension.TOKENS, "tokens", "provider-reported total tokens"),
    "cost.estimated_usd": MetricSpec(Dimension.COST_ESTIMATED, "USD", "client-side cost estimate"),
    "cost.billed_usd": MetricSpec(Dimension.COST_BILLED, "USD", "authoritative billed cost"),
    "quota.used_percent": MetricSpec(Dimension.QUOTA, "percent", "share of a quota window used, as the provider reports it"),
    "context.input_tokens": MetricSpec(Dimension.CONTEXT, "tokens", "input tokens currently in the context window"),
    "context.output_tokens": MetricSpec(Dimension.CONTEXT, "tokens", "output tokens of the most recent response in context"),
    "context.window_size": MetricSpec(Dimension.CONTEXT, "tokens", "maximum context window size"),
    "context.used_percent": MetricSpec(Dimension.CONTEXT, "percent", "share of the context window in use"),
    "time.active_ms": MetricSpec(Dimension.ACTIVE_TIME, "ms", "time a DUET-dispatched turn was running"),
    "time.session_ms": MetricSpec(Dimension.ACTIVE_TIME, "ms", "time a native session has been running"),
    "time.api_ms": MetricSpec(Dimension.ACTIVE_TIME, "ms", "time spent waiting for provider API responses"),
    "time.elapsed_ms": MetricSpec(Dimension.ELAPSED, "ms", "wall clock elapsed against a deadline"),
    "attempts.turns": MetricSpec(Dimension.ATTEMPTS, "turns", "turn attempts dispatched"),
    "discussion.messages": MetricSpec(Dimension.DISCUSSION, "messages", "peer discussion messages"),
})

_INTEGER_UNITS = frozenset({"tokens", "ms", "turns", "messages"})
_MAX_TEXT = 512


@dataclass(frozen=True)
class Observation:
    provider: str
    session_id: str | None  # stable native session/thread id; None for account-scoped capacity or when unknown
    metric: str
    value: Decimal | int | None  # None only with quality UNKNOWN
    unit: str
    scope: Scope
    source: str  # telemetry path, e.g. claude.result.total_cost_usd, claude.statusline
    source_event_id: str  # dedupe identity within (provider, source)
    observed_at_ms: int  # UTC epoch ms: provider timestamp when it has one, else receipt time
    quality: Quality = Quality.OBSERVED
    pool_id: str | None = None  # account/quota pool; None = unknown
    run_id: str | None = None
    key: str = ""  # model id for per-model usage, window identity for quota
    epoch: str = ""  # counter epoch: change it whenever a counter may have restarted
    baseline: Baseline = Baseline.UNKNOWN
    parent_session_id: str | None = None  # with Baseline.INHERITED
    window_minutes: int | None = None  # quota only
    resets_at_ms: int | None = None  # quota only
    note: str = ""

    def __post_init__(self) -> None:
        for name, enum in (("scope", Scope), ("quality", Quality), ("baseline", Baseline)):
            raw = getattr(self, name)
            if not isinstance(raw, enum):
                try:
                    object.__setattr__(self, name, enum(raw))
                except ValueError:
                    raise ObservationError(f"{name} {raw!r} is not one of {[m.value for m in enum]}") from None
        for name in ("provider", "source", "source_event_id"):
            _text(name, getattr(self, name), required=True)
        for name in ("session_id", "pool_id", "run_id", "parent_session_id"):
            _text(name, getattr(self, name), required=False)
        for name in ("key", "epoch", "note"):
            if not isinstance(getattr(self, name), str) or len(getattr(self, name)) > _MAX_TEXT:
                raise ObservationError(f"{name} must be a string of at most {_MAX_TEXT} characters")
        spec = METRICS.get(self.metric)
        if spec is None:
            raise ObservationError(f"unknown metric {self.metric!r}; allowed: {sorted(METRICS)}")
        if self.unit != spec.unit:
            raise ObservationError(f"{self.metric} is measured in {spec.unit!r}, not {self.unit!r}")
        dimension = spec.dimension
        if dimension.semantics is Semantics.COUNTER and not self.scope.counts:
            raise ObservationError(f"{self.metric} is a counter: scope must be call, turn or cumulative, not {self.scope}")
        if dimension is Dimension.QUOTA and self.scope is not Scope.WINDOW:
            raise ObservationError("quota observations are window levels (scope 'window')")
        if dimension in (Dimension.CONTEXT, Dimension.ELAPSED) and self.scope is not Scope.SNAPSHOT:
            raise ObservationError(f"{self.metric} is a point-in-time level (scope 'snapshot'), never cumulative usage")
        if self.value is None:
            if self.quality is not Quality.UNKNOWN:
                raise ObservationError("an observation without a value must have quality 'unknown'")
        else:
            if self.quality is Quality.UNKNOWN:
                raise ObservationError("an 'unknown' observation carries no value")
            _check_number(self.metric, self.value, spec.unit)
        if dimension is Dimension.COST_ESTIMATED and self.quality is Quality.OBSERVED:
            raise ObservationError("an estimated cost cannot have quality 'observed'")
        if dimension is Dimension.COST_BILLED and self.quality is Quality.ESTIMATED:
            raise ObservationError("billed cost is authoritative; an estimate belongs in cost.estimated_usd")
        if isinstance(self.observed_at_ms, bool) or not isinstance(self.observed_at_ms, int) or self.observed_at_ms < 0:
            raise ObservationError("observed_at_ms must be a non-negative integer (UTC epoch milliseconds)")
        if (self.baseline is Baseline.INHERITED) != (self.parent_session_id is not None):
            raise ObservationError("an inherited baseline needs parent_session_id, and only it may carry one")
        if dimension is not Dimension.QUOTA and (self.window_minutes is not None or self.resets_at_ms is not None):
            raise ObservationError("window_minutes/resets_at_ms belong to quota observations only")
        for name in ("window_minutes", "resets_at_ms"):
            raw = getattr(self, name)
            if raw is not None and (isinstance(raw, bool) or not isinstance(raw, int) or raw < 0):
                raise ObservationError(f"{name} must be a non-negative integer")

    @property
    def dimension(self) -> Dimension:
        return METRICS[self.metric].dimension

    @property
    def semantics(self) -> Semantics:
        return self.dimension.semantics

    @property
    def known(self) -> bool:
        return self.value is not None

    @property
    def observed_at(self) -> datetime:
        return datetime.fromtimestamp(self.observed_at_ms / 1000, tz=timezone.utc)

    def age_ms(self, now_ms: int) -> int:
        return max(0, now_ms - self.observed_at_ms)

    def freshness(self, now_ms: int, max_age_ms: int) -> Freshness:
        """EXPIRED once a quota window's reset time has passed (its level no
        longer describes the current window), STALE when older than
        `max_age_ms`, else FRESH."""
        if self.resets_at_ms is not None and now_ms >= self.resets_at_ms:
            return Freshness.EXPIRED
        return Freshness.STALE if self.age_ms(now_ms) > max_age_ms else Freshness.FRESH

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "session_id": self.session_id,
            "pool_id": self.pool_id,
            "run_id": self.run_id,
            "metric": self.metric,
            "dimension": self.dimension.value,
            "value": str(self.value) if isinstance(self.value, Decimal) else self.value,
            "unit": self.unit,
            "scope": self.scope.value,
            "source": self.source,
            "source_event_id": self.source_event_id,
            "quality": self.quality.value,
            "observed_at_ms": self.observed_at_ms,
            "key": self.key,
            "epoch": self.epoch,
            "baseline": self.baseline.value,
            "parent_session_id": self.parent_session_id,
            "window_minutes": self.window_minutes,
            "resets_at_ms": self.resets_at_ms,
            "note": self.note,
        }


def _text(name: str, value: object, *, required: bool) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str) or not value or len(value) > _MAX_TEXT:
        raise ObservationError(f"{name} must be a non-empty string of at most {_MAX_TEXT} characters")


def _check_number(metric: str, value: object, unit: str) -> None:
    if isinstance(value, bool) or isinstance(value, float):
        raise ObservationError(f"{metric}: {type(value).__name__} values are not accepted (use Decimal or int)")
    if unit == "USD":
        if not isinstance(value, Decimal):
            raise ObservationError(f"{metric}: money must be a Decimal")
    elif unit in _INTEGER_UNITS:
        if not isinstance(value, int):
            raise ObservationError(f"{metric}: {unit} must be an integer")
    elif not isinstance(value, (int, Decimal)):
        raise ObservationError(f"{metric}: expected Decimal or int")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ObservationError(f"{metric}: value must be finite")
    if value < 0:
        raise ObservationError(f"{metric}: usage values cannot be negative")


# --- parsing helpers ---------------------------------------------------------------


def parse_decimal(value: object) -> Decimal | None:
    """A provider number as Decimal, or None when missing or invalid
    (booleans, strings, negatives, non-finite). Floats go through their
    shortest repr, so 0.01234 stays 0.01234."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        number = Decimal(repr(value))
    else:
        return None
    if not number.is_finite() or number < 0:
        return None
    return number


def parse_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def event_digest(*parts: object) -> str:
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def pool_id_for(provider: str, account_id: object) -> str | None:
    """`provider:account` when the account is known, else None (unknown).
    Models of one account share its pool; sessions of different runs on
    the same account consume the same pool."""
    if isinstance(account_id, str) and account_id.strip():
        return f"{provider}:{account_id.strip()}"
    return None


# --- D04 provider output --------------------------------------------------------------

TURN_SOURCE = "duet.turn"
CLAUDE_COST_SOURCE = "claude.result.total_cost_usd"
CLAUDE_MODEL_USAGE_SOURCE = "claude.result.modelUsage"
CLAUDE_ASSISTANT_SOURCE = "claude.stream.assistant"
STATUSLINE_SOURCE = "claude.statusline"
CODEX_TOKEN_USAGE = "codex.thread.tokenUsage"  # D04 source name, split below
CODEX_TOTAL_SOURCE = "codex.thread.tokenUsage.total"
CODEX_LAST_SOURCE = "codex.thread.tokenUsage.last"
CODEX_RATE_LIMITS = "codex.account.rateLimits"
CODEX_RATE_LIMITS_UPDATED = "codex.account.rateLimits.updated"

_D04_METRICS = {
    "cost_usd": "cost.estimated_usd",
    "tokens.input": "tokens.input",
    "tokens.output": "tokens.output",
    "tokens.cache_read": "tokens.cache_read",
    "tokens.cache_write": "tokens.cache_write",
    "tokens.reasoning_output": "tokens.reasoning_output",
    "tokens.total": "tokens.total",
    "context.window": "context.window_size",
    "quota.used_percent": "quota.used_percent",
}
_D04_QUOTA_KEY = re.compile(r"^(?P<limit>.*):(?P<name>[^:]+):(?P<minutes>\d+|\?)m:resets=(?P<resets>\d+|\?)$")
_CODEX_FIELDS = (
    ("inputTokens", "tokens.input"), ("cachedInputTokens", "tokens.cache_read"), ("cacheWriteInputTokens", "tokens.cache_write"),
    ("outputTokens", "tokens.output"), ("reasoningOutputTokens", "tokens.reasoning_output"), ("totalTokens", "tokens.total"),
)
# The primary metrics each provider's turn is expected to report. A missing
# one becomes an explicit UNKNOWN marker, never a zero.
CLAUDE_EXPECTED = (
    (CLAUDE_COST_SOURCE, "cost.estimated_usd"),
    (CLAUDE_MODEL_USAGE_SOURCE, "tokens.input"), (CLAUDE_MODEL_USAGE_SOURCE, "tokens.output"),
    (CLAUDE_MODEL_USAGE_SOURCE, "tokens.cache_read"), (CLAUDE_MODEL_USAGE_SOURCE, "tokens.cache_write"),
)
CODEX_EXPECTED = ("tokens.input", "tokens.output", "tokens.total", "tokens.cache_read", "tokens.reasoning_output")
_INCOMPLETE = frozenset({"cancelled", "timeout", "failed", "interrupted"})


def from_usage_observation(
    usage: Any,
    *,
    provider: str,
    session_id: str | None,
    source_event_id: str,
    received_at_ms: int,
    pool_id: str | None = None,
    run_id: str | None = None,
    epoch: str = "",
    baseline: Baseline = Baseline.UNKNOWN,
    parent_session_id: str | None = None,
    scope: str | None = None,
) -> Observation:
    """Translate one D04 `UsageObservation`. Quota levels become account
    capacity (a gauge; the session is only the observer); Codex's context window becomes a gauge; Codex
    `tokenUsage` splits into `.total` (thread cumulative) and `.last` (one
    update). `scope` overrides the reported scope (used for a new Claude
    session, whose first call is also the session's running total)."""
    metric = _D04_METRICS.get(usage.metric)
    if metric is None:
        raise ObservationError(f"no normalised metric for D04 metric {usage.metric!r}")
    source, key = usage.source, usage.key or ""
    the_scope = scope or usage.scope
    at = usage.observed_at_ms if usage.observed_at_ms is not None else received_at_ms
    value = usage.value
    window_minutes = resets_at_ms = None
    if metric == "quota.used_percent":
        match = _D04_QUOTA_KEY.match(key)
        if match:
            key = f"{match['limit']}:{match['name']}"
            window_minutes = None if match["minutes"] == "?" else int(match["minutes"])
            resets_at_ms = None if match["resets"] == "?" else int(match["resets"]) * 1000
        # Account capacity: the session only says which session saw it.
        the_scope = Scope.WINDOW
    elif metric == "context.window_size":
        the_scope = Scope.SNAPSHOT
    elif source == CODEX_TOKEN_USAGE:
        cumulative = the_scope == Scope.THREAD_CUMULATIVE
        source = CODEX_TOTAL_SOURCE if cumulative else CODEX_LAST_SOURCE
        the_scope = Scope.THREAD_CUMULATIVE if cumulative else Scope.CALL
    if not Scope(the_scope).is_cumulative:
        baseline, parent_session_id = Baseline.UNKNOWN, None
    return Observation(
        provider=provider, session_id=session_id, metric=metric, value=value, unit=usage.unit or METRICS[metric].unit,
        scope=Scope(the_scope), source=source, source_event_id=source_event_id, observed_at_ms=at,
        quality=Quality(usage.quality), pool_id=pool_id, run_id=run_id, key=key, epoch=epoch, baseline=baseline,
        parent_session_id=parent_session_id, window_minutes=window_minutes, resets_at_ms=resets_at_ms,
    )


def baseline_for_lineage(lineage: str, parent_session_id: str | None) -> tuple[Baseline, str | None]:
    """A new session starts at zero; a fork (or a resume that returned a new
    id) starts at its parent's level; a plain resume continues whatever was
    observed before, and without an earlier observation its history is
    unknown."""
    if lineage == "new":
        return Baseline.ZERO, None
    if lineage in ("forked", "resumed_new_id") and parent_session_id:
        return Baseline.INHERITED, parent_session_id
    return Baseline.UNKNOWN, None


def from_turn_result(
    result: Any,
    *,
    provider: str,
    turn_id: str,
    received_at_ms: int,
    pool_id: str | None = None,
    run_id: str | None = None,
    epoch: str = "",
) -> tuple[Observation, ...]:
    """Observations for one D04 `TurnResult`.

    `turn_id` is DUET's stable identity for the dispatched turn (its action
    id), so ingesting the same result twice is a no-op. For Codex pass an
    `epoch` that changes with each app-server process: a thread's total is
    only comparable within one. Also emitted: one `attempts.turns` and the
    turn's `time.active_ms`; and UNKNOWN markers for primary metrics the turn
    did not report (for example after cancellation), never zeros."""
    if not isinstance(turn_id, str) or not turn_id:
        raise ObservationError("turn_id is required: it is the turn's dedupe identity")
    session = result.session_id
    requested = getattr(result.settings, "requested", None) or {}
    parent = requested.get("resume") if isinstance(requested.get("resume"), str) else None
    baseline, parent_session = baseline_for_lineage(result.lineage, parent)
    if session is None:
        # Without a session id nothing can be chained: isolate this turn.
        epoch, baseline, parent_session = f"{epoch}|turn:{turn_id}", Baseline.UNKNOWN, None
    common = dict(provider=provider, session_id=session, received_at_ms=received_at_ms, pool_id=pool_id, run_id=run_id)
    out: list[Observation] = []
    if provider == "codex":
        out += _codex_turn(result, turn_id, epoch, baseline, parent_session, common)
    else:
        out += _claude_turn(result, turn_id, epoch, baseline, parent_session, common, expected=provider == "claude")
    base = dict(provider=provider, session_id=session, pool_id=pool_id, run_id=run_id, observed_at_ms=received_at_ms, source=TURN_SOURCE)
    out.append(Observation(metric="attempts.turns", value=1, unit="turns", scope=Scope.CALL,
                           source_event_id=f"{TURN_SOURCE}:{turn_id}", note=str(result.status), **base))
    if result.duration_s and result.duration_s > 0:
        out.append(Observation(metric="time.active_ms", value=int(round(result.duration_s * 1000)), unit="ms",
                               scope=Scope.CALL, source_event_id=f"{TURN_SOURCE}:{turn_id}", **base))
    return tuple(out)


def _gauge_event(usage: Any, at_ms: int) -> str:
    return f"{usage.source}:{usage.key}:{at_ms}:{usage.value}"


def _marker(provider, session, source, metric, scope, event, at_ms, pool_id, run_id, epoch, baseline, parent, note) -> Observation:
    cumulative = Scope(scope).is_cumulative
    return Observation(
        provider=provider, session_id=session, metric=metric, value=None, unit=METRICS[metric].unit, scope=scope,
        source=source, source_event_id=event, observed_at_ms=at_ms, quality=Quality.UNKNOWN, pool_id=pool_id,
        run_id=run_id, epoch=epoch, baseline=baseline if cumulative else Baseline.UNKNOWN,
        parent_session_id=parent if cumulative else None, note=note,
    )


def _claude_turn(result, turn_id, epoch, baseline, parent, common, *, expected: bool) -> list[Observation]:
    session, at = common["session_id"], common["received_at_ms"]
    event = f"{common['provider']}:{session or '?'}:{turn_id}"
    out, seen = [], set()
    for usage in result.usage:
        if usage.metric == "quota.used_percent":
            stamp = usage.observed_at_ms if usage.observed_at_ms is not None else at
            out.append(from_usage_observation(usage, source_event_id=_gauge_event(usage, stamp), **common))
            continue
        scope = usage.scope
        if scope == Scope.CALL and result.lineage == "new" and session is not None:
            # A new session's first call is also the session's running total
            # from zero; later resumed calls report that same counter.
            scope = Scope.SESSION_CUMULATIVE
        obs = from_usage_observation(usage, source_event_id=event, epoch=epoch, baseline=baseline,
                                     parent_session_id=parent, scope=scope, **common)
        out.append(obs)
        seen.add((obs.source, obs.metric))
    if expected:
        scope = Scope.SESSION_CUMULATIVE if session is not None else Scope.CALL
        for source, metric in CLAUDE_EXPECTED:
            if (source, metric) not in seen:
                out.append(_marker(common["provider"], session, source, metric, scope, f"{TURN_SOURCE}:{turn_id}:missing", at,
                                   common["pool_id"], common["run_id"], epoch, baseline, parent,
                                   f"turn {result.status}: no {metric} reported (a zeroed crash figure counts as none)"))
    return out


def codex_token_event_id(thread_id: str | None, turn_id: str | None, total: Mapping[str, int]) -> str:
    """Identity of one `thread/tokenUsage/updated`: the same update seen
    again (replay after reconnect, or via both the raw stream and a
    TurnResult) gets the same id, whatever its timestamp."""
    return f"codex:{thread_id or '?'}:{turn_id or '?'}:{event_digest(sorted((k, str(v)) for k, v in total.items()))}"


def _codex_turn(result, turn_id, epoch, baseline, parent, common) -> list[Observation]:
    session, at = common["session_id"], common["received_at_ms"]
    turn = result.provider_invocation_id or turn_id
    totals = [u for u in result.usage if u.source == CODEX_TOKEN_USAGE and u.scope == Scope.THREAD_CUMULATIVE and u.metric != "context.window"]
    event = codex_token_event_id(session, turn, {_D04_METRICS[u.metric]: u.value for u in totals})
    out = []
    total_ids = {id(u) for u in totals}
    for usage in result.usage:
        if id(usage) in total_ids:
            out.append(from_usage_observation(usage, source_event_id=event, epoch=epoch, baseline=baseline, parent_session_id=parent, **common))
        elif usage.metric == "context.window":
            stamp = usage.observed_at_ms if usage.observed_at_ms is not None else at
            out.append(from_usage_observation(usage, source_event_id=f"{event}:window:{stamp}", **common))
        elif usage.metric == "quota.used_percent":
            stamp = usage.observed_at_ms if usage.observed_at_ms is not None else at
            out.append(from_usage_observation(usage, source_event_id=_gauge_event(usage, stamp), **common))
        # `last` (D04 scope "call": the most recent model request) is not
        # taken from a TurnResult: the collector keeps only the final update,
        # so it is not the turn's usage. Raw notifications
        # (from_codex_token_usage) carry every update.
    present = {_D04_METRICS[u.metric] for u in totals}
    missing = [m for m in CODEX_EXPECTED if m not in present]
    tail = result.status in _INCOMPLETE and bool(totals)
    for metric in (CODEX_EXPECTED if tail else missing):
        note = f"turn {result.status}: final usage absent" if tail else f"turn {result.status}: no {metric} reported"
        out.append(_marker("codex", session, CODEX_TOTAL_SOURCE, metric, Scope.THREAD_CUMULATIVE, f"{TURN_SOURCE}:{turn_id}:missing",
                           at, common["pool_id"], common["run_id"], epoch, baseline, parent, note))
    return out


# --- raw provider events -------------------------------------------------------------


def _codex_breakdown(raw: object) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        return {}
    out = {}
    for field_name, metric in _CODEX_FIELDS:
        value = parse_int(raw.get(field_name))
        if value is not None:
            out[metric] = value
    return out


def from_codex_token_usage(
    message: Mapping,
    *,
    received_at_ms: int,
    pool_id: str | None = None,
    run_id: str | None = None,
    epoch: str = "",
    baseline: Baseline = Baseline.UNKNOWN,
    parent_session_id: str | None = None,
) -> tuple[Observation, ...]:
    """A raw `thread/tokenUsage/updated` notification (the whole message or
    its params): `total` as the thread's cumulative counter, `last` as that
    update's own usage (a validator), the context window size as a gauge.
    Missing or invalid fields are simply not observed."""
    params = message.get("params") if isinstance(message.get("params"), Mapping) else message
    thread, turn = params.get("threadId"), params.get("turnId")
    usage = params.get("tokenUsage")
    if not isinstance(thread, str) or not thread or not isinstance(usage, Mapping):
        return ()
    stamp = parse_int(message.get("emittedAtMs"))
    at = stamp if stamp is not None else received_at_ms
    total, last = _codex_breakdown(usage.get("total")), _codex_breakdown(usage.get("last"))
    event = codex_token_event_id(thread, turn, total) if total else f"codex:{thread}:{turn}:last:{event_digest(sorted(last.items()), at)}"
    common = dict(provider="codex", session_id=thread, source_event_id=event, observed_at_ms=at, pool_id=pool_id, run_id=run_id)
    out = [
        Observation(metric=m, value=v, unit="tokens", scope=Scope.THREAD_CUMULATIVE, source=CODEX_TOTAL_SOURCE,
                    epoch=epoch, baseline=baseline, parent_session_id=parent_session_id, **common)
        for m, v in total.items()
    ]
    out += [Observation(metric=m, value=v, unit="tokens", scope=Scope.CALL, source=CODEX_LAST_SOURCE, **common) for m, v in last.items()]
    window = parse_int(usage.get("modelContextWindow"))
    if window is not None:
        out.append(Observation(metric="context.window_size", value=window, unit="tokens", scope=Scope.SNAPSHOT,
                               source=CODEX_TOKEN_USAGE, **{**common, "source_event_id": f"{event}:window:{at}"}))
    return tuple(out)


def from_codex_rate_limits(
    payload: Mapping,
    *,
    received_at_ms: int,
    pool_id: str | None = None,
    source: str | None = None,
) -> tuple[Observation, ...]:
    """Quota windows as account capacity (never consumption, never tokens).

    Accepts an `account/rateLimits/read` result (single `rateLimits` and the
    multi-bucket `rateLimitsByLimitId`), an `account/rateLimits/updated`
    notification, or either's params. Updates are sparse: a window absent
    here keeps its previous observation in the ledger. The pool is
    `pool_id`, else `codex:<accountId>` when the response names the
    account, else unknown."""
    at = received_at_ms
    if isinstance(payload.get("method"), str):
        params = payload.get("params") if isinstance(payload.get("params"), Mapping) else {}
        stamp = parse_int(payload.get("emittedAtMs"))
        at = stamp if stamp is not None else received_at_ms
        source = source or CODEX_RATE_LIMITS_UPDATED
    elif isinstance(payload.get("result"), Mapping):
        params = payload["result"]
    else:
        params = payload
    source = source or CODEX_RATE_LIMITS
    pool = pool_id or pool_id_for("codex", params.get("accountId"))
    snapshots: list[tuple[str, Mapping]] = []
    by_id = params.get("rateLimitsByLimitId")
    if isinstance(by_id, Mapping):
        for limit, snap in sorted(by_id.items()):
            if isinstance(snap, Mapping):
                snapshots.append((str(snap.get("limitId") or limit), snap))
    single = params.get("rateLimits")
    if isinstance(single, Mapping):
        limit = str(single.get("limitId") or single.get("limitName") or "default")
        if limit not in {name for name, _ in snapshots}:
            snapshots.append((limit, single))
    out = []
    for limit, snap in snapshots:
        for name in ("primary", "secondary"):
            window = snap.get(name)
            if not isinstance(window, Mapping):
                continue
            used = parse_decimal(window.get("usedPercent"))
            if used is None:
                continue
            minutes = parse_int(window.get("windowDurationMins"))
            resets = parse_int(window.get("resetsAt"))
            key = f"{limit}:{name}"
            out.append(Observation(
                provider="codex", session_id=None, metric="quota.used_percent", value=used, unit="percent",
                scope=Scope.WINDOW, source=source, source_event_id=f"{source}:{pool}:{key}:{at}:{used}:{resets}",
                observed_at_ms=at, pool_id=pool, key=key, window_minutes=minutes,
                resets_at_ms=resets * 1000 if resets is not None else None,
            ))
    return tuple(out)


def from_claude_assistant_messages(
    events: Iterable[Mapping],
    *,
    received_at_ms: int,
    session_id: str | None = None,
    pool_id: str | None = None,
    run_id: str | None = None,
) -> tuple[Observation, ...]:
    """Per-step input and cache tokens from stream-json assistant messages,
    one observation per message *event*, identified by the API message id.
    Messages of one step share that id (parallel tool use), so the ledger
    counts each id once. Output tokens are skipped (a placeholder at
    message start) and subagent messages are skipped (the result's
    modelUsage covers the whole tree), so this path only validates."""
    fields = (("input_tokens", "tokens.input"), ("cache_read_input_tokens", "tokens.cache_read"),
              ("cache_creation_input_tokens", "tokens.cache_write"))
    out = []
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "assistant" or event.get("parent_tool_use_id"):
            continue
        message = event.get("message")
        if not isinstance(message, Mapping) or not isinstance(message.get("id"), str) or not isinstance(message.get("usage"), Mapping):
            continue
        session = event.get("session_id") if isinstance(event.get("session_id"), str) and event.get("session_id") else session_id
        for field_name, metric in fields:
            value = parse_int(message["usage"].get(field_name))
            if value is not None:
                out.append(Observation(
                    provider="claude", session_id=session, metric=metric, value=value, unit="tokens", scope=Scope.CALL,
                    source=CLAUDE_ASSISTANT_SOURCE, source_event_id=f"claude.message:{message['id']}",
                    observed_at_ms=received_at_ms, pool_id=pool_id, run_id=run_id,
                ))
    return tuple(out)


__all__ = [
    "Baseline", "Dimension", "Freshness", "METRICS", "MetricSpec", "Observation", "ObservationError", "Quality", "Scope",
    "STATUSLINE_SOURCE", "Semantics", "baseline_for_lineage", "codex_token_event_id", "event_digest", "from_claude_assistant_messages",
    "from_codex_rate_limits", "from_codex_token_usage", "from_turn_result", "from_usage_observation", "parse_decimal",
    "parse_int", "pool_id_for",
]
