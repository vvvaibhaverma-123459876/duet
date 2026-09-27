"""Deterministic state transitions.

`apply(event, get)` maps the current rows (read through `get`) and one event
to the full rows it creates or updates. It never reads the clock, generates
ids or touches the database: everything comes from the event, so replaying
the event log from empty reproduces the materialised tables exactly.

The state machines below are the single source of truth for which
transitions exist. `IN_DOUBT` deliberately has no edge back to DISPATCHING:
an action whose effects are unknown is never blindly repeated."""
from __future__ import annotations

import json
from decimal import Decimal
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
            "attempts", "proposed_by", "state_version", "created_at", "updated_at", "kind",
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
    # D03: evidence model (migration 0002)
    "snapshots": (
        "snapshot_id",
        ("snapshot_id", "run_id", "tree_hash", "base_sha", "author", "file_count", "changed_json", "excluded_json", "manifest_ref", "created_at"),
    ),
    "evidence": (
        "evidence_id",
        (
            "evidence_id", "run_id", "check_id", "snapshot_id", "acceptance_hash", "argv_json", "cwd", "env_fingerprint",
            "status", "exit_code", "started_at", "ended_at", "output_hash", "artifact_ref", "producer", "trust", "detail",
        ),
    ),
    "reviews": (
        "review_id",
        ("review_id", "run_id", "reviewer", "reviewer_provider", "snapshot_id", "acceptance_hash", "scope_json", "disposition", "summary", "created_at"),
    ),
    "findings": (
        "finding_id",
        ("finding_id", "run_id", "review_id", "severity", "summary", "location", "status", "resolution", "resolved_by", "created_at", "updated_at"),
    ),
    "checkpoints": (
        "checkpoint_id",
        ("checkpoint_id", "run_id", "snapshot_id", "acceptance_hash", "report_hash", "artifact_ref", "created_at"),
    ),
    # D05: pairing invitations (migration 0003)
    "invites": (
        "invite_id",
        ("invite_id", "run_id", "provider", "code_hash", "created_by", "expires_at", "used_by", "used_at", "created_at"),
    ),
    # D06: task graph (migration 0004)
    "plans": (
        "plan_id",
        ("plan_id", "run_id", "proposer", "rationale", "tasks_json", "state", "decided_by", "decision_reason", "created_task_ids_json", "created_at", "updated_at"),
    ),
    "task_results": (
        "result_id",
        ("result_id", "task_id", "run_id", "author", "summary", "artifact_ref", "snapshot_id", "decision", "decided_by", "decision_reason", "created_at", "updated_at"),
    ),
    "contributions": (
        "contribution_id",
        ("contribution_id", "run_id", "participant_id", "provider", "kind", "ref", "summary", "created_at"),
    ),
    "interventions": (
        "intervention_id",
        ("intervention_id", "run_id", "status", "task_id", "reason", "evidence_json", "fingerprint", "created_at"),
    ),
    # D07: usage pools and records (migration 0005)
    "usage_pools": (
        "pool_id",
        ("pool_id", "provider", "metric", "unit", "allowance_json", "window_seconds", "enforcement", "defined_by", "created_at", "updated_at"),
    ),
    "usage_records": (
        "record_id",
        ("record_id", "pool_id", "run_id", "action_id", "participant_id", "metric", "quantity_json", "quality", "source", "observed_at", "created_at"),
    ),
    # D08: finishing reserves, provider quota, admission decisions (migration 0006)
    "finishing_reserves": (
        "reservation_id",
        ("reservation_id", "run_id", "purpose", "provider", "pool", "metric", "units_planned", "units_left", "per_unit_json", "shortfall_json", "created_at", "updated_at"),
    ),
    "quota_gauges": (
        "gauge_id",
        ("gauge_id", "provider", "window", "used_percent_json", "previous_percent_json", "resets_at", "observed_at", "source", "created_at", "updated_at"),
    ),
    "quota_holds": (
        "provider",
        ("provider", "state", "reason", "resume_at", "attempts", "probe_action_id", "placed_at", "updated_at"),
    ),
    "admissions": (
        "admission_id",
        ("admission_id", "run_id", "participant_id", "action_id", "provider", "action_class", "purpose", "verdict", "reason", "detail_json", "created_at"),
    ),
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
                "kind": p.get("kind", "code"),
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
        if res.get("draw_from"):
            upserts += _draw(res, p["run_id"], e, get)
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


def _draw(res: dict, run_id: str, e: Event, get: Getter) -> list[Upsert]:
    """A finishing action draws from its run's earmarked reserve: the reserve
    shrinks by exactly what the action's reservation holds, so the capacity
    is held once, never twice (AT14)."""
    source = _require(get, "reservations", res["draw_from"])
    finishing = _require(get, "finishing_reserves", res["draw_from"])
    if source["state"] != ReservationState.HELD.value or source["run_id"] != run_id or source["pool"] != res["pool"] or source["metric"] != res["metric"]:
        raise InvalidTransition(f"reservation {res['draw_from']} cannot fund this action")
    left = Decimal(json.loads(source["quantity_json"])) - Decimal(str(res["quantity"]))
    if left < 0 or finishing["units_left"] < 1:
        raise InvalidTransition(f"finishing reserve {res['draw_from']} does not hold enough for this action")
    source.update(quantity_json=_j(str(left)), updated_at=e.at)
    finishing.update(units_left=finishing["units_left"] - 1, updated_at=e.at)
    return [("reservations", source), ("finishing_reserves", finishing)]


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


def _snapshot_recorded(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "runs", p["run_id"])
    existing = get("snapshots", p["snapshot_id"])
    if existing is not None:
        if existing["tree_hash"] != p["tree_hash"]:
            raise InvalidTransition("snapshot id collision with a different tree")
        return []  # recording the same tree twice is a no-op
    return [
        (
            "snapshots",
            {
                "snapshot_id": p["snapshot_id"],
                "run_id": p["run_id"],
                "tree_hash": p["tree_hash"],
                "base_sha": p.get("base_sha"),
                "author": p.get("author"),
                "file_count": p["file_count"],
                "changed_json": _j(p["changed"]),
                "excluded_json": _j(p["excluded"]),
                "manifest_ref": p.get("manifest_ref"),
                "created_at": e.at,
            },
        )
    ]


def _evidence_recorded(p: dict, e: Event, get: Getter) -> list[Upsert]:
    snapshot = _require(get, "snapshots", p["snapshot_id"])
    if snapshot["run_id"] != p["run_id"]:
        raise InvalidTransition("evidence snapshot belongs to another run")
    if p["status"] not in ("passed", "failed", "unknown", "invalidated", "cancelled"):
        raise ValidationError(f"unknown evidence status {p['status']!r}")
    return [
        (
            "evidence",
            {
                "evidence_id": p["evidence_id"],
                "run_id": p["run_id"],
                "check_id": p["check_id"],
                "snapshot_id": p["snapshot_id"],
                "acceptance_hash": p["acceptance_hash"],
                "argv_json": _j(p["argv"]),
                "cwd": p["cwd"],
                "env_fingerprint": p["env_fingerprint"],
                "status": p["status"],
                "exit_code": p.get("exit_code"),
                "started_at": p["started_at"],
                "ended_at": p["ended_at"],
                "output_hash": p["output_hash"],
                "artifact_ref": p.get("artifact_ref"),
                "producer": p["producer"],
                "trust": p["trust"],
                "detail": p.get("detail", ""),
            },
        )
    ]


def _review_submitted(p: dict, e: Event, get: Getter) -> list[Upsert]:
    snapshot = _require(get, "snapshots", p["snapshot_id"])
    if snapshot["run_id"] != p["run_id"]:
        raise InvalidTransition("reviewed snapshot belongs to another run")
    if p["disposition"] not in ("approve", "changes_requested", "comment"):
        raise ValidationError(f"unknown review disposition {p['disposition']!r}")
    upserts: list[Upsert] = [
        (
            "reviews",
            {
                "review_id": p["review_id"],
                "run_id": p["run_id"],
                "reviewer": p["reviewer"],
                "reviewer_provider": p["reviewer_provider"],
                "snapshot_id": p["snapshot_id"],
                "acceptance_hash": p["acceptance_hash"],
                "scope_json": _j(p["scope"]),
                "disposition": p["disposition"],
                "summary": p["summary"],
                "created_at": e.at,
            },
        )
    ]
    for finding in p.get("findings", []):
        if finding["severity"] not in ("blocking", "non_blocking"):
            raise ValidationError(f"unknown finding severity {finding['severity']!r}")
        upserts.append(
            (
                "findings",
                {
                    "finding_id": finding["finding_id"],
                    "run_id": p["run_id"],
                    "review_id": p["review_id"],
                    "severity": finding["severity"],
                    "summary": finding["summary"],
                    "location": finding.get("location"),
                    "status": "open",
                    "resolution": None,
                    "resolved_by": None,
                    "created_at": e.at,
                    "updated_at": e.at,
                },
            )
        )
    return upserts


def _finding_resolved(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "findings", p["finding_id"])
    if row["status"] != "open":
        raise InvalidTransition(f"finding {p['finding_id']} is already {row['status']}")
    if p["status"] not in ("resolved", "withdrawn"):
        raise ValidationError(f"unknown finding status {p['status']!r}")
    row.update(status=p["status"], resolution=p["resolution"], resolved_by=p["resolved_by"], updated_at=e.at)
    return [("findings", row)]


def _checkpoint_exported(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "snapshots", p["snapshot_id"])
    return [
        (
            "checkpoints",
            {
                "checkpoint_id": p["checkpoint_id"],
                "run_id": p["run_id"],
                "snapshot_id": p["snapshot_id"],
                "acceptance_hash": p["acceptance_hash"],
                "report_hash": p["report_hash"],
                "artifact_ref": p["artifact_ref"],
                "created_at": e.at,
            },
        )
    ]


def _invite_created(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "runs", p["run_id"])
    if get("invites", p["invite_id"]) is not None:
        raise InvalidTransition(f"invite {p['invite_id']} already exists")
    return [
        (
            "invites",
            {
                "invite_id": p["invite_id"],
                "run_id": p["run_id"],
                "provider": p["provider"],
                "code_hash": p["code_hash"],
                "created_by": p["created_by"],
                "expires_at": p["expires_at"],
                "used_by": None,
                "used_at": None,
                "created_at": e.at,
            },
        )
    ]


def _invite_used(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "invites", p["invite_id"])
    if row["used_by"] is not None:
        raise InvalidTransition(f"invite {p['invite_id']} was already used")
    _require(get, "participants", p["participant_id"])
    row.update(used_by=p["participant_id"], used_at=e.at)
    return [("invites", row)]


PLAN_STATES = ("PROPOSED", "ACCEPTED", "REJECTED", "WITHDRAWN")
INTERVENTION_STATUSES = ("replan", "pause", "stalled")


def _plan_proposed(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "runs", p["run_id"])
    if get("plans", p["plan_id"]) is not None:
        raise InvalidTransition(f"plan {p['plan_id']} already exists")
    return [
        (
            "plans",
            {
                "plan_id": p["plan_id"], "run_id": p["run_id"], "proposer": p["proposer"], "rationale": p["rationale"],
                "tasks_json": _j(p["tasks"]), "state": "PROPOSED", "decided_by": None, "decision_reason": None,
                "created_task_ids_json": "[]", "created_at": e.at, "updated_at": e.at,
            },
        )
    ]


def _plan_decided(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "plans", p["plan_id"])
    if row["state"] != "PROPOSED":
        raise InvalidTransition(f"plan {p['plan_id']} is {row['state']}, not PROPOSED")
    if p["state"] not in PLAN_STATES[1:]:
        raise ValidationError(f"invalid plan decision {p['state']!r}")
    row.update(
        state=p["state"], decided_by=p.get("decided_by"), decision_reason=p.get("reason"),
        created_task_ids_json=_j(p.get("created_task_ids", [])), updated_at=e.at,
    )
    return [("plans", row)]


def _task_result(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "tasks", p["task_id"])
    if get("task_results", p["result_id"]) is not None:
        raise InvalidTransition(f"result {p['result_id']} already exists")
    return [
        (
            "task_results",
            {
                "result_id": p["result_id"], "task_id": p["task_id"], "run_id": p["run_id"], "author": p["author"],
                "summary": p["summary"], "artifact_ref": p.get("artifact_ref"), "snapshot_id": p.get("snapshot_id"),
                "decision": None, "decided_by": None, "decision_reason": None, "created_at": e.at, "updated_at": e.at,
            },
        )
    ]


def _task_result_decided(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "task_results", p["result_id"])
    if row["decision"] is not None:
        raise InvalidTransition(f"result {p['result_id']} was already decided")
    if p["decision"] not in ("accepted", "rejected"):
        raise ValidationError(f"invalid result decision {p['decision']!r}")
    row.update(decision=p["decision"], decided_by=p["decided_by"], decision_reason=p.get("reason"), updated_at=e.at)
    return [("task_results", row)]


def _contribution_recorded(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "runs", p["run_id"])
    if get("contributions", p["contribution_id"]) is not None:
        raise InvalidTransition(f"contribution {p['contribution_id']} already recorded")
    return [
        (
            "contributions",
            {
                "contribution_id": p["contribution_id"], "run_id": p["run_id"], "participant_id": p["participant_id"],
                "provider": p["provider"], "kind": p["kind"], "ref": p["ref"], "summary": p["summary"], "created_at": e.at,
            },
        )
    ]


def _intervention_recorded(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "runs", p["run_id"])
    if p["status"] not in INTERVENTION_STATUSES:
        raise ValidationError(f"invalid intervention status {p['status']!r}")
    return [
        (
            "interventions",
            {
                "intervention_id": p["intervention_id"], "run_id": p["run_id"], "status": p["status"], "task_id": p.get("task_id"),
                "reason": p["reason"], "evidence_json": _j(p.get("evidence", [])), "fingerprint": p.get("fingerprint"), "created_at": e.at,
            },
        )
    ]


ENFORCEMENT = ("local_bound", "best_effort", "provider_cap")


def _pool_defined(p: dict, e: Event, get: Getter) -> list[Upsert]:
    if p["enforcement"] not in ENFORCEMENT:
        raise ValidationError(f"enforcement must be one of {ENFORCEMENT}")
    existing = get("usage_pools", p["pool_id"])
    return [
        (
            "usage_pools",
            {
                "pool_id": p["pool_id"], "provider": p["provider"], "metric": p["metric"], "unit": p["unit"],
                "allowance_json": _j(p["allowance"]) if p.get("allowance") is not None else None,
                "window_seconds": p.get("window_seconds"), "enforcement": p["enforcement"], "defined_by": p["defined_by"],
                "created_at": existing["created_at"] if existing else e.at, "updated_at": e.at,
            },
        )
    ]


def _usage_recorded(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "usage_pools", p["pool_id"])
    if get("usage_records", p["record_id"]) is not None:
        raise InvalidTransition(f"usage record {p['record_id']} already exists")
    return [
        (
            "usage_records",
            {
                "record_id": p["record_id"], "pool_id": p["pool_id"], "run_id": p.get("run_id"), "action_id": p.get("action_id"),
                "participant_id": p.get("participant_id"), "metric": p["metric"], "quantity_json": _j(p["quantity"]),
                "quality": p["quality"], "source": p["source"], "observed_at": p["observed_at"], "created_at": e.at,
            },
        )
    ]


def _finishing_reserved(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "runs", p["run_id"])
    if get("reservations", p["reservation_id"]) is not None:
        raise InvalidTransition(f"reservation {p['reservation_id']} already exists")
    if p["purpose"] not in ("review", "repair") or int(p["units"]) < 1:
        raise ValidationError("a finishing reserve is for review or repair, and at least one turn")
    return [
        (
            "reservations",
            {
                "reservation_id": p["reservation_id"], "run_id": p["run_id"], "action_id": None, "provider": p["provider"],
                "pool": p["pool"], "metric": p["metric"], "quantity_json": _j(p["quantity"]), "category": f"finishing:{p['purpose']}",
                "state": ReservationState.HELD.value, "expires_at": None, "actual_json": None, "created_at": e.at, "updated_at": e.at,
            },
        ),
        (
            "finishing_reserves",
            {
                "reservation_id": p["reservation_id"], "run_id": p["run_id"], "purpose": p["purpose"], "provider": p["provider"],
                "pool": p["pool"], "metric": p["metric"], "units_planned": int(p["units"]), "units_left": int(p["units"]),
                "per_unit_json": _j(p["per_unit"]) if p.get("per_unit") is not None else None,
                "shortfall_json": _j(p["shortfall"]) if p.get("shortfall") is not None else None,
                "created_at": e.at, "updated_at": e.at,
            },
        ),
    ]


def _finishing_resized(p: dict, e: Event, get: Getter) -> list[Upsert]:
    reservation = _require(get, "reservations", p["reservation_id"])
    finishing = _require(get, "finishing_reserves", p["reservation_id"])
    _expect(reservation, "state", ReservationState.HELD.value, "finishing reserve")
    reservation.update(quantity_json=_j(p["quantity"]), updated_at=e.at)
    finishing.update(
        per_unit_json=_j(p["per_unit"]) if p.get("per_unit") is not None else None,
        shortfall_json=_j(p["shortfall"]) if p.get("shortfall") is not None else None,
        updated_at=e.at,
    )
    return [("reservations", reservation), ("finishing_reserves", finishing)]


def _quota_observed(p: dict, e: Event, get: Getter) -> list[Upsert]:
    gauge_id = f"{p['provider']}:{p['window']}"
    existing = get("quota_gauges", gauge_id)
    if existing is not None and existing["observed_at"] > p["observed_at"]:
        return []  # an older reading arriving late never replaces a newer one
    return [
        (
            "quota_gauges",
            {
                "gauge_id": gauge_id, "provider": p["provider"], "window": p["window"], "used_percent_json": _j(p["used_percent"]),
                "previous_percent_json": existing["used_percent_json"] if existing else None, "resets_at": p.get("resets_at"),
                "observed_at": p["observed_at"], "source": p["source"],
                "created_at": existing["created_at"] if existing else e.at, "updated_at": e.at,
            },
        )
    ]


def _quota_hold(p: dict, e: Event, get: Getter) -> list[Upsert]:
    return [
        (
            "quota_holds",
            {
                "provider": p["provider"], "state": "HELD", "reason": p["reason"], "resume_at": p.get("resume_at"),
                "attempts": int(p.get("attempts", 0)), "probe_action_id": None,
                # Every placement starts a new hold: only readings taken after it can end it.
                "placed_at": e.at, "updated_at": e.at,
            },
        )
    ]


def _quota_probe(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "quota_holds", p["provider"])
    _expect(row, "state", "HELD", "quota hold")
    row.update(state="PROBING", probe_action_id=p["action_id"], updated_at=e.at)
    return [("quota_holds", row)]


def _quota_released(p: dict, e: Event, get: Getter) -> list[Upsert]:
    row = _require(get, "quota_holds", p["provider"])
    if row["state"] == "RELEASED":
        raise InvalidTransition(f"{p['provider']} is not paused for quota")
    row.update(state="RELEASED", reason=p["reason"], probe_action_id=None, updated_at=e.at)
    return [("quota_holds", row)]


def _admission_decided(p: dict, e: Event, get: Getter) -> list[Upsert]:
    _require(get, "runs", p["run_id"])
    return [
        (
            "admissions",
            {
                "admission_id": p["admission_id"], "run_id": p["run_id"], "participant_id": p.get("participant_id"),
                "action_id": p.get("action_id"), "provider": p["provider"], "action_class": p["action_class"], "purpose": p["purpose"],
                "verdict": p["verdict"], "reason": p["reason"], "detail_json": _j(p.get("detail", {})), "created_at": e.at,
            },
        )
    ]


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
    "snapshot.recorded": _snapshot_recorded,
    "evidence.recorded": _evidence_recorded,
    "review.submitted": _review_submitted,
    "finding.resolved": _finding_resolved,
    "checkpoint.exported": _checkpoint_exported,
    "invite.created": _invite_created,
    "invite.used": _invite_used,
    "plan.proposed": _plan_proposed,
    "plan.decided": _plan_decided,
    "task.result": _task_result,
    "task.result.decided": _task_result_decided,
    "contribution.recorded": _contribution_recorded,
    "intervention.recorded": _intervention_recorded,
    "pool.defined": _pool_defined,
    "usage.recorded": _usage_recorded,
    "finishing.reserved": _finishing_reserved,
    "finishing.resized": _finishing_resized,
    "quota.observed": _quota_observed,
    "quota.hold": _quota_hold,
    "quota.probe": _quota_probe,
    "quota.released": _quota_released,
    "admission.decided": _admission_decided,
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
