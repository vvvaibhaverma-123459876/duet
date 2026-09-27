"""Typed domain contracts for the v2 runtime.

Enums fail closed on unknown values. Text and numbers are validated at the
boundary (length limits, finiteness). JSON is canonicalised before hashing so
that hashes are stable across processes and Python versions."""
from __future__ import annotations

import enum
import hashlib
import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TypeVar

MAX_ID = 128
MAX_SHORT = 512
MAX_TEXT = 64 * 1024
MAX_JSON = 256 * 1024
MAX_LIST = 256


# --- Errors ---------------------------------------------------------------------


class DomainError(Exception):
    """Base for structured runtime errors. `code` is stable and machine-readable."""

    code = "domain_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(DomainError):
    code = "validation"


class NotFound(DomainError):
    code = "not_found"


class Unauthorized(DomainError):
    code = "unauthorized"


class Conflict(DomainError):
    """Optimistic state-version mismatch or a competing claim."""

    code = "conflict"


class InvalidTransition(DomainError):
    code = "invalid_transition"


class StaleLease(DomainError):
    code = "stale_lease"


class PolicyDenied(DomainError):
    code = "policy_denied"


class IdempotencyMismatch(DomainError):
    code = "idempotency_mismatch"


class StoreBusy(DomainError):
    code = "busy"


class SchemaError(DomainError):
    code = "schema"


# --- Enums ------------------------------------------------------------------------

E = TypeVar("E", bound=enum.Enum)


class _Str(str, enum.Enum):
    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class RunLifecycle(_Str):
    CREATED = "CREATED"
    PREFLIGHT = "PREFLIGHT"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    REVIEWING = "REVIEWING"
    REPAIRING = "REPAIRING"
    VERIFYING = "VERIFYING"
    COMPLETED_VERIFIED = "COMPLETED_VERIFIED"
    PAUSED_QUOTA = "PAUSED_QUOTA"
    PAUSED_BUDGET = "PAUSED_BUDGET"
    PAUSED_APPROVAL = "PAUSED_APPROVAL"
    PAUSED_CONTEXT = "PAUSED_CONTEXT"
    RECONCILING = "RECONCILING"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Collaboration(_Str):
    PAIR_ACTIVE = "PAIR_ACTIVE"
    PEER_UNAVAILABLE = "PEER_UNAVAILABLE"
    REVIEW_PENDING = "REVIEW_PENDING"
    SOLO_EXPLICIT = "SOLO_EXPLICIT"


class TaskState(_Str):
    PROPOSED = "PROPOSED"
    READY = "READY"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    WAITING_PEER = "WAITING_PEER"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class ActionState(_Str):
    PLANNED = "PLANNED"
    RESERVED = "RESERVED"
    DISPATCHING = "DISPATCHING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    IN_DOUBT = "IN_DOUBT"


class MessageState(_Str):
    QUEUED = "QUEUED"
    TRANSPORT_DELIVERED = "TRANSPORT_DELIVERED"
    PARTICIPANT_ACKNOWLEDGED = "PARTICIPANT_ACKNOWLEDGED"
    HANDLED = "HANDLED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class MessageKind(_Str):
    QUESTION = "QUESTION"
    ANSWER = "ANSWER"
    FINDING = "FINDING"
    PLAN_PROPOSAL = "PLAN_PROPOSAL"
    TASK_PROPOSAL = "TASK_PROPOSAL"
    REVIEW_REQUEST = "REVIEW_REQUEST"
    REVIEW_RESULT = "REVIEW_RESULT"
    BLOCKER = "BLOCKER"
    STATUS = "STATUS"


class Provider(_Str):
    CLAUDE = "claude"
    CODEX = "codex"


class Origin(_Str):
    NATIVE_ORIGINAL = "native_original"
    MANAGED = "managed"
    RESUMED = "resumed"
    EXPLICIT_FORK = "explicit_fork"
    DELEGATED = "delegated"


class Receive(_Str):
    PUSH = "push"
    CHECKPOINT = "checkpoint"
    POLL_ONLY = "poll_only"
    UNSUPPORTED = "unsupported"


class Control(_Str):
    SUPPORTED = "supported"
    CHECKPOINT_ONLY = "checkpoint_only"
    ADVISORY = "advisory"
    UNSUPPORTED = "unsupported"


class UsageCapability(_Str):
    STRUCTURED = "structured"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class Containment(_Str):
    SANDBOX_ENFORCED = "sandbox_enforced"
    COOPERATIVE = "cooperative"
    UNVERIFIED = "unverified"


class NativeIdentity(_Str):
    VERIFIED = "verified"
    CONNECTION_BOUND = "connection_bound"
    UNVERIFIED = "unverified"


class Liveness(_Str):
    CONNECTED = "connected"
    IDLE = "idle"
    UNAVAILABLE = "unavailable"
    GONE = "gone"


class ReservationState(_Str):
    HELD = "HELD"
    RELEASED = "RELEASED"
    RECONCILED = "RECONCILED"


class OutboxState(_Str):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DONE = "DONE"
    IN_DOUBT = "IN_DOUBT"


TERMINAL_RUN = frozenset({RunLifecycle.COMPLETED_VERIFIED, RunLifecycle.FAILED, RunLifecycle.CANCELLED})
TERMINAL_ACTION = frozenset({ActionState.SUCCEEDED, ActionState.FAILED, ActionState.CANCELLED})


def parse_enum(cls: type[E], value: object, field_name: str) -> E:
    if isinstance(value, cls):
        return value
    try:
        return cls(value)  # type: ignore[call-arg]
    except ValueError:
        allowed = ", ".join(m.value for m in cls)  # type: ignore[attr-defined]
        raise ValidationError(f"unknown {field_name} {value!r}; expected one of: {allowed}") from None


# --- Validation helpers -------------------------------------------------------------


def check_text(value: object, field_name: str, *, limit: int = MAX_SHORT, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    if not allow_empty and not value.strip():
        raise ValidationError(f"{field_name} must not be empty")
    if len(value) > limit:
        raise ValidationError(f"{field_name} exceeds {limit} characters", details={"length": len(value)})
    if "\x00" in value:
        raise ValidationError(f"{field_name} must not contain NUL bytes")
    return value


def check_optional_text(value: object, field_name: str, *, limit: int = MAX_SHORT) -> str | None:
    if value is None:
        return None
    return check_text(value, field_name, limit=limit)


def check_int(value: object, field_name: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{field_name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValidationError(f"{field_name} must be <= {maximum}")
    return value


def check_number(value: object, field_name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field_name} must be a number")
    if not math.isfinite(value):
        raise ValidationError(f"{field_name} must be finite")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{field_name} must be >= {minimum}")
    return float(value)


def check_list(value: object, field_name: str, *, limit: int = MAX_LIST, item_limit: int = MAX_SHORT) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{field_name} must be a list")
    if len(value) > limit:
        raise ValidationError(f"{field_name} has more than {limit} items")
    return [check_text(item, f"{field_name}[]", limit=item_limit) for item in value]


def check_json_size(obj: object, field_name: str, limit: int = MAX_JSON) -> object:
    text = canonical_json(obj)
    if len(text) > limit:
        raise ValidationError(f"{field_name} exceeds {limit} bytes of JSON")
    return obj


# --- Canonical JSON, hashes, ids, time ---------------------------------------------


def canonical_json(obj: object) -> str:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except ValueError as exc:  # NaN/Infinity
        raise ValidationError(f"value is not representable as strict JSON: {exc}") from None
    except TypeError as exc:
        raise ValidationError(f"value is not JSON serialisable: {exc}") from None


def content_hash(obj: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc(text: str) -> datetime:
    if not isinstance(text, str) or not text.endswith("Z"):
        raise ValidationError(f"timestamp must be UTC ISO-8601 ending in Z, got {text!r}")
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            raise ValidationError(f"invalid UTC timestamp {text!r}") from None


def utc_after(seconds: float, *, now: str | None = None) -> str:
    base = parse_utc(now) if now else datetime.now(timezone.utc)
    return datetime.fromtimestamp(base.timestamp() + seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- Principals, capabilities, events -----------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Who is acting. `user` and `controller` are only ever constructed
    in-process by Duet itself; a participant principal only comes from
    authenticating a participant token, never from request parameters."""

    kind: str  # user | controller | participant
    id: str
    run_id: str | None = None
    provider: str | None = None

    @property
    def is_participant(self) -> bool:
        return self.kind == "participant"


USER = Principal("user", "user")
CONTROLLER = Principal("controller", "controller")


@dataclass(frozen=True)
class Capabilities:
    receive: Receive = Receive.UNSUPPORTED
    control_model: Control = Control.UNSUPPORTED
    control_effort: Control = Control.UNSUPPORTED
    usage: UsageCapability = UsageCapability.UNKNOWN
    containment: Containment = Containment.UNVERIFIED
    native_identity: NativeIdentity = NativeIdentity.UNVERIFIED

    def to_dict(self) -> dict:
        return {k: getattr(self, k).value for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, data: dict | None) -> "Capabilities":
        data = data or {}
        if not isinstance(data, dict):
            raise ValidationError("capabilities must be an object")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValidationError(f"unknown capability fields: {sorted(unknown)}")
        types = {
            "receive": Receive,
            "control_model": Control,
            "control_effort": Control,
            "usage": UsageCapability,
            "containment": Containment,
            "native_identity": NativeIdentity,
        }
        return cls(**{k: parse_enum(types[k], v, k) for k, v in data.items()})


@dataclass(frozen=True)
class Event:
    type: str
    payload: dict
    actor: str
    at: str
    run_id: str | None = None
    event_id: str = field(default_factory=lambda: new_id("evt"))

    def to_row(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "type": self.type,
            "actor": self.actor,
            "at": self.at,
            "payload_json": canonical_json(self.payload),
        }
