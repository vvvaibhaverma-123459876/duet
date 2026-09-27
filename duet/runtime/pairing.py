"""Pair coordination: the controller behind the peer tools (D05).

The coordinator turns participant tool calls into runtime commands. It is the
only component that acts as `controller`: it creates strict workspaces,
records snapshots, runs acceptance checks as durable actions, and decides
completion through the D03 gate. Participants never supply their own
identity, workspace path or authority; those come from the authenticated
principal and the run's recorded state.

Rules for this vertical slice (spec 5, D05):
- exactly two root participants, one Claude and one Codex;
- one writer per run, fixed at creation ("self" or "peer");
- sending never waits for an answer; `wait` wakes for questions *and*
  answers, so neither side can block the other (spec 5.3);
- a native session is labelled `native_original` with connection-bound
  identity and checkpoint delivery; nothing here claims push into an idle
  native session;
- a session already in a live run cannot start a second one, and a managed
  peer's credentials cannot create runs or peers (no recursive pairing)."""
from __future__ import annotations

import json
import os
import secrets
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..verification.acceptance import AcceptanceContract, CheckSpec, Criterion
from ..verification.completion import COMPLETED_VERIFIED, CompletionGate
from ..verification.evidence import EvidenceService
from ..verification.runner import run_check
from ..workspaces.manager import StrictWorkspace, WorkspaceManager
from ..workspaces.repo import resolve_repo
from ..workspaces.snapshots import Snapshot, capture_snapshot, materialize
from .api import Runtime
from .artifacts import ArtifactStore
from .contracts import (
    CONTROLLER,
    MAX_ID,
    MAX_TEXT,
    TERMINAL_RUN,
    USER,
    ActionState,
    Capabilities,
    Collaboration,
    Conflict,
    Containment,
    Control,
    DomainError,
    InvalidTransition,
    Liveness,
    MessageKind,
    MessageState,
    NativeIdentity,
    NotFound,
    Origin,
    PolicyDenied,
    Principal,
    Provider,
    Receive,
    RunLifecycle,
    TaskState,
    Unauthorized,
    UsageCapability,
    ValidationError,
    check_list,
    check_optional_text,
    check_text,
    parse_enum,
    parse_utc,
    utc_after,
)
from .identity import ProcessIdentity, hash_token
from .paths import ensure_private_dir
from .policy import AuthorisationPolicy

DEFAULT_WAIT_SECONDS = 25.0
# MCP clients time tool calls out (Codex defaults to 60 s); a wait returns
# well before that and the agent simply calls it again.
MAX_WAIT_SECONDS = 50.0
POLL_SECONDS = 0.5
INVITE_TTL_SECONDS = 3600
MAX_TASKS_PER_RUN = 32
MAX_CHECKS = 8
MAX_DIFF_CHARS = 24_000
OPEN_STATES = (MessageState.QUEUED.value, MessageState.TRANSPORT_DELIVERED.value, MessageState.PARTICIPANT_ACKNOWLEDGED.value)
REQUEST_KINDS = (MessageKind.QUESTION.value, MessageKind.REVIEW_REQUEST.value)
WRITER_CHOICES = ("self", "peer")
PEER_CHOICES = ("managed", "invite")

NATIVE_CAPABILITIES = Capabilities(
    receive=Receive.CHECKPOINT,
    control_model=Control.ADVISORY,
    control_effort=Control.ADVISORY,
    usage=UsageCapability.UNKNOWN,
    containment=Containment.COOPERATIVE,
    native_identity=NativeIdentity.CONNECTION_BOUND,
)


def managed_capabilities(provider: str) -> Capabilities:
    """What DUET controls for a session it launched. Codex's sandbox is
    requested but has not been verified by a containment test here."""
    return Capabilities(
        receive=Receive.PUSH,
        control_model=Control.SUPPORTED,
        control_effort=Control.SUPPORTED,
        usage=UsageCapability.STRUCTURED if provider == Provider.CLAUDE.value else UsageCapability.PARTIAL,
        containment=Containment.COOPERATIVE if provider == Provider.CLAUDE.value else Containment.UNVERIFIED,
        native_identity=NativeIdentity.VERIFIED,
    )


DELIVERY_NOTES = {
    Origin.NATIVE_ORIGINAL.value: (
        "checkpoint delivery: messages reach this session when it calls duet_wait or duet_inbox. "
        "DUET cannot push into an idle native session or change its model mid-turn."
    ),
    Origin.MANAGED.value: (
        "DUET starts a turn when a message arrives; during a turn the peer sees new messages at its next duet_wait."
    ),
}


def native_capabilities(host: str | None) -> Capabilities:
    """Without a host claim nothing binds the participant to a session, so
    its identity is unverified rather than connection-bound."""
    if host:
        return NATIVE_CAPABILITIES
    return Capabilities(**{**NATIVE_CAPABILITIES.__dict__, "native_identity": NativeIdentity.UNVERIFIED})


def other_provider(provider: str) -> str:
    return Provider.CODEX.value if provider == Provider.CLAUDE.value else Provider.CLAUDE.value


@dataclass
class PairSettings:
    """Non-secret per-run pairing record, kept beside the database so a
    restarted service can find the run's workspace again."""

    run_id: str
    repo_path: str
    workspace_path: str
    branch: str
    base_sha: str
    writer_role: str
    peer_mode: str
    writer_provider: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


PeerLauncher = Callable[[str, str], dict]  # (run_id, provider) -> participant view
PeerStopper = Callable[[str], None]  # (run_id)


class PairCoordinator:
    def __init__(
        self,
        runtime: Runtime,
        artifacts: ArtifactStore,
        *,
        state_root: Path,
        policy: AuthorisationPolicy | None = None,
        peer_launcher: PeerLauncher | None = None,
        peer_stopper: PeerStopper | None = None,
        check_env: dict[str, str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.artifacts = artifacts
        self.evidence = EvidenceService(runtime, artifacts)
        self.gate = CompletionGate(runtime, self.evidence, artifacts)
        self.state_root = ensure_private_dir(Path(state_root))
        self.workspaces = WorkspaceManager(runtime, root=self.state_root / "worktrees")
        self.policy = policy or AuthorisationPolicy()
        self.peer_launcher = peer_launcher
        self.peer_stopper = peer_stopper
        self.check_env = check_env
        self._lock = threading.RLock()
        self._run_locks: dict[str, threading.Lock] = {}
        self._cond = threading.Condition()
        self._generation = 0
        self._workspaces: dict[str, StrictWorkspace] = {}
        self._snapshots: dict[str, Snapshot] = {}
        self._checks_running: dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------ notification

    def notify(self) -> None:
        """Wake every waiter: something committed."""
        with self._cond:
            self._generation += 1
            self._cond.notify_all()

    def _run_lock(self, run_id: str) -> threading.Lock:
        with self._lock:
            return self._run_locks.setdefault(run_id, threading.Lock())

    # ------------------------------------------------------------------ join

    def join(
        self,
        principal: Principal | None,
        *,
        provider: str,
        objective: str | None = None,
        repo: str | None = None,
        checks: list | None = None,
        protected: list[str] | None = None,
        peer: str = "managed",
        writer: str = "self",
        run_id: str | None = None,
        invite: str | None = None,
        host: str | None = None,
        native_session_id: str | None = None,
        client: dict | None = None,
    ) -> dict:
        """Register the calling native session. Joining again from an
        authenticated connection returns the existing registration; it never
        creates a second participant or a second run."""
        provider = parse_enum(Provider, provider, "provider").value
        if principal is not None:
            if not principal.is_participant:
                raise Unauthorized("join is for participants")
            if principal.provider != provider:
                raise ValidationError(f"this connection is the {principal.provider} participant, not {provider}")
            return {"rejoined": True, **self.view_for(principal)}
        _check_client(provider, client)
        check_optional_text(host, "host", limit=MAX_ID)
        check_optional_text(native_session_id, "native_session_id", limit=MAX_ID)
        if host:
            self._refuse_second_run(host)
        if run_id is not None or invite is not None:
            if not (run_id and invite):
                raise ValidationError("joining an existing run needs both run_id and invite")
            return self._join_with_invite(run_id, invite, provider, host=host, native_session_id=native_session_id)
        return self._create_run(
            provider, objective=objective, repo=repo, checks=checks, protected=protected, peer=peer, writer=writer,
            host=host, native_session_id=native_session_id,
        )

    def _refuse_second_run(self, host: str) -> None:
        tx = self.runtime.store.read()
        rows = tx.query(
            "SELECT p.participant_id, p.run_id, r.lifecycle FROM participants p JOIN runs r ON r.run_id = p.run_id "
            "WHERE p.connection_id = ? AND p.liveness != ?",
            (host_key(host), Liveness.GONE.value),
        )
        live = [r for r in rows if RunLifecycle(r["lifecycle"]) not in TERMINAL_RUN]
        if live:
            raise Conflict(
                f"this session already participates in run {live[0]['run_id']}; finish or stop it before starting another",
                details={"run_id": live[0]["run_id"], "participant_id": live[0]["participant_id"]},
            )

    def _create_run(
        self,
        provider: str,
        *,
        objective: str | None,
        repo: str | None,
        checks: list | None,
        protected: list[str] | None,
        peer: str,
        writer: str,
        host: str | None,
        native_session_id: str | None,
    ) -> dict:
        objective = check_text(objective, "objective", limit=MAX_TEXT)
        if peer not in PEER_CHOICES:
            raise ValidationError(f"peer must be one of {PEER_CHOICES}")
        if writer not in WRITER_CHOICES:
            raise ValidationError(f"writer must be one of {WRITER_CHOICES}")
        if peer == "managed" and self.peer_launcher is None:
            raise PolicyDenied("this DUET service cannot start managed peers; use peer='invite'")
        contract = contract_from_checks(objective, checks, protected)
        if not repo:
            raise ValidationError("repo (the repository path) is required")
        identity = resolve_repo(repo)
        run = self.runtime.create_run(
            USER,
            repo_id=identity.repo_id,
            objective=objective,
            policy=self.policy,
            acceptance=contract.to_dict(),
            scope={"repo_path": str(identity.toplevel), "peer_mode": peer, "writer": writer, "acceptance_source": "initiator_proposed"},
            base_sha=identity.head,
            deadline_at=utc_after(self.policy.deadline_seconds),
        )
        run_id = run["run_id"]
        self.runtime.transition_run(CONTROLLER, run_id, RunLifecycle.PREFLIGHT, reason="pair requested by a native session")
        try:
            ws = self.workspaces.create(run_id, identity.toplevel)
        except Exception as exc:
            self.runtime.transition_run(CONTROLLER, run_id, RunLifecycle.FAILED, reason=f"workspace: {exc}"[:500])
            if isinstance(exc, DomainError):
                raise
            raise ValidationError(f"could not create the strict workspace: {exc}") from exc
        self._remember_workspace(run_id, ws, writer_role=writer, peer_mode=peer, writer_provider=provider if writer == "self" else other_provider(provider))
        self.runtime.transition_run(CONTROLLER, run_id, RunLifecycle.PLANNING, reason="strict workspace ready")
        registered = self.runtime.register_participant(
            CONTROLLER, run_id, provider=provider, origin=Origin.NATIVE_ORIGINAL, capabilities=native_capabilities(host),
            native_session_id=native_session_id, connection_id=host_key(host) if host else None,
            workspace=str(ws.path) if writer == "self" else None, initiator=True,
        )
        me = registered["participant"]
        self.runtime.propose_task(
            CONTROLLER, run_id=run_id, description=objective, acceptance_ids=[c.id for c in contract.criteria], required=True,
        )
        if writer == "self":
            self.workspaces.acquire_writer(ws, me["participant_id"])
        result: dict[str, Any] = {"token": registered["token"]}
        if peer == "managed":
            try:
                self.peer_launcher(run_id, other_provider(provider))  # type: ignore[misc]
            except Exception as exc:
                # The caller never receives this run's token, so the run must
                # not stay live (it would also block the session from retrying).
                self.cancel(run_id, reason=f"the managed {other_provider(provider)} peer could not start: {exc}"[:500], principal=CONTROLLER)
                raise PolicyDenied(f"the managed {other_provider(provider)} peer could not start: {exc}") from exc
            self._pair_formed(run_id)
        else:
            code = "duet_inv_" + secrets.token_urlsafe(18)
            with self.runtime.store.transaction() as tx:
                tx.emit(
                    self.runtime._event(
                        "invite.created",
                        {
                            "invite_id": "inv_" + secrets.token_hex(12), "run_id": run_id, "provider": other_provider(provider),
                            "code_hash": hash_token(code), "created_by": me["participant_id"],
                            "expires_at": utc_after(INVITE_TTL_SECONDS),
                        },
                        CONTROLLER,
                        run_id,
                    )
                )
            self.runtime.set_collaboration(CONTROLLER, run_id, Collaboration.PEER_UNAVAILABLE, reason="waiting for the invited peer")
            result["invite"] = {
                "code": code, "for_provider": other_provider(provider), "run_id": run_id, "expires_in_seconds": INVITE_TTL_SECONDS,
                "how": f"Ask the user to tell their {other_provider(provider)} session: join DUET run {run_id} with invite {code}",
            }
        self.notify()
        principal = Principal("participant", me["participant_id"], run_id=run_id, provider=provider)
        return {**result, **self.view_for(principal)}

    def _join_with_invite(self, run_id: str, invite: str, provider: str, *, host: str | None, native_session_id: str | None) -> dict:
        check_text(run_id, "run_id", limit=MAX_ID)
        check_text(invite, "invite", limit=MAX_ID)
        tx = self.runtime.store.read()
        rows = tx.query("SELECT * FROM invites WHERE code_hash = ?", (hash_token(invite),))
        if not rows or rows[0]["run_id"] != run_id:
            raise Unauthorized("unknown invite for this run")
        inv = dict(rows[0])
        if inv["used_by"] is not None:
            raise Unauthorized("this invite was already used")
        if parse_utc(inv["expires_at"]).timestamp() <= time.time():
            raise Unauthorized("this invite has expired")
        if inv["provider"] != provider:
            raise Unauthorized(f"this invite is for a {inv['provider']} session")
        settings = self.settings(run_id)
        writer = settings.writer_provider == provider
        registered = self.runtime.register_participant(
            CONTROLLER, run_id, provider=provider, origin=Origin.NATIVE_ORIGINAL, capabilities=native_capabilities(host),
            native_session_id=native_session_id, connection_id=host_key(host) if host else None,
            workspace=settings.workspace_path if writer else None,
        )
        me = registered["participant"]
        with self.runtime.store.transaction() as wtx:
            wtx.emit(self.runtime._event("invite.used", {"invite_id": inv["invite_id"], "participant_id": me["participant_id"]}, CONTROLLER, run_id))
        if writer:
            self.workspaces.acquire_writer(self.workspace(run_id), me["participant_id"])
        self._pair_formed(run_id)
        principal = Principal("participant", me["participant_id"], run_id=run_id, provider=provider)
        return {"token": registered["token"], **self.view_for(principal)}

    def register_managed(self, run_id: str, provider: str, *, native_session_id: str | None = None) -> dict:
        """Called by the peer launcher: register the DUET-launched session."""
        settings = self.settings(run_id)
        writer = settings.writer_provider == provider
        run = self.runtime.get_run(CONTROLLER, run_id)
        registered = self.runtime.register_participant(
            CONTROLLER, run_id, provider=provider, origin=Origin.MANAGED, capabilities=managed_capabilities(provider),
            native_session_id=native_session_id, workspace=settings.workspace_path if writer else None,
            initiator=run["initiating_participant"] is None,
        )
        if writer:
            self.workspaces.acquire_writer(self.workspace(run_id), registered["participant"]["participant_id"])
        return registered

    def create_managed_pair(self, *, objective: str, repo: str, checks: list, protected: list[str] | None = None, writer: str = "claude") -> dict:
        """DUET-originated pair (`duet pair`): both sessions are launched and
        labelled `managed`. This is the strongest-control mode; it does not
        stand in for the native-origin tests."""
        if self.peer_launcher is None:
            raise PolicyDenied("this DUET service cannot start managed peers")
        writer = parse_enum(Provider, writer, "writer").value
        objective = check_text(objective, "objective", limit=MAX_TEXT)
        contract = contract_from_checks(objective, checks, protected)
        identity = resolve_repo(check_text(repo, "repo", limit=4096))
        run = self.runtime.create_run(
            USER, repo_id=identity.repo_id, objective=objective, policy=self.policy, acceptance=contract.to_dict(),
            scope={"repo_path": str(identity.toplevel), "peer_mode": "managed_pair", "writer": writer, "acceptance_source": "user"},
            base_sha=identity.head, deadline_at=utc_after(self.policy.deadline_seconds),
        )
        run_id = run["run_id"]
        self.runtime.transition_run(CONTROLLER, run_id, RunLifecycle.PREFLIGHT, reason="duet pair")
        try:
            ws = self.workspaces.create(run_id, identity.toplevel)
        except Exception as exc:
            self.runtime.transition_run(CONTROLLER, run_id, RunLifecycle.FAILED, reason=f"workspace: {exc}"[:500])
            if isinstance(exc, DomainError):
                raise
            raise ValidationError(f"could not create the strict workspace: {exc}") from exc
        self._remember_workspace(run_id, ws, writer_role="self", peer_mode="managed_pair", writer_provider=writer)
        self.runtime.transition_run(CONTROLLER, run_id, RunLifecycle.PLANNING, reason="strict workspace ready")
        self.runtime.propose_task(CONTROLLER, run_id=run_id, description=objective, acceptance_ids=[c.id for c in contract.criteria], required=True)
        try:
            for provider in (writer, other_provider(writer)):
                self.peer_launcher(run_id, provider)
        except Exception as exc:
            self.cancel(run_id, reason=f"a managed peer could not start: {exc}", principal=CONTROLLER)
            raise PolicyDenied(f"a managed peer could not start: {exc}") from exc
        self._pair_formed(run_id)
        return self.run_status(run_id)

    def _pair_formed(self, run_id: str) -> None:
        run = self.runtime.get_run(CONTROLLER, run_id)
        if run["lifecycle"] == RunLifecycle.PLANNING.value:
            self.runtime.transition_run(CONTROLLER, run_id, RunLifecycle.EXECUTING, reason="both participants joined")
        self.runtime.set_collaboration(CONTROLLER, run_id, Collaboration.PAIR_ACTIVE, reason="both participants joined")
        for part in self.runtime.participants(CONTROLLER, run_id):
            peer = self._peer_of(run_id, part["participant_id"])
            if peer is not None:
                self._status(run_id, part["participant_id"], f"Pair formed: your peer is {peer['provider']} ({peer['origin']}).")
        self.notify()

    # ------------------------------------------------------------------ views

    def view_for(self, principal: Principal) -> dict:
        run_id = principal.run_id or ""
        run = self.runtime.get_run(CONTROLLER, run_id)
        me = self._participant(principal.id)
        peer = self._peer_of(run_id, principal.id)
        settings = self.settings(run_id)
        is_writer = self._writer_id(run_id) == principal.id
        return {
            "run_id": run_id,
            "participant_id": principal.id,
            "provider": principal.provider,
            "origin": me["origin"],
            "role": "writer" if is_writer else "reviewer",
            "objective": run["objective"],
            "lifecycle": run["lifecycle"],
            "collaboration": run["collaboration"],
            "workspace": settings.workspace_path if is_writer else None,
            "branch": settings.branch,
            "task_id": self._main_task_id(run_id),
            "acceptance": run["acceptance"],
            "peer": _peer_view(peer),
            "capabilities": me["capabilities"],
            "delivery": DELIVERY_NOTES.get(me["origin"], ""),
        }

    def status(self, principal: Principal) -> dict:
        run_id = principal.run_id or ""
        return self.run_status(run_id, viewer=principal.id)

    def run_status(self, run_id: str, *, viewer: str | None = None) -> dict:
        run = self.runtime.get_run(CONTROLLER, run_id)
        tx = self.runtime.store.read()
        participants = [
            {
                "participant_id": p["participant_id"], "provider": p["provider"], "origin": p["origin"], "liveness": p["liveness"],
                "capabilities": p["capabilities"], "role": "writer" if p["participant_id"] == self._writer_id(run_id) else "reviewer",
                "delivery": DELIVERY_NOTES.get(p["origin"], ""),
            }
            for p in self.runtime.participants(CONTROLLER, run_id)
        ]
        tasks = [
            {k: t[k] for k in ("task_id", "description", "state", "owner", "required", "revision", "proposed_by")}
            for t in self.runtime.tasks(CONTROLLER, run_id)
        ]
        latest = self._latest_snapshot(run_id)
        verification: dict[str, Any] = {"snapshot_id": None}
        if latest is not None:
            evidence = self.evidence.evidence_for(run_id, latest["snapshot_id"], run["acceptance_hash"])
            reviews = self.evidence.reviews_for(run_id, latest["snapshot_id"], run["acceptance_hash"])
            report = self.gate.evaluate(run_id, latest["snapshot_id"])
            verification = {
                "snapshot_id": latest["snapshot_id"],
                "changed": json.loads(latest["changed_json"]),
                "checks_running": latest["snapshot_id"] in self._checks_running,
                "checks": {k: {"status": v["status"], "detail": v["detail"]} for k, v in evidence.items()},
                "reviews": [{"reviewer_provider": r["reviewer_provider"], "disposition": r["disposition"], "summary": r["summary"]} for r in reviews],
                "completion": report.to_dict(),
            }
        open_requests = [
            {"message_id": m["message_id"], "kind": m["kind"], "from": m["sender"], "to": m["recipient"], "state": m["state"]}
            for m in tx.query(
                f"SELECT * FROM messages WHERE run_id = ? AND kind IN ({','.join('?' * len(REQUEST_KINDS))}) "
                f"AND state IN ({','.join('?' * len(OPEN_STATES))}) ORDER BY seq",
                (run_id, *REQUEST_KINDS, *OPEN_STATES),
            )
        ]
        settings = self.settings(run_id)
        return {
            "schema": "duet.pair-status/1",
            "run_id": run_id,
            "objective": run["objective"],
            "lifecycle": run["lifecycle"],
            "collaboration": run["collaboration"],
            "repo": settings.repo_path,
            "workspace": settings.workspace_path,
            "branch": settings.branch,
            "participants": participants,
            "tasks": tasks,
            "open_requests": open_requests,
            "verification": verification,
            "profile": "fixed (peer-alpha): no adaptive model or effort routing",
            "control_coverage": (
                "DUET bounds the turns, checks and messages it schedules. Work a native session does outside these "
                "tools, and anything else run under the same account, is not controlled or counted."
            ),
            "viewer": viewer,
        }

    # ------------------------------------------------------------------ messaging

    def send(
        self,
        principal: Principal,
        *,
        kind: str,
        body: str,
        to: str | None = None,
        reply_to: str | None = None,
        task_id: str | None = None,
        snapshot_id: str | None = None,
        review: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        self._require_participant(principal)
        kind_value = parse_enum(MessageKind, kind, "kind").value
        recipient = to or self._peer_id(principal)
        if kind_value == MessageKind.REVIEW_RESULT.value:
            return self._send_review(principal, body=body, to=recipient, reply_to=reply_to, task_id=task_id, snapshot_id=snapshot_id, review=review, idempotency_key=idempotency_key)
        if review is not None:
            raise ValidationError("review is only valid with kind REVIEW_RESULT")
        if kind_value == MessageKind.REVIEW_REQUEST.value:
            raise ValidationError("use duet_request_review to request a review of a snapshot")
        receipt = self.runtime.send_message(
            principal, kind=kind_value, body=body, recipient=recipient, reply_to=reply_to, task_id=task_id,
            snapshot_ref=snapshot_id, idempotency_key=idempotency_key,
        )
        self.notify()
        return {**receipt, "note": "sent; this does not wait for an answer. Keep working or call duet_wait."}

    def _send_review(
        self, principal: Principal, *, body: str, to: str, reply_to: str | None, task_id: str | None,
        snapshot_id: str | None, review: dict | None, idempotency_key: str | None,
    ) -> dict:
        if not isinstance(review, dict):
            raise ValidationError("REVIEW_RESULT needs review={disposition, findings?, resolves?}")
        unknown = set(review) - {"snapshot_id", "disposition", "summary", "findings", "resolves", "scope"}
        if unknown:
            raise ValidationError(f"unknown review fields {sorted(unknown)}")
        snap = review.get("snapshot_id") or snapshot_id
        if snap is None and reply_to is not None:
            parent = self.runtime.store.read().get("messages", reply_to)
            snap = parent["snapshot_ref"] if parent else None
        if not snap:
            raise ValidationError("which snapshot is this review for? pass review.snapshot_id or reply_to the review request")
        resolves = check_list(review.get("resolves"), "review.resolves", item_limit=MAX_ID)
        recorded = self.evidence.submit_review(
            principal, snap, disposition=review.get("disposition", ""), summary=review.get("summary") or body,
            scope=review.get("scope"), findings=review.get("findings"),
        )
        for finding_id in resolves:
            self.evidence.resolve_finding(principal, finding_id, resolution=f"verified fixed in {snap}")
        receipt = self.runtime.send_message(
            principal, kind=MessageKind.REVIEW_RESULT, body=body, recipient=to, reply_to=reply_to, task_id=task_id,
            snapshot_ref=snap, idempotency_key=idempotency_key,
        )
        self.notify()
        run_id = principal.run_id or ""
        if recorded["disposition"] == "changes_requested":
            self._changes_requested(run_id, snap, f"{principal.provider} requested changes: {(review.get('summary') or body)[:500]}")
        self.try_complete(run_id)
        return {**receipt, "review_id": recorded["review_id"], "findings": recorded["findings"]}

    def inbox(self, principal: Principal, *, ack_through: int | None = None, since: int | None = None, limit: int = 50) -> dict:
        self._require_participant(principal)
        if ack_through is not None:
            self.runtime.ack(principal, up_to_seq=ack_through)
        box = self.runtime.read_inbox(principal, after=since, limit=limit)
        return {
            "cursor": box["cursor"], "messages": [_message_view(m) for m in box["messages"]],
            "pending_requests": self._pending_requests(principal),
        }

    def wait(
        self,
        principal: Principal,
        *,
        since: int | None = None,
        watch: str | None = None,
        timeout: float = DEFAULT_WAIT_SECONDS,
        ack_through: int | None = None,
        cancel: threading.Event | None = None,
    ) -> dict:
        """Bounded, event-driven wait. Returns as soon as there is a new
        message for the caller (question, answer, review, status), the run or
        peer state changes, or the timeout passes. Holds no transaction."""
        self._require_participant(principal)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValidationError("timeout must be a number of seconds")
        timeout = max(0.0, min(float(timeout), MAX_WAIT_SECONDS))
        if ack_through is not None:
            self.runtime.ack(principal, up_to_seq=ack_through)
        floor = self._cursor(principal) if since is None else max(int(since), 0)
        deadline = time.monotonic() + timeout
        while True:
            with self._cond:
                generation = self._generation
            state = self._watch_state(principal)
            newest = self._newest_for(principal, floor)
            changed = watch is not None and watch != state["watch"]
            terminal = RunLifecycle(state["lifecycle"]) in TERMINAL_RUN or state["lifecycle"].startswith("PAUSED_")
            if newest > floor or changed or terminal or (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                break
            with self._cond:
                if self._generation == generation:
                    self._cond.wait(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        messages: list[dict] = []
        if newest > floor:
            box = self.runtime.read_inbox(principal, after=floor, limit=50)
            messages = [_message_view(m) for m in box["messages"] if m["seq"] > floor]
        last_seq = max([floor, *[m["seq"] for m in messages]])
        woke = "messages" if messages else ("run_ended" if terminal else ("state_changed" if changed else ("cancelled" if cancel is not None and cancel.is_set() else "timeout")))
        return {
            "woke_for": woke,
            "messages": messages,
            "last_seq": last_seq,
            "pending_requests": self._pending_requests(principal),
            "awaiting_replies": self._awaiting(principal),
            "run": {"lifecycle": state["lifecycle"], "collaboration": state["collaboration"]},
            "peer": state["peer"],
            "watch": state["watch"],
            "advice": _wait_advice(woke, messages, state),
        }

    def _cursor(self, principal: Principal) -> int:
        row = self.runtime.store.read().get("inbox_cursors", principal.id)
        return int(row["acked_seq"]) if row else 0

    def _newest_for(self, principal: Principal, floor: int) -> int:
        value = self.runtime.store.read().scalar(
            "SELECT MAX(seq) FROM messages WHERE run_id = ? AND seq > ? AND sender != ? AND (recipient = ? OR recipient IS NULL)",
            (principal.run_id, floor, principal.id, principal.id),
        )
        return int(value or 0)

    def _watch_state(self, principal: Principal) -> dict:
        tx = self.runtime.store.read()
        run = tx.require("runs", principal.run_id or "")
        peer = self._peer_of(principal.run_id or "", principal.id)
        liveness = peer["liveness"] if peer else "absent"
        return {
            "lifecycle": run["lifecycle"], "collaboration": run["collaboration"],
            "peer": _peer_view(peer),
            "watch": f"{run['lifecycle']}|{run['collaboration']}|{liveness}",
        }

    def _pending_requests(self, principal: Principal) -> list[dict]:
        rows = self.runtime.store.read().query(
            f"SELECT message_id, kind, seq, snapshot_ref FROM messages WHERE run_id = ? AND recipient = ? "
            f"AND kind IN ({','.join('?' * len(REQUEST_KINDS))}) AND state IN ({','.join('?' * len(OPEN_STATES))}) ORDER BY seq",
            (principal.run_id, principal.id, *REQUEST_KINDS, *OPEN_STATES),
        )
        return [{"message_id": r["message_id"], "kind": r["kind"], "seq": r["seq"], "snapshot_id": r["snapshot_ref"]} for r in rows]

    def _awaiting(self, principal: Principal) -> list[dict]:
        rows = self.runtime.store.read().query(
            f"SELECT message_id, kind, seq FROM messages WHERE run_id = ? AND sender = ? "
            f"AND kind IN ({','.join('?' * len(REQUEST_KINDS))}) AND state IN ({','.join('?' * len(OPEN_STATES))}) ORDER BY seq",
            (principal.run_id, principal.id, *REQUEST_KINDS, *OPEN_STATES),
        )
        return [{"message_id": r["message_id"], "kind": r["kind"], "seq": r["seq"]} for r in rows]

    # ------------------------------------------------------------------ tasks

    def propose_task(self, principal: Principal, *, description: str, depends_on: list[str] | None = None, acceptance_ids: list[str] | None = None) -> dict:
        self._require_participant(principal)
        count = self.runtime.store.read().scalar("SELECT COUNT(*) FROM tasks WHERE run_id = ?", (principal.run_id,)) or 0
        if count >= MAX_TASKS_PER_RUN:
            raise PolicyDenied(f"a run holds at most {MAX_TASKS_PER_RUN} tasks")
        task = self.runtime.propose_task(principal, description=description, depends_on=depends_on, acceptance_ids=acceptance_ids)
        task = self.runtime.transition_task(CONTROLLER, task["task_id"], TaskState.READY, expected_version=task["state_version"], reason="accepted by the controller")
        self.notify()
        return _task_view(task)

    def claim(self, principal: Principal, *, task_id: str | None = None) -> dict:
        self._require_participant(principal)
        run_id = principal.run_id or ""
        writer = self._writer_id(run_id)
        if writer != principal.id:
            raise PolicyDenied("this run has one writer and it is your peer; review their snapshots instead of editing")
        task_id = task_id or self._main_task_id(run_id)
        task = self._task(task_id, run_id)
        if task["state"] in (TaskState.CLAIMED.value, TaskState.RUNNING.value) and task["owner"] == principal.id:
            claimed = task
        else:
            claimed = self.runtime.claim_task(principal, task_id, expected_version=task["state_version"])["task"]
        if claimed["state"] == TaskState.CLAIMED.value:
            claimed = self.runtime.transition_task(
                principal, task_id, TaskState.RUNNING, expected_version=claimed["state_version"], fence=self._task_fence(task_id, principal.id), reason="work started"
            )
        run = self.runtime.get_run(CONTROLLER, run_id)
        if run["lifecycle"] == RunLifecycle.REVIEWING.value:
            self.runtime.transition_run(CONTROLLER, run_id, RunLifecycle.REPAIRING, reason="writer resumed work")
        self.notify()
        settings = self.settings(run_id)
        return {
            "task": _task_view(claimed),
            "workspace": settings.workspace_path,
            "base_sha": settings.base_sha,
            "branch": settings.branch,
            "rules": (
                f"Edit files only under {settings.workspace_path}; the user's checkout is not the workspace. "
                "Call duet_submit when the change is ready; do not edit while checks and review run."
            ),
        }

    def submit(self, principal: Principal, *, task_id: str | None = None, summary: str = "", request_review: bool = True, note: str = "") -> dict:
        self._require_participant(principal)
        run_id = principal.run_id or ""
        check_text(summary, "summary", limit=MAX_TEXT, allow_empty=True)
        task_id = task_id or self._main_task_id(run_id)
        task = self._task(task_id, run_id)
        if task["owner"] != principal.id or task["state"] != TaskState.RUNNING.value:
            raise InvalidTransition(f"task {task_id} is {task['state']}; claim it with duet_claim before submitting")
        ws = self.workspace(run_id)
        self.workspaces.check_writer(ws, principal.id, self._writer_fence(ws, principal.id))
        snap = capture_snapshot(ws.path, base_sha=ws.base_sha, store=self.artifacts)
        row = self.evidence.record_snapshot(CONTROLLER, run_id, snap, author=principal.id)
        snapshot_id = row["snapshot_id"]
        with self._lock:
            self._snapshots[snapshot_id] = snap
        self.runtime.transition_task(
            principal, task_id, TaskState.REVIEW_REQUIRED, expected_version=task["state_version"],
            fence=self._task_fence(task_id, principal.id), reason=summary or "submitted for review",
        )
        run = self.runtime.get_run(CONTROLLER, run_id)
        if run["lifecycle"] in (RunLifecycle.EXECUTING.value, RunLifecycle.REPAIRING.value):
            self.runtime.transition_run(CONTROLLER, run_id, RunLifecycle.REVIEWING, reason=f"{snapshot_id} submitted")
        self._start_checks(run_id, snapshot_id, task_id)
        result: dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "changed": list(snap.changed),
            "excluded": list(snap.excluded),
            "checks": "running; results arrive as a STATUS message",
        }
        if not snap.changed:
            result["warning"] = "the snapshot has no changes against the base commit"
        if request_review:
            result["review_request"] = self.request_review(principal, snapshot_id=snapshot_id, note=note or summary)
        self.notify()
        return result

    def request_review(self, principal: Principal, *, snapshot_id: str | None = None, note: str = "", criteria: list[str] | None = None) -> dict:
        self._require_participant(principal)
        run_id = principal.run_id or ""
        check_text(note, "note", limit=MAX_TEXT, allow_empty=True)
        criteria = check_list(criteria, "criteria", item_limit=MAX_ID)
        snap_row = self.evidence.snapshot(snapshot_id) if snapshot_id else self._latest_snapshot(run_id)
        if snap_row is None or snap_row["run_id"] != run_id:
            raise NotFound("no submitted snapshot to review; call duet_submit first")
        snapshot_id = snap_row["snapshot_id"]
        reviewer = self._peer_id(principal)
        path = self.materialized(snapshot_id)
        diff = self._diff_text(run_id, snap_row)
        diff_ref = self.artifacts.put_text(diff)
        run = self.runtime.get_run(CONTROLLER, run_id)
        wanted = criteria or [c["id"] for c in run["acceptance"].get("criteria", [])]
        shown = diff if len(diff) <= MAX_DIFF_CHARS else diff[:MAX_DIFF_CHARS] + f"\n... diff truncated ({len(diff)} chars); read the snapshot files for the rest\n"
        body = (
            f"Please review snapshot {snapshot_id} against criteria {', '.join(wanted)}.\n"
            f"Objective: {run['objective']}\n"
            f"Changed files: {', '.join(json.loads(snap_row['changed_json'])) or '(none)'}\n"
            f"Read-only copy of the snapshot: {path}\n"
            f"Checks: run by DUET on this exact snapshot; see duet_status.\n"
            + (f"Author's note: {note}\n" if note else "")
            + "Reply with duet_send(kind='REVIEW_RESULT', reply_to=<this message id>, review={disposition: approve|changes_requested|comment, "
            "findings: [{severity: blocking|non_blocking, summary, location}]}).\n\n"
            f"Diff against the base commit:\n{shown}"
        )
        receipt = self.runtime.send_message(
            principal, kind=MessageKind.REVIEW_REQUEST, body=body[:MAX_TEXT], recipient=reviewer, snapshot_ref=snapshot_id,
            artifact_refs=[diff_ref], task_id=self._main_task_id(run_id),
        )
        self.notify()
        return {**receipt, "snapshot_id": snapshot_id, "reviewer": reviewer}

    def request_profile(self, principal: Principal, *, model: str | None = None, effort: str | None = None, reason: str = "") -> dict:
        self._require_participant(principal)
        check_optional_text(model, "model", limit=MAX_ID)
        check_optional_text(effort, "effort", limit=64)
        check_text(reason, "reason", limit=MAX_TEXT, allow_empty=True)
        return {
            "decision": "declined",
            "requested": {"model": model, "effort": effort},
            "reason": "peer-alpha runs fixed, supported profiles; evidence-based model and effort routing is not implemented yet",
            "applied": None,
        }

    # ------------------------------------------------------------------ checks and completion

    def _start_checks(self, run_id: str, snapshot_id: str, task_id: str) -> None:
        run = self.runtime.get_run(CONTROLLER, run_id)
        contract = AcceptanceContract.from_dict(run["acceptance"])
        planned = []
        for check_id in contract.required_checks():
            spec = contract.check(check_id)
            planned.append(
                self.runtime.plan_action(
                    CONTROLLER, run_id=run_id, type="check", task_id=task_id,
                    input={"snapshot_id": snapshot_id, "check_id": check_id, "argv": spec.command},
                )["action"]["action_id"]
            )
        thread = threading.Thread(target=self._run_checks, args=(run_id, snapshot_id, contract, planned), name=f"duet-checks-{snapshot_id[:12]}", daemon=True)
        with self._lock:
            self._checks_running[snapshot_id] = thread
        thread.start()

    def _run_checks(self, run_id: str, snapshot_id: str, contract: AcceptanceContract, action_ids: list[str]) -> None:
        lines = []
        try:
            ws = self.workspace(run_id)
            snap = self._snapshots.get(snapshot_id)
            for action_id, check_id in zip(action_ids, contract.required_checks()):
                claimed = self.runtime.claim_action(CONTROLLER, action_id)
                fence = claimed["lease"]["fencing_token"]
                self.runtime.record_action(CONTROLLER, action_id, ActionState.RUNNING, fence=fence)
                try:
                    outcome = run_check(contract.check(check_id), ws.path, snapshot_before=snap, parent_env=self.check_env)
                    self.evidence.record_check(CONTROLLER, run_id, snapshot_id, contract, outcome)
                except Exception as exc:  # the outcome is recorded, never lost silently
                    self.runtime.record_action(CONTROLLER, action_id, ActionState.FAILED, fence=fence, result={"error": str(exc)[:2000]})
                    lines.append(f"{check_id}: error ({exc})")
                    continue
                self.runtime.record_action(CONTROLLER, action_id, ActionState.SUCCEEDED, fence=fence, result={"status": outcome.status, "exit_code": outcome.exit_code})
                lines.append(f"{check_id}: {outcome.status}" + (f" ({outcome.detail})" if outcome.status != "passed" and outcome.detail else ""))
        except DomainError as exc:
            lines.append(f"checks stopped: {exc.message}")
        finally:
            with self._lock:
                self._checks_running.pop(snapshot_id, None)
        passed = bool(lines) and all(line.endswith(": passed") for line in lines)
        try:
            run = self.runtime.get_run(CONTROLLER, run_id)
            if RunLifecycle(run["lifecycle"]) not in TERMINAL_RUN:
                text = f"Checks on {snapshot_id}: " + "; ".join(lines)
                for part in self.runtime.participants(CONTROLLER, run_id):
                    self._status(run_id, part["participant_id"], text, snapshot_id=snapshot_id)
                if not passed:
                    self._changes_requested(run_id, snapshot_id, "required checks did not pass: " + "; ".join(lines))
                self.try_complete(run_id)
        except DomainError:
            pass
        self.notify()

    def _changes_requested(self, run_id: str, snapshot_id: str, reason: str) -> None:
        latest = self._latest_snapshot(run_id)
        if latest is None or latest["snapshot_id"] != snapshot_id:
            return  # feedback on an older snapshot does not reopen newer work
        task_id = self._main_task_id(run_id)
        task = self._task(task_id, run_id)
        if task["state"] == TaskState.REVIEW_REQUIRED.value:
            self.runtime.transition_task(CONTROLLER, task_id, TaskState.CHANGES_REQUESTED, expected_version=task["state_version"], reason=reason[:MAX_TEXT])
        run = self.runtime.get_run(CONTROLLER, run_id)
        if run["lifecycle"] in (RunLifecycle.REVIEWING.value, RunLifecycle.VERIFYING.value):
            self.runtime.transition_run(CONTROLLER, run_id, RunLifecycle.REPAIRING, reason=reason[:500])
        writer = self._writer_id(run_id)
        if writer:
            self._status(run_id, writer, f"Changes needed on {snapshot_id}: {reason[:1500]}\nClaim the task again (duet_claim), fix, and submit.", snapshot_id=snapshot_id, kind=MessageKind.BLOCKER)
        self.notify()

    def try_complete(self, run_id: str) -> dict | None:
        """Mark the main task verified from evidence and finalise the run when
        the D03 predicate holds for the latest submitted snapshot. Managed
        peers are stopped after the run lock is released: a peer thread may be
        waiting for that lock."""
        report = self._try_complete_locked(run_id)
        if report is not None and report.get("outcome") == COMPLETED_VERIFIED and report.get("satisfied"):
            if self.peer_stopper is not None:
                self.peer_stopper(run_id)
        return report

    def _try_complete_locked(self, run_id: str) -> dict | None:
        with self._run_lock(run_id):
            run = self.runtime.get_run(CONTROLLER, run_id)
            if RunLifecycle(run["lifecycle"]) in TERMINAL_RUN:
                return None
            latest = self._latest_snapshot(run_id)
            if latest is None or latest["snapshot_id"] in self._checks_running:
                return None
            snapshot_id = latest["snapshot_id"]
            contract = AcceptanceContract.from_dict(run["acceptance"])
            evidence = self.evidence.evidence_for(run_id, snapshot_id, run["acceptance_hash"])
            if not all(evidence.get(c, {}).get("status") == "passed" for c in contract.required_checks()):
                return None
            reviews = self.evidence.reviews_for(run_id, snapshot_id, run["acceptance_hash"])
            latest_by_reviewer: dict[str, str] = {}
            for review in reviews:
                if review["disposition"] in ("approve", "changes_requested"):
                    latest_by_reviewer[review["reviewer"]] = review["disposition"]
            if "approve" not in latest_by_reviewer.values() or "changes_requested" in latest_by_reviewer.values():
                return None
            if self.evidence.open_blocking_findings(run_id):
                return None
            ws = self.workspace(run_id)
            snap = self._snapshots.get(snapshot_id)
            current = capture_snapshot(ws.path, base_sha=ws.base_sha)
            if current.tree_hash != latest["tree_hash"]:
                writer = self._writer_id(run_id)
                if writer:
                    self._status(run_id, writer, f"The workspace changed after {snapshot_id} was submitted; its evidence no longer describes the files. Submit again.", kind=MessageKind.BLOCKER)
                return None
            snap = snap or current
            task_id = self._main_task_id(run_id)
            task = self._task(task_id, run_id)
            if task["state"] == TaskState.REVIEW_REQUIRED.value:
                self.runtime.transition_task(CONTROLLER, task_id, TaskState.VERIFIED, expected_version=task["state_version"], reason=f"checks passed and review approved on {snapshot_id}")
            report = self.gate.finalize(run_id, snapshot_id, workspace=ws.path, snapshot=snap)
            if report.outcome != COMPLETED_VERIFIED or not report.satisfied:
                # Actions still in flight (e.g. the reviewer's own turn) settle
                # shortly and trigger another evaluation; no need to announce.
                transient = {"no_unobserved_actions", "checkpoint_exported"}
                if any(item.name not in transient for item in report.missing):
                    missing = "; ".join(f"{i.name}: {i.detail}" for i in report.missing)
                    for part in self.runtime.participants(CONTROLLER, run_id):
                        self._status(run_id, part["participant_id"], f"Not complete yet ({report.outcome}): {missing}")
                self.notify()
                return report.to_dict()
            commit = self._commit_deliverable(run_id, ws, snap, snapshot_id)
            self._release_writer(run_id)
            for part in self.runtime.participants(CONTROLLER, run_id):
                self._status_terminal(run_id, part["participant_id"], f"COMPLETED_VERIFIED: {snapshot_id} passed every required check and a {self._reviewer_provider(run_id, snapshot_id)} review. Deliverable committed on branch {ws.branch} as {commit}. Nothing was pushed or merged.")
            self.notify()
            return report.to_dict()

    def _reviewer_provider(self, run_id: str, snapshot_id: str) -> str:
        run = self.runtime.get_run(CONTROLLER, run_id)
        reviews = self.evidence.reviews_for(run_id, snapshot_id, run["acceptance_hash"])
        approved = [r["reviewer_provider"] for r in reviews if r["disposition"] == "approve"]
        return approved[-1] if approved else "peer"

    def _commit_deliverable(self, run_id: str, ws: StrictWorkspace, snap: Snapshot, snapshot_id: str) -> str:
        """Commit the verified snapshot's files to the DUET-owned branch. Only
        paths the snapshot recorded are staged, so excluded files (secrets)
        never reach the commit. Nothing is pushed."""
        paths = list(snap.changed)
        if not paths:
            return "(no changes)"
        subprocess.run(["git", "add", "-A", "--", *paths], cwd=ws.path, check=True, capture_output=True)
        run = self.runtime.get_run(CONTROLLER, run_id)
        message = f"duet: {run['objective'][:72]}\n\nDuet-Run: {run_id}\nDuet-Snapshot: {snapshot_id}\n"
        proc = subprocess.run(
            ["git", "-c", "user.name=Duet", "-c", "user.email=duet@localhost.invalid", "commit", "-q", "--no-verify", "-m", message],
            cwd=ws.path, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return f"(commit failed: {(proc.stderr or proc.stdout).strip()[:200]})"
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ws.path, capture_output=True, text=True).stdout.strip()

    # ------------------------------------------------------------------ liveness and lifecycle

    def touch(self, principal: Principal) -> None:
        """A call from a participant proves its connection is alive."""
        part = self._participant(principal.id)
        if part["liveness"] != Liveness.CONNECTED.value:
            self.runtime.update_participant(CONTROLLER, principal.id, liveness=Liveness.CONNECTED)
            self._maybe_pair_active(principal.run_id or "")
            self.notify()

    def disconnect(self, principal: Principal, *, reason: str = "connection closed") -> None:
        part = self._participant(principal.id)
        if part["liveness"] in (Liveness.GONE.value, Liveness.UNAVAILABLE.value):
            return
        self.runtime.update_participant(CONTROLLER, principal.id, liveness=Liveness.UNAVAILABLE)
        self._peer_lost(principal.run_id or "", principal.id, reason)

    def mark_gone(self, participant_id: str, reason: str) -> None:
        part = self._participant(participant_id)
        if part["liveness"] == Liveness.GONE.value:
            return
        self.runtime.update_participant(CONTROLLER, participant_id, liveness=Liveness.GONE)
        self._peer_lost(part["run_id"], participant_id, reason)

    def _peer_lost(self, run_id: str, participant_id: str, reason: str) -> None:
        run = self.runtime.get_run(CONTROLLER, run_id)
        if RunLifecycle(run["lifecycle"]) not in TERMINAL_RUN:
            self.runtime.set_collaboration(CONTROLLER, run_id, Collaboration.PEER_UNAVAILABLE, reason=reason)
            peer = self._peer_of(run_id, participant_id)
            if peer is not None and peer["liveness"] != Liveness.GONE.value:
                lost = self._participant(participant_id)
                self._status(run_id, peer["participant_id"], f"Your peer ({lost['provider']}) is unavailable: {reason}. Pair completion is on hold; continue unaffected work or wait.")
        self.notify()

    def _maybe_pair_active(self, run_id: str) -> None:
        run = self.runtime.get_run(CONTROLLER, run_id)
        if RunLifecycle(run["lifecycle"]) in TERMINAL_RUN or run["collaboration"] != Collaboration.PEER_UNAVAILABLE.value:
            return
        parts = self.runtime.participants(CONTROLLER, run_id)
        if len(parts) == 2 and all(p["liveness"] == Liveness.CONNECTED.value for p in parts):
            self.runtime.set_collaboration(CONTROLLER, run_id, Collaboration.PAIR_ACTIVE, reason="both participants connected")

    def check_hosts(self) -> list[str]:
        """Native participants whose host process has exited are gone: a later
        resume of that conversation is a different session (R02)."""
        gone = []
        tx = self.runtime.store.read()
        rows = tx.query(
            "SELECT p.participant_id, p.connection_id, r.lifecycle FROM participants p JOIN runs r ON r.run_id = p.run_id "
            "WHERE p.connection_id LIKE 'host:%' AND p.liveness != ?",
            (Liveness.GONE.value,),
        )
        for row in rows:
            if RunLifecycle(row["lifecycle"]) in TERMINAL_RUN:
                continue
            identity = ProcessIdentity.parse(row["connection_id"][len("host:"):])
            if identity is not None and not identity.is_alive():
                self.mark_gone(row["participant_id"], "its host process exited")
                gone.append(row["participant_id"])
        return gone

    def expire_overdue(self) -> list[str]:
        """Enforce run deadlines: an overdue active run pauses (PAUSED_BUDGET)
        and its managed peers stop. Pausing is not success and not failure;
        the user decides what happens next."""
        paused = []
        now = time.time()
        for row in self.runtime.store.read().query("SELECT run_id, lifecycle, deadline_at FROM runs WHERE deadline_at IS NOT NULL"):
            lifecycle = RunLifecycle(row["lifecycle"])
            if lifecycle in TERMINAL_RUN or lifecycle.value.startswith("PAUSED_") or parse_utc(row["deadline_at"]).timestamp() > now:
                continue
            try:
                self.runtime.transition_run(CONTROLLER, row["run_id"], RunLifecycle.PAUSED_BUDGET, reason="the run's elapsed-time deadline passed")
            except DomainError:
                continue
            if self.peer_stopper is not None:
                self.peer_stopper(row["run_id"])
            for part in self.runtime.participants(CONTROLLER, row["run_id"]):
                self._status_terminal(row["run_id"], part["participant_id"], "Run paused: its deadline passed. Stop working on it; the user decides whether to resume.")
            paused.append(row["run_id"])
        if paused:
            self.notify()
        return paused

    def cancel(self, run_id: str, *, reason: str, principal: Principal = USER) -> dict:
        run = self.runtime.transition_run(principal, run_id, RunLifecycle.CANCELLED, reason=reason)
        if self.peer_stopper is not None:
            self.peer_stopper(run_id)
        self._release_writer(run_id)
        for part in self.runtime.participants(CONTROLLER, run_id):
            self._status_terminal(run_id, part["participant_id"], f"Run cancelled: {reason}. Stop working on it.")
        self.notify()
        return run

    # ------------------------------------------------------------------ helpers

    def _status(self, run_id: str, recipient: str, body: str, *, snapshot_id: str | None = None, kind: MessageKind = MessageKind.STATUS) -> None:
        """Controller notices. STATUS informs; BLOCKER asks the recipient to act
        (a managed peer gets a new turn for a BLOCKER, not for a STATUS)."""
        self.runtime.send_message(CONTROLLER, kind=kind, body=body[:MAX_TEXT], recipient=recipient, run_id=run_id, snapshot_ref=snapshot_id)

    def _status_terminal(self, run_id: str, recipient: str, body: str) -> None:
        """Closing status after the run ended (the controller may still post
        STATUS to a terminal run; participants may not send anything)."""
        try:
            self._status(run_id, recipient, body)
        except DomainError:
            pass

    @staticmethod
    def _require_participant(principal: Principal) -> None:
        if not principal.is_participant:
            raise Unauthorized("this operation is for participants")

    def _participant(self, participant_id: str) -> dict:
        row = self.runtime.store.read().require("participants", participant_id)
        view = {k: v for k, v in dict(row).items() if k != "token_hash"}
        view["capabilities"] = json.loads(view.pop("capabilities_json"))
        return view

    def _peer_of(self, run_id: str, participant_id: str) -> dict | None:
        for part in self.runtime.participants(CONTROLLER, run_id):
            if part["participant_id"] != participant_id:
                return part
        return None

    def _peer_id(self, principal: Principal) -> str:
        peer = self._peer_of(principal.run_id or "", principal.id)
        if peer is None:
            raise PolicyDenied("no peer has joined this run yet; keep working and check duet_wait for the join")
        return peer["participant_id"]

    def _writer_id(self, run_id: str) -> str | None:
        settings = self.settings(run_id)
        for part in self.runtime.participants(CONTROLLER, run_id):
            if part["workspace"] == settings.workspace_path:
                return part["participant_id"]
        return None

    def _main_task_id(self, run_id: str) -> str:
        task_id = self.runtime.store.read().scalar(
            "SELECT task_id FROM tasks WHERE run_id = ? AND proposed_by = 'controller' ORDER BY created_at, rowid LIMIT 1", (run_id,)
        )
        if not task_id:
            raise NotFound(f"run {run_id} has no main task")
        return task_id

    def _task(self, task_id: str, run_id: str) -> dict:
        row = self.runtime.store.read().require("tasks", check_text(task_id, "task_id", limit=MAX_ID))
        if row["run_id"] != run_id:
            raise Unauthorized("task belongs to another run")
        return dict(row)

    def _task_fence(self, task_id: str, owner: str) -> int:
        value = self.runtime.store.read().scalar(
            "SELECT fencing_token FROM leases WHERE resource = ? AND owner = ? AND released_at IS NULL", (f"task:{task_id}", owner)
        )
        if value is None:
            raise InvalidTransition(f"{owner} does not hold task {task_id}; claim it first")
        return int(value)

    def _writer_fence(self, ws: StrictWorkspace, owner: str) -> int:
        value = self.runtime.store.read().scalar(
            "SELECT fencing_token FROM leases WHERE resource = ? AND owner = ? AND released_at IS NULL", (ws.lease_resource, owner)
        )
        if value is None:
            # Expired or released: take it again (one writer; a Conflict means someone else holds it).
            return int(self.workspaces.acquire_writer(ws, owner)["fencing_token"])
        return int(value)

    def _release_writer(self, run_id: str) -> None:
        try:
            ws = self.workspace(run_id)
        except DomainError:
            return
        row = self.runtime.store.read().get("leases", ws.lease_resource)
        if row and row["owner"] and not row["released_at"]:
            self.workspaces.release_writer(ws, int(row["fencing_token"]))

    def _latest_snapshot(self, run_id: str) -> dict | None:
        rows = self.runtime.store.read().query(
            "SELECT * FROM snapshots WHERE run_id = ? AND author IS NOT NULL ORDER BY created_at DESC, rowid DESC LIMIT 1", (run_id,)
        )
        return dict(rows[0]) if rows else None

    def materialized(self, snapshot_id: str) -> Path:
        dest = self.state_root / "snapshots" / snapshot_id
        if not dest.exists():
            row = self.evidence.snapshot(snapshot_id)
            manifest = json.loads(self.artifacts.get_bytes(row["manifest_ref"]).decode("utf-8"))
            ensure_private_dir(dest.parent)
            tmp = dest.parent / f".{snapshot_id}.{os.getpid()}.{threading.get_ident()}"
            materialize(manifest, self.artifacts, tmp)
            try:
                os.rename(tmp, dest)
            except OSError:
                if not dest.exists():
                    raise
        return dest

    def _diff_text(self, run_id: str, snap_row: dict) -> str:
        """Unified diff of the snapshot's recorded changes against the base,
        built from the read-only copy, so later edits cannot leak in."""
        settings = self.settings(run_id)
        base = snap_row["base_sha"] or settings.base_sha
        root = self.materialized(snap_row["snapshot_id"])
        chunks = []
        total = 0
        for rel in json.loads(snap_row["changed_json"]):
            old = subprocess.run(["git", "show", f"{base}:{rel}"], cwd=settings.repo_path, capture_output=True)
            old_text = old.stdout.decode("utf-8", errors="replace") if old.returncode == 0 else None
            path = root / rel
            new_text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
            chunk = _unified(rel, old_text, new_text)
            chunks.append(chunk)
            total += len(chunk)
            if total > 4 * MAX_DIFF_CHARS:
                chunks.append("... (more files omitted)\n")
                break
        return "".join(chunks) or "(no textual changes)\n"

    # ------------------------------------------------------------------ settings

    def _settings_path(self, run_id: str) -> Path:
        return self.state_root / "runs" / f"{run_id}.json"

    def _remember_workspace(self, run_id: str, ws: StrictWorkspace, *, writer_role: str, peer_mode: str, writer_provider: str) -> None:
        settings = PairSettings(run_id, str(ws.repo.toplevel), str(ws.path), ws.branch, ws.base_sha, writer_role, peer_mode, writer_provider)
        path = self._settings_path(run_id)
        ensure_private_dir(path.parent)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)
        with self._lock:
            self._workspaces[run_id] = ws

    def settings(self, run_id: str) -> PairSettings:
        path = self._settings_path(run_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise NotFound(f"run {run_id} is not a pair run managed by this service") from None
        return PairSettings(**data)

    def workspace(self, run_id: str) -> StrictWorkspace:
        with self._lock:
            ws = self._workspaces.get(run_id)
        if ws is not None:
            return ws
        settings = self.settings(run_id)
        ws = StrictWorkspace(run_id=run_id, repo=resolve_repo(settings.repo_path), path=Path(settings.workspace_path), branch=settings.branch, base_sha=settings.base_sha)
        with self._lock:
            self._workspaces[run_id] = ws
        return ws


# --- helpers ------------------------------------------------------------------------------


def host_key(host: str) -> str:
    return "host:" + host


def contract_from_checks(objective: str, checks: list | None, protected: list[str] | None) -> AcceptanceContract:
    """The initiator's proposed contract: one criterion (the objective) that
    every check verifies, reviewed by the other provider. Commands are argv
    lists; a string is split with shlex, never run through a shell."""
    if not checks:
        raise ValidationError(
            "give at least one check command that verifies the objective (for example the test command); "
            "without one nothing can establish verified completion"
        )
    if not isinstance(checks, list) or len(checks) > MAX_CHECKS:
        raise ValidationError(f"checks must be a list of at most {MAX_CHECKS} commands")
    specs = []
    for index, raw in enumerate(checks, start=1):
        if isinstance(raw, str):
            argv = shlex.split(raw)
        elif isinstance(raw, list) and all(isinstance(a, str) for a in raw):
            argv = list(raw)
        else:
            raise ValidationError("each check is a command string or a list of strings")
        if not argv:
            raise ValidationError("empty check command")
        specs.append(CheckSpec(f"check{index}", argv=tuple(argv)))
    protected_paths = tuple(check_list(protected, "protected", item_limit=512))
    return AcceptanceContract(
        criteria=(Criterion("AC1", objective[:4000], checks=tuple(s.id for s in specs)),),
        checks=tuple(specs),
        protected_paths=protected_paths,
        notes="proposed by the initiating session; only the user can change it after creation",
    )


def _check_client(provider: str, client: dict | None) -> None:
    """The MCP client's self-reported name is weak evidence, but a mismatch
    (a Codex client claiming to be Claude) is refused rather than recorded."""
    if not client:
        return
    name = str(client.get("name", "")).lower()
    if not name:
        return
    if provider == Provider.CLAUDE.value and "codex" in name:
        raise ValidationError(f"the MCP client identifies as {client.get('name')!r}; it cannot join as claude")
    if provider == Provider.CODEX.value and "claude" in name:
        raise ValidationError(f"the MCP client identifies as {client.get('name')!r}; it cannot join as codex")


def _peer_view(peer: dict | None) -> dict | None:
    if peer is None:
        return None
    return {"participant_id": peer["participant_id"], "provider": peer["provider"], "origin": peer["origin"], "liveness": peer["liveness"]}


def _task_view(task: dict) -> dict:
    return {k: task[k] for k in ("task_id", "description", "state", "owner", "required", "revision", "state_version") if k in task}


def _message_view(message: dict) -> dict:
    return {
        "seq": message["seq"],
        "message_id": message["message_id"],
        "kind": message["kind"],
        "from": message.get("sender_provider") or message["sender"],
        "reply_to": message["reply_to"],
        "correlation_id": message["correlation_id"],
        "task_id": message["task_id"],
        "snapshot_id": message["snapshot_ref"],
        "state": message["state"],
        "body": message["body"],
    }


def _wait_advice(woke: str, messages: list[dict], state: dict) -> str:
    if RunLifecycle(state["lifecycle"]) in TERMINAL_RUN:
        return f"The run is {state['lifecycle']}; stop working on it and report to the user."
    if state["lifecycle"].startswith("PAUSED_"):
        return f"The run is {state['lifecycle']}; stop working on it and tell the user why it paused."
    if any(m["kind"] in REQUEST_KINDS for m in messages):
        return "Answer the peer's questions or review requests first (duet_send with reply_to), then resume your own work or wait again."
    if woke == "timeout":
        return "Nothing new. Continue useful work, or call duet_wait again; do not ask the user to relay messages."
    return "Handle these messages, then acknowledge them with ack_through=last_seq on your next duet_wait or duet_inbox call."


def _unified(rel: str, old: str | None, new: str | None) -> str:
    import difflib

    a = [] if old is None else old.splitlines(keepends=True)
    b = [] if new is None else new.splitlines(keepends=True)
    lines = list(difflib.unified_diff(a, b, fromfile="/dev/null" if old is None else f"a/{rel}", tofile="/dev/null" if new is None else f"b/{rel}"))
    text = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    return text or f"(binary or mode-only change: {rel})\n"
