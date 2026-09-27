"""Authorised command API over the runtime store.

Every operation takes a Principal. Participant principals are only produced by
`authenticate(token)`; the sender of a message, the owner of a claim and the
run an operation applies to are derived from that identity, never from
request parameters. `user` and `controller` principals exist only inside the
Duet process (CLI and dispatcher) and are never reachable through a token.

Each command runs in one transaction: validation, authorisation, and the
events it emits commit together or not at all."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from .contracts import (
    CONTROLLER,
    MAX_ID,
    MAX_JSON,
    MAX_TEXT,
    TERMINAL_ACTION,
    TERMINAL_RUN,
    USER,
    ActionState,
    Capabilities,
    Collaboration,
    Conflict,
    Event,
    InvalidTransition,
    Liveness,
    MessageKind,
    MessageState,
    NotFound,
    Origin,
    OutboxState,
    PolicyDenied,
    Principal,
    Provider,
    ReservationState,
    RunLifecycle,
    StaleLease,
    TaskState,
    Unauthorized,
    ValidationError,
    check_int,
    check_json_size,
    check_list,
    check_optional_text,
    check_text,
    content_hash,
    new_id,
    parse_enum,
    parse_utc,
    utc_after,
    utc_now,
)
from .identity import ProcessIdentity, hash_token, new_token, owner_is_dead
from .policy import AuthorisationPolicy
from .reducer import RUN_TRANSITIONS, TASK_TRANSITIONS, check_transition
from .store import Store, Tx

MAX_OUTSTANDING_REQUESTS = 8
MAX_REPLY_DEPTH = 32
DEFAULT_TASK_LEASE_SECONDS = 1800
DEFAULT_ACTION_LEASE_SECONDS = 900
REQUEST_KINDS = frozenset({MessageKind.QUESTION.value, MessageKind.REVIEW_REQUEST.value})
REPLY_KINDS = frozenset({MessageKind.ANSWER.value, MessageKind.REVIEW_RESULT.value})
OPEN_MESSAGE_STATES = (MessageState.QUEUED.value, MessageState.TRANSPORT_DELIVERED.value, MessageState.PARTICIPANT_ACKNOWLEDGED.value)


class Runtime:
    def __init__(
        self,
        store: Store,
        *,
        identity: ProcessIdentity | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.store = store
        self.identity = identity or ProcessIdentity.current()
        self.clock = clock

    # ------------------------------------------------------------------ helpers

    def _event(self, type_: str, payload: dict, principal: Principal, run_id: str | None) -> Event:
        return Event(type=type_, payload=payload, actor=f"{principal.kind}:{principal.id}", at=self.clock(), run_id=run_id)

    def _command(self, principal: Principal, key: str | None, command: str, request: dict, fn: Callable[[Tx], dict]) -> dict:
        with self.store.transaction() as tx:
            if key is not None:
                check_text(key, "idempotency_key", limit=MAX_ID)
                cached = tx.idempotent_response(f"{principal.kind}:{principal.id}", key, command, request)
                if cached is not None:
                    return cached
            response = fn(tx)
            if key is not None:
                tx.remember_response(f"{principal.kind}:{principal.id}", key, command, request, response)
            return response

    @staticmethod
    def _require_user(principal: Principal, what: str) -> None:
        if principal.kind != "user":
            raise Unauthorized(f"only the user may {what}")

    @staticmethod
    def _require_trusted(principal: Principal, what: str) -> None:
        if principal.kind not in ("user", "controller"):
            raise Unauthorized(f"only the user or the Duet controller may {what}")

    @staticmethod
    def _bind_run(principal: Principal, run_id: str) -> None:
        if principal.is_participant and principal.run_id != run_id:
            raise Unauthorized("participants may only act on their own run")

    def _run(self, tx: Tx, run_id: str) -> dict:
        return tx.require("runs", check_text(run_id, "run_id", limit=MAX_ID))

    def _live_run(self, tx: Tx, run_id: str) -> dict:
        run = self._run(tx, run_id)
        if RunLifecycle(run["lifecycle"]) in TERMINAL_RUN:
            raise InvalidTransition(f"run {run_id} is {run['lifecycle']}")
        return run

    def _now(self) -> datetime:
        return parse_utc(self.clock())

    # ------------------------------------------------------------------ runs

    def create_run(
        self,
        principal: Principal,
        *,
        repo_id: str,
        objective: str,
        policy: AuthorisationPolicy,
        acceptance: dict,
        scope: dict | None = None,
        base_sha: str | None = None,
        deadline_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        self._require_user(principal, "create a run")
        check_text(repo_id, "repo_id", limit=MAX_ID)
        check_text(objective, "objective", limit=MAX_TEXT)
        if not isinstance(acceptance, dict):
            raise ValidationError("acceptance must be an object")
        check_json_size(acceptance, "acceptance")
        scope = scope or {}
        if not isinstance(scope, dict):
            raise ValidationError("scope must be an object")
        check_json_size(scope, "scope")
        check_optional_text(base_sha, "base_sha", limit=MAX_ID)
        if deadline_at is not None:
            parse_utc(deadline_at)
        request = {
            "repo_id": repo_id, "objective": objective, "policy": policy.to_dict(), "acceptance": acceptance,
            "scope": scope, "base_sha": base_sha, "deadline_at": deadline_at,
        }

        def run(tx: Tx) -> dict:
            run_id = new_id("run")
            policy_hash = policy.hash()
            tx.emit(self._event("policy.registered", {"policy_hash": policy_hash, "body": policy.to_dict()}, principal, run_id))
            tx.emit(
                self._event(
                    "run.created",
                    {
                        "run_id": run_id, "repo_id": repo_id, "objective": objective, "scope": scope,
                        "base_sha": base_sha, "acceptance": acceptance, "acceptance_hash": content_hash(acceptance),
                        "policy_hash": policy_hash, "deadline_at": deadline_at,
                    },
                    principal,
                    run_id,
                )
            )
            return self._run_view(tx.require("runs", run_id))

        return self._command(principal, idempotency_key, "create_run", request, run)

    def transition_run(
        self, principal: Principal, run_id: str, to: RunLifecycle | str, *, reason: str = "", expected_version: int | None = None
    ) -> dict:
        self._require_trusted(principal, "change a run's lifecycle")
        target = parse_enum(RunLifecycle, to, "lifecycle")
        if target == RunLifecycle.COMPLETED_VERIFIED and principal.kind != "controller":
            raise Unauthorized("only the controller can establish verified completion")
        with self.store.transaction() as tx:
            run = self._run(tx, run_id)
            if expected_version is not None and run["state_version"] != expected_version:
                raise Conflict("run changed since it was read", details={"expected": expected_version, "actual": run["state_version"]})
            check_transition(RUN_TRANSITIONS, RunLifecycle(run["lifecycle"]), target, "run lifecycle")
            tx.emit(self._event("run.lifecycle", {"run_id": run_id, "from": run["lifecycle"], "to": target.value, "reason": reason}, principal, run_id))
            return self._run_view(tx.require("runs", run_id))

    def set_collaboration(self, principal: Principal, run_id: str, to: Collaboration | str, *, reason: str = "") -> dict:
        self._require_trusted(principal, "change collaboration status")
        target = parse_enum(Collaboration, to, "collaboration")
        if target == Collaboration.SOLO_EXPLICIT and principal.kind != "user":
            raise Unauthorized("only the user can switch a run to explicit solo")
        with self.store.transaction() as tx:
            self._run(tx, run_id)
            tx.emit(self._event("run.collaboration", {"run_id": run_id, "to": target.value, "reason": reason}, principal, run_id))
            return self._run_view(tx.require("runs", run_id))

    def change_acceptance(self, principal: Principal, run_id: str, acceptance: dict, *, reason: str) -> dict:
        """A new acceptance contract version. Only the user can change the
        contract; agents cannot relax the criteria they are judged by."""
        self._require_user(principal, "change the acceptance contract")
        if not isinstance(acceptance, dict):
            raise ValidationError("acceptance must be an object")
        check_json_size(acceptance, "acceptance")
        check_text(reason, "reason", limit=MAX_TEXT)
        with self.store.transaction() as tx:
            run = self._live_run(tx, run_id)
            payload = {
                "run_id": run_id, "acceptance": acceptance, "acceptance_hash": content_hash(acceptance),
                "version": run["acceptance_version"] + 1, "reason": reason,
            }
            tx.emit(self._event("run.acceptance", payload, principal, run_id))
            return self._run_view(tx.require("runs", run_id))

    def get_run(self, principal: Principal, run_id: str) -> dict:
        self._bind_run(principal, run_id)
        return self._run_view(self._run(self.store.read(), run_id))

    def policy_for(self, run_id: str) -> AuthorisationPolicy:
        tx = self.store.read()
        run = self._run(tx, run_id)
        body = tx.require("policies", run["policy_hash"])
        return AuthorisationPolicy.from_dict(json.loads(body["body_json"]))

    @staticmethod
    def _run_view(row: dict) -> dict:
        view = dict(row)
        view["scope"] = json.loads(view.pop("scope_json"))
        view["acceptance"] = json.loads(view.pop("acceptance_json"))
        return view

    # ------------------------------------------------------------------ participants

    def register_participant(
        self,
        principal: Principal,
        run_id: str,
        *,
        provider: Provider | str,
        origin: Origin | str,
        capabilities: Capabilities | dict | None = None,
        native_session_id: str | None = None,
        account_scope: str | None = None,
        workspace: str | None = None,
        connection_id: str | None = None,
        initiator: bool = False,
    ) -> dict:
        """Register a root participant. Returns the participant and a bearer
        token that is shown exactly once; only its hash is stored."""
        self._require_trusted(principal, "register participants")
        provider_value = parse_enum(Provider, provider, "provider")
        origin_value = parse_enum(Origin, origin, "origin")
        caps = capabilities if isinstance(capabilities, Capabilities) else Capabilities.from_dict(capabilities)
        check_optional_text(native_session_id, "native_session_id", limit=MAX_ID)
        check_optional_text(account_scope, "account_scope", limit=MAX_ID)
        check_optional_text(workspace, "workspace", limit=4096)
        check_optional_text(connection_id, "connection_id", limit=MAX_ID)
        token = new_token()
        with self.store.transaction() as tx:
            self._live_run(tx, run_id)
            existing = tx.scalar("SELECT participant_id FROM participants WHERE run_id = ? AND provider = ?", (run_id, provider_value.value))
            if existing:
                raise Conflict(f"run {run_id} already has a {provider_value.value} participant ({existing})")
            participant_id = new_id("prt")
            tx.emit(
                self._event(
                    "participant.registered",
                    {
                        "participant_id": participant_id, "run_id": run_id, "provider": provider_value.value,
                        "origin": origin_value.value, "native_session_id": native_session_id,
                        "connection_id": connection_id, "capabilities": caps.to_dict(),
                        "account_scope": account_scope, "workspace": workspace,
                        "liveness": Liveness.CONNECTED.value, "token_hash": hash_token(token),
                    },
                    principal,
                    run_id,
                )
            )
            if initiator:
                tx.emit(self._event("run.initiator", {"run_id": run_id, "participant_id": participant_id}, principal, run_id))
            return {"participant": self._participant_view(tx.require("participants", participant_id)), "token": token}

    def authenticate(self, token: str) -> Principal:
        if not isinstance(token, str) or not token.startswith("duet_pt_"):
            raise Unauthorized("missing or malformed participant token")
        tx = self.store.read()
        rows = tx.query("SELECT participant_id, run_id, provider, liveness FROM participants WHERE token_hash = ?", (hash_token(token),))
        if not rows:
            raise Unauthorized("unknown participant token")
        row = rows[0]
        if row["liveness"] == Liveness.GONE.value:
            raise Unauthorized("participant has left the run")
        return Principal("participant", row["participant_id"], run_id=row["run_id"], provider=row["provider"])

    def update_participant(self, principal: Principal, participant_id: str, **changes: Any) -> dict:
        allowed_self = {"liveness", "connection_id"}
        allowed_trusted = allowed_self | {"native_session_id", "workspace", "capabilities"}
        if principal.is_participant:
            if principal.id != participant_id:
                raise Unauthorized("participants may only update themselves")
            forbidden = set(changes) - allowed_self
            if forbidden:
                raise Unauthorized(f"participants cannot change {sorted(forbidden)}")
        elif principal.kind not in ("user", "controller"):
            raise Unauthorized("not allowed")
        unknown = set(changes) - allowed_trusted
        if unknown:
            raise ValidationError(f"unknown participant fields {sorted(unknown)}")
        payload: dict[str, Any] = {"participant_id": participant_id}
        if "liveness" in changes:
            payload["liveness"] = parse_enum(Liveness, changes["liveness"], "liveness").value
        for key in ("connection_id", "native_session_id", "workspace"):
            if key in changes:
                payload[key] = check_optional_text(changes[key], key, limit=4096)
        if "capabilities" in changes:
            caps = changes["capabilities"]
            payload["capabilities"] = (caps if isinstance(caps, Capabilities) else Capabilities.from_dict(caps)).to_dict()
        with self.store.transaction() as tx:
            row = tx.require("participants", participant_id)
            tx.emit(self._event("participant.updated", payload, principal, row["run_id"]))
            return self._participant_view(tx.require("participants", participant_id))

    def participants(self, principal: Principal, run_id: str) -> list[dict]:
        self._bind_run(principal, run_id)
        rows = self.store.read().query("SELECT * FROM participants WHERE run_id = ? ORDER BY created_at", (run_id,))
        return [self._participant_view(dict(row)) for row in rows]

    @staticmethod
    def _participant_view(row: dict) -> dict:
        view = {k: v for k, v in row.items() if k != "token_hash"}
        view["capabilities"] = json.loads(view.pop("capabilities_json"))
        return view

    # ------------------------------------------------------------------ approvals

    def grant_approval(
        self,
        principal: Principal,
        run_id: str,
        *,
        scope: str,
        action: str,
        constraints: dict | None = None,
        expires_at: str | None = None,
    ) -> dict:
        """Only a user-origin principal can approve. Text in a peer message
        that claims approval has no path to this method (AT33)."""
        self._require_user(principal, "grant approvals")
        check_text(scope, "scope")
        check_text(action, "action")
        constraints = constraints or {}
        check_json_size(constraints, "constraints")
        if expires_at is not None:
            parse_utc(expires_at)
        with self.store.transaction() as tx:
            run = self._live_run(tx, run_id)
            approval_id = new_id("apv")
            tx.emit(
                self._event(
                    "approval.granted",
                    {
                        "approval_id": approval_id, "run_id": run_id, "scope": scope, "action": action,
                        "constraints": constraints, "policy_hash": run["policy_hash"], "granted_by": principal.id,
                        "expires_at": expires_at,
                    },
                    principal,
                    run_id,
                )
            )
            return dict(tx.require("approvals", approval_id))

    def revoke_approval(self, principal: Principal, approval_id: str) -> None:
        self._require_user(principal, "revoke approvals")
        with self.store.transaction() as tx:
            row = tx.require("approvals", approval_id)
            tx.emit(self._event("approval.revoked", {"approval_id": approval_id}, principal, row["run_id"]))

    def is_approved(self, run_id: str, scope: str, action: str) -> bool:
        tx = self.store.read()
        run = self._run(tx, run_id)
        now = self._now()
        for row in tx.query(
            "SELECT * FROM approvals WHERE run_id = ? AND scope = ? AND action = ? AND revoked_at IS NULL",
            (run_id, scope, action),
        ):
            if row["policy_hash"] != run["policy_hash"] or not row["granted_by"] == "user":
                continue
            if row["expires_at"] and parse_utc(row["expires_at"]) <= now:
                continue
            return True
        return False

    # ------------------------------------------------------------------ messages

    def send_message(
        self,
        principal: Principal,
        *,
        kind: MessageKind | str,
        body: str,
        recipient: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        reply_to: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        snapshot_ref: str | None = None,
        artifact_refs: list[str] | None = None,
        expires_in_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Queue a structured message and return a receipt immediately; this
        never waits for the peer's answer (no synchronous ask/answer deadlock)."""
        if principal.is_participant:
            run_id = principal.run_id
            if recipient is None:
                raise ValidationError("participants must address a recipient")
        elif principal.kind not in ("controller", "user"):
            raise Unauthorized("not allowed")
        if run_id is None:
            raise ValidationError("run_id is required")
        kind_value = parse_enum(MessageKind, kind, "kind")
        check_text(body, "body", limit=MAX_TEXT)
        refs = check_list(artifact_refs, "artifact_refs", limit=32, item_limit=MAX_ID)
        for optional, name in ((task_id, "task_id"), (reply_to, "reply_to"), (correlation_id, "correlation_id"), (causation_id, "causation_id"), (snapshot_ref, "snapshot_ref"), (recipient, "recipient")):
            check_optional_text(optional, name, limit=MAX_ID)
        if expires_in_seconds is not None:
            check_int(expires_in_seconds, "expires_in_seconds", minimum=1, maximum=7 * 24 * 3600)
        request = {
            "kind": kind_value.value, "body": body, "recipient": recipient, "run_id": run_id, "task_id": task_id,
            "reply_to": reply_to, "correlation_id": correlation_id, "causation_id": causation_id,
            "snapshot_ref": snapshot_ref, "artifact_refs": refs, "expires_in_seconds": expires_in_seconds,
        }

        def run(tx: Tx) -> dict:
            # A terminal run's log is closed, except for the controller's
            # closing STATUS (e.g. "completed; committed as abc123").
            closing = principal.kind == "controller" and kind_value == MessageKind.STATUS
            run_row = self._run(tx, run_id) if closing else self._live_run(tx, run_id)
            policy = self._policy(tx, run_row)
            sender = principal.id if principal.is_participant else principal.kind
            recipient_id = self._resolve_recipient(tx, run_id, recipient) if recipient is not None else None
            if principal.is_participant and recipient_id == principal.id:
                raise ValidationError("a participant cannot message itself")
            total = tx.scalar("SELECT COUNT(*) FROM messages WHERE run_id = ?", (run_id,)) or 0
            if principal.is_participant and total >= policy.max_discussion_messages:
                raise PolicyDenied(f"discussion budget exhausted ({policy.max_discussion_messages} messages)")
            if principal.is_participant and kind_value.value in REQUEST_KINDS:
                outstanding = tx.scalar(
                    f"SELECT COUNT(*) FROM messages WHERE sender = ? AND kind IN ({','.join('?' * len(REQUEST_KINDS))}) "
                    f"AND state IN ({','.join('?' * len(OPEN_MESSAGE_STATES))})",
                    (principal.id, *sorted(REQUEST_KINDS), *OPEN_MESSAGE_STATES),
                )
                if outstanding >= MAX_OUTSTANDING_REQUESTS:
                    raise PolicyDenied(f"too many outstanding requests ({MAX_OUTSTANDING_REQUESTS}); wait for answers first")
            if task_id is not None:
                task = tx.require("tasks", task_id)
                if task["run_id"] != run_id:
                    raise Unauthorized("task belongs to another run")
            parent = None
            if reply_to is not None:
                parent = tx.require("messages", reply_to)
                if parent["run_id"] != run_id:
                    raise Unauthorized("reply_to belongs to another run")
                if self._reply_depth(tx, reply_to) >= MAX_REPLY_DEPTH:
                    raise PolicyDenied(f"reply chain deeper than {MAX_REPLY_DEPTH}")
            seq = (tx.scalar("SELECT MAX(seq) FROM messages WHERE run_id = ?", (run_id,)) or 0) + 1
            message_id = new_id("msg")
            expires_at = utc_after(expires_in_seconds, now=self.clock()) if expires_in_seconds else None
            tx.emit(
                self._event(
                    "message.queued",
                    {
                        "message_id": message_id, "run_id": run_id, "seq": seq, "sender": sender,
                        "recipient": recipient_id, "kind": kind_value.value, "task_id": task_id,
                        "reply_to": reply_to, "correlation_id": correlation_id or (parent["correlation_id"] if parent else None) or message_id,
                        "causation_id": causation_id or reply_to, "snapshot_ref": snapshot_ref, "body": body,
                        "artifact_refs": refs, "expires_at": expires_at,
                    },
                    principal,
                    run_id,
                )
            )
            # Answering a request you received acknowledges and closes it.
            if parent is not None and kind_value.value in REPLY_KINDS and parent["recipient"] == principal.id:
                self._close_message(tx, parent, principal)
            row = tx.require("messages", message_id)
            return {"message_id": message_id, "seq": seq, "state": row["state"], "correlation_id": row["correlation_id"]}

        return self._command(principal, idempotency_key, "send_message", request, run)

    def read_inbox(self, principal: Principal, *, after: int | None = None, limit: int = 50) -> dict:
        """Messages addressed to the caller (or broadcast by the controller)
        after the persisted cursor, oldest first. Reading marks them
        TRANSPORT_DELIVERED; it does not acknowledge them. Unacknowledged
        messages are redelivered on the next read (at-least-once)."""
        if not principal.is_participant:
            raise Unauthorized("only participants have an inbox")
        check_int(limit, "limit", minimum=1, maximum=200)
        with self.store.transaction() as tx:
            cursor = tx.require("inbox_cursors", principal.id)["acked_seq"]
            floor = cursor if after is None else max(cursor, check_int(after, "after", minimum=0))
            rows = tx.query(
                "SELECT * FROM messages WHERE run_id = ? AND seq > ? AND sender != ? "
                "AND (recipient = ? OR recipient IS NULL) ORDER BY seq LIMIT ?",
                (principal.run_id, floor, principal.id, principal.id, limit),
            )
            now = self._now()
            messages = []
            for row in rows:
                message = dict(row)
                if message["state"] == MessageState.QUEUED.value:
                    if message["expires_at"] and parse_utc(message["expires_at"]) <= now:
                        self._advance_message(tx, message, MessageState.EXPIRED, principal)
                        continue
                    if message["recipient"] is not None:
                        self._advance_message(tx, message, MessageState.TRANSPORT_DELIVERED, principal)
                        message = tx.require("messages", message["message_id"])
                messages.append(self._message_view(tx, message))
            return {"cursor": cursor, "messages": messages}

    def ack(self, principal: Principal, *, up_to_seq: int) -> dict:
        if not principal.is_participant:
            raise Unauthorized("only participants have an inbox")
        check_int(up_to_seq, "up_to_seq", minimum=0)
        with self.store.transaction() as tx:
            cursor = tx.require("inbox_cursors", principal.id)["acked_seq"]
            if up_to_seq < cursor:
                raise InvalidTransition("inbox cursor cannot move backwards", details={"cursor": cursor})
            highest = tx.scalar("SELECT MAX(seq) FROM messages WHERE run_id = ?", (principal.run_id,)) or 0
            if up_to_seq > highest:
                raise ValidationError(f"cannot acknowledge beyond the last message (seq {highest})")
            for row in tx.query(
                "SELECT * FROM messages WHERE run_id = ? AND recipient = ? AND seq > ? AND seq <= ? AND state IN (?, ?)",
                (principal.run_id, principal.id, cursor, up_to_seq, MessageState.QUEUED.value, MessageState.TRANSPORT_DELIVERED.value),
            ):
                self._advance_message(tx, dict(row), MessageState.PARTICIPANT_ACKNOWLEDGED, principal)
            if up_to_seq != cursor:
                tx.emit(self._event("inbox.acked", {"participant_id": principal.id, "acked_seq": up_to_seq}, principal, principal.run_id))
            return {"cursor": up_to_seq}

    def mark_handled(self, principal: Principal, message_id: str) -> dict:
        with self.store.transaction() as tx:
            message = tx.require("messages", message_id)
            if not principal.is_participant or message["recipient"] != principal.id:
                raise Unauthorized("only the recipient can mark a message handled")
            self._close_message(tx, message, principal)
            return self._message_view(tx, tx.require("messages", message_id))

    def _close_message(self, tx: Tx, message: dict, principal: Principal) -> None:
        """Move a received message to HANDLED, acknowledging it first if needed.
        Terminal messages (already handled, expired, cancelled) are left alone."""
        if message["state"] in (MessageState.QUEUED.value, MessageState.TRANSPORT_DELIVERED.value):
            self._advance_message(tx, message, MessageState.PARTICIPANT_ACKNOWLEDGED, principal)
            message = tx.require("messages", message["message_id"])
        if message["state"] == MessageState.PARTICIPANT_ACKNOWLEDGED.value:
            self._advance_message(tx, message, MessageState.HANDLED, principal)

    def messages(self, principal: Principal, run_id: str, *, since: int = 0, limit: int = 200) -> list[dict]:
        self._bind_run(principal, run_id)
        tx = self.store.read()
        rows = tx.query("SELECT * FROM messages WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?", (run_id, since, limit))
        return [self._message_view(tx, dict(row)) for row in rows]

    def _advance_message(self, tx: Tx, message: dict, target: MessageState, principal: Principal) -> None:
        tx.emit(
            self._event(
                "message.state",
                {"message_id": message["message_id"], "from": message["state"], "to": target.value},
                principal,
                message["run_id"],
            )
        )

    def _resolve_recipient(self, tx: Tx, run_id: str, recipient: str) -> str:
        """Accept a participant id or a provider name within the same run."""
        row = tx.get("participants", recipient)
        if row is None:
            rows = tx.query("SELECT * FROM participants WHERE run_id = ? AND provider = ?", (run_id, recipient))
            row = dict(rows[0]) if rows else None
        if row is None or row["run_id"] != run_id:
            raise NotFound(f"no participant {recipient!r} in this run")
        return row["participant_id"]

    def _reply_depth(self, tx: Tx, message_id: str) -> int:
        depth = 0
        current = message_id
        while current is not None and depth <= MAX_REPLY_DEPTH:
            current = tx.scalar("SELECT reply_to FROM messages WHERE message_id = ?", (current,))
            depth += 1
        return depth

    @staticmethod
    def _message_view(tx: Tx, row: dict) -> dict:
        view = dict(row)
        view["artifact_refs"] = json.loads(view.pop("artifact_refs_json"))
        sender = tx.get("participants", view["sender"]) if view["sender"] not in ("controller", "user") else None
        view["sender_provider"] = sender["provider"] if sender else view["sender"]
        return view

    # ------------------------------------------------------------------ tasks

    def propose_task(
        self,
        principal: Principal,
        *,
        description: str,
        run_id: str | None = None,
        deliverables: list[str] | None = None,
        acceptance_ids: list[str] | None = None,
        depends_on: list[str] | None = None,
        parent_id: str | None = None,
        required: bool | None = None,
        kind: str = "code",
        idempotency_key: str | None = None,
    ) -> dict:
        from .taskplan import TASK_KINDS

        if kind not in TASK_KINDS:
            raise ValidationError(f"task kind must be one of {TASK_KINDS}")
        if principal.is_participant:
            run_id = principal.run_id
        elif principal.kind not in ("user", "controller"):
            raise Unauthorized("not allowed")
        if run_id is None:
            raise ValidationError("run_id is required")
        check_text(description, "description", limit=MAX_TEXT)
        deliverables = check_list(deliverables, "deliverables")
        acceptance_ids = check_list(acceptance_ids, "acceptance_ids", item_limit=MAX_ID)
        depends_on = check_list(depends_on, "depends_on", item_limit=MAX_ID)
        check_optional_text(parent_id, "parent_id", limit=MAX_ID)
        request = {
            "run_id": run_id, "description": description, "deliverables": deliverables,
            "acceptance_ids": acceptance_ids, "depends_on": depends_on, "parent_id": parent_id, "required": required,
            "kind": kind,
        }

        def run(tx: Tx) -> dict:
            run_row = self._live_run(tx, run_id)
            known_criteria = {c.get("id") for c in json.loads(run_row["acceptance_json"]).get("criteria", []) if isinstance(c, dict)}
            unknown = [a for a in acceptance_ids if a not in known_criteria]
            if unknown:
                raise ValidationError(f"unknown acceptance ids {unknown}; they must exist in the run's acceptance contract")
            for dep in depends_on:
                dep_row = tx.require("tasks", dep)
                if dep_row["run_id"] != run_id:
                    raise Unauthorized("dependencies must belong to the same run")
            if parent_id is not None and tx.require("tasks", parent_id)["run_id"] != run_id:
                raise Unauthorized("parent task belongs to another run")
            # Work tied to acceptance criteria is required; only the user decides otherwise.
            is_required = bool(acceptance_ids) if required is None else bool(required)
            if principal.is_participant and required is False and acceptance_ids:
                raise Unauthorized("participants cannot mark acceptance work optional")
            state = TaskState.PROPOSED if principal.is_participant else TaskState.READY
            task_id = new_id("tsk")
            tx.emit(
                self._event(
                    "task.proposed",
                    {
                        "task_id": task_id, "run_id": run_id, "parent_id": parent_id, "description": description,
                        "deliverables": deliverables, "acceptance_ids": acceptance_ids, "depends_on": depends_on,
                        "required": is_required, "state": state.value, "proposed_by": principal.id, "kind": kind,
                    },
                    principal,
                    run_id,
                )
            )
            return self._task_view(tx.require("tasks", task_id))

        return self._command(principal, idempotency_key, "propose_task", request, run)

    def set_dependencies(self, principal: Principal, task_id: str, depends_on: list[str]) -> dict:
        """Replace a task's dependencies, rejecting cycles."""
        self._require_trusted(principal, "rewire task dependencies")
        depends_on = check_list(depends_on, "depends_on", item_limit=MAX_ID)
        with self.store.transaction() as tx:
            task = tx.require("tasks", task_id)
            graph = {row["task_id"]: json.loads(row["depends_on_json"]) for row in tx.query("SELECT task_id, depends_on_json FROM tasks WHERE run_id = ?", (task["run_id"],))}
            for dep in depends_on:
                if dep not in graph:
                    raise NotFound(f"task {dep} not found in this run")
            graph[task_id] = depends_on
            cycle = _find_cycle(graph)
            if cycle:
                raise ValidationError("dependency cycle rejected: " + " -> ".join(cycle))
            tx.emit(self._event("task.dependencies", {"task_id": task_id, "depends_on": depends_on}, principal, task["run_id"]))
            return self._task_view(tx.require("tasks", task_id))

    def claim_task(self, principal: Principal, task_id: str, *, expected_version: int, lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS) -> dict:
        if not principal.is_participant:
            raise Unauthorized("tasks are claimed by participants")
        check_int(expected_version, "expected_version", minimum=1)
        check_int(lease_seconds, "lease_seconds", minimum=1, maximum=24 * 3600)
        with self.store.transaction() as tx:
            task = tx.require("tasks", task_id)
            self._bind_run(principal, task["run_id"])
            self._live_run(tx, task["run_id"])
            if task["state_version"] != expected_version:
                raise Conflict("task changed since it was read", details={"expected": expected_version, "actual": task["state_version"]})
            if task["state"] not in (TaskState.READY.value, TaskState.CHANGES_REQUESTED.value):
                raise Conflict(f"task is {task['state']}, not claimable")
            unmet = [d for d in json.loads(task["depends_on_json"]) if tx.require("tasks", d)["state"] != TaskState.VERIFIED.value]
            if unmet:
                raise Conflict("dependencies are not verified yet", details={"unmet": unmet})
            lease = self._acquire_lease(tx, f"task:{task_id}", principal.id, lease_seconds, principal)
            tx.emit(
                self._event(
                    "task.transition",
                    {"task_id": task_id, "from": task["state"], "to": TaskState.CLAIMED.value, "owner": principal.id, "reason": "claimed"},
                    principal,
                    task["run_id"],
                )
            )
            return {"task": self._task_view(tx.require("tasks", task_id)), "lease": lease}

    def transition_task(
        self,
        principal: Principal,
        task_id: str,
        to: TaskState | str,
        *,
        expected_version: int,
        fence: int | None = None,
        reason: str = "",
        blocked_reason: str | None = None,
        next_action: str | None = None,
        bump_revision: bool = False,
    ) -> dict:
        target = parse_enum(TaskState, to, "task state")
        check_int(expected_version, "expected_version", minimum=1)
        check_text(reason, "reason", limit=MAX_TEXT, allow_empty=True)
        check_optional_text(blocked_reason, "blocked_reason", limit=MAX_TEXT)
        check_optional_text(next_action, "next_action", limit=MAX_TEXT)
        with self.store.transaction() as tx:
            task = tx.require("tasks", task_id)
            self._bind_run(principal, task["run_id"])
            self._live_run(tx, task["run_id"])
            if task["state_version"] != expected_version:
                raise Conflict("task changed since it was read", details={"expected": expected_version, "actual": task["state_version"]})
            current = TaskState(task["state"])
            check_transition(TASK_TRANSITIONS, current, target, "task")
            self._authorise_task_transition(tx, principal, task, current, target, fence)
            payload: dict[str, Any] = {
                "task_id": task_id, "from": current.value, "to": target.value, "reason": reason,
                "blocked_reason": blocked_reason, "next_action": next_action, "bump_revision": bump_revision,
            }
            releasing = target in (TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED, TaskState.REVIEW_REQUIRED)
            if target == TaskState.READY:
                payload["owner"] = None
            tx.emit(self._event("task.transition", payload, principal, task["run_id"]))
            if releasing:
                self._release_if_held(tx, f"task:{task_id}", principal)
            return self._task_view(tx.require("tasks", task_id))

    def _authorise_task_transition(
        self, tx: Tx, principal: Principal, task: dict, current: TaskState, target: TaskState, fence: int | None
    ) -> None:
        if target == TaskState.VERIFIED:
            if principal.kind != "controller":
                raise Unauthorized("only the controller marks a task verified, from evidence")
            return
        if target == TaskState.CANCELLED and task["required"]:
            if principal.kind != "user":
                raise Unauthorized("required work can only be cancelled by the user (acceptance contract change)")
            return
        if principal.kind in ("user", "controller"):
            return
        # Participants: owner operations need the owner and a valid fence;
        # review outcomes must come from someone other than the owner.
        if current == TaskState.PROPOSED:
            raise Unauthorized("proposed tasks are accepted by the controller, not by participants")
        if target == TaskState.CHANGES_REQUESTED:
            if task["owner"] == principal.id:
                raise Unauthorized("the author cannot review their own work")
            return
        if task["owner"] != principal.id:
            raise Unauthorized("only the task owner can do that")
        if fence is None:
            raise StaleLease("owner transitions require the task lease fencing token")
        self._check_fence(tx, f"task:{task['task_id']}", fence)

    @staticmethod
    def _task_view(row: dict) -> dict:
        view = dict(row)
        for key in ("deliverables", "acceptance_ids", "depends_on"):
            view[key] = json.loads(view.pop(f"{key}_json"))
        view["required"] = bool(view["required"])
        return view

    def tasks(self, principal: Principal, run_id: str) -> list[dict]:
        self._bind_run(principal, run_id)
        rows = self.store.read().query("SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at", (run_id,))
        return [self._task_view(dict(row)) for row in rows]

    # ------------------------------------------------------------------ actions & outbox

    def plan_action(
        self,
        principal: Principal,
        *,
        run_id: str,
        type: str,
        input: dict,
        task_id: str | None = None,
        participant_id: str | None = None,
        reservations: list[dict] | None = None,
        reserve: bool = True,
        idempotency_key: str | None = None,
    ) -> dict:
        """Persist the action, its reservations and its outbox record in one
        transaction, before anything is dispatched."""
        if principal.kind != "controller":
            raise Unauthorized("only the controller plans actions")
        check_text(type, "type", limit=64)
        if not isinstance(input, dict):
            raise ValidationError("input must be an object")
        check_json_size(input, "input", MAX_JSON)
        reservations = list(reservations or [])
        clean_reservations = []
        for res in reservations:
            if not isinstance(res, dict):
                raise ValidationError("reservation must be an object")
            clean_reservations.append(
                {
                    "reservation_id": new_id("rsv"),
                    "provider": check_text(res.get("provider"), "reservation.provider", limit=64),
                    "pool": check_text(res.get("pool"), "reservation.pool", limit=MAX_ID),
                    "metric": check_text(res.get("metric"), "reservation.metric", limit=64),
                    "quantity": check_json_size(res.get("quantity"), "reservation.quantity", 4096),
                    "category": check_text(res.get("category", "action"), "reservation.category", limit=64),
                    "expires_at": res.get("expires_at"),
                }
            )
        request = {"run_id": run_id, "type": type, "input": input, "task_id": task_id, "participant_id": participant_id, "reservations": reservations, "reserve": reserve}

        def run(tx: Tx) -> dict:
            self._live_run(tx, run_id)
            task_revision = None
            if task_id is not None:
                task = tx.require("tasks", task_id)
                if task["run_id"] != run_id:
                    raise Unauthorized("task belongs to another run")
                task_revision = task["revision"]
            if participant_id is not None and tx.require("participants", participant_id)["run_id"] != run_id:
                raise Unauthorized("participant belongs to another run")
            action_id = new_id("act")
            outbox_id = new_id("obx")
            tx.emit(
                self._event(
                    "action.planned",
                    {
                        "action_id": action_id, "outbox_id": outbox_id, "run_id": run_id, "type": type,
                        "task_id": task_id, "task_revision": task_revision, "participant_id": participant_id,
                        "input_digest": content_hash(input), "reservations": clean_reservations, "reserve": reserve,
                    },
                    principal,
                    run_id,
                )
            )
            return {"action": dict(tx.require("actions", action_id)), "outbox_id": outbox_id if reserve else None}

        return self._command(principal, idempotency_key, "plan_action", request, run)

    def claim_next_action(self, principal: Principal, *, lease_seconds: int = DEFAULT_ACTION_LEASE_SECONDS) -> dict | None:
        """Claim the oldest pending outbox record for this process. Serialised
        by the write lock: two dispatchers can never claim the same action."""
        if principal.kind != "controller":
            raise Unauthorized("only the controller dispatches actions")
        with self.store.transaction() as tx:
            rows = tx.query("SELECT * FROM outbox WHERE state = ? ORDER BY created_at, outbox_id LIMIT 1", (OutboxState.PENDING.value,))
            if not rows:
                return None
            return self._claim_outbox(tx, dict(rows[0]), principal, lease_seconds)

    def claim_action(self, principal: Principal, action_id: str, *, lease_seconds: int = DEFAULT_ACTION_LEASE_SECONDS) -> dict:
        """Claim one specific planned action, for a dispatcher that planned it
        itself (a peer driver, a check runner). Fails if anyone else already
        claimed it."""
        if principal.kind != "controller":
            raise Unauthorized("only the controller dispatches actions")
        with self.store.transaction() as tx:
            rows = tx.query("SELECT * FROM outbox WHERE action_id = ?", (check_text(action_id, "action_id", limit=MAX_ID),))
            if not rows:
                raise NotFound(f"action {action_id} has no outbox record")
            if rows[0]["state"] != OutboxState.PENDING.value:
                raise Conflict(f"action {action_id} was already claimed ({rows[0]['state']})")
            return self._claim_outbox(tx, dict(rows[0]), principal, lease_seconds)

    def _claim_outbox(self, tx: Tx, outbox: dict, principal: Principal, lease_seconds: int) -> dict:
        owner = str(self.identity)
        action = tx.require("actions", outbox["action_id"])
        lease = self._acquire_lease(tx, f"action:{action['action_id']}", owner, lease_seconds, principal)
        tx.emit(
            self._event(
                "outbox.claimed",
                {"outbox_id": outbox["outbox_id"], "claimed_by": owner, "fencing_token": lease["fencing_token"]},
                principal,
                outbox["run_id"],
            )
        )
        tx.emit(
            self._event(
                "action.transition",
                {"action_id": action["action_id"], "from": action["state"], "to": ActionState.DISPATCHING.value},
                principal,
                outbox["run_id"],
            )
        )
        return {"action": dict(tx.require("actions", action["action_id"])), "lease": lease}

    def record_action(
        self,
        principal: Principal,
        action_id: str,
        to: ActionState | str,
        *,
        fence: int,
        result: dict | None = None,
        provider_invocation_id: str | None = None,
        actuals: dict | None = None,
    ) -> dict:
        """Record dispatch progress or an outcome. The fencing token must match
        the current action lease: a worker whose lease was reclaimed cannot
        write a stale result."""
        if principal.kind != "controller":
            raise Unauthorized("only the controller records action outcomes")
        target = parse_enum(ActionState, to, "action state")
        if target == ActionState.IN_DOUBT:
            raise ValidationError("IN_DOUBT is set by reconciliation, not reported")
        if result is not None:
            check_json_size(result, "result")
        with self.store.transaction() as tx:
            action = tx.require("actions", action_id)
            self._check_fence(tx, f"action:{action_id}", fence)
            payload: dict[str, Any] = {"action_id": action_id, "from": action["state"], "to": target.value}
            if result is not None:
                payload["result"] = result
            if provider_invocation_id:
                payload["provider_invocation_id"] = check_text(provider_invocation_id, "provider_invocation_id", limit=MAX_ID)
            tx.emit(self._event("action.transition", payload, principal, action["run_id"]))
            if target in TERMINAL_ACTION:
                self._finish_action(tx, action, principal, actuals)
                self._release_if_held(tx, f"action:{action_id}", principal)
            return dict(tx.require("actions", action_id))

    def resolve_in_doubt(
        self, principal: Principal, action_id: str, to: ActionState | str, *, reconciliation: str, result: dict | None = None
    ) -> dict:
        """Settle an IN_DOUBT action from inspected evidence (files, commits,
        provider session state). Never re-dispatches it."""
        if principal.kind not in ("controller", "user"):
            raise Unauthorized("only the controller or user reconciles actions")
        target = parse_enum(ActionState, to, "action state")
        check_text(reconciliation, "reconciliation", limit=MAX_TEXT)
        with self.store.transaction() as tx:
            action = tx.require("actions", action_id)
            if action["state"] != ActionState.IN_DOUBT.value:
                raise InvalidTransition(f"action {action_id} is {action['state']}, not IN_DOUBT")
            payload: dict[str, Any] = {"action_id": action_id, "from": action["state"], "to": target.value, "reconciliation": reconciliation}
            if result is not None:
                payload["result"] = result
            tx.emit(self._event("action.transition", payload, principal, action["run_id"]))
            self._finish_action(tx, action, principal, None)
            return dict(tx.require("actions", action_id))

    def _finish_action(self, tx: Tx, action: dict, principal: Principal, actuals: dict | None) -> None:
        outbox = tx.query("SELECT * FROM outbox WHERE action_id = ?", (action["action_id"],))
        if outbox and outbox[0]["state"] != OutboxState.DONE.value:
            tx.emit(
                self._event("outbox.state", {"outbox_id": outbox[0]["outbox_id"], "from": outbox[0]["state"], "to": OutboxState.DONE.value}, principal, action["run_id"])
            )
        for res in tx.query("SELECT * FROM reservations WHERE action_id = ? AND state = ?", (action["action_id"], ReservationState.HELD.value)):
            payload: dict[str, Any] = {"reservation_id": res["reservation_id"]}
            if actuals and res["reservation_id"] in actuals:
                payload.update(to=ReservationState.RECONCILED.value, actual=actuals[res["reservation_id"]])
            else:
                payload["to"] = ReservationState.RELEASED.value
            tx.emit(self._event("reservation.state", payload, principal, action["run_id"]))

    def cancel_action(self, principal: Principal, action_id: str, *, reason: str) -> dict:
        """Cancel an action that has not been dispatched yet."""
        self._require_trusted(principal, "cancel actions")
        with self.store.transaction() as tx:
            action = tx.require("actions", action_id)
            if action["state"] not in (ActionState.PLANNED.value, ActionState.RESERVED.value):
                raise InvalidTransition(f"action {action_id} is {action['state']}; only undispatched actions can be cancelled")
            tx.emit(self._event("action.transition", {"action_id": action_id, "from": action["state"], "to": ActionState.CANCELLED.value, "result": {"reason": reason}}, principal, action["run_id"]))
            self._finish_action(tx, action, principal, None)
            return dict(tx.require("actions", action_id))

    # ------------------------------------------------------------------ leases

    def acquire_lease(self, principal: Principal, resource: str, *, owner: str | None = None, lease_seconds: int = DEFAULT_ACTION_LEASE_SECONDS) -> dict:
        self._require_trusted(principal, "take leases directly")
        with self.store.transaction() as tx:
            return self._acquire_lease(tx, resource, owner or str(self.identity), lease_seconds, principal)

    def renew_lease(self, principal: Principal, resource: str, *, fence: int, lease_seconds: int) -> dict:
        check_int(lease_seconds, "lease_seconds", minimum=1, maximum=24 * 3600)
        with self.store.transaction() as tx:
            row = self._check_fence(tx, resource, fence)
            if principal.is_participant and row["owner"] != principal.id:
                raise Unauthorized("only the lease owner can renew it")
            tx.emit(self._event("lease.renewed", {"resource": resource, "fencing_token": fence, "expires_at": utc_after(lease_seconds, now=self.clock())}, principal, None))
            return dict(tx.require("leases", resource))

    def release_lease(self, principal: Principal, resource: str, *, fence: int) -> None:
        with self.store.transaction() as tx:
            row = self._check_fence(tx, resource, fence)
            if principal.is_participant and row["owner"] != principal.id:
                raise Unauthorized("only the lease owner can release it")
            tx.emit(self._event("lease.released", {"resource": resource, "fencing_token": fence}, principal, None))

    def check_fence(self, resource: str, fence: int) -> dict:
        return self._check_fence(self.store.read(), resource, fence)

    def _acquire_lease(self, tx: Tx, resource: str, owner: str, lease_seconds: int, principal: Principal) -> dict:
        check_text(resource, "resource", limit=MAX_ID + 16)
        check_int(lease_seconds, "lease_seconds", minimum=1, maximum=24 * 3600)
        row = tx.get("leases", resource)
        if row is not None and row["owner"] is not None and row["owner"] != owner:
            expired = row["expires_at"] is not None and parse_utc(row["expires_at"]) <= self._now()
            # Inside a write transaction: the liveness check must never spawn
            # a subprocess (sysctl/ps where /proc is missing). Without /proc it
            # is conservative; reconcile() does the full check outside the tx.
            if not expired and not owner_is_dead(row["owner"], allow_subprocess=False):
                raise Conflict(f"{resource} is leased to another owner until {row['expires_at']}", details={"owner": row["owner"]})
        token = (row["fencing_token"] if row else 0) + 1
        tx.emit(
            self._event(
                "lease.acquired",
                {"resource": resource, "owner": owner, "fencing_token": token, "expires_at": utc_after(lease_seconds, now=self.clock())},
                principal,
                None,
            )
        )
        return dict(tx.require("leases", resource))

    def _check_fence(self, tx: Tx, resource: str, fence: int) -> dict:
        check_int(fence, "fence", minimum=1)
        row = tx.get("leases", resource)
        if row is None or row["owner"] is None or row["fencing_token"] != fence:
            raise StaleLease(f"lease on {resource} is no longer held with token {fence}", details={"current": row["fencing_token"] if row else None})
        if row["expires_at"] is not None and parse_utc(row["expires_at"]) <= self._now():
            raise StaleLease(f"lease on {resource} expired at {row['expires_at']}")
        return row

    def _release_if_held(self, tx: Tx, resource: str, principal: Principal) -> None:
        row = tx.get("leases", resource)
        if row is not None and row["owner"] is not None:
            tx.emit(self._event("lease.released", {"resource": resource, "fencing_token": row["fencing_token"]}, principal, None))

    # ------------------------------------------------------------------ recovery

    def reconcile(self, principal: Principal = CONTROLLER) -> dict:
        """Run after a (re)start before dispatching anything.

        Leases whose owner process is provably dead, or that expired, are
        released. Actions that were DISPATCHING/RUNNING under such a lease
        become IN_DOUBT: their effects are unknown, so they are never retried
        automatically. Undispatched (RESERVED) work stays ready."""
        self._require_trusted(principal, "reconcile runtime state")
        report: dict[str, list[str]] = {"released_leases": [], "in_doubt": []}
        # Judge owner liveness before the write transaction: the full check may
        # spawn `ps`/`sysctl` where /proc is unavailable, which must never
        # happen while the transaction holds the database's write lock.
        dead_owned = self._leases_with_dead_owners()
        with self.store.transaction() as tx:
            now = self._now()
            for lease in tx.query("SELECT * FROM leases WHERE owner IS NOT NULL"):
                expired = lease["expires_at"] is not None and parse_utc(lease["expires_at"]) <= now
                # A dead-owner verdict applies only to the exact lease that was
                # judged: if the owner or fencing token changed in between, the
                # lease was re-acquired and is left alone.
                judged_dead = (lease["resource"], lease["owner"], lease["fencing_token"]) in dead_owned
                if not (expired or judged_dead):
                    continue
                resource = lease["resource"]
                if resource.startswith("action:"):
                    action = tx.get("actions", resource.split(":", 1)[1])
                    if action and action["state"] in (ActionState.DISPATCHING.value, ActionState.RUNNING.value):
                        tx.emit(self._event("action.transition", {"action_id": action["action_id"], "from": action["state"], "to": ActionState.IN_DOUBT.value, "reconciliation": "owner lost before an outcome was recorded"}, principal, action["run_id"]))
                        for ob in tx.query("SELECT * FROM outbox WHERE action_id = ? AND state = ?", (action["action_id"], OutboxState.CLAIMED.value)):
                            tx.emit(self._event("outbox.state", {"outbox_id": ob["outbox_id"], "from": ob["state"], "to": OutboxState.IN_DOUBT.value}, principal, action["run_id"]))
                        report["in_doubt"].append(action["action_id"])
                tx.emit(self._event("lease.released", {"resource": resource, "fencing_token": lease["fencing_token"]}, principal, None))
                report["released_leases"].append(resource)
        return report

    def _leases_with_dead_owners(self) -> set[tuple[str, str, int]]:
        """(resource, owner, fencing_token) of held leases whose owner process
        is provably dead. Runs outside any transaction (it may spawn `ps`)."""
        rows = self.store.read().query("SELECT resource, owner, fencing_token FROM leases WHERE owner IS NOT NULL")
        verdicts: dict[str, bool] = {}
        dead: set[tuple[str, str, int]] = set()
        for row in rows:
            owner = row["owner"]
            if owner not in verdicts:
                verdicts[owner] = owner_is_dead(owner)
            if verdicts[owner]:
                dead.add((row["resource"], owner, row["fencing_token"]))
        return dead

    # ------------------------------------------------------------------ status

    def status(self, principal: Principal, run_id: str) -> dict:
        self._bind_run(principal, run_id)
        tx = self.store.read()
        run = self._run_view(self._run(tx, run_id))
        return {
            "run": run,
            "participants": self.participants(principal, run_id),
            "tasks": self.tasks(principal, run_id),
            "open_messages": tx.scalar(
                f"SELECT COUNT(*) FROM messages WHERE run_id = ? AND state IN ({','.join('?' * len(OPEN_MESSAGE_STATES))})",
                (run_id, *OPEN_MESSAGE_STATES),
            ),
            "actions": {row["state"]: row["n"] for row in tx.query("SELECT state, COUNT(*) AS n FROM actions WHERE run_id = ? GROUP BY state", (run_id,))},
        }

    def _policy(self, tx: Tx, run: dict) -> AuthorisationPolicy:
        body = tx.require("policies", run["policy_hash"])
        return AuthorisationPolicy.from_dict(json.loads(body["body_json"]))


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {node: WHITE for node in graph}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        colour[node] = GREY
        stack.append(node)
        for dep in graph.get(node, []):
            if colour.get(dep, WHITE) == GREY:
                return stack[stack.index(dep):] + [dep]
            if colour.get(dep, WHITE) == WHITE:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        colour[node] = BLACK
        return None

    for node in list(graph):
        if colour[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return None
