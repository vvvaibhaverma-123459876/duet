"""Capability-oriented provider contract (spec 6.1).

Adapters start or resume a provider session, run one turn, stream events,
cancel, and report what they observed. They never decide completion or task
policy: a TurnResult is input to the controller, not its authority.

Three things are kept deliberately separate:
- settings: what was requested, what the adapter accepted and passed on, and
  what the provider reported back (R08). A setting that could not be
  confirmed is reported as unobserved, never as applied;
- usage: raw observations with a scope (this turn, this call, the session so
  far) and a quality (observed/estimated). Nothing unknown becomes zero (R06);
- session lineage: whether the turn continued the same native session, got a
  new id, or forked, as actually reported by the provider (R02, AT42)."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable, Protocol

from ..adapters import AgentError
from ..runtime.contracts import Control, UsageCapability, ValidationError

PERMISSION_PROFILES = ("read_only", "workspace_write")


class UnsupportedSetting(ValidationError):
    """A requested model/effort/permission setting this provider or version
    cannot apply. Raised before dispatch instead of silently ignoring it."""

    code = "unsupported_setting"


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    protocol: str
    version: str | None
    streaming: bool
    session_resume: bool
    session_fork: bool
    session_id_preassign: bool
    cancel: str  # interrupt | terminate
    model_control: Control
    effort_control: Control
    usage: UsageCapability
    efforts: tuple[str, ...] | None = None  # None: not discoverable
    models: tuple[str, ...] | None = None
    provider_budget_cap: bool = False  # provider enforces a per-call spend cap
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "protocol": self.protocol,
            "version": self.version,
            "streaming": self.streaming,
            "session_resume": self.session_resume,
            "session_fork": self.session_fork,
            "session_id_preassign": self.session_id_preassign,
            "cancel": self.cancel,
            "model_control": self.model_control.value,
            "effort_control": self.effort_control.value,
            "usage": self.usage.value,
            "efforts": list(self.efforts) if self.efforts is not None else None,
            "models": list(self.models) if self.models is not None else None,
            "provider_budget_cap": self.provider_budget_cap,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class TurnRequest:
    prompt: str
    cwd: Path
    session_id: str | None = None  # resume this native session
    new_session_id: str | None = None  # pre-assign an id for a new session (when supported)
    fork: bool = False
    model: str | None = None
    effort: str | None = None
    permission_profile: str = "workspace_write"
    timeout_seconds: float = 900.0
    extra_instructions: str | None = None
    mcp_config: dict | None = None
    env: dict[str, str] | None = None
    max_budget_usd: Decimal | None = None  # provider-enforced cap, when supported

    def __post_init__(self) -> None:
        if self.permission_profile not in PERMISSION_PROFILES:
            raise ValidationError(f"permission_profile must be one of {PERMISSION_PROFILES}")
        if self.fork and not self.session_id:
            raise ValidationError("fork requires a session_id to fork from")
        if self.session_id and self.new_session_id:
            raise ValidationError("give either session_id (resume) or new_session_id, not both")
        if self.timeout_seconds <= 0:
            raise ValidationError("timeout_seconds must be positive")


@dataclass(frozen=True)
class SettingsRecord:
    requested: dict
    accepted: dict  # what the adapter actually passed to the provider
    observed: dict  # what the provider reported back; missing keys are unobserved

    def to_dict(self) -> dict:
        return {"requested": dict(self.requested), "accepted": dict(self.accepted), "observed": dict(self.observed)}


@dataclass(frozen=True)
class UsageObservation:
    """One provider-reported number, with its meaning spelled out.

    metric: e.g. cost_usd, tokens.input, tokens.output, tokens.cache_read,
            tokens.reasoning_output, quota.used_percent, context.window.
    scope:  turn | call | session_cumulative | thread_cumulative | window.
    value:  Decimal (cost) or int; never None - an unknown value is simply
            not observed."""

    metric: str
    value: Decimal | int
    scope: str
    source: str
    quality: str = "observed"  # observed | estimated
    unit: str = ""
    key: str = ""  # window/limit identity for quota metrics, model for per-model usage
    observed_at_ms: int | None = None

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "value": str(self.value) if isinstance(self.value, Decimal) else self.value,
            "scope": self.scope,
            "source": self.source,
            "quality": self.quality,
            "unit": self.unit,
            "key": self.key,
            "observed_at_ms": self.observed_at_ms,
        }


@dataclass(frozen=True)
class TurnResult:
    status: str  # completed | failed | interrupted | timeout | cancelled
    text: str
    session_id: str | None
    lineage: str  # new | resumed_same | resumed_new_id | forked | unknown
    settings: SettingsRecord
    usage: tuple[UsageObservation, ...] = ()
    duration_s: float = 0.0
    exit_code: int | None = None
    error: AgentError | None = None
    permission_denials: tuple[dict, ...] = ()
    warnings: tuple[str, ...] = ()
    provider_invocation_id: str | None = None
    events: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "completed" and self.error is None


EventCallback = Callable[[dict], None]


class ProviderAdapter(Protocol):
    name: str

    def capabilities(self) -> ProviderCapabilities:
        ...

    def run_turn(self, request: TurnRequest, *, on_event: EventCallback | None = None, cancel: threading.Event | None = None) -> TurnResult:
        ...


def lineage_for(requested: str | None, fork: bool, observed: str | None) -> str:
    """Describe what actually happened to the native session."""
    if observed is None:
        return "unknown"
    if requested is None:
        return "new"
    if fork:
        return "forked" if observed != requested else "unknown"
    return "resumed_same" if observed == requested else "resumed_new_id"


def decimal_or_none(value: object) -> Decimal | None:
    """Parse a provider number into Decimal; booleans, strings, negatives and
    non-finite values are not usage."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        return None
    return number


def int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass
class EventLog:
    """Bounded record of the most recent provider events for diagnostics."""

    limit: int = 200
    items: list = field(default_factory=list)

    def add(self, event: dict) -> None:
        self.items.append(event)
        if len(self.items) > self.limit:
            del self.items[: len(self.items) - self.limit]
