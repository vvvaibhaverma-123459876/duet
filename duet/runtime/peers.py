"""Managed peers: sessions DUET launches and drives (D05).

A managed peer is a provider session DUET started itself (origin `managed`).
Its driver thread sleeps on the peer's inbox and runs one provider turn when a
message arrives, or once at kickoff when the peer is the writer. During the
turn the provider reaches DUET through the same MCP tools a native session
uses (`duet mcp serve --token-file ...`), so there is one protocol for both.

Every turn is a durable action (planned, claimed, recorded): a turn the
service loses while it runs becomes IN_DOUBT and is never replayed blindly.
Turns are capped by the run policy's `max_invocations`, and each one is
admitted first (D08): its class (finishing review or repair, required work,
optional work) decides what it may draw on, and a refusal defers or pauses
it. Quota failures pause the peer until the provider's reset; authentication
and billing failures stop it and mark it unavailable. Nothing falls back to
another account, provider or paid API (R13)."""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from importlib import resources
from pathlib import Path
from typing import Any

import time
from decimal import Decimal

from ..adapters import AgentError, AuthError, BillingError, QuotaError
from ..providers.base import ProviderAdapter, SettingsRecord, TurnRequest, TurnResult, UnsupportedSetting
from .contracts import CONTROLLER, TERMINAL_RUN, ActionState, DomainError, Liveness, MessageKind, MessageState, RunLifecycle, TaskState
from .pairing import MAX_WAIT_SECONDS, PairCoordinator, other_provider
from .pools import PoolStore
from ..usage.reservations import gauge_from_observation

TURN_METRIC = "turns"
COST_METRIC = "cost.estimated_usd"

log = logging.getLogger("duet.peers")

DEFAULT_TURN_TIMEOUT = 900.0
POLL_SECONDS = 1.0
# A turn that failed for an ordinary reason (a crash, a bad exit) is retried
# this many times in a row before the peer stops and says why: waiting for a
# message after a failed turn can deadlock the pair (nothing else will come).
MAX_TURN_FAILURES = 2
RETRY_PAUSE_SECONDS = 2.0
FALLBACK_PREFIX = "[DUET: delivered from the end of the peer's turn because it did not reply with duet_send] "

# These are the run-scoped coordination operations a managed peer needs.
# Creating/joining runs is deliberately absent: the private token already
# binds the MCP proxy to this participant, and the service checks its role.
# Keep this explicit so adding an MCP tool does not silently authorize it.
MANAGED_PEER_TOOLS = (
    "duet_send", "duet_inbox", "duet_wait", "duet_status",
    "duet_propose_task", "duet_propose_plan", "duet_decide_plan",
    "duet_claim", "duet_complete_task", "duet_decide_task", "duet_handoff",
    "duet_submit", "duet_request_review", "duet_request_profile",
)


ROUTING_ADVICE = {
    "diagnose_environment": ("The last failure looks environmental (a missing tool, module or permission), not a reasoning problem: "
                             "repair or report the environment first; a stronger model would not help."),
    "clarify": "The requirement looks unclear: state an explicit assumption, or ask your peer or the user one precise question.",
    "replan": ("Repairs under this approach have failed repeatedly: propose a different approach with duet_propose_plan "
               "instead of another attempt of the same kind."),
}


def _terminal(lifecycle: str) -> bool:
    return RunLifecycle(lifecycle) in TERMINAL_RUN


def _paused(lifecycle: str) -> bool:
    """A paused run takes no turns, but its peers stay: a quota pause ends at
    the provider's reset and a budget pause when the user resumes."""
    return lifecycle.startswith("PAUSED_")


ACTIONABLE_FROM_PEER = frozenset(
    k.value for k in (MessageKind.QUESTION, MessageKind.ANSWER, MessageKind.REVIEW_REQUEST, MessageKind.FINDING,
                      MessageKind.BLOCKER, MessageKind.PLAN_PROPOSAL, MessageKind.TASK_PROPOSAL)
)


def _actionable(message: dict) -> bool:
    """A turn costs a model call. Start one for peer messages that ask for
    something (questions, answers to our questions, review requests,
    findings, proposals, blockers), for controller BLOCKERs and for a
    controller TASK_PROPOSAL (a task the scheduler suggests for this peer).
    Status notes and review results wait for the next real turn: a rejected
    review also produces a controller BLOCKER."""
    if message["from"] == "controller":
        return message["kind"] in (MessageKind.BLOCKER.value, MessageKind.TASK_PROPOSAL.value)
    return message["kind"] in ACTIONABLE_FROM_PEER


def participant_instructions() -> str:
    return resources.files("duet").joinpath("resources/instructions/participant.md").read_text(encoding="utf-8")


def mcp_server_config(state_root: Path, token_file: Path, provider: str) -> dict:
    """Claude-style MCP config for a managed peer's DUET tools. The token is
    passed as a 0600 file path, never on a command line or in the config."""
    server: dict[str, Any] = {
        "command": sys.executable,
        "args": ["-m", "duet", "mcp", "serve", "--state-root", str(state_root), "--token-file", str(token_file)],
        "env": {"DUET_MANAGED_PEER": "1"},
    }
    if provider == "codex":
        # Codex times MCP tool calls out (60 s by default). The key is accepted
        # by 0.157.1; whether it takes effect was not observable here.
        server["tool_timeout_sec"] = int(MAX_WAIT_SECONDS + 30)
        # A noninteractive Codex turn keeps approvalPolicy=never, which
        # rejects MCP calls that would prompt. Authorize only our own local,
        # token-bound coordination tools in this thread's server config.
        # Shell/file approvals, other MCP servers, and native sessions retain
        # their policies. See the Codex per-tool approval_mode reference:
        # https://learn.chatgpt.com/docs/config-file/config-reference
        server["enabled_tools"] = list(MANAGED_PEER_TOOLS)
        server["tools"] = {name: {"approval_mode": "approve"} for name in MANAGED_PEER_TOOLS}
    return {"mcpServers": {"duet": server}}


class ManagedPeer:
    def __init__(
        self,
        coordinator: PairCoordinator,
        *,
        run_id: str,
        provider: str,
        adapter: ProviderAdapter,
        state_root: Path,
        turn_timeout: float = DEFAULT_TURN_TIMEOUT,
    ) -> None:
        self.co = coordinator
        self.run_id = run_id
        self.provider = provider
        self.adapter = adapter
        self.state_root = Path(state_root)
        self.turn_timeout = turn_timeout
        registered = coordinator.register_managed(run_id, provider)
        self.participant = registered["participant"]
        self.participant_id = self.participant["participant_id"]
        self.principal = coordinator.runtime.authenticate(registered["token"])
        peers_dir = self.state_root / "peers"
        peers_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.token_file = peers_dir / f"{self.participant_id}.token"
        fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(registered["token"])
        self.session_id: str | None = None
        self.pools = PoolStore(coordinator.runtime)
        self.book = coordinator.budget.book  # one admission policy per service
        self._budget_cap: bool | None = None
        self._caps: Any = None
        self._caps_probed = False
        self._rejected_models: set[str] = set()
        self._rejected_efforts: set[str] = set()
        self._readings: list[dict] = []
        from ..usage.ledger import Ledger

        self.ledger = Ledger()
        self._cost_total: Decimal | None = Decimal(0)
        self.turns = 0
        self._failures = 0  # consecutive ordinary turn failures
        self.results: list[TurnResult] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"duet-peer-{provider}-{run_id[-8:]}", daemon=True)

    # -- lifecycle -------------------------------------------------------------------------

    def start(self) -> "ManagedPeer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=30)
        close = getattr(self.adapter, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                log.exception("closing %s adapter failed", self.provider)
        try:
            self.token_file.unlink()
        except FileNotFoundError:
            pass

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    # -- loop ------------------------------------------------------------------------------

    def _is_writer(self) -> bool:
        return self.co._writer_id(self.run_id) == self.participant_id

    def _run_state(self) -> str:
        return self.co.runtime.get_run(CONTROLLER, self.run_id)["lifecycle"]

    def _loop(self) -> None:
        since = None
        kickoff = self._is_writer()
        backlog: list[dict] = []  # informational notices, shown with the next real turn
        pending: list[dict] | None = None  # a turn that could not run yet: retried with the same messages
        try:
            while not self._stop.is_set():
                state = self._run_state()
                if _terminal(state):
                    return
                if _paused(state):
                    self._sleep_while_paused()
                    continue
                if pending is not None:
                    messages, pending = pending, None
                elif kickoff:
                    messages = []
                else:
                    if not any(_actionable(m) for m in backlog):
                        result = self.co.wait(self.principal, since=since, timeout=MAX_WAIT_SECONDS, cancel=self._stop)
                        if self._stop.is_set() or _terminal(result["run"]["lifecycle"]):
                            return
                        since = result["last_seq"]
                        backlog.extend(result["messages"])
                        if _paused(result["run"]["lifecycle"]):
                            continue
                        if not any(_actionable(m) for m in backlog):
                            if result["messages"] and since > self.co._cursor(self.principal):
                                self.co.runtime.ack(self.principal, up_to_seq=since)
                            continue
                    messages, backlog = backlog, []
                kickoff = False
                policy = self.co.runtime.policy_for(self.run_id)
                used = self.co.runtime.store.read().scalar(
                    "SELECT COUNT(*) FROM actions WHERE run_id = ? AND type = 'provider_turn'", (self.run_id,)
                ) or 0
                if used >= policy.max_invocations:
                    self._give_up(f"the run's invocation budget ({policy.max_invocations} provider turns) is spent")
                    return
                outcome = self._turn(messages)
                if outcome == "stop":
                    return
                if outcome == "retry":
                    pending = messages
                    continue
                # The model may have acknowledged further during the turn
                # (duet_wait/duet_inbox ack_through); never move backwards.
                cursor = self.co._cursor(self.principal)
                if since is not None and since > cursor:
                    self.co.runtime.ack(self.principal, up_to_seq=since)
                since = max(since or 0, cursor)
        except Exception as exc:  # the driver must never die silently
            log.exception("managed %s peer failed", self.provider)
            self._give_up(f"driver error: {type(exc).__name__}: {exc}")

    def _sleep_while_paused(self) -> None:
        while not self._stop.is_set() and _paused(self._run_state()):
            self._stop.wait(POLL_SECONDS)

    def _wait_until_admissible(self) -> None:
        """After a pause: sleep (no model call) until the run is resumed and
        this provider may try again."""
        while not self._stop.is_set():
            state = self._run_state()
            if _terminal(state):
                return
            if not _paused(state) and self.book.hold_passed(self.provider):
                return
            self._stop.wait(POLL_SECONDS)

    # -- admission -------------------------------------------------------------------------

    def _classify(self, messages: list[dict]) -> tuple[str, str]:
        """What this turn is for decides what it may draw on (D08)."""
        kinds = {m["kind"] for m in messages}
        if MessageKind.REVIEW_REQUEST.value in kinds:
            return "finishing", "review"
        writer = self._is_writer()
        try:
            main = self.co.runtime.store.read().require("tasks", self.co._main_task_id(self.run_id))["state"]
        except DomainError:
            main = None
        if writer and main == TaskState.CHANGES_REQUESTED.value:
            return "finishing", "repair"
        if writer and main not in (TaskState.REVIEW_REQUIRED.value, TaskState.VERIFIED.value, TaskState.CANCELLED.value):
            return "required", "implement"
        if kinds & {MessageKind.QUESTION.value, MessageKind.BLOCKER.value, MessageKind.ANSWER.value}:
            return "required", "discussion"
        return "optional", "investigate"

    def _capabilities(self):
        """D04 discovery, once per peer. None when the adapter cannot say:
        routing then leaves the provider's own defaults alone."""
        if not self._caps_probed:
            self._caps_probed = True
            try:
                self._caps = self.adapter.capabilities()
            except Exception:
                log.info("%s capabilities unavailable", self.provider, exc_info=True)
                self._caps = None
        return self._caps

    def _provider_budget_cap(self) -> bool:
        if self._budget_cap is None:
            self._budget_cap = bool(getattr(self._capabilities(), "provider_budget_cap", False))
        return self._budget_cap

    def _route(self, purpose: str):
        """Model and effort for this turn (D09). Routing never blocks a turn:
        without a decision the provider's defaults apply."""
        try:
            return self.co.routing.decide(
                run_id=self.run_id, participant_id=self.participant_id, purpose=purpose, turn_index=self.turns,
                capabilities=self._capabilities(), rejected_models=frozenset(self._rejected_models), rejected_efforts=frozenset(self._rejected_efforts),
            )
        except Exception:
            log.exception("routing failed for %s; using the provider's defaults", self.provider)
            return None, None

    def _routed(self, decision_id: str | None, **outcome) -> None:
        if decision_id is None:
            return
        try:
            self.co.routing.record_outcome(decision_id, **outcome)
        except Exception:
            log.exception("could not record the routing outcome")

    def _refresh_quota(self) -> None:
        """Before retrying after a quota pause, read the provider's quota
        windows passively where it offers that (no model call), so a reset
        is seen from telemetry rather than by spending a turn."""
        if self.book.hold(self.provider) is None or not self.book.hold_passed(self.provider):
            return
        read = getattr(self.adapter, "read_rate_limits", None)
        if not callable(read):
            return
        try:
            observations, _reason = read()
        except Exception:
            log.info("passive quota read for %s failed", self.provider, exc_info=True)
            return
        now = self.book.now_ms()
        readings = [r for r in (gauge_from_observation(o, now) for o in observations) if r is not None]
        if readings:
            self.book.observe_quota(self.provider, readings, run_id=self.run_id)
            self.book.release_if_fresh(self.provider, run_id=self.run_id)

    def _record_quota(self, result: TurnResult) -> None:
        now = self.book.now_ms()
        self._readings = [r for r in (gauge_from_observation(o, now) for o in result.usage) if r is not None]
        try:
            self.book.observe_quota(self.provider, self._readings, run_id=self.run_id)
        except DomainError:
            log.exception("could not record %s quota readings", self.provider)

    def _quota_failure(self, kind: str, message: str) -> None:
        """A quota or rate-limit failure: pause this provider until the
        window's reset (from this turn's readings when they carry one) or a
        bounded backoff, then wait. Never another account or provider."""
        stop = self.book.policy.quota_stop_percent
        resets = [r["resets_at_ms"] for r in self._readings if r["resets_at_ms"] and r["used_percent"] >= stop]
        reason = f"{kind}: {message}"[:500]
        hold = self.book.place_hold(self.provider, reason, resets_at_ms=max(resets) if resets else None, run_id=self.run_id)
        self.co.budget.pause_for(self.run_id, self.participant_id, self.provider, "quota", reason, hold.resume_at_ms if hold else None)
        self._wait_until_admissible()

    def _deferred(self, messages: list[dict], reason: str) -> None:
        peer = self.co._peer_of(self.run_id, self.participant_id)
        if peer is None:
            return
        kinds = ", ".join(sorted({m["kind"] for m in messages})) or "its own work"
        try:
            self.co._status(self.run_id, peer["participant_id"], f"{self.provider} deferred optional work ({kinds}): {reason}")
        except DomainError:
            log.exception("could not report a deferral")

    # -- turn ------------------------------------------------------------------------------

    def _turn(self, messages: list[dict]) -> str:
        """One admitted provider turn. Returns "done", "retry" (the same
        messages again later: paused or waiting) or "stop"."""
        runtime = self.co.runtime
        action_class, purpose = self._classify(messages)
        decision_id, routing = self._route(purpose)
        self._refresh_quota()
        try:
            admitted = self.book.admit(
                run_id=self.run_id, provider=self.provider, action_class=action_class, purpose=purpose, participant_id=self.participant_id,
                input={"provider": self.provider, "turn": self.turns + 1, "messages": [m["message_id"] for m in messages], "resume": self.session_id},
                per_call_budget_cap=self._provider_budget_cap(), finishing=self.co.budget.finishing_parties(self.run_id),
            )
        except DomainError:
            if _terminal(self._run_state()):
                return "stop"  # the run ended between the loop's check and the admission
            raise
        decision = admitted["decision"]
        if decision.verdict == "pause":
            applied = self.co.budget.pause_for(self.run_id, self.participant_id, self.provider, decision.pause_kind, decision.reason, decision.resume_at_ms)
            if decision.pause_kind != "quota" and applied != "run_paused":
                self._stop.wait(5 * POLL_SECONDS)  # the run could not pause: never spin on admissions
            self._wait_until_admissible()
            return "retry"
        if decision.verdict == "defer":
            if decision.transient:
                # Wait (reads only) for the probe or the unknown-size turn to settle.
                while not self._stop.is_set() and not _terminal(self._run_state()) and self.book.settling(self.provider):
                    self._stop.wait(POLL_SECONDS)
                return "retry"
            self._deferred(messages, decision.reason)
            return "done"
        action_id = admitted["action"]["action_id"]
        # The action lease must outlive the turn's own timeout.
        fence = runtime.claim_action(CONTROLLER, action_id, lease_seconds=int(self.turn_timeout) + 300)["lease"]["fencing_token"]
        runtime.record_action(CONTROLLER, action_id, ActionState.RUNNING, fence=fence)
        writer = self._is_writer()
        settings = self.co.settings(self.run_id)
        enforce = routing is not None and routing.coverage != "advisory"
        request = TurnRequest(
            prompt=self._prompt(messages, writer, routing),
            model=routing.model if enforce else None,
            effort=routing.effort if enforce else None,
            cwd=Path(settings.workspace_path),
            session_id=self.session_id,
            permission_profile="workspace_write" if writer else "read_only",
            mcp_config=mcp_server_config(self.state_root, self.token_file, self.provider),
            env={"DUET_MANAGED_PEER": "1"},
            timeout_seconds=self.turn_timeout,
            # Only where the provider enforces it itself (provider_cap pools).
            max_budget_usd=decision.per_call_budget if decision.per_call_budget is not None and self._provider_budget_cap() else None,
        )
        self.turns += 1
        sent_before = self._last_sent_seq()
        self._readings = []
        try:
            result = self.adapter.run_turn(request, cancel=self._stop)
        except UnsupportedSetting as exc:
            # Refused before dispatch: nothing ran. Exclude the setting and
            # route again (an explicit downgrade, with the evidence recorded).
            runtime.record_action(CONTROLLER, action_id, ActionState.FAILED, fence=fence, result={"error": str(exc)[:2000], "kind": "unsupported_setting"})
            self._routed(decision_id, status="rejected", action_id=action_id, error=str(exc),
                         settings=SettingsRecord({"model": request.model, "effort": request.effort}, {}, {}))
            if request.effort is not None:
                self._rejected_efforts.add(request.effort)
            if request.model is not None and (request.effort is None or "model" in str(exc).lower()):
                self._rejected_models.add(request.model)
            self.turns -= 1
            return "retry"
        except QuotaError as exc:
            runtime.record_action(CONTROLLER, action_id, ActionState.FAILED, fence=fence, result={"error": str(exc)[:2000], "kind": exc.kind})
            self._routed(decision_id, status="failed", action_id=action_id, error=str(exc))
            self._quota_failure(exc.kind, str(exc))
            return "retry"
        except (AuthError, BillingError) as exc:
            runtime.record_action(CONTROLLER, action_id, ActionState.FAILED, fence=fence, result={"error": str(exc)[:2000], "kind": exc.kind})
            self._give_up(f"{exc.kind}: {exc}. DUET does not switch accounts, providers or billing to continue.")
            return "stop"
        except AgentError as exc:
            runtime.record_action(CONTROLLER, action_id, ActionState.FAILED, fence=fence, result={"error": str(exc)[:2000], "kind": exc.kind})
            self._give_up(f"{exc.kind}: {exc}")
            return "stop"
        except Exception as exc:
            # Record the outcome before anything else: an action left RUNNING
            # would block completion as "unobserved".
            runtime.record_action(CONTROLLER, action_id, ActionState.FAILED, fence=fence, result={"error": f"{type(exc).__name__}: {exc}"[:2000]})
            raise
        self.results.append(result)
        outcome = ActionState.SUCCEEDED if result.ok else ActionState.FAILED
        self._routed(decision_id, status="succeeded" if result.ok else "failed", action_id=action_id, settings=result.settings,
                     error=None if result.ok else str(result.error or result.status))
        self._record_quota(result)
        actuals = self._record_usage(action_id, result)
        runtime.record_action(
            CONTROLLER, action_id, outcome, fence=fence, provider_invocation_id=result.provider_invocation_id, actuals=actuals,
            result={
                "status": result.status, "session_id": result.session_id, "lineage": result.lineage,
                "settings": {"requested": result.settings.requested, "accepted": result.settings.accepted, "observed": result.settings.observed},
                "usage": [u.to_dict() for u in result.usage], "warnings": list(result.warnings)[:20],
                "permission_denials": list(result.permission_denials)[:20], "text_excerpt": result.text[-2000:],
                "admission": {"class": action_class, "purpose": purpose, "enforcement": dict(decision.enforcement), "quota": decision.quota},
            },
        )
        quota_failed = isinstance(result.error, QuotaError)
        self.book.settle_probe(self.provider, action_id, quota_failed=quota_failed, run_id=self.run_id)
        try:
            self.book.resize_finishing(self.run_id, self.provider)
        except DomainError:
            log.exception("could not re-estimate finishing reserves")
        if result.session_id and result.session_id != self.session_id:
            self.session_id = result.session_id
            runtime.update_participant(CONTROLLER, self.participant_id, native_session_id=result.session_id)
        if not result.ok:
            if result.status in ("cancelled", "interrupted") and self._stop.is_set():
                return "stop"
            if quota_failed:
                self._quota_failure(result.error.kind, str(result.error))
                return "retry"
            if result.error is not None and isinstance(result.error, (AuthError, BillingError)):
                self._give_up(f"{result.error.kind}: {result.error}. DUET does not switch accounts, providers or billing to continue.")
                return "stop"
            self._failures += 1
            reason = str(result.error or result.status)[:500]
            if self._failures >= MAX_TURN_FAILURES:
                self._give_up(f"{self._failures} turns in a row failed; last: {reason}")
                return "stop"
            log.warning("%s turn failed (%s); retrying once", self.provider, reason)
            self._stop.wait(RETRY_PAUSE_SECONDS)
            return "retry"
        self._failures = 0
        if self._last_sent_seq() == sent_before:
            self._fallback_answers(messages, result)
        # The turn's action is settled now; completion may have been waiting on it.
        self.co.try_complete(self.run_id)
        return "done"

    def _record_usage(self, action_id: str, result: TurnResult) -> dict:
        """Record this turn's usage in the matching pools, once per pool and
        action. Cost goes through the usage ledger, so a resumed session's
        cumulative figure becomes this turn's delta, or stays unknown."""
        cost = self._turn_cost(action_id, result)
        if cost is not None and cost < 0:
            log.warning("turn %s: cost delta %s is negative; recorded as unknown", action_id, cost)
            cost = None
        actuals: dict = {}
        tx = self.co.runtime.store.read()
        for row in tx.query("SELECT reservation_id, pool, metric FROM reservations WHERE action_id = ?", (action_id,)):
            quantity = 1 if row["metric"] == TURN_METRIC else cost
            quality = "observed" if row["metric"] == TURN_METRIC else ("estimated" if cost is not None else "unknown")
            try:
                self.pools.record_usage(
                    CONTROLLER, record_id=f"{action_id}:{row['pool']}", pool_id=row["pool"], metric=row["metric"],
                    quantity=None if quantity is None else str(quantity), quality=quality, source=f"{self.provider}.turn",
                    run_id=self.run_id, action_id=action_id, participant_id=self.participant_id,
                )
            except DomainError:
                log.exception("could not record usage for %s", row["pool"])
            actuals[row["reservation_id"]] = {"quantity": None if quantity is None else str(quantity), "quality": quality}
        return actuals

    def _turn_cost(self, action_id: str, result: TurnResult) -> Decimal | None:
        from ..usage.observations import ObservationError, from_turn_result

        try:
            observations = from_turn_result(result, provider=self.provider, turn_id=action_id, received_at_ms=int(time.time() * 1000), run_id=self.run_id)
            self.ledger.ingest_all(observations)
            # The run's total across this peer's sessions: a resume that
            # returns a new session id continues from its parent's level, so
            # per-session totals would undercount (the delta could go negative).
            metric = self.ledger.run_consumption(self.run_id).get(self.provider, COST_METRIC)
        except (ObservationError, DomainError, ValueError) as exc:
            log.warning("usage for %s could not be normalised: %s", action_id, exc)
            self._cost_total = None
            return None
        total = None if metric is None else metric.value
        previous, self._cost_total = self._cost_total, (Decimal(str(total)) if total is not None else None)
        if previous is None or self._cost_total is None:
            return None
        return self._cost_total - previous

    def _last_sent_seq(self) -> int:
        value = self.co.runtime.store.read().scalar(
            "SELECT MAX(seq) FROM messages WHERE run_id = ? AND sender = ?", (self.run_id, self.participant_id)
        )
        return int(value or 0)

    def _fallback_answers(self, messages: list[dict], result: TurnResult) -> None:
        """If the peer ended a turn without sending anything at all, deliver
        its final message as the answer to the questions it was given, clearly
        labelled, so the asker is not left waiting on a reply that will never
        come. A peer that replied, or asked a clarifying question instead of
        answering yet, is left alone."""
        if not result.text.strip():
            return
        tx = self.co.runtime.store.read()
        for message in messages:
            if message["kind"] != MessageKind.QUESTION.value:
                continue
            row = tx.get("messages", message["message_id"])
            if row is None or row["state"] in (MessageState.HANDLED.value, MessageState.EXPIRED.value, MessageState.CANCELLED.value):
                continue
            try:
                self.co.send(self.principal, kind="ANSWER", body=FALLBACK_PREFIX + result.text[-8000:], reply_to=message["message_id"])
            except Exception:
                log.exception("fallback answer failed")

    def _give_up(self, reason: str) -> None:
        try:
            part = self.co._participant(self.participant_id)
            if part["liveness"] != Liveness.GONE.value:
                self.co.runtime.update_participant(CONTROLLER, self.participant_id, liveness=Liveness.UNAVAILABLE)
                self.co._peer_lost(self.run_id, self.participant_id, reason)
        except Exception:
            log.exception("could not record that the %s peer stopped", self.provider)

    # -- prompt ----------------------------------------------------------------------------

    def _prompt(self, messages: list[dict], writer: bool, routing=None) -> str:
        run = self.co.runtime.get_run(CONTROLLER, self.run_id)
        settings = self.co.settings(self.run_id)
        checks = [" ".join(c.get("argv") or [c.get("shell", "")]) for c in run["acceptance"].get("checks", [])]
        lines = [
            participant_instructions().strip(),
            "",
            "## This session",
            f"You are the managed {self.provider} participant in DUET run {self.run_id} (origin: managed, started by DUET).",
            f"Role: {'writer' if writer else 'reviewer'}. Your peer is {other_provider(self.provider)}.",
            f"Objective: {run['objective']}",
            f"Acceptance: DUET runs {', '.join(checks) or '(no checks)'} on the submitted snapshot, and a {other_provider(self.provider)} review is required.",
        ]
        if writer:
            lines.append(f"Your workspace is {settings.workspace_path}. Call duet_claim, edit only there, then duet_submit.")
        else:
            lines.append("You cannot edit files. Review snapshots when asked and answer questions.")
        lines += [
            "",
            "Managed sessions end their turn instead of waiting for long: when a message arrives for you, DUET starts a new turn.",
            "Before ending this turn, reply to every question and review request below with duet_send and reply_to.",
        ]
        try:
            suggestions = self.co.graph.next_for(self.principal)[:3]
        except Exception:
            suggestions = []
        advice = ROUTING_ADVICE.get(getattr(routing, "action", "run"))
        if advice:
            lines += ["", "## DUET routing", advice, routing.explanation]
        if suggestions:
            lines += ["", "## DUET suggests (highest priority first)"]
            lines += [f"- {s['action']}" + (f" {s['task_id']}" if s.get("task_id") else "") + f": {s['reason']}" for s in suggestions]
        if messages:
            lines += ["", "## New DUET messages"]
            for m in messages:
                lines.append(f"[seq {m['seq']}] {m['kind']} from {m['from']} (message_id {m['message_id']}"
                             + (f", snapshot {m['snapshot_id']}" if m.get("snapshot_id") else "") + f"):\n{m['body']}")
        else:
            lines += ["", "No messages yet: start the task."]
        return "\n".join(lines)


def default_peer_factory(service: Any, run_id: str, provider: str) -> ManagedPeer:
    """Launch a real managed peer with the installed CLI and the user's
    existing login. Binary overrides exist for emulator tests only."""
    from ..providers.claude_cli import ClaudeCLIAdapter
    from ..providers.codex_appserver import CodexAppServerAdapter

    binary = os.environ.get("DUET_CLAUDE_BIN" if provider == "claude" else "DUET_CODEX_BIN", provider)
    if shutil.which(binary) is None:
        # Fail before registering: a peer that can never take a turn must not
        # appear in the run as if it were there.
        raise AgentError(f"{provider}: executable not found: {binary}", kind="not_found")
    if provider == "claude":
        adapter: ProviderAdapter = ClaudeCLIAdapter(binary, allowed_tools=("mcp__duet",), env={"DUET_MANAGED_PEER": "1"})
    else:
        adapter = CodexAppServerAdapter(binary, env={"DUET_MANAGED_PEER": "1"})
    peer = ManagedPeer(service.coordinator, run_id=run_id, provider=provider, adapter=adapter, state_root=service.paths.root)
    return peer.start()


def peer_summary(peer: ManagedPeer) -> dict:
    return {"participant_id": peer.participant_id, "provider": peer.provider, "turns": peer.turns, "session_id": peer.session_id, "alive": peer.alive}
