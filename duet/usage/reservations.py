"""Admission in the runtime (D08): finishing reserves, admission decisions,
provider quota gauges and holds, all inside store transactions.

`ReservationBook.admit()` gathers the pools, this run's finishing reserves,
the provider's quota gauges and hold, and the estimates, then asks the pure
`admission.decide()` and applies the answer in the same write transaction:
an admitted action is planned with its reservations (lines drawn from a
finishing reserve move capacity instead of holding it twice), a refusal is
recorded, a quota pause places a provider hold. SQLite's write lock
serialises all of it across processes, like the D07 allowance check.

Scope and limits:
- Pools, reserves and holds bound only the work DUET schedules (managed
  peers). A native session's own turns are not DUET's to admit.
- Provider quota is keyed by provider: one authenticated login per provider
  CLI on this machine. Usage outside DUET shows up only as a changed gauge.
- A reservation cannot stop a running turn: a turn can exceed its estimate.
  The overshoot is recorded and reported (`pool_usage(...)["overshoot"]`).
- Nothing here can pick another provider, account or billing mode."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from decimal import Decimal

from ..runtime.contracts import CONTROLLER, ReservationState, new_id, parse_utc
from ..runtime.pools import pool_usage
from .admission import (
    ActionRequest,
    AdmissionPolicy,
    Gauge,
    Hold,
    PoolView,
    ReserveView,
    backoff_ms,
    decide,
    plan_finishing,
)
from .estimation import TURNS, Estimate, estimate_turn

UNKNOWN_SIZE = "provider_turn:unknown_size"


def to_ms(text: str | None) -> int | None:
    return None if text is None else int(parse_utc(text).timestamp() * 1000)


def from_ms(ms: int | None) -> str | None:
    if ms is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_RESETS = re.compile(r":resets=(\d+|\?)$")


def gauge_from_observation(observation, now_ms: int) -> dict | None:
    """A provider `quota.used_percent` observation as a gauge reading. Codex
    keys look like `codex:primary:300m:resets=1790500000` (seconds)."""
    if getattr(observation, "metric", None) != "quota.used_percent":
        return None
    key = observation.key or "default"
    match = _RESETS.search(key)
    resets_ms = None
    if match:
        key = key[: match.start()]
        if match.group(1) != "?":
            value = int(match.group(1))
            resets_ms = value * 1000 if value < 10**11 else value  # seconds or already milliseconds
    return {"window": key, "used_percent": Decimal(str(observation.value)), "resets_at_ms": resets_ms,
            "observed_at_ms": observation.observed_at_ms or now_ms, "source": observation.source}


class ReservationBook:
    def __init__(self, runtime, policy: AdmissionPolicy | None = None) -> None:
        self.runtime = runtime
        self.policy = policy or AdmissionPolicy()

    # -- clock -----------------------------------------------------------------------------

    def now_ms(self) -> int:
        return to_ms(self.runtime.clock())

    # -- reads -----------------------------------------------------------------------------

    @staticmethod
    def _pool_view(usage: dict) -> PoolView:
        return PoolView(
            pool_id=usage["pool_id"], provider=usage["provider"], metric=usage["metric"], unit=usage["unit"],
            allowance=None if usage["allowance"] is None else Decimal(usage["allowance"]), used=Decimal(usage["used"]),
            held=Decimal(usage["held"]), enforcement=usage["enforcement"], unknown_records=usage["unknown_records"],
        )

    def _pools(self, tx, provider: str | None = None) -> list[PoolView]:
        now = self.runtime.clock()
        rows = tx.query("SELECT * FROM usage_pools" + (" WHERE provider = ?" if provider else "") + " ORDER BY pool_id", (provider,) if provider else ())
        return [self._pool_view(pool_usage(tx, dict(row), now)) for row in rows]

    @staticmethod
    def _history(tx, provider: str, metric: str) -> list[Decimal]:
        """Known per-turn quantities for (provider, metric), one per action,
        oldest first."""
        rows = tx.query(
            "SELECT r.action_id AS action_id, MAX(r.quantity_json) AS quantity_json, MAX(r.observed_at) AS at"
            " FROM usage_records r JOIN usage_pools p ON p.pool_id = r.pool_id"
            " WHERE p.provider = ? AND r.metric = ? AND r.quality != 'unknown' AND r.action_id IS NOT NULL"
            " GROUP BY r.action_id ORDER BY at DESC LIMIT 20",
            (provider, metric),
        )
        return [Decimal(json.loads(row["quantity_json"])) for row in reversed(rows) if json.loads(row["quantity_json"]) is not None]

    def estimates(self, tx, provider: str, metrics) -> dict:
        return {metric: estimate_turn(metric, [] if metric == TURNS else self._history(tx, provider, metric)) for metric in set(metrics)}

    @staticmethod
    def _reserves(tx, run_id: str, provider: str | None = None) -> list[dict]:
        rows = tx.query(
            "SELECT f.*, r.quantity_json AS quantity_json, r.state AS state FROM finishing_reserves f"
            " JOIN reservations r ON r.reservation_id = f.reservation_id WHERE f.run_id = ?" + (" AND f.provider = ?" if provider else "")
            + " ORDER BY f.created_at, f.reservation_id",
            (run_id, provider) if provider else (run_id,),
        )
        return [dict(row) for row in rows]

    @staticmethod
    def _gauges(tx, provider: str) -> list[Gauge]:
        rows = tx.query("SELECT * FROM quota_gauges WHERE provider = ? ORDER BY gauge_id", (provider,))
        return [Gauge(row["provider"], row["window"], Decimal(json.loads(row["used_percent_json"])), to_ms(row["resets_at"]), to_ms(row["observed_at"])) for row in rows]

    @staticmethod
    def _hold(tx, provider: str) -> Hold | None:
        row = tx.get("quota_holds", provider)
        if row is None or row["state"] == "RELEASED":
            return None
        return Hold(row["provider"], row["state"], row["reason"], to_ms(row["resume_at"]), to_ms(row["placed_at"]), row["attempts"])

    def hold(self, provider: str) -> Hold | None:
        return self._hold(self.runtime.store.read(), provider)

    # -- finishing reserves --------------------------------------------------------------

    def ensure_finishing(self, run_id: str, *, writer: str | None, reviewer: str | None) -> list[dict]:
        """Reserve the run's finishing capacity once: review turns on the
        reviewer's provider, repair turns on the writer's (None for a
        participant DUET does not schedule). Idempotent."""
        with self.runtime.store.transaction() as tx:
            self._ensure_finishing(tx, run_id, writer=writer, reviewer=reviewer)
            return self._reserves(tx, run_id)

    def _ensure_finishing(self, tx, run_id: str, *, writer: str | None, reviewer: str | None) -> None:
        existing = {(r["purpose"], r["pool"]) for r in self._reserves(tx, run_id)}
        pools = self._pools(tx)
        estimates = {}
        for provider in {p for p in (writer, reviewer) if p}:
            for metric, estimate in self.estimates(tx, provider, [p.metric for p in pools if p.provider == provider]).items():
                estimates[(provider, metric)] = estimate
        by_id = {p.pool_id: p for p in pools}
        for line in plan_finishing(writer_provider=writer, reviewer_provider=reviewer, pools=pools, estimates=estimates, policy=self.policy):
            if (line.purpose, line.pool_id) in existing:
                continue
            pool = by_id[line.pool_id]
            free = max(pool.allowance - pool.used - pool.held, Decimal(0))
            take = min(line.quantity, free)
            shortfall = line.quantity - take
            tx.emit(self.runtime._event(
                "finishing.reserved",
                {
                    "reservation_id": new_id("rsv"), "run_id": run_id, "purpose": line.purpose, "provider": line.provider,
                    "pool": line.pool_id, "metric": line.metric, "units": line.units,
                    "per_unit": None if line.per_unit is None else str(line.per_unit), "quantity": str(take),
                    "shortfall": str(shortfall) if shortfall > 0 else None,
                },
                CONTROLLER, run_id,
            ))
            # The pool's held grows by `take` for the next line in this transaction.
            by_id[line.pool_id] = replace(pool, held=pool.held + take)

    def resize_finishing(self, run_id: str, provider: str) -> None:
        """Re-estimate after new usage: each unfinished reserve of this
        provider holds units_left x the current high estimate, growing only
        into free capacity (the rest is a recorded shortfall)."""
        with self.runtime.store.transaction() as tx:
            reserves = [r for r in self._reserves(tx, run_id, provider) if r["state"] == ReservationState.HELD.value and r["metric"] != TURNS]
            if not reserves:
                return
            pools = {p.pool_id: p for p in self._pools(tx, provider)}
            estimates = self.estimates(tx, provider, {r["metric"] for r in reserves})
            for reserve in reserves:
                estimate: Estimate = estimates[reserve["metric"]]
                pool = pools.get(reserve["pool"])
                if estimate.high is None or pool is None or pool.allowance is None:
                    continue
                current = Decimal(json.loads(reserve["quantity_json"]))
                want = estimate.high * reserve["units_left"]
                if want == current and reserve["per_unit_json"] == json.dumps(str(estimate.high)):
                    continue
                free = max(pool.allowance - pool.used - pool.held, Decimal(0))
                target = want if want <= current else min(want, current + free)
                shortfall = want - target
                tx.emit(self.runtime._event(
                    "finishing.resized",
                    {"reservation_id": reserve["reservation_id"], "per_unit": str(estimate.high), "quantity": str(target),
                     "shortfall": str(shortfall) if shortfall > 0 else None},
                    CONTROLLER, run_id,
                ))
                pools[pool.pool_id] = replace(pool, held=pool.held + (target - current))

    # -- admission -------------------------------------------------------------------------

    def admit(
        self,
        *,
        run_id: str,
        provider: str,
        action_class: str,
        purpose: str,
        input: dict,
        participant_id: str | None = None,
        per_call_budget_cap: bool = False,
        finishing: dict | None = None,
        type: str = "provider_turn",
    ) -> dict:
        """Decide, and when admitted plan the action with its reservations,
        in one transaction. Returns {decision, admission_id, action,
        outbox_id}; action is None unless admitted. `finishing` names the
        parties ({"writer": provider|None, "reviewer": provider|None}) so the
        run's finishing reserve exists before its first admitted action."""
        now_ms = self.now_ms()
        with self.runtime.store.transaction() as tx:
            self.runtime._live_run(tx, run_id)
            if finishing is not None:
                self._ensure_finishing(tx, run_id, writer=finishing.get("writer"), reviewer=finishing.get("reviewer"))
            pools = self._pools(tx, provider)
            reserves = [
                ReserveView(r["reservation_id"], r["purpose"], r["pool"], Decimal(json.loads(r["quantity_json"])), r["units_left"])
                for r in self._reserves(tx, run_id, provider) if r["state"] == ReservationState.HELD.value
            ]
            estimates = self.estimates(tx, provider, [TURNS, *(p.metric for p in pools)])
            optional = tx.scalar(
                "SELECT COUNT(*) FROM admissions WHERE run_id = ? AND provider = ? AND action_class = 'optional' AND verdict = 'admit'", (run_id, provider)
            ) or 0
            in_flight = {
                row["pool"]: row["n"]
                for row in tx.query(
                    "SELECT r.pool AS pool, COUNT(*) AS n FROM reservations r WHERE r.category = ? AND r.state = 'HELD'"
                    " AND NOT EXISTS (SELECT 1 FROM usage_records u WHERE u.action_id = r.action_id AND u.pool_id = r.pool) GROUP BY r.pool",
                    (UNKNOWN_SIZE,),
                )
            }
            hold = self._hold(tx, provider)
            request = ActionRequest(provider, action_class, purpose, estimates, per_call_budget_cap, optional, in_flight)
            decision = decide(request, pools, reserves, self._gauges(tx, provider), hold, now_ms=now_ms, policy=self.policy)
            planned = None
            if decision.admitted:
                lines = [
                    {"provider": provider, "pool": line.pool_id, "metric": line.metric, "quantity": str(line.quantity),
                     "category": UNKNOWN_SIZE if line.unknown_size else ("finishing_draw" if line.draw_from else "provider_turn"),
                     "draw_from": line.draw_from}
                    for line in decision.lines
                ]
                planned = self.runtime._plan_in_tx(
                    tx, CONTROLLER, run_id=run_id, type=type, input=input, task_id=None, participant_id=participant_id,
                    reservations=self.runtime._clean_reservations(lines), reserve=True,
                )
                if decision.hold == "probe":
                    tx.emit(self.runtime._event("quota.probe", {"provider": provider, "action_id": planned["action"]["action_id"]}, CONTROLLER, run_id))
                elif decision.hold == "release":
                    tx.emit(self.runtime._event("quota.released", {"provider": provider, "reason": "a fresh quota reading is below the stop threshold"}, CONTROLLER, run_id))
            elif decision.hold == "place":
                attempts = hold.attempts + 1 if hold is not None else 0
                tx.emit(self.runtime._event("quota.hold", {"provider": provider, "reason": decision.reason, "resume_at": from_ms(decision.resume_at_ms), "attempts": attempts}, CONTROLLER, run_id))
            admission_id = new_id("adm")
            detail = decision.to_dict()
            detail["estimates"] = {m: e.to_dict() for m, e in estimates.items()}
            tx.emit(self.runtime._event(
                "admission.decided",
                {
                    "admission_id": admission_id, "run_id": run_id, "participant_id": participant_id,
                    "action_id": planned["action"]["action_id"] if planned else None, "provider": provider,
                    "action_class": action_class, "purpose": purpose, "verdict": decision.verdict, "reason": decision.reason[:2000],
                    "detail": detail,
                },
                CONTROLLER, run_id,
            ))
            return {"decision": decision, "admission_id": admission_id, "action": planned["action"] if planned else None,
                    "outbox_id": planned["outbox_id"] if planned else None}

    # -- provider quota ------------------------------------------------------------------

    def observe_quota(self, provider: str, readings: list[dict], *, run_id: str | None = None) -> int:
        """Record gauge readings (see `gauge_from_observation`). Returns how
        many were recorded. An older reading never replaces a newer one."""
        if not readings:
            return 0
        with self.runtime.store.transaction() as tx:
            for reading in readings:
                tx.emit(self.runtime._event(
                    "quota.observed",
                    {"provider": provider, "window": reading["window"][:128], "used_percent": str(reading["used_percent"]),
                     "resets_at": from_ms(reading.get("resets_at_ms")), "observed_at": from_ms(reading["observed_at_ms"]),
                     "source": str(reading.get("source") or "unknown")[:128]},
                    CONTROLLER, run_id,
                ))
        return len(readings)

    def place_hold(self, provider: str, reason: str, *, resets_at_ms: int | None = None, run_id: str | None = None) -> Hold:
        """A provider failed for quota: pause it until the window resets (when
        a reset time is known) or for a bounded, growing backoff."""
        now_ms = self.now_ms()
        with self.runtime.store.transaction() as tx:
            hold = self._hold(tx, provider)
            attempts = hold.attempts + 1 if hold is not None else 0
            resume = (resets_at_ms + self.policy.reset_grace_ms) if resets_at_ms and resets_at_ms > now_ms else now_ms + backoff_ms(attempts, self.policy)
            tx.emit(self.runtime._event("quota.hold", {"provider": provider, "reason": reason[:2000], "resume_at": from_ms(resume), "attempts": attempts}, CONTROLLER, run_id))
            return self._hold(tx, provider)

    def settle_probe(self, provider: str, action_id: str, *, quota_failed: bool, run_id: str | None = None) -> None:
        """The probe turn after a quota pause finished. Success (or a failure
        that is not about quota) ends the pause; another quota failure is
        handled by place_hold."""
        with self.runtime.store.transaction() as tx:
            row = tx.get("quota_holds", provider)
            if row is None or row["state"] != "PROBING" or row["probe_action_id"] != action_id or quota_failed:
                return
            tx.emit(self.runtime._event("quota.released", {"provider": provider, "reason": "a turn ran after the quota reset"}, CONTROLLER, run_id))

    def release_if_fresh(self, provider: str, *, run_id: str | None = None) -> bool:
        """End a hold when a reading taken after it is below the stop threshold."""
        now_ms = self.now_ms()
        with self.runtime.store.transaction() as tx:
            hold = self._hold(tx, provider)
            if hold is None:
                return True
            fresh = [g for g in self._gauges(tx, provider) if g.observed_at_ms > hold.placed_at_ms
                     and not (g.resets_at_ms is not None and now_ms >= g.resets_at_ms) and now_ms - g.observed_at_ms <= self.policy.stale_after_ms]
            if fresh and max(g.used_percent for g in fresh) < self.policy.quota_stop_percent:
                tx.emit(self.runtime._event("quota.released", {"provider": provider, "reason": "a fresh quota reading is below the stop threshold"}, CONTROLLER, run_id))
                return True
            return False

    def settling(self, provider: str) -> bool:
        """True while a transient deferral still applies: a probe turn is
        running for this provider, or one of its turns of unknown size has
        not reported yet. A read, so waiting on it writes nothing."""
        tx = self.runtime.store.read()
        hold = tx.get("quota_holds", provider)
        if hold is not None and hold["state"] == "PROBING":
            return True
        return bool(tx.scalar(
            "SELECT COUNT(*) FROM reservations r WHERE r.provider = ? AND r.category = ? AND r.state = 'HELD'"
            " AND NOT EXISTS (SELECT 1 FROM usage_records u WHERE u.action_id = r.action_id AND u.pool_id = r.pool)",
            (provider, UNKNOWN_SIZE),
        ))

    def hold_passed(self, provider: str) -> bool:
        """True when the provider may try again: no hold, or its resume time
        has passed (the next admission is then the single probe)."""
        hold = self.hold(provider)
        if hold is None:
            return True
        return hold.state == "HELD" and (hold.resume_at_ms is None or self.now_ms() >= hold.resume_at_ms)

    # -- reports -------------------------------------------------------------------------

    def status(self, run_id: str | None = None) -> dict:
        tx = self.runtime.store.read()
        out: dict = {
            "gauges": [
                {"provider": row["provider"], "window": row["window"], "used_percent": json.loads(row["used_percent_json"]),
                 "previous_percent": json.loads(row["previous_percent_json"]) if row["previous_percent_json"] else None,
                 "resets_at": row["resets_at"], "observed_at": row["observed_at"], "source": row["source"],
                 # A percentage of a provider window, including usage outside DUET; never a balance.
                 "note": "observed level, includes usage outside DUET; not a guaranteed balance"}
                for row in tx.query("SELECT * FROM quota_gauges ORDER BY gauge_id")
            ],
            "holds": [dict(row) for row in tx.query("SELECT * FROM quota_holds WHERE state != 'RELEASED' ORDER BY provider")],
        }
        if run_id is not None:
            out["finishing"] = [
                {"reservation_id": r["reservation_id"], "purpose": r["purpose"], "provider": r["provider"], "pool": r["pool"], "metric": r["metric"],
                 "units_planned": r["units_planned"], "units_left": r["units_left"], "held": json.loads(r["quantity_json"]), "state": r["state"],
                 "per_unit": json.loads(r["per_unit_json"]) if r["per_unit_json"] else None,
                 "shortfall": json.loads(r["shortfall_json"]) if r["shortfall_json"] else None}
                for r in self._reserves(tx, run_id)
            ]
            out["admissions"] = [
                {k: row[k] for k in ("admission_id", "participant_id", "action_id", "provider", "action_class", "purpose", "verdict", "reason", "created_at")}
                | {"detail": json.loads(row["detail_json"])}
                for row in tx.query("SELECT * FROM admissions WHERE run_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 20", (run_id,))
            ]
        return out


__all__ = ["ReservationBook", "gauge_from_observation", "to_ms", "from_ms", "UNKNOWN_SIZE"]
