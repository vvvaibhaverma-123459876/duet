"""D06: the pure scheduler, unit by unit and as whole simulated runs.

The simulation (`PairSim`) holds a run's task graph and plays it forward:
each round both participants read the same state, as two live sessions
would, and act on their top recommendation. The harness plays the runtime:
it checks claims the way `Runtime.claim_task` does, moves tasks only along
`reducer.TASK_TRANSITIONS`, records failures and progress samples, and asks
`detect_loop` whether to go on, re-plan or pause. Properties are asserted
over whole runs, not single calls."""
from __future__ import annotations

import copy
import dataclasses
import itertools
import random
import time

import pytest

from duet.runtime.contracts import PolicyDenied, TaskState, ValidationError
from duet.runtime.reducer import TASK_TRANSITIONS
from duet.runtime.scheduler import (
    NOTHING_LEFT,
    assign,
    critical_path,
    detect_loop,
    progress_fingerprint,
    ready_tasks,
    recommend,
    validate_plan,
)
from duet.runtime.taskplan import (
    ACTIVE,
    CLAIMABLE,
    CLOSED,
    DONE,
    MAX_ACTIVE_CLAIMS,
    MAX_DELEGATION_DEPTH,
    MAX_PLAN_TASKS,
    MAX_TASKS_PER_RUN,
    TASK_KINDS,
    WRITE_KINDS,
    FailureRecord,
    GraphState,
    GraphTask,
    ParticipantView,
    ProgressSample,
    ResolvedTask,
    TaskSpec,
)

WRITER = ParticipantView("claude", "claude", is_writer=True)
REVIEWER = ParticipantView("codex", "codex", is_writer=False)
PAIR = {"claude": WRITER, "codex": REVIEWER}
ACCEPTANCE = frozenset({"ac-tests", "ac-docs"})


def tid(n: int) -> str:
    return f"tsk_{n:032x}"


def task(n, kind="investigate", state="READY", owner=None, required=True, deps=(), parent=None, order=None) -> GraphTask:
    return GraphTask(
        task_id=tid(n), kind=kind, state=state, owner=owner, required=required,
        depends_on=tuple(tid(d) for d in deps), parent_id=tid(parent) if parent is not None else None,
        order=n if order is None else order,
    )


def graph(*tasks, participants=PAIR, pending=None, waiting=None) -> GraphState:
    return GraphState(
        tasks={t.task_id: t for t in tasks}, participants=dict(participants), acceptance_ids=ACCEPTANCE,
        pending_requests=dict(pending or {}), waiting_on=dict(waiting or {}),
    )


def spec(key, kind="investigate", deps=(), parent=None, acc=()) -> TaskSpec:
    return TaskSpec(key=key, description=f"do {key}", kind=kind, depends_on=tuple(deps), parent=parent, acceptance_ids=tuple(acc))


def keys(resolved: list[ResolvedTask]) -> list[str]:
    return [r.spec.key for r in resolved]


# --- validate_plan ---------------------------------------------------------------------


def test_plan_comes_back_in_dependency_order_with_depths():
    specs = [
        spec("impl", "code", deps=["probe"], acc=["ac-tests"]),
        spec("probe"),
        spec("check", "review", deps=["impl"], parent="impl"),
        spec("edge", "test_design", parent="check"),
    ]
    out = validate_plan(graph(), specs, proposer="codex")
    assert keys(out) == ["probe", "impl", "check", "edge"]
    assert [r.depth for r in out] == [0, 0, 1, 2]
    assert out[1].depends_on == ("probe",) and out[2].parent == "impl" and out[3].parent == "check"
    assert all(isinstance(r, ResolvedTask) for r in out)


def test_an_already_valid_order_is_kept():
    specs = [spec("c"), spec("a"), spec("b", deps=["c"]), spec("d", deps=["a", "b"]), spec("e")]
    assert keys(validate_plan(graph(), specs, proposer="claude")) == ["c", "a", "b", "d", "e"]


def test_duplicate_dependencies_collapse():
    out = validate_plan(graph(), [spec("a"), spec("b", deps=["a", "a"])], proposer="claude")
    assert out[1].depends_on == ("a",)


def test_plan_builds_on_existing_tasks():
    state = graph(task(1, state="VERIFIED"), task(2, state="RUNNING", owner="claude", kind="code"), task(3, parent=2))
    out = validate_plan(state, [spec("more", deps=[tid(1)], parent=tid(3)), spec("sib", parent=tid(2))], proposer="codex")
    assert [(r.spec.key, r.depends_on, r.parent, r.depth) for r in out] == [
        ("more", (tid(1),), tid(3), 2),
        ("sib", (), tid(2), 1),
    ]


@pytest.mark.parametrize(
    "specs,fragment",
    [
        ([], "at least one task"),
        ([spec(f"t{i}") for i in range(MAX_PLAN_TASKS + 1)], f"at most {MAX_PLAN_TASKS}"),
        ([spec("a"), spec("a")], "duplicate task key 'a'"),
        ([spec("a", deps=["ghost"])], "neither a key in this plan nor a task in this run"),
        ([spec("a", deps=[tid(99)])], "neither a key in this plan nor a task in this run"),
        ([spec("a", parent="ghost")], "neither a key in this plan nor a task in this run"),
        ([spec("a", acc=["ac-tests", "ac-invented"])], "unknown acceptance ids ['ac-invented']"),
        ([spec("a", deps=["a"])], "dependency cycle rejected: a -> a"),
        ([spec("a", deps=["b"]), spec("b", deps=["c"]), spec("c", deps=["a"]), spec("d")], "dependency cycle rejected: a -> b -> c -> a"),
        ([spec("a", deps=["b"]), spec("b", parent="a")], "dependency cycle rejected: a -> b -> a (parent of b)"),
        ([spec("a", parent="b"), spec("b", parent="a")], "cycle rejected"),
        ("not a list", "must be a list"),
        ([{"key": "a", "description": "x"}], "not a task spec"),
    ],
)
def test_invalid_plans_are_rejected(specs, fragment):
    with pytest.raises(ValidationError) as err:
        validate_plan(graph(), specs, proposer="claude")
    assert fragment in err.value.message


def test_unknown_kinds_are_rejected_even_if_the_spec_was_forged():
    with pytest.raises(ValidationError):
        TaskSpec(key="a", description="x", kind="deploy")
    forged = spec("a")
    object.__setattr__(forged, "kind", "deploy")
    with pytest.raises(ValidationError, match="kind must be one of"):
        validate_plan(graph(), [forged], proposer="claude")


def test_dependency_on_cancelled_work_is_rejected():
    state = graph(task(1, state="CANCELLED"))
    with pytest.raises(ValidationError, match="CANCELLED"):
        validate_plan(state, [spec("a", deps=[tid(1)])], proposer="claude")
    with pytest.raises(ValidationError, match="CANCELLED"):
        validate_plan(state, [spec("a", parent=tid(1))], proposer="claude")


def test_cycle_among_existing_tasks_is_reported():
    state = graph(task(1, deps=[2]), task(2, deps=[1]))
    with pytest.raises(ValidationError, match=f"{tid(1)} -> {tid(2)} -> {tid(1)}"):
        validate_plan(state, [spec("a")], proposer="claude")


def test_key_may_not_shadow_an_existing_id():
    state = GraphState(tasks={"probe": dataclasses.replace(task(1), task_id="probe")}, participants=PAIR)
    with pytest.raises(ValidationError, match="collides"):
        validate_plan(state, [spec("probe")], proposer="claude")


def test_run_size_is_bounded():
    existing = [task(i) for i in range(1, MAX_TASKS_PER_RUN - 1)]  # 30 tasks
    state = graph(*existing)
    assert len(validate_plan(state, [spec("a"), spec("b")], proposer="claude")) == 2
    with pytest.raises(PolicyDenied, match=f"at most {MAX_TASKS_PER_RUN} tasks per run"):
        validate_plan(state, [spec("a"), spec("b"), spec("c")], proposer="claude")


def test_delegation_depth_is_bounded_across_existing_and_new_parents():
    # Existing chain: 1 <- 2 <- 3 (task 3 has two ancestors).
    state = graph(task(1), task(2, parent=1), task(3, parent=2))
    ok = validate_plan(state, [spec("d3", parent=tid(3))], proposer="claude")
    assert ok[0].depth == MAX_DELEGATION_DEPTH
    with pytest.raises(PolicyDenied) as err:
        validate_plan(state, [spec("d3", parent=tid(3)), spec("d4", parent="d3")], proposer="claude")
    assert "task d4 would have 4 ancestors" in err.value.message
    assert err.value.details["ancestors"] == ["d3", tid(3), tid(2), tid(1)]
    chain = [spec("a"), spec("b", parent="a"), spec("c", parent="b"), spec("d", parent="c")]
    assert [r.depth for r in validate_plan(graph(), chain, proposer="claude")] == [0, 1, 2, 3]
    with pytest.raises(PolicyDenied, match="delegation is limited to 3 levels"):
        validate_plan(graph(), chain + [spec("e", parent="d")], proposer="claude")


@pytest.mark.parametrize("proposer", ["controller", "user", "mallory"])
def test_only_participants_propose(proposer):
    with pytest.raises(PolicyDenied, match="not a participant"):
        validate_plan(graph(), [spec("a")], proposer=proposer)


def test_a_plan_can_only_add_tasks():
    # No output field can express an edit, removal or weakening.
    assert set(ResolvedTask.__dataclass_fields__) == {"spec", "depends_on", "parent", "depth"}
    assert set(TaskSpec.__dataclass_fields__) == {"key", "description", "kind", "depends_on", "acceptance_ids", "parent", "deliverables"}
    for field in ("required", "objective", "acceptance", "cancel", "replaces", "state", "owner"):
        with pytest.raises(ValidationError, match="unknown task fields"):
            TaskSpec.from_dict({"key": "a", "description": "x", field: None})
    state = graph(task(1, state="RUNNING", owner="claude", kind="code"), task(2, deps=[1]))
    before = copy.deepcopy(state)
    validate_plan(state, [spec("a", deps=[tid(2)], parent=tid(1), acc=["ac-docs"])], proposer="codex")
    assert state == before


# --- ready_tasks and critical_path -------------------------------------------------------


def test_ready_tasks_filters_and_orders():
    state = graph(
        task(1, state="VERIFIED", owner="claude"),
        task(2, deps=[1]),  # ready, cp 1
        task(3, deps=[4]),  # unmet dependency
        task(4, state="RUNNING", owner="codex"),
        task(5, owner="claude"),  # READY but owned: not claimable by anyone else
        task(6, state="CHANGES_REQUESTED", owner="claude", kind="code"),  # reclaimable by its owner
        task(7, state="CLAIMED", owner="codex"),
        task(8),  # waiting on an open request
        task(9, required=False),  # optional, cp 1
        task(10),  # cp 3 through 11 <- 12
        task(11, state="BLOCKED", deps=[10]),
        task(12, deps=[11]),
        task(13, state="CANCELLED"),
        waiting={tid(8): "question msg_1 to codex"},
    )
    assert ready_tasks(state) == [tid(10), tid(2), tid(6), tid(9)]


def test_critical_path_counts_open_downstream_chains():
    # 1 <- 2 <- 3, 1 <- 4; 5 verified with open dependent 6; 7 cancelled.
    state = graph(
        task(1), task(2, deps=[1]), task(3, deps=[2]), task(4, deps=[1]),
        task(5, state="VERIFIED"), task(6, deps=[5]), task(7, state="CANCELLED", deps=[6]),
        task(8, state="VERIFIED", deps=[6]), task(9, deps=[8]),
    )
    assert critical_path(state) == {tid(1): 3, tid(2): 2, tid(3): 1, tid(4): 1, tid(6): 1, tid(9): 1}


def test_critical_path_on_a_dense_run_sized_graph():
    n = MAX_TASKS_PER_RUN
    state = graph(*[task(i, deps=range(1, i)) for i in range(1, n + 1)])
    start = time.perf_counter()
    path = critical_path(state)
    assert time.perf_counter() - start < 1.0
    assert path == {tid(i): n - i + 1 for i in range(1, n + 1)}
    assert ready_tasks(state) == [tid(1)]


# --- assign -----------------------------------------------------------------------------


def test_code_goes_only_to_the_writer():
    assert assign(graph(task(1, kind="code"))) == {"codex": None, "claude": tid(1)}


def test_fewer_active_tasks_gets_the_top_task():
    # codex is busy with one task; claude is free: claude takes the top task.
    state = graph(task(1), task(2, deps=[1]), task(3), task(4, state="RUNNING", owner="codex"))
    assert critical_path(state)[tid(1)] == 2
    assert assign(state) == {"claude": tid(1), "codex": tid(3)}
    # Reversed load: codex is free, claude busy.
    state = graph(task(1), task(2, deps=[1]), task(3), task(4, kind="code", state="RUNNING", owner="claude"))
    assert assign(state) == {"codex": tid(1), "claude": tid(3)}


def test_both_get_work_when_that_is_possible():
    state = graph(task(1, kind="review", deps=[]), task(2, kind="code"), task(3, deps=[1]))
    assert ready_tasks(state) == [tid(1), tid(2)]
    assert assign(state) == {"codex": tid(1), "claude": tid(2)}
    # claude has fewer active tasks and goes first, but taking the top task
    # (the reviewer's only option) would leave codex idle: claude takes code.
    state = graph(task(1, kind="review"), task(2, kind="code"), task(3, deps=[1]), task(4, state="RUNNING", owner="codex"))
    assert assign(state) == {"claude": tid(2), "codex": tid(1)}


def test_a_single_shared_task_goes_to_the_non_writer_on_a_tie():
    state = graph(task(1), task(2, kind="code", deps=[1]))
    assert assign(state) == {"codex": tid(1), "claude": None}


def test_completed_work_balances_later_ties():
    state = graph(task(1, state="VERIFIED", owner="codex"), task(2, deps=[1]))
    assert assign(state) == {"claude": tid(2), "codex": None}


def test_active_claims_are_bounded():
    busy = [task(i, kind="code", state=s, owner="claude") for i, s in ((1, "RUNNING"), (2, "CLAIMED"))]
    state = graph(*busy, task(3, kind="code"), task(4))
    assert assign(state) == {"codex": tid(4), "claude": None}


def test_changes_requested_goes_back_to_its_owner_only():
    state = graph(task(1, state="CHANGES_REQUESTED", owner="codex"), task(2, kind="code"))
    assert assign(state) == {"codex": tid(1), "claude": tid(2)}
    full = [task(i, state="RUNNING", owner="codex") for i in (3, 4)]
    state = graph(task(1, state="CHANGES_REQUESTED", owner="codex"), *full)
    assert assign(state) == {"claude": None, "codex": None}


def test_unavailable_participants_get_nothing():
    state = graph(task(1, kind="code"), task(2), participants={"claude": dataclasses.replace(WRITER, available=False), "codex": REVIEWER})
    assert assign(state) == {"codex": tid(2)}


def test_assignment_ignores_dict_order():
    tasks = [task(1), task(2, kind="code"), task(3, kind="review"), task(4, deps=[1])]
    a = graph(*tasks)
    b = GraphState(tasks=dict(reversed(list(a.tasks.items()))), participants={"codex": REVIEWER, "claude": WRITER}, acceptance_ids=ACCEPTANCE)
    assert assign(a) == assign(b) and ready_tasks(a) == ready_tasks(b)
    assert recommend(a, "codex") == recommend(b, "codex") and recommend(a, "claude") == recommend(b, "claude")


# --- recommend --------------------------------------------------------------------------


def test_peer_requests_come_first_then_own_work_then_claims():
    state = graph(
        task(1, state="RUNNING", owner="codex"), task(2), task(3, deps=[2]), task(4, kind="code", state="RUNNING", owner="claude"),
        pending={"codex": 2},
    )
    recs = recommend(state, "codex")
    assert [(r.action, r.task_id, r.priority) for r in recs] == [
        ("answer", None, 100), ("continue", tid(1), 80), ("claim", tid(2), 62),
    ]
    assert "2 open peer request" in recs[0].reason
    # With claude free (fewer active tasks), the only ready task is claude's.
    state = graph(task(1, state="RUNNING", owner="codex"), task(2), task(3, deps=[2]), pending={"codex": 2})
    assert [r.action for r in recommend(state, "codex")] == ["answer", "continue"]
    assert [(r.action, r.task_id) for r in recommend(state, "claude")] == [("claim", tid(2))]


def test_claim_priority_stays_below_continue():
    state = graph(*[task(i, deps=[i - 1] if i > 1 else []) for i in range(1, 31)])
    [rec] = recommend(state, "codex")
    assert (rec.action, rec.task_id, rec.priority) == ("claim", tid(1), 79)


def test_own_repair_is_offered_and_other_peoples_never_are():
    state = graph(task(1, kind="code", state="CHANGES_REQUESTED", owner="claude"), task(2, state="CHANGES_REQUESTED", owner="claude"))
    claude = recommend(state, "claude")
    assert {r.task_id for r in claude if r.action == "claim"} == {tid(1), tid(2)}
    assert all("claim it again and repair" in r.reason for r in claude)
    [codex] = recommend(state, "codex")
    assert codex.action == "wait" and f"{tid(2)} is claude's to repair" in codex.reason


def test_a_non_writer_is_never_offered_code():
    state = graph(task(1, kind="code"), task(2, kind="code"))
    [rec] = recommend(state, "codex")
    assert rec.action == "wait" and "code work for the writer" in rec.reason


def test_wait_names_what_blocked_work_waits_for():
    state = graph(
        task(1, state="RUNNING", owner="claude", kind="code"), task(2, deps=[1]), task(3),
        waiting={tid(3): "review request msg_7 to claude"},
    )
    [rec] = recommend(state, "codex")
    assert (rec.action, rec.priority) == ("wait", 10)
    assert f"{tid(3)} waits on review request msg_7 to claude" in rec.reason
    assert f"{tid(2)} waits for {tid(1)}" in rec.reason
    assert f"{tid(1)} is RUNNING with claude" in rec.reason


def test_wait_mentions_released_work_of_an_unavailable_owner():
    gone = dataclasses.replace(WRITER, available=False)
    state = graph(task(1, state="RUNNING", owner="claude", kind="code"), participants={"claude": gone, "codex": REVIEWER})
    [rec] = recommend(state, "codex")
    assert "claude is unavailable; the task can be released for reassignment" in rec.reason
    [own] = recommend(state, "claude")
    assert own.action == "wait" and "claude is unavailable" in own.reason


def test_nothing_left_and_nothing_planned():
    done = graph(task(1, state="VERIFIED", owner="claude"), task(2, state="CANCELLED"))
    assert [(r.action, r.reason) for r in recommend(done, "claude")] == [("wait", NOTHING_LEFT)]
    [empty] = recommend(graph(), "codex")
    assert empty.action == "wait" and "propose a plan" in empty.reason


def test_no_claims_beyond_the_active_limit():
    state = graph(task(1, state="RUNNING", owner="codex"), task(2, state="WAITING_PEER", owner="codex"), task(3))
    recs = recommend(state, "codex")
    assert [r.action for r in recs] == ["continue", "continue"]
    state = graph(task(1, state="RUNNING", owner="codex"), task(2, state="CHANGES_REQUESTED", owner="codex"), task(3))
    assert [r.action for r in recommend(state, "codex")] == ["continue", "claim"]


def test_unknown_participant_is_an_error():
    with pytest.raises(ValidationError):
        recommend(graph(), "mallory")


# --- progress_fingerprint ------------------------------------------------------------------


def _fp(tasks=(), snaps=(), evidence=(), findings=()):
    return progress_fingerprint(list(tasks), list(snaps), list(evidence), list(findings))


BASE_TASKS = [{"task_id": tid(1), "state": "RUNNING", "revision": 1, "owner": "claude"}, {"task_id": tid(2), "state": "READY", "revision": 0, "owner": None}]
BASE_SNAPS = [{"snapshot_id": "snp_1", "tree_hash": "sha256:aa"}]
BASE_EVIDENCE = [{"snapshot_id": "snp_1", "check_id": "tests", "status": "failed"}]
BASE_FINDINGS = [{"finding_id": "fnd_1", "status": "open"}, {"finding_id": "fnd_2", "status": "resolved"}]


def test_fingerprint_ignores_text_and_order():
    base = _fp(BASE_TASKS, BASE_SNAPS, BASE_EVIDENCE, BASE_FINDINGS)
    chatty = _fp(
        [dict(t, description="rewritten " * 20, last_message="I think we should...") for t in reversed(BASE_TASKS)],
        [dict(s, note="new words", created_at="2026-09-27T00:00:00Z") for s in BASE_SNAPS] * 2,
        [dict(e, detail="stderr differs", evidence_id="ev_9") for e in BASE_EVIDENCE],
        [dict(f, summary="reworded finding") for f in reversed(BASE_FINDINGS)] + [{"finding_id": "fnd_3", "status": "resolved"}],
    )
    assert base == chatty and base.startswith("sha256:")


@pytest.mark.parametrize(
    "change",
    [
        lambda t, s, e, f: t[0].update(state="REVIEW_REQUIRED"),
        lambda t, s, e, f: t[0].update(revision=2),
        lambda t, s, e, f: t[1].update(owner="codex"),
        lambda t, s, e, f: s.append({"snapshot_id": "snp_2", "tree_hash": "sha256:bb"}),
        lambda t, s, e, f: e[0].update(status="passed"),
        lambda t, s, e, f: e.append({"snapshot_id": "snp_2", "check_id": "tests", "status": "failed"}),
        lambda t, s, e, f: f[0].update(status="resolved"),
        lambda t, s, e, f: f[1].update(status="open"),
    ],
)
def test_fingerprint_moves_with_real_progress(change):
    t, s, e, f = (copy.deepcopy(x) for x in (BASE_TASKS, BASE_SNAPS, BASE_EVIDENCE, BASE_FINDINGS))
    before = _fp(t, s, e, f)
    change(t, s, e, f)
    assert _fp(t, s, e, f) != before


def test_fingerprint_rejects_malformed_input():
    with pytest.raises(ValidationError):
        progress_fingerprint("tasks", [], [], [])
    with pytest.raises(ValidationError):
        progress_fingerprint([["task"]], [], [], [])


# --- detect_loop -----------------------------------------------------------------------------


def fail(task_id, signature, index, snapshot="snp"):
    return FailureRecord(task_id=task_id, snapshot_id=f"{snapshot}_{index}", signature=signature, index=index)


def loop(samples=(), failures=(), replans=0, repairs=2, idle=4):
    return detect_loop(list(samples), list(failures), replans_done=replans, max_repairs=repairs, max_idle_messages=idle)


def test_same_failure_twice_replans_then_pauses():
    failures = [fail(tid(1), "tests:KeyError", 3), fail(tid(1), "tests:KeyError", 7)]
    first = loop(failures=failures)
    assert first.status == "replan" and first.task_id == tid(1)
    assert first.evidence == (tid(1), "tests:KeyError", "tests:KeyError")
    assert "focused re-plan" in first.reason and "acceptance contract stay" in first.reason
    second = loop(failures=failures, replans=1)
    assert second.status == "pause" and "pause and report honestly" in second.reason


def test_failure_thresholds():
    assert loop(failures=[fail(tid(1), "a", 1)]).status == "ok"
    assert loop(failures=[fail(tid(1), "a", 1), fail(tid(1), "b", 2)]).status == "ok"
    assert loop(failures=[fail(tid(1), "a", 1), fail(tid(1), "b", 2), fail(tid(1), "c", 3)]).status == "replan"
    # Failures on different tasks are different hypotheses.
    assert loop(failures=[fail(tid(1), "a", 1), fail(tid(2), "a", 2), fail(tid(3), "a", 3)]).status == "ok"
    # A laxer policy allows more repairs.
    assert loop(failures=[fail(tid(1), "a", 1), fail(tid(1), "a", 2)], repairs=3).status == "ok"
    assert loop(failures=[fail(tid(1), "a", i) for i in (1, 2, 3)], repairs=3).status == "replan"


def test_first_task_over_the_limit_is_reported():
    failures = [fail(tid(2), "x", 1), fail(tid(1), "y", 2), fail(tid(1), "y", 3), fail(tid(2), "x", 4)]
    verdict = loop(failures=reversed(failures))
    assert verdict.task_id == tid(1)


def _messages(fp, start, n, kind="message"):
    return [ProgressSample(index=start + i, kind=kind, fingerprint=fp) for i in range(n)]


def test_message_ping_pong_without_change_stalls():
    samples = _messages("fp-a", 1, 5)
    verdict = loop(samples=samples)
    assert verdict.status == "stalled" and "decide, propose a plan, or pause" in verdict.reason
    assert loop(samples=samples[:4]).status == "ok"  # no baseline before the four messages
    assert loop(samples=samples, idle=5).status == "ok"


def test_any_real_change_or_non_message_breaks_a_stall():
    base = _messages("fp-a", 1, 1, kind="check")
    assert loop(samples=base + _messages("fp-b", 2, 4)).status == "ok"  # the first message changed state
    assert loop(samples=base + _messages("fp-a", 2, 4)).status == "stalled"
    mixed = base + _messages("fp-a", 2, 2) + _messages("fp-a", 4, 1, kind="turn") + _messages("fp-a", 5, 1)
    assert loop(samples=mixed).status == "ok"
    # Samples are ordered by index, not by list position.
    assert loop(samples=list(reversed(base + _messages("fp-a", 2, 4)))).status == "stalled"


def test_failures_take_precedence_over_stalls():
    verdict = loop(samples=_messages("fp-a", 1, 9), failures=[fail(tid(1), "a", 1), fail(tid(1), "a", 2)])
    assert verdict.status == "replan"


@pytest.mark.parametrize("kwargs", [{"replans": -1}, {"repairs": 0}, {"idle": 0}, {"repairs": True}])
def test_loop_policy_is_validated(kwargs):
    with pytest.raises(ValidationError):
        loop(**kwargs)


# --- Simulation harness -------------------------------------------------------------------------


class PairSim:
    MAX_REPAIRS = 2
    MAX_IDLE = 4

    def __init__(self, participants=PAIR, script=None, ping_pong=0, pending=None):
        self.participants = dict(participants)
        self.tasks: dict[str, GraphTask] = {}
        self.revisions: dict[str, int] = {}
        self.key_of: dict[str, str] = {}
        # plan key -> outcome per attempt (None passes, a string fails with
        # that signature; past the end it passes), or a string: always fails.
        self.script = {k: (v if isinstance(v, str) else list(v)) for k, v in (script or {}).items()}
        self.pending: dict[str, int] = dict(pending or {})
        self.waiting_on: dict[str, str] = {}
        self.ping_pong = ping_pong  # peer questions each answer provokes, in total
        self.snapshots: list[dict] = []
        self.evidence: list[dict] = []
        self.samples: list[ProgressSample] = []
        self.failures: list[FailureRecord] = []  # under the current approach
        self.replans = 0
        self.attempts: dict[str, int] = {}
        self.completed_by: dict[str, str] = {}
        self.verdicts: list[str] = []
        self.advice_log: list[dict[str, list]] = []
        self.clock = 0
        self.chatter = ""

    # -- the runtime's side

    def state(self) -> GraphState:
        return GraphState(
            tasks=dict(self.tasks), participants=dict(self.participants), acceptance_ids=ACCEPTANCE,
            pending_requests=dict(self.pending), waiting_on=dict(self.waiting_on),
        )

    def propose(self, specs: list[TaskSpec], proposer: str) -> list[str]:
        resolved = validate_plan(self.state(), specs, proposer=proposer)
        local: dict[str, str] = {}
        created = []
        for entry in resolved:
            n = len(self.tasks) + 1
            task_id = tid(n)
            self.tasks[task_id] = GraphTask(
                task_id=task_id, kind=entry.spec.kind, state="READY", owner=None, required=bool(entry.spec.acceptance_ids),
                depends_on=tuple(local.get(d, d) for d in entry.depends_on),
                parent_id=local.get(entry.parent, entry.parent) if entry.parent else None,
                acceptance_ids=entry.spec.acceptance_ids, proposed_by=proposer, order=n,
            )
            local[entry.spec.key] = task_id
            self.revisions[task_id] = 0
            self.key_of[task_id] = entry.spec.key
            created.append(task_id)
        return created

    def id_of(self, key: str) -> str:
        return next(t for t, k in self.key_of.items() if k == key)

    def move(self, task_id: str, *path: str, **changes) -> None:
        current = self.tasks[task_id]
        state = current.state
        for nxt in path:
            assert TaskState(nxt) in TASK_TRANSITIONS[TaskState(state)], f"{state} -> {nxt} is not a task transition"
            state = nxt
        self.tasks[task_id] = dataclasses.replace(current, state=state, **changes)

    def fingerprint(self) -> str:
        # Text that changes every sample rides along and must not count.
        tasks = [
            {"task_id": t.task_id, "state": t.state, "revision": self.revisions[t.task_id], "owner": t.owner,
             "description": self.chatter, "updated_at": self.clock}
            for t in self.tasks.values()
        ]
        snaps = [dict(s, note=self.chatter) for s in self.snapshots]
        return progress_fingerprint(tasks, snaps, self.evidence, [])

    def sample(self, kind: str) -> None:
        self.clock += 1
        self.samples.append(ProgressSample(index=self.clock, kind=kind, fingerprint=self.fingerprint()))

    def active(self, pid: str) -> int:
        return sum(1 for t in self.tasks.values() if t.owner == pid and t.state in ACTIVE)

    # -- the participants' side

    def _answer(self, pid: str, rec) -> None:
        self.pending[pid] -= 1
        self.chatter = f"{pid} answered with fresh wording #{self.clock}"
        if self.ping_pong > 0:
            self.ping_pong -= 1
            other = next(p for p in sorted(self.participants) if p != pid)
            self.pending[other] = self.pending.get(other, 0) + 1
        self.sample("message")

    def _claim(self, pid: str, rec) -> None:
        # What Runtime.claim_task checks, plus the scheduler's bounds.
        t = self.tasks[rec.task_id]
        assert t.state in CLAIMABLE, f"claim of a {t.state} task"
        assert t.owner in (None, pid), "claim of another participant's task"
        assert all(self.tasks[d].state == DONE for d in t.depends_on), "claim with unmet dependencies"
        assert self.participants[pid].is_writer or t.kind not in WRITE_KINDS, "code claimed by a non-writer"
        assert self.active(pid) < MAX_ACTIVE_CLAIMS, "claim beyond the active limit"
        self.move(rec.task_id, "CLAIMED", owner=pid)
        self.sample("turn")

    def _continue(self, pid: str, rec) -> None:
        t = self.tasks[rec.task_id]
        assert t.owner == pid
        if t.state == "CLAIMED":
            self.move(t.task_id, "RUNNING")
            self.attempts[t.task_id] = self.attempts.get(t.task_id, 0) + 1
            self.sample("turn")
        elif t.state == "RUNNING":
            self._submit(pid, t)

    def _submit(self, pid: str, t: GraphTask) -> None:
        key = self.key_of[t.task_id]
        planned = self.script.get(key)
        outcome = planned if isinstance(planned, str) else (planned.pop(0) if planned else None)
        snapshot_id = f"snp_{self.clock}"
        self.snapshots.append({"snapshot_id": snapshot_id, "tree_hash": f"tree:{key}:{self.attempts[t.task_id]}"})
        if outcome is None:
            self.evidence.append({"snapshot_id": snapshot_id, "check_id": "tests", "status": "passed"})
            self.move(t.task_id, "REVIEW_REQUIRED", "VERIFIED")
            self.completed_by[t.task_id] = pid
        else:
            self.evidence.append({"snapshot_id": snapshot_id, "check_id": "tests", "status": "failed"})
            self.failures.append(FailureRecord(t.task_id, snapshot_id, outcome, self.clock + 1))
            self.move(t.task_id, "REVIEW_REQUIRED", "CHANGES_REQUESTED")  # the owner repairs
            self.revisions[t.task_id] += 1
        self.sample("check")

    # -- one round and a whole run

    def check_advice(self, state: GraphState, advice: dict[str, list]) -> None:
        holders: dict[str, str] = {}
        for pid, recs in advice.items():
            me = state.participants[pid]
            assert recs, "recommend always says something"
            assert [r.priority for r in recs] == sorted((r.priority for r in recs), reverse=True)
            claims = [r for r in recs if r.action == "claim"]
            assert len(claims) <= MAX_ACTIVE_CLAIMS - self.active(pid)
            for r in recs:
                if r.task_id is None:
                    continue
                t = state.tasks[r.task_id]
                assert t.owner in (None, pid), "(a) advised to take another participant's task"
                assert me.is_writer or t.kind not in WRITE_KINDS, "(d) code advised to a non-writer"
                assert holders.setdefault(r.task_id, pid) == pid, "(a) one task advised to both participants"
        for t in state.tasks.values():
            if t.state in ACTIVE:
                assert t.owner in state.participants
        for pid in state.participants:
            assert self.active(pid) <= MAX_ACTIVE_CLAIMS

    def round(self) -> bool:
        state = self.state()
        advice = {pid: recommend(state, pid) for pid in sorted(self.participants)}
        self.check_advice(state, advice)
        self.advice_log.append(advice)
        acted = False
        for pid in sorted(self.participants):
            top = advice[pid][0]
            if top.action != "wait":
                acted = True
                getattr(self, "_" + top.action)(pid, top)
        return acted

    def replan(self, verdict) -> None:
        """A focused, peer-assisted re-plan: the reviewer adds an
        investigation under the failing task. Nothing existing changes."""
        before = dict(self.tasks)
        self.replans += 1
        self.failures = []
        self.propose([spec(f"rethink-{self.replans}", "investigate", parent=verdict.task_id)], proposer="codex")
        assert {k: self.tasks[k] for k in before} == before
        assert self.state().acceptance_ids == ACCEPTANCE

    def run(self, max_rounds: int = 250) -> str:
        for _ in range(max_rounds):
            if self.tasks and all(t.state in CLOSED for t in self.tasks.values()) and not any(self.pending.values()):
                return "done"
            acted = self.round()
            verdict = detect_loop(
                self.samples, self.failures, replans_done=self.replans, max_repairs=self.MAX_REPAIRS, max_idle_messages=self.MAX_IDLE
            )
            self.verdicts.append(verdict.status)
            if verdict.status in ("pause", "stalled"):
                return verdict.status
            if verdict.status == "replan":
                self.replan(verdict)
            elif not acted:
                return "idle"
        raise AssertionError(f"run did not terminate within {max_rounds} rounds")

    def done_by(self, pid: str) -> int:
        return sum(1 for owner in self.completed_by.values() if owner == pid)


FEATURE_PLAN = [
    spec("probe", "investigate", acc=["ac-tests"]),
    spec("cases", "test_design", acc=["ac-tests"]),
    spec("impl", "code", deps=["probe", "cases"], acc=["ac-tests"]),
    spec("docs", "code", deps=["impl"], acc=["ac-docs"]),
    spec("review", "review", deps=["impl"], parent="impl"),
]


def test_sim_both_participants_contribute_without_duplication():
    sim = PairSim(script={"impl": ["tests:AssertionError"]})
    sim.propose(FEATURE_PLAN, proposer="claude")
    assert sim.run() == "done"
    assert all(t.state == DONE for t in sim.tasks.values())
    # (b) both did meaningful work; the writer did all the code.
    assert sim.done_by("claude") >= 1 and sim.done_by("codex") >= 1
    for task_id, owner in sim.completed_by.items():
        if sim.tasks[task_id].kind in WRITE_KINDS:
            assert owner == "claude"
    # (a) every task was worked by exactly one participant.
    assert len(sim.completed_by) == len(sim.tasks)
    assert sim.attempts[sim.id_of("impl")] == 2 and sim.replans == 0
    assert "stalled" not in sim.verdicts


def test_sim_reviewer_takes_the_opening_investigation():
    sim = PairSim()
    sim.propose([spec("probe"), spec("impl", "code", deps=["probe"])], proposer="claude")
    assert sim.run() == "done"
    assert sim.completed_by == {sim.id_of("probe"): "codex", sim.id_of("impl"): "claude"}


@pytest.mark.parametrize("how", ["waiting_on", "blocked_state"])
def test_sim_blocked_work_does_not_stop_unaffected_work(how):
    sim = PairSim()
    sim.propose(
        [spec("ask"), spec("api", "code", deps=["ask"]), spec("cases", "test_design"), spec("tidy", "code"), spec("audit", "review")],
        proposer="codex",
    )
    ask = sim.id_of("ask")
    if how == "waiting_on":
        sim.waiting_on[ask] = "question msg_3 to the user about the API name"
    else:
        sim.move(ask, "BLOCKED")
    assert sim.run() == "idle"
    states = {sim.key_of[t]: v.state for t, v in sim.tasks.items()}
    # (c) everything not downstream of the blocked task got done.
    assert states == {"ask": "READY" if how == "waiting_on" else "BLOCKED", "api": "READY", "cases": DONE, "tidy": DONE, "audit": DONE}
    final = sim.advice_log[-1]
    for pid in ("claude", "codex"):
        [rec] = final[pid]
        assert rec.action == "wait"
        assert f"{sim.id_of('api')} waits for {ask}" in rec.reason
        if how == "waiting_on":
            assert "waits on question msg_3" in rec.reason
        else:
            assert f"{ask} is BLOCKED" in rec.reason


def test_sim_repeated_failure_replans_then_pauses():
    sim = PairSim(script={"impl": "tests::test_parse KeyError 'x'"})
    sim.propose(FEATURE_PLAN, proposer="claude")
    before = {k: v for k, v in sim.tasks.items()}
    assert sim.run(max_rounds=80) == "pause"
    impl = sim.id_of("impl")
    # (e) two repairs per approach, one focused re-plan, then an honest pause.
    assert sim.attempts[impl] == 2 * PairSim.MAX_REPAIRS
    assert sim.replans == 1 and sim.verdicts.count("replan") == 1 and sim.verdicts[-1] == "pause"
    assert sim.tasks[sim.id_of("rethink-1")].parent_id == impl
    assert sim.tasks[sim.id_of("rethink-1")].state == DONE and sim.completed_by[sim.id_of("rethink-1")] == "codex"
    # Downstream work never started; the original tasks were never rewritten.
    assert sim.tasks[sim.id_of("docs")].state == "READY" and sim.tasks[sim.id_of("review")].state == "READY"
    for task_id, original in before.items():
        now = sim.tasks[task_id]
        assert (now.kind, now.depends_on, now.parent_id, now.acceptance_ids, now.required) == (
            original.kind, original.depends_on, original.parent_id, original.acceptance_ids, original.required,
        )


def test_sim_varied_failures_also_replan():
    sim = PairSim(script={"impl": ["a", "b", "c", None]})
    sim.propose(FEATURE_PLAN, proposer="claude")
    assert sim.run() == "done"
    assert sim.replans == 1 and sim.attempts[sim.id_of("impl")] == 4


def test_sim_message_ping_pong_stalls():
    sim = PairSim(pending={"claude": 1}, ping_pong=100)
    sim.propose([spec("impl", "code")], proposer="claude")
    sim.move(sim.id_of("impl"), "BLOCKED")
    assert sim.run() == "stalled"
    # (f)+(g) every message had new wording, none changed the fingerprint.
    assert len(sim.samples) == PairSim.MAX_IDLE + 1
    assert {s.kind for s in sim.samples} == {"message"}
    assert len({s.fingerprint for s in sim.samples}) == 1


def test_sim_discussion_alongside_real_work_is_not_a_stall():
    sim = PairSim(pending={"codex": 1}, ping_pong=6)
    sim.propose(FEATURE_PLAN, proposer="claude")
    assert sim.run() == "done"
    assert "stalled" not in sim.verdicts


def test_sim_rejects_invalid_proposals_and_keeps_state():
    sim = PairSim()
    sim.propose([spec("a"), spec("b", parent="a"), spec("c", parent="b"), spec("d", parent="c")], proposer="claude")
    snapshot = dict(sim.tasks)
    bad = [
        (ValidationError, [spec("x", deps=["y"]), spec("y", deps=["x"])]),
        (PolicyDenied, [spec("deep", parent=sim.id_of("d"))]),
        (ValidationError, [spec(f"p{i}") for i in range(MAX_PLAN_TASKS + 1)]),
        (ValidationError, [spec("x", acc=["ac-perf"])]),
    ]
    for error, specs in bad:
        with pytest.raises(error):
            sim.propose(specs, proposer="codex")
    with pytest.raises(PolicyDenied):
        sim.propose([spec("x")], proposer="controller")
    batch = 0
    while len(sim.tasks) < MAX_TASKS_PER_RUN:
        room = min(MAX_PLAN_TASKS, MAX_TASKS_PER_RUN - len(sim.tasks))
        sim.propose([spec(f"f{batch}-{j}") for j in range(room)], proposer="codex")
        batch += 1
    assert len(sim.tasks) == MAX_TASKS_PER_RUN
    with pytest.raises(PolicyDenied):
        sim.propose([spec("one-more")], proposer="codex")
    assert all(sim.tasks[k] == v for k, v in snapshot.items())


def test_sim_non_writer_never_gets_code_even_alone():
    gone = dataclasses.replace(WRITER, available=False)
    sim = PairSim(participants={"claude": gone, "codex": REVIEWER})
    sim.propose([spec("impl", "code"), spec("probe"), spec("audit", "review", deps=["impl"])], proposer="codex")
    assert sim.run() == "idle"
    assert sim.completed_by == {sim.id_of("probe"): "codex"}
    [rec] = sim.advice_log[-1]["codex"]
    assert rec.action == "wait" and f"{sim.id_of('impl')} is code work for the writer" in rec.reason


# --- Property tests over random graphs ------------------------------------------------------------


def random_plan(rng: random.Random, n: int, kinds=TASK_KINDS) -> tuple[list[TaskSpec], dict[str, int]]:
    """A random DAG in rank order; parents only where the depth bound allows."""
    names = [f"k{i}" for i in range(n)]
    specs, depth = [], {}
    for i, key in enumerate(names):
        deps = sorted(rng.sample(names[:i], rng.randint(0, min(i, 3))))
        parent = None
        if i and rng.random() < 0.4:
            options = [k for k in names[:i] if depth[k] < MAX_DELEGATION_DEPTH]
            parent = rng.choice(options) if options else None
        depth[key] = 0 if parent is None else depth[parent] + 1
        specs.append(spec(key, rng.choice(kinds), deps=deps, parent=parent, acc=rng.sample(sorted(ACCEPTANCE), rng.randint(0, 1))))
    return specs, depth


def longest_downstream(state: GraphState, task_id: str) -> int:
    """Brute force: enumerate every downstream path of open tasks."""
    open_ids = {t.task_id for t in state.tasks.values() if t.state not in CLOSED}
    best = 1
    stack = [(task_id, 1)]
    while stack:
        node, length = stack.pop()
        best = max(best, length)
        for t in state.tasks.values():
            if node in t.depends_on and t.task_id in open_ids:
                stack.append((t.task_id, length + 1))
    return best


def test_random_plans_validate_in_topological_order():
    rng = random.Random(0xD06)
    for _ in range(300):
        specs, depth = random_plan(rng, rng.randint(1, MAX_PLAN_TASKS))
        assert keys(validate_plan(graph(), specs, proposer="claude")) == [s.key for s in specs]  # stable
        shuffled = specs[:]
        rng.shuffle(shuffled)
        out = validate_plan(graph(), shuffled, proposer="codex")
        position = {r.spec.key: i for i, r in enumerate(out)}
        assert sorted(position) == sorted(s.key for s in specs)
        for r in out:
            assert all(position[d] < position[r.spec.key] for d in r.depends_on)
            assert r.parent is None or position[r.parent] < position[r.spec.key]
            assert r.depth == depth[r.spec.key]
        # Closing a loop anywhere is rejected, and the reported path is real.
        if len(specs) >= 2:
            a, b = sorted(rng.sample(range(len(specs)), 2))
            looped = specs[:]
            looped[a] = dataclasses.replace(specs[a], depends_on=specs[a].depends_on + (specs[b].key,))
            if specs[a].key not in specs[b].depends_on:
                looped[b] = dataclasses.replace(specs[b], depends_on=specs[b].depends_on + (specs[a].key,))
            rng.shuffle(looped)
            with pytest.raises(ValidationError, match="dependency cycle rejected") as err:
                validate_plan(graph(), looped, proposer="claude")
            cycle = err.value.details["cycle"]
            assert len(cycle) >= 2 and cycle[0] == cycle[-1]
            by_key = {s.key: s for s in looped}
            for x, y in zip(cycle, cycle[1:]):
                assert y in by_key[x].depends_on or by_key[x].parent == y


STATES = ("READY", "READY", "READY", "CLAIMED", "RUNNING", "WAITING_PEER", "REVIEW_REQUIRED", "CHANGES_REQUESTED", "VERIFIED", "VERIFIED", "BLOCKED", "CANCELLED", "PROPOSED")


def random_state(rng: random.Random) -> GraphState:
    specs, _ = random_plan(rng, rng.randint(1, MAX_PLAN_TASKS))
    ids = {s.key: tid(i + 1) for i, s in enumerate(specs)}
    tasks = []
    for i, s in enumerate(specs):
        state = rng.choice(STATES)
        owner = None
        if state in ACTIVE or state in ("REVIEW_REQUIRED", "CHANGES_REQUESTED", "VERIFIED"):
            owner = "claude" if s.kind in WRITE_KINDS else rng.choice(["claude", "codex"])
        tasks.append(GraphTask(
            task_id=ids[s.key], kind=s.kind, state=state, owner=owner, required=rng.random() < 0.7,
            depends_on=tuple(ids[d] for d in s.depends_on), parent_id=ids.get(s.parent), order=rng.randint(0, 3),
        ))
    people = {
        "claude": dataclasses.replace(WRITER, available=rng.random() < 0.9),
        "codex": dataclasses.replace(REVIEWER, available=rng.random() < 0.9),
    }
    waiting = {t.task_id: "question" for t in tasks if rng.random() < 0.1}
    pending = {p: rng.randint(0, 1) for p in people}
    return GraphState(tasks={t.task_id: t for t in tasks}, participants=people, pending_requests=pending, waiting_on=waiting)


def test_random_states_keep_every_scheduler_invariant():
    rng = random.Random(606)
    for _ in range(400):
        state = random_state(rng)
        path = critical_path(state)
        open_ids = {t.task_id for t in state.tasks.values() if t.state not in CLOSED}
        assert set(path) == open_ids
        for task_id in open_ids:
            assert path[task_id] == longest_downstream(state, task_id)
        ready = ready_tasks(state)
        sort_key = {t: (-path[t], not state.tasks[t].required, state.tasks[t].order, t) for t in ready}
        assert ready == sorted(ready, key=sort_key.__getitem__)
        for t in ready:
            task_ = state.tasks[t]
            assert task_.state in CLAIMABLE and t not in state.waiting_on
            assert all(state.tasks[d].state == DONE for d in task_.depends_on)
        plan = assign(state)
        assert set(plan) == {p for p, v in state.participants.items() if v.available}
        chosen = [t for t in plan.values() if t is not None]
        assert len(chosen) == len(set(chosen))
        options = {}
        for pid, t in plan.items():
            me = state.participants[pid]
            active = sum(1 for x in state.tasks.values() if x.owner == pid and x.state in ACTIVE)
            eligible = [
                r for r in ready
                if (me.is_writer or state.tasks[r].kind not in WRITE_KINDS) and state.tasks[r].owner in (None, pid)
            ]
            options[pid] = eligible if active < MAX_ACTIVE_CLAIMS else []
            if t is not None:
                assert t in eligible and active < MAX_ACTIVE_CLAIMS
            elif active < MAX_ACTIVE_CLAIMS:
                # Nobody idles while work it could do sits unassigned.
                assert all(r in chosen for r in eligible)
        # As many participants busy as any non-duplicating assignment allows.
        best = max(
            (sum(x is not None for x in combo) for combo in itertools.product(*[[None] + options[p] for p in plan])
             if len([x for x in combo if x]) == len({x for x in combo if x})),
            default=0,
        )
        assert len(chosen) == best
        for pid, me in state.participants.items():
            recs = recommend(state, pid)
            assert recs and [r.priority for r in recs] == sorted((r.priority for r in recs), reverse=True)
            for r in recs:
                if r.task_id is not None:
                    assert state.tasks[r.task_id].owner in (None, pid)
                    assert me.is_writer or state.tasks[r.task_id].kind not in WRITE_KINDS
                if r.action == "claim":
                    assert r.task_id == plan.get(pid) or state.tasks[r.task_id].owner == pid
                    assert 60 < r.priority < 80


def test_random_runs_finish_with_both_participants_contributing():
    rng = random.Random(2026)
    for _ in range(60):
        n = rng.randint(2, MAX_PLAN_TASKS)
        specs, _ = random_plan(rng, n)
        # At least one task of each side's kind, so both have something to do.
        specs[0] = dataclasses.replace(specs[0], kind="code")
        specs[-1] = dataclasses.replace(specs[-1], kind=rng.choice(("investigate", "test_design", "review")))
        script = {s.key: [f"sig-{s.key}"] for s in specs if rng.random() < 0.3}
        # A little discussion on the side (fewer messages than a stall needs).
        sim = PairSim(script=script, pending={"codex": rng.randint(0, 1)}, ping_pong=rng.randint(0, 2))
        sim.propose(specs, proposer=rng.choice(["claude", "codex"]))
        assert sim.run(max_rounds=12 * 8) == "done"
        assert sim.done_by("claude") >= 1 and sim.done_by("codex") >= 1
        assert sim.replans == 0 and "stalled" not in sim.verdicts


def test_random_all_shared_work_still_splits_between_both():
    rng = random.Random(7)
    for _ in range(40):
        specs, _ = random_plan(rng, rng.randint(2, MAX_PLAN_TASKS), kinds=("investigate", "test_design", "review"))
        sim = PairSim()
        sim.propose(specs, proposer="codex")
        assert sim.run() == "done"
        assert sim.done_by("claude") >= 1 and sim.done_by("codex") >= 1
