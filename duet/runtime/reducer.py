"""Deterministic state transitions.

`apply(event, get)` maps the current rows (read through `get`) and one event
to the full rows it creates or updates. It never reads the clock, generates
ids or touches the database: everything comes from the event, so replaying
the event log from empty reproduces the materialised tables exactly.

The state machines below are the single source of truth for which
transitions exist. `IN_DOUBT` deliberately has no edge back to DISPATCHING:
an action whose effects are unknown is never blindly repeated."""
from __future__ import annotations

from typing import Callable

from .contracts import (
    ActionState,
    Collaboration,
    InvalidTransition,
    MessageState,
    NotFound,
    OutboxState,
    ReservationState,
    RunLifecycle,
    TaskState,
    ValidationError,
    canonical_json,
)
from .contracts import Event

Row = dict
Getter = Callable[[str, str], "Row | None"]
Upsert = tuple[str, Row]

# Table name -> (primary key column, columns). Must match migrations/*.sql.
TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "policies": ("policy_hash", ("policy_hash", "body_json", "created_at")),
    "runs": (
        "run_id",
        (
            "run_id", "repo_id", "objective", "scope_json", "base_sha", "acceptance_json", "acceptance_hash",
            "acceptance_version", "policy_hash", "initiating_participant", "lifecycle", "collaboration",
            "deadline_at", "state_version", "created_at", "updated_at",
        ),
    ),
    "participants": (
        "participant_id",
        (
            "participant_id", "run_id", "provider", "origin", "native_session_id", "connection_id",
            "capabilities_json", "account_scope", "workspace", "liveness", "token_hash", "state_version",
            "created_at", "updated_at",
        ),
    ),
    "tasks": (
        "task_id",
        (
            "task_id", "run_id", "parent_id", "description", "deliverables_json", "acceptance_ids_json",
            "depends_on_json", "required", "revision", "owner", "state", "blocked_reason", "next_action",
            "attempts", "proposed_by", "state_version", "created_at", "updated_at",
        ),
    ),
    "messages": (
        "message_id",
        (
            "message_id", "run_id", "seq", "sender", "recipient", "kind", "task_id", "reply_to", "correlation_id",
            "causation_id", "snapshot_ref", "body", "artifact_refs_json", "state", "expires_at", "created_at",
            "updated_at",
        ),
    ),
    "inbox_cursors": ("participant_id", ("participant_id", "acked_seq", "updated_at")),
    "actions": (
        "action_id",
        (
            "action_id", "run_id", "type", "task_id", "task_revision", "participant_id", "input_digest",
            "provider_invocation_id", "state", "result_json", "reconciliation", "state_version", "created_at",
            "updated_at",
        ),
    ),
    "reservations": (
        "reservation_id",
        (
            "reservation_id", "run_id", "action_id", "provider", "pool", "metric", "quantity_json", "category",
            "state", "expires_at", "actual_json", "created_at", "updated_at",
        ),
    ),
    "outbox": (
        "outbox_id",
        ("outbox_id", "run_id", "action_id", "state", "claimed_by", "fencing_token", "attempts", "created_at", "updated_at"),
    ),
    "approvals": (
        "approval_id",
        (
            "approval_id", "run_id", "scope", "action", "constraints_json", "policy_hash", "granted_by",
            "granted_at", "expires_at", "revoked_at",
        ),
    ),
    "leases": ("resource", ("resource", "owner", "fencing_token", "acquired_at", "expires_at", "released_at")),
}
REPLAYED_TABLES = tuple(TABLES)

# --- State machines -----------------------------------------------------------------

R = RunLifecycle
_ACTIVE_RUN = (R.PREFLIGHT, R.PLANNING, R.EXECUTING, R.REVIEWING, R.REPAIRING, R.VERIFYING, R.RECONCILING)
_PAUSED_RUN = (R.PAUSED_QUOTA, R.PAUSED_BUDGET, R.PAUSED_APPROVAL, R.PAUSED_CONTEXT)
_STOP_RUN = (*_PAUSED_RUN, R.FAILED, R.CANCELLED)

RUN_TRANSITIONS: dict[RunLifecycle, frozenset[RunLifecycle]] = {
    R.CREATED: frozenset({R.PREFLIGHT, R.FAILED, R.CANCELLED}),
    R.PREFLIGHT: frozenset({R.PLANNING, *_STOP_RUN}),
    R.PLANNING: frozenset({R.EXECUTING, *_STOP_RUN}),
    R.EXECUTING: frozenset({R.REVIEWING, R.VERIFYING, *_STOP_RUN}),
    R.REVIEWING: frozenset({R.EXECUTING, R.REPAIRING, R.VERIFYING, *_STOP_RUN}),
    R.REPAIRING: frozenset({R.REVIEWING, R.VERIFYING, *_STOP_RUN}),
    R.VERIFYING: frozenset({R.COMPLETED_VERIFIED, R.REPAIRING, R.REVIEWING, *_STOP_RUN}),
    R.RECONCILING: frozenset({R.PLANNING, R.EXECUTING, R.REVIEWING, R.REPAIRING, R.VERIFYING, *_STOP_RUN}),
    **{paused: frozenset({R.RECONCILING, R.FAILED, R.CANCELLED}) for paused in _PAUSED_RUN},
    R.COMPLETED_VERIFIED: frozenset(),
    R.FAILED: frozenset(),
    R.CANCELLED: frozenset(),
}

T = TaskState
TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    T.PROPOSED: frozenset({T.READY, T.BLOCKED, T.CANCELLED}),
    T.READY: frozenset({T.CLAIMED, T.BLOCKED, T.CANCELLED}),
    T.CLAIMED: frozenset({T.RUNNING, T.READY, T.BLOCKED, T.CANCELLED}),
    T.RUNNING: frozenset({T.WAITING_PEER, T.REVIEW_REQUIRED, T.READY, T.BLOCKED, T.CANCELLED}),
    T.WAITING_PEER: frozenset({T.RUNNING, T.BLOCKED, T.CANCELLED}),
    T.REVIEW_REQUIRED: frozenset({T.CHANGES_REQUESTED, T.VERIFIED, T.BLOCKED, T.CANCELLED}),
    T.CHANGES_REQUESTED: frozenset({T.CLAIMED, T.READY, T.BLOCKED, T.CANCELLED}),
    # Changed evidence can make a verified task need review again (R10).
    T.VERIFIED: frozenset({T.REVIEW_REQUIRED}),
    T.BLOCKED: frozenset({T.READY, T.CANCELLED}),
    T.CANCELLED: frozenset(),
}

A = ActionState
ACTION_TRANSITIONS: dict[ActionState, frozenset[ActionState]] = {
    A.PLANNED: frozenset({A.RESERVED, A.CANCELLED}),
    A.RESERVED: frozenset({A.DISPATCHING, A.CANCELLED}),
    A.DISPATCHING: frozenset({A.RUNNING, A.FAILED, A.IN_DOUBT}),
    A.RUNNING: frozenset({A.SUCCEEDED, A.FAILED, A.CANCELLED, A.IN_DOUBT}),
    # Only reconciliation with evidence leaves IN_DOUBT, and never by re-dispatch.
    A.IN_DOUBT: frozenset({A.SUCCEEDED, A.FAILED, A.CANCELLED}),
    A.SUCCEEDED: frozenset(),
    A.FAILED: frozenset(),
    A.CANCELLED: frozenset(),
}

M = MessageState
MESSAGE_TRANSITIONS: dict[MessageState, frozenset[MessageState]] = {
    M.QUEUED: frozenset({M.TRANSPORT_DELIVERED, M.PARTICIPANT_ACKNOWLEDGED, M.EXPIRED, M.CANCELLED}),
    M.TRANSPORT_DELIVERED: frozenset({M.PARTICIPANT_ACKNOWLEDGED, M.EXPIRED, M.CANCELLED}),
    M.PARTICIPANT_ACKNOWLEDGED: frozenset({M.HANDLED, M.EXPIRED, M.CANCELLED}),
    M.HANDLED: frozenset(),
    M.EXPIRED: frozenset(),
    M.CANCELLED: frozenset(),
}


def check_transition(machine: dict, current, target, what: str) -> None:
    if target not in machine.get(current, frozenset()):
        raise InvalidTransition(
            f"{what}: {getattr(current, 'value', current)} -> {getattr(target, 'value', target)} is not allowed",
            details={"from": getattr(current, "value", current), "to": getattr(target, "value", target)},
        )


# --- Reducer -------------------------------------------------------------------------


def apply(event: Event, get: Getter) -> list[Upsert]:
    handler = _HANDLERS.get(event.type)
    if handler is None:
        raise ValidationError(f"unknown event type {event.type!r}")
    return handler(event.payload, event, get)


def _require(get: Getter, table: str, key: str) -> Row:
    row = get(table, key)
    if row is None:
        raise NotFound(f"{table[:-1] if table.endswith('s') else table} {key} not found")
    return dict(row)


def _expect(row: Row, column: str, expected: str, what: str) -> None:
    if row[column] != expected:
        raise InvalidTransition(
            f"{what}: expected state {expected}, found {row[column]}",
            details={"expected": expected, "actual": row[column]},
        )


def _j(value) -> str:
    return canonical_json(value)


def _policy_registered(p: dict, e: Event, get: Getter) -> list[Upsert]:
    if get("policies", p["policy_hash"]) is not None:
        return []
    return [("policies", {"policy_hash": p["policy_hash"], "body_json": _j(p["body"]), "created_at": e.at})]


def _run_created(p: dict, e: Event, get: Getter) -> list[Upsert]:
    if get("runs", p["run_id"]) is not None:
        raise InvalidTransition(f"run {p['run_id']} already exists")
    return [
        (
            "runs",
            {
                "run_id": p["run_id"],
                "repo_id": p["repo_id"],
                "objective": p["objective"],
                "scope_json": _j(p["scope"]),
                "base_sha": p.get("base_sha"),
                "acceptance_json": _j(p["acceptance"]),
                "acceptance_hash": p["acceptance_hash"],
                "acceptance_version": 1,
                "policy_hash": p["policy_hash"],
                "initiating_participant": None,
                "lifecycle": RunLifecycle.CREATED.value,
                "collaboration": Collaboration.PAIR_ACTIVE.value,
                "deadline_at": p.get("deadline_at"),
                "state_version": 1,
                "created_at": e.at,
                "updated_at": e.at,
            },
        )
    ]


def _run_lifecycle(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "runs", p["run_id"])
    _expect(row, "lifecycle", p["from"], "run lifecycle")
    check_transition(RUN_TRANSITIONS, RunLifecycle(p["from"]), RunLifecycle(p["to"]), "run lifecycle")
    row.update(lifecycle=p["to"], state_version=row["state_version"] + 1, updated_at=e.at)
    return [("runs", row)]


def _run_collaboration(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "runs", p["run_id"])
    Collaboration(p["to"])
    row.update(collaboration=p["to"], state_version=row["state_version"] + 1, updated_at=e.at)
    return [("runs", row)]


def _run_acceptance(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "runs", p["run_id"])
    if row["acceptance_version"] + 1 != p["version"]:
        raise InvalidTransition("acceptance contract versions must increase by one")
    row.update(
        acceptance_json=_j(p["acceptance"]),
        acceptance_hash=p["acceptance_hash"],
        acceptance_version=p["version"],
        state_version=row["state_version"] + 1,
        updated_at=e.at,
    )
    return [("runs", row)]


def _run_initiator(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "runs", p["run_id"])
    row.update(initiating_participant=p["participant_id"], state_version=row["state_version"] + 1, updated_at=e.at)
    return [("runs", row)]


def _participant_registered(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "runs", p["run_id"])
    if get("participants", p["participant_id"]) is not None:
        raise InvalidTransition(f"participant {p['participant_id']} already exists")
    return [
        (
            "participants",
            {
                "participant_id": p["participant_id"],
                "run_id": p["run_id"],
                "provider": p["provider"],
                "origin": p["origin"],
                "native_session_id": p.get("native_session_id"),
                "connection_id": p.get("connection_id"),
                "capabilities_json": _j(p["capabilities"]),
                "account_scope": p.get("account_scope"),
                "workspace": p.get("workspace"),
                "liveness": p["liveness"],
                "token_hash": p["token_hash"],
                "state_version": 1,
                "created_at": e.at,
                "updated_at": e.at,
            },
        ),
        ("inbox_cursors", {"participant_id": p["participant_id"], "acked_seq": 0, "updated_at": e.at}),
    ]


def _participant_updated(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "participants", p["participant_id"])
    for column in ("liveness", "native_session_id", "connection_id", "workspace"):
        if column in p:
            row[column] = p[column]
    if "capabilities" in p:
        row["capabilities_json"] = _j(p["capabilities"])
    row.update(state_version=row["state_version"] + 1, updated_at=e.at)
    return [("participants", row)]


def _task_proposed(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "runs", p["run_id"])
    if get("tasks", p["task_id"]) is not None:
        raise InvalidTransition(f"task {p['task_id']} already exists")
    TaskState(p["state"])
    return [
        (
            "tasks",
            {
                "task_id": p["task_id"],
                "run_id": p["run_id"],
                "parent_id": p.get("parent_id"),
                "description": p["description"],
                "deliverables_json": _j(p["deliverables"]),
                "acceptance_ids_json": _j(p["acceptance_ids"]),
                "depends_on_json": _j(p["depends_on"]),
                "required": 1 if p["required"] else 0,
                "revision": 1,
                "owner": None,
                "state": p["state"],
                "blocked_reason": None,
                "next_action": None,
                "attempts": 0,
                "proposed_by": p["proposed_by"],
                "state_version": 1,
                "created_at": e.at,
                "updated_at": e.at,
            },
        )
    ]


def _task_transition(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "tasks", p["task_id"])
    _expect(row, "state", p["from"], "task")
    target = TaskState(p["to"])
    check_transition(TASK_TRANSITIONS, TaskState(p["from"]), target, "task")
    if target == TaskState.BLOCKED and not (p.get("blocked_reason") and p.get("next_action")):
        raise ValidationError("a blocked task needs a concrete blocked_reason and next_action")
    row["state"] = target.value
    if "owner" in p:
        row["owner"] = p["owner"]
    row["blocked_reason"] = p.get("blocked_reason") if target == TaskState.BLOCKED else None
    row["next_action"] = p.get("next_action") if target == TaskState.BLOCKED else None
    if target == TaskState.RUNNING and p["from"] != TaskState.WAITING_PEER.value:
        row["attempts"] = row["attempts"] + 1
    if p.get("bump_revision"):
        row["revision"] = row["revision"] + 1
    row.update(state_version=row["state_version"] + 1, updated_at=e.at)
    return [("tasks", row)]


def _task_dependencies(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "tasks", p["task_id"])
    if TaskState(row["state"]) in (TaskState.VERIFIED, TaskState.CANCELLED):
        raise InvalidTransition(f"cannot rewire dependencies of a {row['state']} task")
    row.update(depends_on_json=_j(p["depends_on"]), state_version=row["state_version"] + 1, updated_at=e.at)
    return [("tasks", row)]


def _message_queued(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "runs", p["run_id"])
    if get("messages", p["message_id"]) is not None:
        raise InvalidTransition(f"message {p['message_id']} already exists")
    return [
        (
            "messages",
            {
                "message_id": p["message_id"],
                "run_id": p["run_id"],
                "seq": p["seq"],
                "sender": p["sender"],
                "recipient": p.get("recipient"),
                "kind": p["kind"],
                "task_id": p.get("task_id"),
                "reply_to": p.get("reply_to"),
                "correlation_id": p.get("correlation_id"),
                "causation_id": p.get("causation_id"),
                "snapshot_ref": p.get("snapshot_ref"),
                "body": p["body"],
                "artifact_refs_json": _j(p.get("artifact_refs", [])),
                "state": MessageState.QUEUED.value,
                "expires_at": p.get("expires_at"),
                "created_at": e.at,
                "updated_at": e.at,
            },
        )
    ]


def _message_state(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "messages", p["message_id"])
    _expect(row, "state", p["from"], "message")
    check_transition(MESSAGE_TRANSITIONS, MessageState(p["from"]), MessageState(p["to"]), "message")
    row.update(state=p["to"], updated_at=e.at)
    return [("messages", row)]


def _inbox_acked(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "inbox_cursors", p["participant_id"])
    if p["acked_seq"] < row["acked_seq"]:
        raise InvalidTransition("inbox cursor cannot move backwards")
    row.update(acked_seq=p["acked_seq"], updated_at=e.at)
    return [("inbox_cursors", row)]


def _action_planned(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "runs", p["run_id"])
    if get("actions", p["action_id"]) is not None:
        raise InvalidTransition(f"action {p['action_id']} already exists")
    state = ActionState.RESERVED if p.get("reserve", True) else ActionState.PLANNED
    upserts: list[Upsert] = [
        (
            "actions",
            {
                "action_id": p["action_id"],
                "run_id": p["run_id"],
                "type": p["type"],
                "task_id": p.get("task_id"),
                "task_revision": p.get("task_revision"),
                "participant_id": p.get("participant_id"),
                "input_digest": p["input_digest"],
                "provider_invocation_id": None,
                "state": state.value,
                "result_json": None,
                "reconciliation": None,
                "state_version": 1,
                "created_at": e.at,
                "updated_at": e.at,
            },
        )
    ]
    for res in p.get("reservations", []):
        upserts.append(
            (
                "reservations",
                {
                    "reservation_id": res["reservation_id"],
                    "run_id": p["run_id"],
                    "action_id": p["action_id"],
                    "provider": res["provider"],
                    "pool": res["pool"],
                    "metric": res["metric"],
                    "quantity_json": _j(res["quantity"]),
                    "category": res["category"],
                    "state": ReservationState.HELD.value,
                    "expires_at": res.get("expires_at"),
                    "actual_json": None,
                    "created_at": e.at,
                    "updated_at": e.at,
                },
            )
        )
    if state == ActionState.RESERVED:
        upserts.append(
            (
                "outbox",
                {
                    "outbox_id": p["outbox_id"],
                    "run_id": p["run_id"],
                    "action_id": p["action_id"],
                    "state": OutboxState.PENDING.value,
                    "claimed_by": None,
                    "fencing_token": None,
                    "attempts": 0,
                    "created_at": e.at,
                    "updated_at": e.at,
                },
            )
        )
    return upserts


def _action_transition(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "actions", p["action_id"])
    _expect(row, "state", p["from"], "action")
    target = ActionState(p["to"])
    check_transition(ACTION_TRANSITIONS, ActionState(p["from"]), target, "action")
    if ActionState(p["from"]) == ActionState.IN_DOUBT and not p.get("reconciliation"):
        raise ValidationError("leaving IN_DOUBT requires a reconciliation record")
    row["state"] = target.value
    if "result" in p:
        row["result_json"] = _j(p["result"])
    if p.get("provider_invocation_id"):
        row["provider_invocation_id"] = p["provider_invocation_id"]
    if p.get("reconciliation"):
        row["reconciliation"] = p["reconciliation"]
    row.update(state_version=row["state_version"] + 1, updated_at=e.at)
    return [("actions", row)]


def _outbox_claimed(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "outbox", p["outbox_id"])
    _expect(row, "state", OutboxState.PENDING.value, "outbox claim")
    row.update(
        state=OutboxState.CLAIMED.value,
        claimed_by=p["claimed_by"],
        fencing_token=p["fencing_token"],
        attempts=row["attempts"] + 1,
        updated_at=e.at,
    )
    return [("outbox", row)]


def _outbox_state(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "outbox", p["outbox_id"])
    _expect(row, "state", p["from"], "outbox")
    allowed = {
        OutboxState.CLAIMED.value: {OutboxState.DONE.value, OutboxState.IN_DOUBT.value},
        OutboxState.PENDING.value: {OutboxState.DONE.value},  # cancelled before dispatch
        OutboxState.IN_DOUBT.value: {OutboxState.DONE.value},
    }
    if p["to"] not in allowed.get(p["from"], set()):
        raise InvalidTransition(f"outbox: {p['from']} -> {p['to']} is not allowed")
    row.update(state=p["to"], updated_at=e.at)
    return [("outbox", row)]


def _reservation_state(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "reservations", p["reservation_id"])
    if row["state"] != ReservationState.HELD.value:
        raise InvalidTransition(f"reservation {p['reservation_id']} is already {row['state']}")
    ReservationState(p["to"])
    row["state"] = p["to"]
    if "actual" in p:
        row["actual_json"] = _j(p["actual"])
    row["updated_at"] = e.at
    return [("reservations", row)]


def _approval_granted(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "runs", p["run_id"])
    return [
        (
            "approvals",
            {
                "approval_id": p["approval_id"],
                "run_id": p["run_id"],
                "scope": p["scope"],
                "action": p["action"],
                "constraints_json": _j(p["constraints"]),
                "policy_hash": p["policy_hash"],
                "granted_by": p["granted_by"],
                "granted_at": e.at,
                "expires_at": p.get("expires_at"),
                "revoked_at": None,
            },
        )
    ]


def _approval_revoked(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "approvals", p["approval_id"])
    if row["revoked_at"] is not None:
        return []
    row["revoked_at"] = e.at
    return [("approvals", row)]


def _lease_acquired(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = get("leases", p["resource"])
    previous = row["fencing_token"] if row else 0
    if p["fencing_token"] != previous + 1:
        raise InvalidTransition("fencing tokens must increase by exactly one per acquisition")
    return [
        (
            "leases",
            {
                "resource": p["resource"],
                "owner": p["owner"],
                "fencing_token": p["fencing_token"],
                "acquired_at": e.at,
                "expires_at": p["expires_at"],
                "released_at": None,
            },
        )
    ]


def _lease_renewed(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "leases", p["resource"])
    if row["fencing_token"] != p["fencing_token"] or row["released_at"] is not None:
        raise InvalidTransition("cannot renew a lease that is no longer held")
    row["expires_at"] = p["expires_at"]
    return [("leases", row)]


def _lease_released(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "leases", p["resource"])
    if row["fencing_token"] != p["fencing_token"]:
        raise InvalidTransition("cannot release a lease that is no longer held")
    row.update(owner=None, released_at=e.at)
    return [("leases", row)]


_HANDLERS: dict[str, Callable[[dict, Event, Getter], list[Upsert]]] = {
    "policy.registered": _policy_registered,
    "run.created": _run_created,
    "run.lifecycle": _run_lifecycle,
    "run.collaboration": _run_collaboration,
    "run.acceptance": _run_acceptance,
    "run.initiator": _run_initiator,
    "participant.registered": _participant_registered,
    "participant.updated": _participant_updated,
    "task.proposed": _task_proposed,
    "task.transition": _task_transition,
    "task.dependencies": _task_dependencies,
    "message.queued": _message_queued,
    "message.state": _message_state,
    "inbox.acked": _inbox_acked,
    "action.planned": _action_planned,
    "action.transition": _action_transition,
    "outbox.claimed": _outbox_claimed,
    "outbox.state": _outbox_state,
    "reservation.state": _reservation_state,
    "approval.granted": _approval_granted,
    "approval.revoked": _approval_revoked,
    "lease.acquired": _lease_acquired,
    "lease.renewed": _lease_renewed,
    "lease.released": _lease_released,
}
EVENT_TYPES = frozenset(_HANDLERS)


class MemoryState:
    """In-memory table set for replay: fold events through `apply`."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, Row]] = {name: {} for name in TABLES}

    def get(self, table: str, key: str) -> Row | None:
        row = self.tables[table].get(key)
        return dict(row) if row is not None else None

    def apply(self, event: Event) -> None:
        for table, row in apply(event, self.get):
            pk = TABLES[table][0]
            self.tables[table][row[pk]] = dict(row)
