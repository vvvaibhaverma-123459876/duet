"""Claude Code status-line telemetry (D07).

Claude Code runs the configured `statusLine.command` in a shell, writes a
JSON object describing the session to its stdin and shows whatever it
prints. The field list below follows the official documentation,
https://code.claude.com/docs/en/statusline ("Available data" and "Full JSON
schema", retrieved 2026-09-27; the documented example is recorded in
tests/fixtures/telemetry/claude/statusline-docs-full-schema.json).

What the fields mean, per that page:
- `cost.total_cost_usd` is an estimate computed client-side, for the whole
  session; it restarts at $0 when `/clear` starts a new session (new
  `session_id`). It is a session-cumulative *estimated* cost.
- `context_window.*` describes the context window after the most recent API
  response: occupancy, not consumption. It falls after `/compact`.
- `rate_limits.five_hour|seven_day|spend_limit` carry `used_percentage` and
  `resets_at` (epoch seconds). Each may be absent: only some plans report
  them, only after the first response, and a window is dropped once it
  resets. There is no account identity in the payload.
- `session_id` is stable for the session and unique per session; the docs
  recommend it over process ids for per-session state.

Only the allowlisted fields are read. Paths (cwd, workspace, transcript,
worktree), names, prompt ids, PR links and anything unknown are never
copied, and the transcript file is never opened. Parsing never changes what
the user's own status-line command prints: `run_status_line` passes the
original stdin through unchanged and returns the command's output
byte-for-byte. Installing the wrapper is a later milestone."""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Mapping, Sequence

from ..usage.observations import (
    STATUSLINE_SOURCE,
    Observation,
    Quality,
    Scope,
    event_digest,
    parse_decimal,
    parse_int,
)

DOCS_URL = "https://code.claude.com/docs/en/statusline"
DOCS_RETRIEVED = "2026-09-27"
MAX_PAYLOAD_BYTES = 1024 * 1024
RATE_LIMIT_WINDOWS: Mapping[str, int | None] = {"five_hour": 300, "seven_day": 7 * 24 * 60, "spend_limit": None}
ALLOWED_FIELDS = (
    "session_id",
    "version",
    "model.id",
    "model.display_name",
    "cost.total_cost_usd",
    "cost.total_duration_ms",
    "cost.total_api_duration_ms",
    "context_window.total_input_tokens",
    "context_window.total_output_tokens",
    "context_window.context_window_size",
    "context_window.used_percentage",
    *(f"rate_limits.{name}.{field}" for name in RATE_LIMIT_WINDOWS for field in ("used_percentage", "resets_at")),
)
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LABEL = re.compile(r"^[\w .:/()+-]{1,128}$")


class StatusLineError(ValueError):
    """The payload cannot be attributed to a session (invalid JSON, not an
    object, too large, or no usable session_id)."""


@dataclass(frozen=True)
class RateLimitReading:
    name: str  # five_hour | seven_day | spend_limit
    used_percent: Decimal
    resets_at_s: int | None
    window_minutes: int | None


@dataclass(frozen=True)
class StatusLineSnapshot:
    """The allowlisted content of one status-line payload. Missing, null or
    invalid fields are None: never zero."""

    session_id: str
    claude_code_version: str | None = None
    model_id: str | None = None
    model_display_name: str | None = None
    cost_usd: Decimal | None = None
    total_duration_ms: int | None = None
    total_api_duration_ms: int | None = None
    context_input_tokens: int | None = None
    context_output_tokens: int | None = None
    context_window_size: int | None = None
    context_used_percent: Decimal | None = None
    rate_limits: tuple[RateLimitReading, ...] = ()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "claude_code_version": self.claude_code_version,
            "model_id": self.model_id,
            "model_display_name": self.model_display_name,
            "cost_usd": _text(self.cost_usd),
            "total_duration_ms": self.total_duration_ms,
            "total_api_duration_ms": self.total_api_duration_ms,
            "context_input_tokens": self.context_input_tokens,
            "context_output_tokens": self.context_output_tokens,
            "context_window_size": self.context_window_size,
            "context_used_percent": _text(self.context_used_percent),
            "rate_limits": [
                {"name": r.name, "used_percent": str(r.used_percent), "resets_at_s": r.resets_at_s, "window_minutes": r.window_minutes}
                for r in self.rate_limits
            ],
        }


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _get(payload: Mapping, path: str) -> object:
    node: object = payload
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


def _label(value: object) -> str | None:
    return value if isinstance(value, str) and _LABEL.match(value) else None


def _count(value: object) -> int | None:
    """Integer fields; an integral Decimal (e.g. `45000.0`) is accepted."""
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value() and value >= 0:
        return int(value)
    return parse_int(value)


def _load(payload: str | bytes | Mapping) -> Mapping:
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise StatusLineError("status-line payload too large")
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StatusLineError("status-line payload is not UTF-8") from exc
    if not isinstance(payload, str):
        raise StatusLineError("status-line payload must be JSON text or a mapping")
    if len(payload.encode("utf-8", "ignore")) > MAX_PAYLOAD_BYTES:
        raise StatusLineError("status-line payload too large")
    try:
        # parse_float=Decimal: money and percentages never pass through float.
        data = json.loads(payload, parse_float=Decimal)
    except (ValueError, RecursionError) as exc:
        raise StatusLineError(f"status-line payload is not JSON: {exc}") from None
    if not isinstance(data, Mapping):
        raise StatusLineError("status-line payload is not a JSON object")
    return data


def parse_status_line(payload: str | bytes | Mapping) -> StatusLineSnapshot:
    """Read only the allowlisted fields. Everything telemetry depends on is
    keyed by `session_id`; without a usable one nothing is attributed (a
    process id is never a substitute)."""
    data = _load(payload)
    session = _get(data, "session_id")
    if not isinstance(session, str) or not _SESSION_ID.match(session):
        raise StatusLineError("status-line payload has no usable session_id")
    limits = []
    for name, minutes in RATE_LIMIT_WINDOWS.items():
        used = parse_decimal(_get(data, f"rate_limits.{name}.used_percentage"))
        if used is not None:
            limits.append(RateLimitReading(name, used, _count(_get(data, f"rate_limits.{name}.resets_at")), minutes))
    return StatusLineSnapshot(
        session_id=session,
        claude_code_version=_label(_get(data, "version")),
        model_id=_label(_get(data, "model.id")),
        model_display_name=_label(_get(data, "model.display_name")),
        cost_usd=parse_decimal(_get(data, "cost.total_cost_usd")),
        total_duration_ms=_count(_get(data, "cost.total_duration_ms")),
        total_api_duration_ms=_count(_get(data, "cost.total_api_duration_ms")),
        context_input_tokens=_count(_get(data, "context_window.total_input_tokens")),
        context_output_tokens=_count(_get(data, "context_window.total_output_tokens")),
        context_window_size=_count(_get(data, "context_window.context_window_size")),
        context_used_percent=parse_decimal(_get(data, "context_window.used_percentage")),
        rate_limits=tuple(limits),
    )


def status_line_observations(
    snapshot: StatusLineSnapshot,
    *,
    observed_at_ms: int,
    pool_id: str | None = None,
    run_id: str | None = None,
) -> tuple[Observation, ...]:
    """Observations for one snapshot, keyed by its session id:
    - cost: session-cumulative, ESTIMATED; the first value seen for a session
      has an unknown baseline (the session may predate the wrapper);
    - session and API durations: session-cumulative;
    - context figures: point-in-time gauges, never consumption;
    - rate-limit windows: account capacity gauges. The payload names no
      account, so the pool is unknown unless the caller supplies it."""
    session = snapshot.session_id
    event = f"{STATUSLINE_SOURCE}:{session}:{observed_at_ms}:{event_digest(snapshot.to_dict())}"
    common = dict(provider="claude", session_id=session, source=STATUSLINE_SOURCE, source_event_id=event,
                  observed_at_ms=observed_at_ms, pool_id=pool_id, run_id=run_id)
    out: list[Observation] = []
    if snapshot.cost_usd is not None:
        out.append(Observation(metric="cost.estimated_usd", value=snapshot.cost_usd, unit="USD", scope=Scope.SESSION_CUMULATIVE,
                               quality=Quality.ESTIMATED, **common))
    for metric, value in (("time.session_ms", snapshot.total_duration_ms), ("time.api_ms", snapshot.total_api_duration_ms)):
        if value is not None:
            out.append(Observation(metric=metric, value=value, unit="ms", scope=Scope.SESSION_CUMULATIVE, **common))
    for metric, value, unit in (
        ("context.input_tokens", snapshot.context_input_tokens, "tokens"),
        ("context.output_tokens", snapshot.context_output_tokens, "tokens"),
        ("context.window_size", snapshot.context_window_size, "tokens"),
        ("context.used_percent", snapshot.context_used_percent, "percent"),
    ):
        if value is not None:
            out.append(Observation(metric=metric, value=value, unit=unit, scope=Scope.SNAPSHOT, key=snapshot.model_id or "", **common))
    for limit in snapshot.rate_limits:
        out.append(Observation(
            metric="quota.used_percent", value=limit.used_percent, unit="percent", scope=Scope.WINDOW, key=limit.name,
            window_minutes=limit.window_minutes, resets_at_ms=limit.resets_at_s * 1000 if limit.resets_at_s is not None else None,
            **common,
        ))
    return tuple(out)


def observe_status_line(
    payload: str | bytes | Mapping,
    *,
    observed_at_ms: int,
    pool_id: str | None = None,
    run_id: str | None = None,
) -> tuple[Observation, ...]:
    return status_line_observations(parse_status_line(payload), observed_at_ms=observed_at_ms, pool_id=pool_id, run_id=run_id)


@dataclass(frozen=True)
class WrappedStatusLine:
    stdout: bytes  # the original command's output, unchanged
    stderr: bytes
    returncode: int
    observations: tuple[Observation, ...]
    telemetry_error: str | None  # why nothing was observed; never shown to the user


Sink = Callable[[tuple[Observation, ...]], None]


def run_status_line(
    original_command: str | Sequence[str] | None,
    stdin_data: bytes | str,
    *,
    sink: Sink | None = None,
    observed_at_ms: int | None = None,
    pool_id: str | None = None,
    run_id: str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    timeout_s: float | None = None,
) -> WrappedStatusLine:
    """Run the user's original status-line command with the untouched
    stdin, then observe the payload.

    A string command runs through the shell (`/bin/sh -c`), as a
    `statusLine.command` does; a sequence runs as argv. The environment is
    inherited unless `env` is given, so `COLUMNS`/`LINES` reach the command.
    No command (the user had none) prints nothing. Telemetry failures,
    including a failing `sink`, are recorded in `telemetry_error` and never
    alter the output or exit status."""
    raw = stdin_data.encode("utf-8") if isinstance(stdin_data, str) else bytes(stdin_data)
    stdout, stderr, returncode = b"", b"", 0
    if original_command:
        try:
            proc = subprocess.run(
                original_command, input=raw, capture_output=True, shell=isinstance(original_command, str),
                env=dict(env) if env is not None else None, cwd=cwd, timeout=timeout_s,
            )
            stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, stderr, returncode = exc.stdout or b"", exc.stderr or b"", 124
        except OSError as exc:
            stderr, returncode = f"status line: {exc}\n".encode(), 127
    observations: tuple[Observation, ...] = ()
    error = None
    try:
        at = observed_at_ms if observed_at_ms is not None else int(time.time() * 1000)
        observations = observe_status_line(raw, observed_at_ms=at, pool_id=pool_id, run_id=run_id)
        if sink is not None and observations:
            sink(observations)
    except Exception as exc:  # noqa: BLE001 - telemetry must never break the user's status line
        error = f"{type(exc).__name__}: {exc}"
    return WrappedStatusLine(stdout, stderr, returncode, observations, error)


__all__ = [
    "ALLOWED_FIELDS", "DOCS_URL", "RateLimitReading", "StatusLineError", "StatusLineSnapshot", "WrappedStatusLine",
    "observe_status_line", "parse_status_line", "run_status_line", "status_line_observations",
]
