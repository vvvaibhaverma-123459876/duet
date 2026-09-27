"""Local usage pools, allowances and deduplicated usage records (D07).

A pool is a user-defined allowance for one provider metric (for example "at
most 40 Codex turns per 5 hours" or "at most $5.00 of Claude estimated cost
per day"). Reservations made by `Runtime.plan_action` draw on it, and the
capacity check runs inside that write transaction. SQLite's write lock
serialises it across processes, so two runs cannot both take the last of an
allowance.

What a pool is not: a view of the provider's own quota. Provider quota
windows are observed gauges (see `duet.usage`); a local reservation cannot
lock provider-side capacity, and usage outside DUET is not visible here. The
`enforcement` label says what a pool can promise:
- local_bound: DUET refuses work that would exceed it (for work DUET schedules);
- best_effort: tracked and reported, but reservations are not refused;
- provider_cap: a limit the provider itself enforces (DUET only mirrors it).

Consumption in a pool = usage records inside the window (known quantities)
+ reservations still HELD. Records carry their quality; a record whose
quantity is unknown is counted separately and makes the pool uncertain,
never zero."""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from .contracts import (
    MAX_ID,
    Conflict,
    PolicyDenied,
    Principal,
    Unauthorized,
    ValidationError,
    check_int,
    check_optional_text,
    check_text,
    parse_utc,
    utc_now,
)
from .store import Tx

ENFORCEMENT = ("local_bound", "best_effort", "provider_cap")
QUALITIES = ("observed", "estimated", "unknown")


def as_quantity(value: object, field_name: str = "quantity") -> Decimal:
    """Quantities are exact: ints or decimal strings, never floats (money
    accumulation must not drift), never negative."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValidationError(f"{field_name} must be an integer or a decimal string, not {type(value).__name__}")
    try:
        number = Decimal(str(value)) if isinstance(value, (int, str, Decimal)) else None
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite():
        raise ValidationError(f"{field_name} must be a finite number")
    if number < 0:
        raise ValidationError(f"{field_name} must not be negative")
    return number


def _dec(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(json.loads(text))


def pool_usage(tx: Tx, pool: dict, now: str | None = None) -> dict:
    """Consumption of `pool` as seen inside the current transaction."""
    now = now or utc_now()
    since = None
    if pool["window_seconds"]:
        since = (parse_utc(now) - timedelta(seconds=pool["window_seconds"])).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    query = "SELECT quantity_json, quality FROM usage_records WHERE pool_id = ?" + (" AND observed_at > ?" if since else "")
    rows = tx.query(query, (pool["pool_id"], since) if since else (pool["pool_id"],))
    used = Decimal(0)
    unknown = 0
    for row in rows:
        quantity = json.loads(row["quantity_json"])
        if quantity is None or row["quality"] == "unknown":
            unknown += 1
        else:
            used += Decimal(quantity)
    held = Decimal(0)
    finishing = Decimal(0)
    # A HELD reservation whose action already has a usage record in this pool
    # is superseded by that record: counting both would take the same turn
    # twice between recording usage and settling the action, or forever
    # after a crash in between.
    for row in tx.query(
        "SELECT r.quantity_json, r.action_id FROM reservations r WHERE r.pool = ? AND r.state = 'HELD'"
        " AND NOT EXISTS (SELECT 1 FROM usage_records u WHERE u.action_id = r.action_id AND u.pool_id = r.pool)",
        (pool["pool_id"],),
    ):
        quantity = as_quantity(json.loads(row["quantity_json"]), "reservation quantity")
        held += quantity
        if row["action_id"] is None:
            finishing += quantity
    # Overshoot: finished actions that used more than they reserved (D08).
    # Reported, never hidden; a local reservation cannot stop a running turn.
    overshoot = Decimal(0)
    # A turn admitted without a size estimate reserved nothing to overshoot.
    for row in tx.query(
        "SELECT quantity_json, actual_json FROM reservations WHERE pool = ? AND state = 'RECONCILED' AND actual_json IS NOT NULL"
        " AND category != 'provider_turn:unknown_size'",
        (pool["pool_id"],),
    ):
        actual = json.loads(row["actual_json"])
        spent = actual.get("quantity") if isinstance(actual, dict) else None
        if spent is not None:
            overshoot += max(Decimal(str(spent)) - as_quantity(json.loads(row["quantity_json"]), "reservation quantity"), Decimal(0))
    allowance = _dec(pool["allowance_json"])
    available = None if allowance is None else allowance - used - held
    return {
        "pool_id": pool["pool_id"], "provider": pool["provider"], "metric": pool["metric"], "unit": pool["unit"],
        "enforcement": pool["enforcement"], "window_seconds": pool["window_seconds"],
        "allowance": None if allowance is None else str(allowance), "used": str(used), "held": str(held),
        "finishing_held": str(finishing),
        "available": None if available is None else str(available),
        "unknown_records": unknown,
        "overshoot": str(overshoot),
        # With unknown records the true consumption is higher than `used`:
        # `available` is then an upper bound, not a promise.
        "uncertain": unknown > 0,
    }


def check_reservation(tx: Tx, pool_id: str, quantity: object, now: str | None = None, *, provider: str | None = None, metric: str | None = None) -> None:
    """Called inside plan_action's transaction for each reservation. A pool
    that does not exist is not a limit (nothing was authorised or bounded);
    a local_bound pool refuses a reservation it cannot cover. The
    reservation's provider and metric must be the pool's own: capacity of one
    provider never funds another's work, and units never mix (D08)."""
    pool = tx.get("usage_pools", pool_id)
    if pool is None:
        return
    if provider is not None and provider != pool["provider"]:
        raise PolicyDenied(f"pool {pool_id} holds {pool['provider']} capacity; it cannot fund {provider} work", details={"pool": pool_id, "provider": provider})
    if metric is not None and metric != pool["metric"]:
        raise PolicyDenied(f"pool {pool_id} counts {pool['metric']}, not {metric}", details={"pool": pool_id, "metric": metric})
    requested = as_quantity(quantity, "reservation quantity")
    if pool["enforcement"] != "local_bound" or pool["allowance_json"] is None:
        return
    usage = pool_usage(tx, pool, now)
    available = Decimal(usage["available"])
    # A zero reservation (a turn of unknown size) still needs some room left:
    # an exhausted pool, or a zero allowance, refuses it.
    if requested > available or (requested == 0 and available <= 0):
        raise PolicyDenied(
            f"pool {pool_id} cannot cover {requested} {pool['unit']}: {usage['used']} used"
            f"{' in the window' if pool['window_seconds'] else ''}, {usage['held']} reserved, allowance {usage['allowance']}",
            details={"pool": pool_id, "requested": str(requested), **{k: usage[k] for k in ("allowance", "used", "held", "available")}},
        )


class PoolStore:
    def __init__(self, runtime) -> None:
        self.runtime = runtime

    def define_pool(
        self,
        principal: Principal,
        pool_id: str,
        *,
        provider: str,
        metric: str,
        unit: str,
        allowance: object = None,
        window_seconds: int | None = None,
        enforcement: str = "local_bound",
    ) -> dict:
        """Only the user authorises spending: agents and the controller cannot
        create or widen an allowance (R13)."""
        if principal.kind != "user":
            raise Unauthorized("only the user can define or change a usage pool")
        check_text(pool_id, "pool_id", limit=MAX_ID)
        check_text(provider, "provider", limit=64)
        check_text(metric, "metric", limit=64)
        check_text(unit, "unit", limit=32)
        if enforcement not in ENFORCEMENT:
            raise ValidationError(f"enforcement must be one of {ENFORCEMENT}")
        if window_seconds is not None:
            check_int(window_seconds, "window_seconds", minimum=1, maximum=366 * 24 * 3600)
        amount = None if allowance is None else str(as_quantity(allowance, "allowance"))
        if amount is not None and unit == "turns" and Decimal(amount) != Decimal(amount).to_integral_value():
            raise ValidationError("a turns allowance must be a whole number")
        with self.runtime.store.transaction() as tx:
            tx.emit(
                self.runtime._event(
                    "pool.defined",
                    {
                        "pool_id": pool_id, "provider": provider, "metric": metric, "unit": unit, "allowance": amount,
                        "window_seconds": window_seconds, "enforcement": enforcement, "defined_by": principal.id,
                    },
                    principal,
                    None,
                )
            )
            return pool_usage(tx, tx.require("usage_pools", pool_id))

    def record_usage(
        self,
        principal: Principal,
        *,
        record_id: str,
        pool_id: str,
        metric: str,
        quantity: object,
        quality: str,
        source: str,
        observed_at: str | None = None,
        run_id: str | None = None,
        action_id: str | None = None,
        participant_id: str | None = None,
    ) -> tuple[dict, bool]:
        """Record consumed usage once. The same record_id again is a no-op
        (replays, reconnects, delayed duplicates); the same id with a
        different quantity is a conflict, never silently overwritten.
        Returns (record, created)."""
        if principal.kind != "controller":
            raise Unauthorized("only the controller records usage")
        check_text(record_id, "record_id", limit=256)
        check_text(source, "source", limit=128)
        check_text(metric, "metric", limit=64)
        if quality not in QUALITIES:
            raise ValidationError(f"quality must be one of {QUALITIES}")
        amount = None if quantity is None else str(as_quantity(quantity))
        if (amount is None) != (quality == "unknown"):
            raise ValidationError("a quantity is unknown exactly when quality is 'unknown'")
        for value, name in ((run_id, "run_id"), (action_id, "action_id"), (participant_id, "participant_id")):
            check_optional_text(value, name, limit=MAX_ID)
        observed_at = observed_at or utc_now()
        parse_utc(observed_at)
        with self.runtime.store.transaction() as tx:
            tx.require("usage_pools", pool_id)
            existing = tx.get("usage_records", record_id)
            if existing is not None:
                if json.loads(existing["quantity_json"]) != amount or existing["pool_id"] != pool_id:
                    raise Conflict(f"usage record {record_id} was already recorded with a different quantity", details={"stored": json.loads(existing["quantity_json"]), "new": amount})
                return dict(existing), False
            tx.emit(
                self.runtime._event(
                    "usage.recorded",
                    {
                        "record_id": record_id, "pool_id": pool_id, "run_id": run_id, "action_id": action_id,
                        "participant_id": participant_id, "metric": metric, "quantity": amount, "quality": quality,
                        "source": source, "observed_at": observed_at,
                    },
                    principal,
                    run_id,
                )
            )
            return dict(tx.require("usage_records", record_id)), True

    def status(self, pool_id: str | None = None) -> list[dict]:
        tx = self.runtime.store.read()
        rows = tx.query("SELECT * FROM usage_pools" + (" WHERE pool_id = ?" if pool_id else "") + " ORDER BY pool_id", (pool_id,) if pool_id else ())
        return [pool_usage(tx, dict(row)) for row in rows]

    def pools_for(self, provider: str, metric: str) -> list[dict]:
        rows = self.runtime.store.read().query("SELECT * FROM usage_pools WHERE provider = ? AND metric = ? ORDER BY pool_id", (provider, metric))
        return [dict(row) for row in rows]

    def run_usage(self, run_id: str) -> list[dict]:
        rows = self.runtime.store.read().query("SELECT * FROM usage_records WHERE run_id = ? ORDER BY observed_at, rowid", (run_id,))
        return [{**dict(row), "quantity": json.loads(row["quantity_json"])} for row in rows]
