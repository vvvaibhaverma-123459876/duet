"""D02: state-machine properties and schema/reducer agreement."""
from __future__ import annotations

import pytest

from duet.runtime import reducer
from duet.runtime.contracts import (
    TERMINAL_ACTION,
    TERMINAL_RUN,
    ActionState,
    Event,
    MessageState,
    RunLifecycle,
    TaskState,
    ValidationError,
    utc_now,
)
from duet.runtime.store import Store


def _reachable(machine, start):
    seen, stack = {start}, [start]
    while stack:
        for nxt in machine[stack.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


@pytest.mark.parametrize(
    "machine,enum",
    [
        (reducer.RUN_TRANSITIONS, RunLifecycle),
        (reducer.TASK_TRANSITIONS, TaskState),
        (reducer.ACTION_TRANSITIONS, ActionState),
        (reducer.MESSAGE_TRANSITIONS, MessageState),
    ],
)
def test_every_state_is_defined_and_targets_are_valid(machine, enum):
    assert set(machine) == set(enum)
    for targets in machine.values():
        assert targets <= set(enum)


def test_terminal_states_have_no_exits():
    for state in TERMINAL_RUN:
        assert reducer.RUN_TRANSITIONS[state] == frozenset()
    for state in TERMINAL_ACTION:
        assert reducer.ACTION_TRANSITIONS[state] == frozenset()


def test_all_run_states_reachable_from_created():
    assert _reachable(reducer.RUN_TRANSITIONS, RunLifecycle.CREATED) == set(RunLifecycle)


def test_completed_verified_only_via_verifying():
    sources = {s for s, targets in reducer.RUN_TRANSITIONS.items() if RunLifecycle.COMPLETED_VERIFIED in targets}
    assert sources == {RunLifecycle.VERIFYING}


def test_paused_runs_resume_only_through_reconciling():
    for paused in (RunLifecycle.PAUSED_QUOTA, RunLifecycle.PAUSED_BUDGET, RunLifecycle.PAUSED_APPROVAL, RunLifecycle.PAUSED_CONTEXT):
        active_targets = reducer.RUN_TRANSITIONS[paused] - {RunLifecycle.FAILED, RunLifecycle.CANCELLED}
        assert active_targets == {RunLifecycle.RECONCILING}


def test_in_doubt_never_returns_to_dispatch():
    assert ActionState.DISPATCHING not in reducer.ACTION_TRANSITIONS[ActionState.IN_DOUBT]
    assert ActionState.RESERVED not in reducer.ACTION_TRANSITIONS[ActionState.IN_DOUBT]
    # Only an action in flight can become IN_DOUBT.
    sources = {s for s, t in reducer.ACTION_TRANSITIONS.items() if ActionState.IN_DOUBT in t}
    assert sources == {ActionState.DISPATCHING, ActionState.RUNNING}


def test_verified_task_can_only_be_reopened_for_review():
    assert reducer.TASK_TRANSITIONS[TaskState.VERIFIED] == frozenset({TaskState.REVIEW_REQUIRED})


def test_unknown_event_rejected():
    with pytest.raises(ValidationError):
        reducer.apply(Event("mystery", {}, "x", utc_now()), lambda t, k: None)


def test_reducer_tables_match_migrated_schema(tmp_path):
    conn = Store(tmp_path / "d.db").connection()
    for table, (pk, columns) in reducer.TABLES.items():
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        assert tuple(row["name"] for row in info) == columns, table
        assert [row["name"] for row in info if row["pk"]] == [pk], table


def test_reducer_is_pure_given_the_same_inputs():
    event = Event("policy.registered", {"policy_hash": "sha256:" + "0" * 64, "body": {"a": 1}}, "user:user", "2026-01-01T00:00:00.000000Z")
    assert reducer.apply(event, lambda t, k: None) == reducer.apply(event, lambda t, k: None)
