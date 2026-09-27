"""Routing in the runtime (D09): facts in, decisions persisted, outcomes out.

`RoutingControl` builds a `RoutingRequest` from the store (the task's changed
paths, protected paths, checks and failures; the participant's previous
decisions; the provider's discovered controls; the user's pins and profile
mappings; pressure from admission), asks the pure `duet.routing.policy.route`
and records the decision as an event. After a managed turn it records what
the provider accepted and observed, so a clamped or ignored setting shows up
as a difference instead of a claimed success (R08, AT17).

What it never does: pick another provider or participant (routing cannot
remove the second provider's obligation), change a native session's model
(its decisions are advice), write a provider's own configuration (pins and
mappings live in DUET's store and apply per invocation), or let an agent's
prose lower scrutiny."""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from ..routing.contracts import (
    PROFILES,
    AgentAssessment,
    Candidate,
    FailureRecord,
    Pin,
    PriorDecision,
    ProviderControls,
    RoutingDecision,
    RoutingPolicy,
    RoutingRequest,
    TaskFacts,
)
from ..verification.acceptance import AcceptanceContract
from .contracts import CONTROLLER, MAX_ID, MAX_TEXT, Principal, Unauthorized, ValidationError, check_optional_text, check_text, new_id

if TYPE_CHECKING:
    from .pairing import PairCoordinator

log = logging.getLogger("duet.routing")

PURPOSE_FOR_ROLE = {"writer": "implement", "reviewer": "review"}
MAX_FAILURE_TEXT = 2000


def _value(control) -> str:
    return getattr(control, "value", None) or str(control or "unknown")


class RoutingControl:
    def __init__(self, coordinator: "PairCoordinator", policy: RoutingPolicy | None = None) -> None:
        self.co = coordinator
        self.rt = coordinator.runtime
        self.policy = policy or RoutingPolicy()

    # -- the user's settings -----------------------------------------------------------------

    def pin(self, principal: Principal, provider: str, *, model: str | None = None, effort: str | None = None,
            min_profile: str | None = None, max_profile: str | None = None) -> dict:
        """Only the user pins. All fields None removes the pin."""
        if principal.kind != "user":
            raise Unauthorized("only the user can pin a provider's model or effort")
        check_text(provider, "provider", limit=64)
        check_optional_text(model, "model", limit=MAX_ID)
        check_optional_text(effort, "effort", limit=64)
        for value, name in ((min_profile, "min_profile"), (max_profile, "max_profile")):
            if value is not None and value not in PROFILES:
                raise ValidationError(f"{name} must be one of {PROFILES}")
        if min_profile and max_profile and PROFILES.index(min_profile) > PROFILES.index(max_profile):
            raise ValidationError("min_profile is above max_profile")
        with self.rt.store.transaction() as tx:
            tx.emit(self.rt._event("routing.pinned", {"provider": provider, "model": model, "effort": effort, "min_profile": min_profile,
                                                      "max_profile": max_profile, "set_by": principal.id}, principal, None))
            return dict(tx.require("routing_pins", provider))

    def map(self, principal: Principal, provider: str, profile: str, *, model: str | None = None, effort: str | None = None) -> dict:
        """Map a logical profile to a provider setting. Model names come only
        from the user. Both None removes the mapping."""
        if principal.kind != "user":
            raise Unauthorized("only the user can map profiles to models")
        check_text(provider, "provider", limit=64)
        if profile not in PROFILES:
            raise ValidationError(f"profile must be one of {PROFILES}")
        check_optional_text(model, "model", limit=MAX_ID)
        check_optional_text(effort, "effort", limit=64)
        map_id = f"{provider}:{profile}"
        with self.rt.store.transaction() as tx:
            tx.emit(self.rt._event("routing.mapped", {"map_id": map_id, "provider": provider, "profile": profile, "model": model,
                                                      "effort": effort, "set_by": principal.id}, principal, None))
            return dict(tx.require("routing_maps", map_id))

    def pins(self) -> tuple[Pin, ...]:
        rows = self.rt.store.read().query("SELECT * FROM routing_pins ORDER BY provider")
        return tuple(
            Pin(r["provider"], r["model"], r["effort"], r["min_profile"], r["max_profile"])
            for r in rows if any(r[k] for k in ("model", "effort", "min_profile", "max_profile"))
        )

    def user_map(self, provider: str | None = None) -> tuple[Candidate, ...]:
        rows = self.rt.store.read().query("SELECT * FROM routing_maps ORDER BY provider, profile")
        return tuple(
            Candidate(r["provider"], r["profile"], r["model"], r["effort"], "user_map")
            for r in rows if (provider is None or r["provider"] == provider) and (r["model"] or r["effort"])
        )

    # -- facts ------------------------------------------------------------------------------

    @staticmethod
    def controls_from(capabilities, provider: str, *, rejected_models: frozenset = frozenset(), rejected_efforts: frozenset = frozenset()) -> ProviderControls:
        """What DUET can set, from D04 discovery. Settings the provider
        refused at dispatch are removed, so the next decision excludes them
        with evidence instead of retrying them."""
        if capabilities is None:
            return ProviderControls(provider, "unknown", "unknown", None, None)
        efforts = getattr(capabilities, "efforts", None)
        models = getattr(capabilities, "models", None)
        if rejected_efforts:
            efforts = tuple(e for e in (efforts or ()) if e not in rejected_efforts)
        if rejected_models:
            models = tuple(m for m in (models or ()) if m not in rejected_models)
        return ProviderControls(
            provider, _value(getattr(capabilities, "model_control", None)), _value(getattr(capabilities, "effort_control", None)),
            tuple(efforts) if efforts is not None else None, tuple(models) if models is not None else None,
        )

    def failures(self, run_id: str, task_id: str) -> tuple[FailureRecord, ...]:
        """The task's failed attempts, oldest first: failed checks (with the
        tail of their output), changes-requested reviews, rejected results
        and failed provider turns (with their error kind)."""
        tx = self.rt.store.read()
        main = self.co._main_task_id(run_id)
        items: list[tuple[str, FailureRecord]] = []
        if task_id == main:
            for row in tx.query(
                "SELECT e.check_id, e.status, e.exit_code, e.ended_at, e.artifact_ref, e.detail FROM evidence e JOIN snapshots s ON s.snapshot_id = e.snapshot_id"
                " WHERE e.run_id = ? AND e.status != 'passed' AND s.author IS NOT NULL ORDER BY e.ended_at, e.rowid",
                (run_id,),
            ):
                text = row["detail"] or ""
                if row["artifact_ref"]:
                    try:
                        text = self.co.artifacts.get_bytes(row["artifact_ref"]).decode("utf-8", errors="replace")[-MAX_FAILURE_TEXT:]
                    except Exception:  # a missing artifact must not break routing
                        pass
                if row["exit_code"] in (126, 127):
                    text += f"\n(exit code {row['exit_code']})"
                items.append((row["ended_at"], FailureRecord("check", f"check:{row['check_id']}:{row['status']}:{row['exit_code']}", text)))
            for row in tx.query("SELECT review_id, summary, created_at FROM reviews WHERE run_id = ? AND disposition = 'changes_requested' ORDER BY created_at, rowid", (run_id,)):
                items.append((row["created_at"], FailureRecord("review", f"review:{row['review_id']}", f"changes requested: {row['summary']}"[:MAX_FAILURE_TEXT])))
        for row in tx.query("SELECT result_id, decision_reason, updated_at FROM task_results WHERE task_id = ? AND decision = 'rejected' ORDER BY updated_at, rowid", (task_id,)):
            items.append((row["updated_at"], FailureRecord("review", f"result:{row['result_id']}", f"result rejected: {row['decision_reason'] or ''}"[:MAX_FAILURE_TEXT])))
        for row in tx.query("SELECT action_id, result_json, updated_at FROM actions WHERE run_id = ? AND type = 'provider_turn' AND state = 'FAILED' ORDER BY updated_at, rowid", (run_id,)):
            result = json.loads(row["result_json"] or "{}")
            items.append((row["updated_at"], FailureRecord("turn", f"turn:{row['action_id']}", str(result.get("error") or result.get("status") or "")[:MAX_FAILURE_TEXT], result.get("kind"))))
        items.sort(key=lambda item: item[0])
        return tuple(record for _, record in items[-12:])

    def task_facts(self, run_id: str, task_id: str) -> TaskFacts:
        tx = self.rt.store.read()
        task = tx.require("tasks", task_id)
        run = self.rt.get_run(CONTROLLER, run_id)
        contract = AcceptanceContract.from_dict(run["acceptance"])
        changed: tuple[str, ...] = ()
        if task_id == self.co._main_task_id(run_id):
            latest = self.co._latest_snapshot(run_id)
            if latest is not None:
                changed = tuple(json.loads(latest["changed_json"]))
        depends = json.loads(task["depends_on_json"] or "[]")
        return TaskFacts(
            task_id=task_id, revision=task["revision"], kind=task["kind"] or "code",
            description=task["description"], changed_paths=changed, changed_lines=None,
            protected_paths=tuple(contract.protected_paths), has_checks=bool(contract.required_checks()),
            dependencies=len(depends), failures=self.failures(run_id, task_id),
        )

    def pressure(self, provider: str) -> str:
        """From admission (D08): "critical" when the provider is paused for
        quota, a fresh window is exhausted, or a bounded pool is spent;
        "pressure" near the quota threshold or with under a quarter of a
        bounded pool left; otherwise "none"."""
        book = self.co.budget.book
        if book.hold(provider) is not None:
            return "critical"
        from ..usage.admission import fresh_gauges

        tx = self.rt.store.read()
        fresh, _ = fresh_gauges(book._gauges(tx, provider), book.now_ms(), book.policy)
        top = max((g.used_percent for g in fresh), default=None)
        if top is not None and top >= book.policy.quota_stop_percent:
            return "critical"
        level = "pressure" if top is not None and top >= book.policy.quota_defer_percent else "none"
        for pool in book._pools(tx, provider):
            if pool.allowance is None or pool.enforcement == "best_effort":
                continue
            available = pool.allowance - pool.used - pool.held
            if available <= 0:
                return "critical"
            if pool.allowance > 0 and available < pool.allowance / Decimal(4):
                level = "pressure"
        return level

    def history(self, participant_id: str) -> tuple[PriorDecision, ...]:
        rows = self.rt.store.read().query(
            "SELECT * FROM routing_decisions WHERE participant_id = ? AND action != 'requested' ORDER BY created_at, rowid", (participant_id,)
        )
        out = []
        for row in rows[-20:]:
            outcome = json.loads(row["outcome_json"]) if row["outcome_json"] else None
            out.append(PriorDecision(row["task_id"] or "", row["profile"], row["model"], row["effort"], row["turn_index"],
                                     outcome.get("status") if outcome else None, bool(row["escalated"])))
        return tuple(out)

    def agent_request(self, participant_id: str, since_turn: int) -> AgentAssessment | None:
        rows = self.rt.store.read().query(
            "SELECT * FROM routing_decisions WHERE participant_id = ? AND action = 'requested' AND turn_index >= ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (participant_id, since_turn),
        )
        if not rows:
            return None
        detail = json.loads(rows[0]["detail_json"])
        return AgentAssessment(profile=rows[0]["profile"], reason=str(detail.get("reason") or ""))

    # -- decide and record -----------------------------------------------------------------

    def _task_for(self, run_id: str, participant_id: str, purpose: str) -> str:
        main = self.co._main_task_id(run_id)
        if purpose in ("implement", "repair", "review"):
            return main
        owned = self.rt.store.read().query(
            "SELECT task_id FROM tasks WHERE run_id = ? AND owner = ? AND state IN ('CLAIMED', 'RUNNING') ORDER BY updated_at DESC LIMIT 1", (run_id, participant_id)
        )
        return owned[0]["task_id"] if owned else main

    def build_request(self, *, run_id: str, participant_id: str, purpose: str, turn_index: int, capabilities=None,
                      rejected_models: frozenset = frozenset(), rejected_efforts: frozenset = frozenset(),
                      agent: AgentAssessment | None = None) -> RoutingRequest:
        part = self.co._participant(participant_id)
        role = "writer" if self.co._writer_id(run_id) == participant_id else "reviewer"
        task_id = self._task_for(run_id, participant_id, purpose)
        provider = part["provider"]
        return RoutingRequest(
            run_id=run_id, participant_id=participant_id, provider=provider, origin=part["origin"], role=role, purpose=purpose,
            task=self.task_facts(run_id, task_id),
            controls=self.controls_from(capabilities, provider, rejected_models=rejected_models, rejected_efforts=rejected_efforts),
            turn_index=turn_index, agent=agent or self.agent_request(participant_id, turn_index),
            pins=tuple(p for p in self.pins() if p.provider == provider), user_map=self.user_map(provider),
            history=self.history(participant_id), pressure=self.pressure(provider),
        )

    def decide(self, *, run_id: str, participant_id: str, purpose: str, turn_index: int, capabilities=None,
               rejected_models: frozenset = frozenset(), rejected_efforts: frozenset = frozenset(),
               agent: AgentAssessment | None = None, requested_by: str = "controller", persist: bool = True) -> tuple[str | None, RoutingDecision]:
        """Route one turn. `persist=False` only previews (a participant's
        request, native advice): nothing is recorded as if a turn happened."""
        from ..routing.policy import route

        request = self.build_request(run_id=run_id, participant_id=participant_id, purpose=purpose, turn_index=turn_index,
                                     capabilities=capabilities, rejected_models=rejected_models, rejected_efforts=rejected_efforts, agent=agent)
        decision = route(request, self.policy)
        if decision.model is not None or decision.effort is not None:
            # Defensive: the router only ever decides for this participant.
            assert request.provider == self.co._participant(participant_id)["provider"]
        if not persist:
            return None, decision
        decision_id = new_id("rte")
        with self.rt.store.transaction() as tx:
            tx.emit(self.rt._event(
                "routing.decided",
                {
                    "decision_id": decision_id, "run_id": run_id, "participant_id": participant_id, "provider": request.provider,
                    "task_id": request.task.task_id, "task_revision": request.task.revision, "role": request.role, "purpose": purpose,
                    "turn_index": turn_index, "action": decision.action, "profile": decision.profile, "model": decision.model,
                    "effort": decision.effort, "coverage": decision.coverage, "floor_met": decision.floor_met, "escalated": decision.escalated,
                    "requested_by": requested_by,
                    "detail": {**decision.to_dict(), "pressure": request.pressure, "controls": request.controls.__dict__},
                },
                CONTROLLER, run_id,
            ))
        return decision_id, decision

    def record_outcome(self, decision_id: str, *, status: str, action_id: str | None = None, settings=None, error: str | None = None) -> dict:
        """What happened: requested, accepted (what the adapter passed) and
        observed (what the provider reported). Differences are flagged, never
        smoothed over (AT17)."""
        requested = dict(getattr(settings, "requested", None) or {})
        accepted = dict(getattr(settings, "accepted", None) or {})
        observed = dict(getattr(settings, "observed", None) or {})
        differences = []
        for key in ("model", "effort"):
            want = requested.get(key)
            if want is None:
                continue
            if accepted.get(key) not in (None, want):
                differences.append(f"{key}: requested {want}, accepted {accepted.get(key)}")
            elif key not in accepted:
                differences.append(f"{key}: requested {want}, not passed to the provider")
            seen = observed.get(key)
            if seen is not None and seen != want:
                differences.append(f"{key}: requested {want}, observed {seen}")
        outcome = {
            "status": status, "requested": {k: requested.get(k) for k in ("model", "effort")},
            "accepted": {k: accepted.get(k) for k in ("model", "effort") if k in accepted},
            "observed": {k: observed.get(k) for k in ("model", "effort") if k in observed},
            "differences": differences, "error": (error or "")[:MAX_TEXT] or None,
        }
        with self.rt.store.transaction() as tx:
            tx.emit(self.rt._event("routing.outcome", {"decision_id": decision_id, "action_id": action_id, "outcome": outcome}, CONTROLLER,
                                   tx.require("routing_decisions", decision_id)["run_id"]))
        return outcome

    # -- participants ----------------------------------------------------------------------

    def request_profile(self, principal: Principal, *, profile: str | None = None, model: str | None = None, effort: str | None = None, reason: str = "") -> dict:
        """duet_request_profile. A participant may ask for more scrutiny (a
        higher profile), never less than the controller's floor. Specific
        model names are the user's to map (`duet routing map`)."""
        check_text(reason, "reason", limit=MAX_TEXT, allow_empty=True)
        check_optional_text(model, "model", limit=MAX_ID)
        check_optional_text(effort, "effort", limit=64)
        if profile is not None and profile not in PROFILES:
            raise ValidationError(f"profile must be one of {PROFILES}")
        requested = {"profile": profile, "model": model, "effort": effort}
        if profile is None:
            return {"decision": "declined", "requested": requested, "applied": None,
                    "reason": f"ask for a logical profile ({', '.join(PROFILES)}); DUET maps it to this provider's settings. "
                              "Specific models are mapped by the user (`duet routing map`)."}
        run_id = principal.run_id or ""
        part = self.co._participant(principal.id)
        role = "writer" if self.co._writer_id(run_id) == principal.id else "reviewer"
        turn_index = self._next_turn_index(principal.id)
        _, decision = self.decide(
            run_id=run_id, participant_id=principal.id, purpose=PURPOSE_FOR_ROLE[role], turn_index=turn_index,
            agent=AgentAssessment(profile=profile, reason=reason), requested_by=principal.id, persist=False,
        )
        honoured = PROFILES.index(decision.profile) >= PROFILES.index(profile)
        # The request itself is remembered for this participant's next turn.
        with self.rt.store.transaction() as tx:
            tx.emit(self.rt._event(
                "routing.decided",
                {
                    "decision_id": new_id("rte"), "run_id": run_id, "participant_id": principal.id, "provider": part["provider"],
                    "task_id": None, "task_revision": None, "role": role, "purpose": PURPOSE_FOR_ROLE[role],
                    "turn_index": turn_index, "action": "requested", "profile": profile, "model": None, "effort": None,
                    "coverage": decision.coverage, "floor_met": decision.floor_met, "escalated": False, "requested_by": principal.id,
                    "detail": {"reason": reason, "preview": decision.to_dict()},
                },
                principal, run_id,
            ))
        if part["origin"] != "managed":
            status = "advisory"
            note = ("a native session keeps its own model and effort: DUET cannot change them. If you agree, switch in your client; "
                    "the suggestion is recorded.")
        elif honoured:
            status = "accepted"
            note = "applies from your next turn (settings change only between turns)."
        else:
            status = "limited"
            note = f"a user bound caps this provider at {decision.profile}."
        return {"decision": status, "requested": requested, "reason": note, "explanation": decision.explanation,
                "applied": {"profile": decision.profile, "model": decision.model, "effort": decision.effort, "coverage": decision.coverage}}

    def advise(self, run_id: str, participant_id: str, purpose: str) -> dict | None:
        """A decision for a native participant, as advice (coverage advisory)."""
        try:
            decision_id, decision = self.decide(run_id=run_id, participant_id=participant_id, purpose=purpose,
                                                turn_index=self._next_turn_index(participant_id), requested_by="advice")
        except Exception:  # advice must never break the tool call it rides on
            log.exception("routing advice failed")
            return None
        return {"profile": decision.profile, "coverage": decision.coverage, "action": decision.action, "explanation": decision.explanation}

    def _next_turn_index(self, participant_id: str) -> int:
        value = self.rt.store.read().scalar(
            "SELECT MAX(turn_index) FROM routing_decisions WHERE participant_id = ? AND action != 'requested'", (participant_id,)
        )
        return 0 if value is None else int(value) + 1

    def status(self, run_id: str) -> dict:
        rows = self.rt.store.read().query("SELECT * FROM routing_decisions WHERE run_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 30", (run_id,))
        records = []
        for row in rows:
            detail = json.loads(row["detail_json"])
            records.append({
                **{k: row[k] for k in ("decision_id", "participant_id", "provider", "task_id", "role", "purpose", "turn_index", "action",
                                        "profile", "model", "effort", "coverage", "requested_by", "action_id", "created_at")},
                "floor_met": bool(row["floor_met"]), "escalated": bool(row["escalated"]),
                "explanation": detail.get("explanation"), "reason": detail.get("reason"),
                "outcome": json.loads(row["outcome_json"]) if row["outcome_json"] else None,
            })
        latest: dict = {}
        for record in records:
            if record["action"] != "requested":
                latest.setdefault(record["participant_id"], record)
        return {"latest": latest, "decisions": records, "pins": [p.__dict__ for p in self.pins()],
                "maps": [c.to_dict() for c in self.user_map()]}
