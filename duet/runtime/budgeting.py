"""Run-level consequences of admission (D08): finishing reserves at pair
formation, quota and budget pauses, and reset-aware resume.

Quota loss pauses the obligations of the participant whose provider ran
out, not the whole pair (spec 7.5, AT07). The run itself pauses
(PAUSED_QUOTA) only when nobody else can move it forward: the held
participant owns the current obligation (the writer while the main task is
open, the reviewer while it waits for review) and every other participant is
a managed session with nothing else to do. With a native participant in the
run, DUET cannot know what else it is doing, so the run stays active and the
native session is told what is pending and until when. Either way the
review stays required: completion still needs it, and passing checks alone
never replace it.

A local allowance that cannot fund required or finishing work pauses the run
for the user (PAUSED_BUDGET), and a pool that demands a provider-enforced cap
the provider does not have pauses it for approval (PAUSED_APPROVAL). The user
resumes after raising the allowance or changing the pool (`duet resume`).

A quota pause ends by itself: the service monitor resumes the run once the
provider's hold reaches its resume time (the window's reset, or a bounded
backoff when no reset time is known). The next admission is then the single
probe turn; a fresh quota reading, when the provider offers a passive read,
ends the hold without any model call."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..usage.reservations import ReservationBook, from_ms
from .contracts import CONTROLLER, TERMINAL_RUN, USER, DomainError, InvalidTransition, Principal, RunLifecycle, TaskState

if TYPE_CHECKING:
    from .pairing import PairCoordinator

log = logging.getLogger("duet.budgeting")

PAUSE_STATE = {"quota": RunLifecycle.PAUSED_QUOTA, "budget": RunLifecycle.PAUSED_BUDGET, "approval": RunLifecycle.PAUSED_APPROVAL}


def _other(provider: str) -> str:
    return "codex" if provider == "claude" else "claude"


class Budgeting:
    def __init__(self, coordinator: "PairCoordinator") -> None:
        self.co = coordinator
        self.book = ReservationBook(coordinator.runtime)

    # -- parties ---------------------------------------------------------------------------

    def managed_providers(self, run_id: str) -> set[str]:
        settings = self.co.settings(run_id)
        if settings.peer_mode == "managed_pair":
            return {settings.writer_provider, _other(settings.writer_provider)}
        if settings.peer_mode == "managed":
            initiator = settings.writer_provider if settings.writer_role == "self" else _other(settings.writer_provider)
            return {_other(initiator)}
        return set()

    def finishing_parties(self, run_id: str) -> dict:
        """Who needs finishing capacity DUET can reserve: the reviewer's
        provider for the review, the writer's for repairs; None for a native
        participant, whose turns DUET does not schedule."""
        settings = self.co.settings(run_id)
        managed = self.managed_providers(run_id)
        writer = settings.writer_provider
        reviewer = _other(writer)
        return {"writer": writer if writer in managed else None, "reviewer": reviewer if reviewer in managed else None}

    def ensure_finishing(self, run_id: str) -> None:
        try:
            parties = self.finishing_parties(run_id)
            if parties["writer"] or parties["reviewer"]:
                self.book.ensure_finishing(run_id, writer=parties["writer"], reviewer=parties["reviewer"])
        except DomainError:
            log.exception("could not reserve finishing capacity for %s", run_id)

    # -- pauses ----------------------------------------------------------------------------

    def _obligation_owner(self, run_id: str) -> str | None:
        """The participant the run is waiting on: the reviewer while the main
        task waits for review, otherwise the writer."""
        try:
            main = self.co.runtime.store.read().require("tasks", self.co._main_task_id(run_id))
        except DomainError:
            return None
        writer = self.co._writer_id(run_id)
        if main["state"] == TaskState.REVIEW_REQUIRED.value:
            peer = self.co._peer_of(run_id, writer) if writer else None
            return peer["participant_id"] if peer else None
        return writer

    def pause_for(self, run_id: str, participant_id: str, provider: str, kind: str, reason: str, resume_at_ms: int | None = None) -> str:
        """Apply a pause verdict for one participant. Returns what happened:
        "run_paused" or "participant_paused"."""
        run = self.co.runtime.get_run(CONTROLLER, run_id)
        lifecycle = RunLifecycle(run["lifecycle"])
        if lifecycle in TERMINAL_RUN or lifecycle.value.startswith("PAUSED_"):
            return "run_paused" if lifecycle.value.startswith("PAUSED_") else "run_ended"
        when = f" until about {from_ms(resume_at_ms)[:16].replace('T', ' ')} UTC" if resume_at_ms else ""
        if kind != "quota":
            self._pause_run(run_id, PAUSE_STATE[kind], f"{provider}: {reason}")
            self._tell_all(run_id, f"Run paused ({PAUSE_STATE[kind].value}): {provider} work cannot be admitted: {reason} "
                                   "DUET never switches to paid API usage, another account or another provider to continue. "
                                   "The user can raise the allowance or change the pool, then run `duet resume`.")
            return "run_paused"
        others = [p for p in self.co.runtime.participants(CONTROLLER, run_id) if p["participant_id"] != participant_id]
        owner = self._obligation_owner(run_id)
        if owner == participant_id and all(p["origin"] == "managed" for p in others):
            self._pause_run(run_id, RunLifecycle.PAUSED_QUOTA, f"{provider} quota: {reason}")
            self._tell_all(run_id, f"Run paused for {provider} quota{when}: {reason.rstrip('.')}. DUET resumes it after the reset; it does not "
                                   "switch accounts, providers or billing to continue.")
            return "run_paused"
        role = "review" if owner == participant_id and owner != self.co._writer_id(run_id) else "turns"
        for p in others:
            self.co._status(run_id, p["participant_id"],
                            f"Your peer ({provider}) is paused for quota{when}: {reason.rstrip('.')}. Its {role} will resume after the reset; "
                            "DUET does not switch accounts, providers or billing to continue. A required review stays required, "
                            "so completion waits for it. You can continue other work meanwhile.")
        self.co.notify()
        return "participant_paused"

    def _pause_run(self, run_id: str, state: RunLifecycle, reason: str) -> None:
        try:
            self.co.runtime.transition_run(CONTROLLER, run_id, state, reason=reason[:500])
        except InvalidTransition:
            log.info("run %s could not pause (%s): already changed", run_id, state.value)
        self.co.notify()

    def _tell_all(self, run_id: str, body: str) -> None:
        for p in self.co.runtime.participants(CONTROLLER, run_id):
            self.co._status_terminal(run_id, p["participant_id"], body)
        self.co.notify()

    # -- resume ----------------------------------------------------------------------------

    def resume(self, run_id: str, *, reason: str, principal: Principal = USER) -> dict:
        """Paused -> RECONCILING -> the active state the main task implies."""
        run = self.co.runtime.get_run(CONTROLLER, run_id)
        if not run["lifecycle"].startswith("PAUSED_"):
            raise InvalidTransition(f"run {run_id} is {run['lifecycle']}, not paused")
        self.co.runtime.transition_run(principal, run_id, RunLifecycle.RECONCILING, reason=reason[:500])
        target = RunLifecycle.EXECUTING
        try:
            main = self.co.runtime.store.read().require("tasks", self.co._main_task_id(run_id))
            target = {
                TaskState.REVIEW_REQUIRED.value: RunLifecycle.REVIEWING,
                TaskState.CHANGES_REQUESTED.value: RunLifecycle.REPAIRING,
                TaskState.VERIFIED.value: RunLifecycle.VERIFYING,
            }.get(main["state"], RunLifecycle.EXECUTING)
        except DomainError:
            pass
        resumed = self.co.runtime.transition_run(CONTROLLER, run_id, target, reason="resumed")
        self._tell_all(run_id, f"Run resumed ({target.value}): {reason}")
        self.co.try_complete(run_id)
        return resumed

    def check_quota(self) -> list[str]:
        """Monitor hook: resume PAUSED_QUOTA runs whose provider may try
        again (hold released, or its resume time reached)."""
        resumed = []
        rows = self.co.runtime.store.read().query("SELECT run_id FROM runs WHERE lifecycle = ?", (RunLifecycle.PAUSED_QUOTA.value,))
        for row in rows:
            providers = sorted({p["provider"] for p in self.co.runtime.participants(CONTROLLER, row["run_id"])})
            held = [p for p in providers if self.book.hold(p) is not None]
            if not all(self.book.hold_passed(p) for p in held):
                continue
            why = f"{' and '.join(held)} quota pause reached its resume time" if held else "no provider is paused for quota any more"
            try:
                self.resume(row["run_id"], reason=why, principal=CONTROLLER)
                resumed.append(row["run_id"])
            except DomainError:
                log.exception("could not resume %s", row["run_id"])
        return resumed

    def settle_in_doubt_turns(self, action_ids: list[str]) -> list[str]:
        """A provider turn the service lost while it ran was dispatched: it
        counts as one turn, and whatever else it used is unknown, never zero.
        It is settled FAILED from that evidence and never re-dispatched. A
        lost probe turn puts its provider's quota pause back (with backoff)."""
        from .pools import PoolStore

        pools = PoolStore(self.co.runtime)
        settled = []
        tx = self.co.runtime.store.read()
        for action_id in action_ids:
            row = tx.get("actions", action_id)
            if row is None or row["type"] != "provider_turn" or row["state"] != "IN_DOUBT":
                continue
            for res in tx.query("SELECT pool, metric FROM reservations WHERE action_id = ?", (action_id,)):
                if tx.get("usage_pools", res["pool"]) is None:
                    continue
                known = res["metric"] == "turns"
                try:
                    pools.record_usage(
                        CONTROLLER, record_id=f"{action_id}:{res['pool']}", pool_id=res["pool"], metric=res["metric"],
                        quantity="1" if known else None, quality="observed" if known else "unknown", source="duet.reconcile",
                        run_id=row["run_id"], action_id=action_id, participant_id=row["participant_id"],
                    )
                except DomainError:
                    log.info("usage for in-doubt turn %s was already recorded", action_id)
            for held in tx.query("SELECT provider FROM quota_holds WHERE state = 'PROBING' AND probe_action_id = ?", (action_id,)):
                self.book.place_hold(held["provider"], "the probe turn was lost when the service stopped; its outcome is unknown", run_id=row["run_id"])
            self.co.runtime.resolve_in_doubt(
                CONTROLLER, action_id, "FAILED",
                reconciliation="the service stopped during this provider turn: counted as one dispatched turn; its other usage is unknown",
            )
            settled.append(action_id)
        return settled

    def status(self, run_id: str) -> dict:
        report = self.book.status(run_id)
        report["enforcement_note"] = (
            "local_bound pools bound only the turns DUET schedules; a running turn can exceed its estimate (see overshoot). "
            "provider_cap applies only where the provider enforces a per-invocation limit. Quota gauges are observed "
            "percentages of provider windows, including usage outside DUET, never a guaranteed balance."
        )
        return report
