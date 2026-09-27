"""Deterministic task scheduling for a pair run (D06).

Pure functions over `taskplan.GraphState`: no database, clock or randomness,
so the same state always gives the same answer and the simulation suite can
replay whole runs. The runtime calls these and turns the results into events
and messages; nothing here has authority of its own.

Ordering everywhere: ties are broken by `GraphTask.order`, then `task_id`
(participants: by `participant_id`), never by dict or set iteration order."""
from __future__ import annotations

import heapq

from .contracts import PolicyDenied, ValidationError, canonical_json, content_hash
from .taskplan import (
    ACTIVE,
    CLAIMABLE,
    CLOSED,
    DONE,
    MAX_ACTIVE_CLAIMS,
    MAX_DELEGATION_DEPTH,
    MAX_PLAN_TASKS,
    MAX_TASKS_PER_RUN,
    TASK_KINDS,
    ISOLATED_KINDS,
    WRITE_KINDS,
    FailureRecord,
    GraphState,
    GraphTask,
    LoopVerdict,
    ParticipantView,
    ProgressSample,
    Recommendation,
    ResolvedTask,
    TaskSpec,
)

CANCELLED = "CANCELLED"
CHANGES_REQUESTED = "CHANGES_REQUESTED"

PRIORITY_ANSWER = 100
PRIORITY_CONTINUE = 80
PRIORITY_CLAIM = 60  # + critical-path length, capped below PRIORITY_CONTINUE
PRIORITY_WAIT = 10

NOTHING_LEFT = "nothing left to do; completion is up to DUET's checks and review"
NO_TASKS_YET = "no tasks are planned yet; propose a plan (tasks with dependencies and acceptance ids) or wait for the peer's"
_MAX_REASON_ITEMS = 8


# --- Plan validation -----------------------------------------------------------------


def validate_plan(state: GraphState, specs: list[TaskSpec], *, proposer: str) -> list[ResolvedTask]:
    """Validate a plan proposal against the current graph and bounds, and
    return its tasks in dependency order. Raises ValidationError or
    PolicyDenied (from runtime.contracts) with a precise message.

    A plan only adds tasks. It has no way to edit, remove or weaken existing
    tasks, the objective or the acceptance contract, and nothing here changes
    `state`. Dependencies and parents name keys of this plan or existing task
    ids. Both count as "must exist first" for the returned order, so a cycle
    through dependencies and parent links together is rejected, as is any
    dependency cycle among the existing tasks. Adding work under a CANCELLED
    parent is rejected like depending on a CANCELLED task."""
    if proposer not in state.participants:
        raise PolicyDenied(f"{proposer!r} is not a participant of this run; only participants propose plans")
    if not isinstance(specs, (list, tuple)):
        raise ValidationError("a plan must be a list of tasks")
    if not specs:
        raise ValidationError("a plan must contain at least one task")
    if len(specs) > MAX_PLAN_TASKS:
        raise ValidationError(f"a plan may propose at most {MAX_PLAN_TASKS} tasks, got {len(specs)}")
    total = len(state.tasks) + len(specs)
    if total > MAX_TASKS_PER_RUN:
        raise PolicyDenied(
            f"the run would have {total} tasks; at most {MAX_TASKS_PER_RUN} tasks per run "
            f"({len(state.tasks)} exist, {len(specs)} proposed)",
            details={"existing": len(state.tasks), "proposed": len(specs), "limit": MAX_TASKS_PER_RUN},
        )

    index: dict[str, int] = {}
    for i, spec in enumerate(specs):
        if not isinstance(spec, TaskSpec):
            raise ValidationError(f"plan entry {i} is not a task spec")
        if spec.kind not in TASK_KINDS:
            raise ValidationError(f"task {spec.key}: kind must be one of {TASK_KINDS}, got {spec.kind!r}")
        if spec.key in index:
            raise ValidationError(f"duplicate task key {spec.key!r} in the plan")
        if spec.key in state.tasks:
            raise ValidationError(f"task key {spec.key!r} collides with an existing task id")
        index[spec.key] = i

    deps: dict[str, list[str]] = {}
    for spec in specs:
        resolved: list[str] = []
        for ref in _str_refs(spec.depends_on, f"task {spec.key} depends_on"):
            _check_ref(state, index, spec.key, ref, "depends on")
            if ref not in resolved:
                resolved.append(ref)
        deps[spec.key] = resolved
        if spec.parent is not None:
            if not isinstance(spec.parent, str):
                raise ValidationError(f"task {spec.key}: parent must be a task key or task id")
            _check_ref(state, index, spec.key, spec.parent, "has parent")
        unknown = [a for a in _str_refs(spec.acceptance_ids, f"task {spec.key} acceptance_ids") if a not in state.acceptance_ids]
        if unknown:
            raise ValidationError(
                f"task {spec.key}: unknown acceptance ids {unknown}; they must exist in the run's acceptance contract",
                details={"task": spec.key, "unknown": unknown},
            )

    # Combined "must exist first" graph: new keys point at their dependencies
    # and parent, existing tasks at their dependencies. Existing tasks never
    # point at new keys, so a cycle through a new key lies wholly in the plan.
    parent_of = {spec.key: spec.parent for spec in specs}
    edges: dict[str, list[str]] = {}
    for spec in specs:
        before = list(deps[spec.key])
        if spec.parent is not None and spec.parent not in before:
            before.append(spec.parent)
        edges[spec.key] = before
    existing = _ordered(state)
    for task in existing:
        edges[task.task_id] = [d for d in task.depends_on if d in state.tasks]
    cycle = _find_cycle([spec.key for spec in specs] + [t.task_id for t in existing], edges)
    if cycle:
        steps = [cycle[0]]
        for a, b in zip(cycle, cycle[1:]):
            is_dep = b in deps.get(a, ()) or (a in state.tasks and b in state.tasks[a].depends_on)
            steps.append(b if is_dep else f"{b} (parent of {a})")
        raise ValidationError("dependency cycle rejected: " + " -> ".join(steps), details={"cycle": cycle})

    order = _plan_order(specs, index, edges)
    depth: dict[str, int] = {}
    for key in order:
        chain = _ancestors(state, parent_of, key)
        if len(chain) > MAX_DELEGATION_DEPTH:
            raise PolicyDenied(
                f"task {key} would have {len(chain)} ancestors ({' -> '.join(chain)}); "
                f"delegation is limited to {MAX_DELEGATION_DEPTH} levels",
                details={"task": key, "ancestors": chain, "limit": MAX_DELEGATION_DEPTH},
            )
        depth[key] = len(chain)

    by_key = {spec.key: spec for spec in specs}
    return [ResolvedTask(spec=by_key[k], depends_on=tuple(deps[k]), parent=parent_of[k], depth=depth[k]) for k in order]


def _str_refs(values: object, field_name: str) -> list[str]:
    if not isinstance(values, (list, tuple)) or not all(isinstance(v, str) and v for v in values):
        raise ValidationError(f"{field_name} must be a list of non-empty strings")
    return list(values)


def _check_ref(state: GraphState, index: dict[str, int], key: str, ref: str, relation: str) -> None:
    if ref in index:
        return
    task = state.tasks.get(ref)
    if task is None:
        raise ValidationError(
            f"task {key} {relation} {ref!r}, which is neither a key in this plan nor a task in this run",
            details={"task": key, "ref": ref},
        )
    if task.state == CANCELLED:
        raise ValidationError(f"task {key} {relation} {ref}, which is CANCELLED", details={"task": key, "ref": ref})


def _find_cycle(nodes: list[str], edges: dict[str, list[str]]) -> list[str] | None:
    """Iterative DFS; returns the first cycle found as a closed path."""
    white, grey, black = 0, 1, 2
    colour = dict.fromkeys(nodes, white)
    for root in nodes:
        if colour[root] != white:
            continue
        colour[root] = grey
        path = [root]
        pending = [iter(edges.get(root, ()))]
        while pending:
            for nxt in pending[-1]:
                seen = colour.get(nxt)
                if seen is None or seen == black:
                    continue
                if seen == grey:
                    return path[path.index(nxt):] + [nxt]
                colour[nxt] = grey
                path.append(nxt)
                pending.append(iter(edges.get(nxt, ())))
                break
            else:
                colour[path.pop()] = black
                pending.pop()
    return None


def _plan_order(specs: list[TaskSpec], index: dict[str, int], edges: dict[str, list[str]]) -> list[str]:
    """Kahn's algorithm over the plan's keys, always taking the earliest
    proposed key that is free, so an already valid order is kept as is."""
    indegree = {spec.key: 0 for spec in specs}
    after: dict[str, list[str]] = {spec.key: [] for spec in specs}
    for spec in specs:
        for ref in edges[spec.key]:
            if ref in index:
                indegree[spec.key] += 1
                after[ref].append(spec.key)
    heap = [index[k] for k, n in indegree.items() if n == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        key = specs[heapq.heappop(heap)].key
        order.append(key)
        for nxt in after[key]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(heap, index[nxt])
    return order


def _ancestors(state: GraphState, parent_of: dict[str, str | None], key: str) -> list[str]:
    """Ancestors of a plan key, nearest first: through plan keys (acyclic,
    checked before), then through existing tasks' `parent_id`."""
    chain: list[str] = []
    parent = parent_of[key]
    while parent is not None:
        chain.append(parent)
        if parent not in parent_of:
            return chain + _existing_ancestors(state, parent)
        parent = parent_of[parent]
    return chain


def _existing_ancestors(state: GraphState, task_id: str) -> list[str]:
    """Ancestors of an existing task. A parent id missing from the state
    still counts as an ancestor."""
    chain: list[str] = []
    seen = {task_id}
    task = state.tasks.get(task_id)
    while task is not None and task.parent_id is not None:
        if task.parent_id in seen:
            raise ValidationError(f"the parent links above {task_id} loop; the task graph is inconsistent")
        chain.append(task.parent_id)
        seen.add(task.parent_id)
        task = state.tasks.get(task.parent_id)
    return chain


# --- Readiness, critical path, assignment ---------------------------------------------


def _ordered(state: GraphState) -> list[GraphTask]:
    return sorted(state.tasks.values(), key=lambda t: (t.order, t.task_id))


def _unmet(state: GraphState, task: GraphTask) -> list[str]:
    return [d for d in task.depends_on if d not in state.tasks or state.tasks[d].state != DONE]


def _is_ready(state: GraphState, task: GraphTask) -> bool:
    if task.state not in CLAIMABLE:
        return False
    if task.owner is not None and task.state != CHANGES_REQUESTED:
        return False
    if task.task_id in state.waiting_on:
        return False
    return not _unmet(state, task)


def ready_tasks(state: GraphState) -> list[str]:
    """Claimable tasks (READY or CHANGES_REQUESTED, not owned by anyone else)
    whose dependencies are all VERIFIED, in priority order.

    A CHANGES_REQUESTED task keeps its owner and is listed: only that owner
    may claim it again (see `assign`). A task with an open request in
    `state.waiting_on` is blocked, not ready. Order: longest critical path
    first, then required before optional, then creation order, then id."""
    path = critical_path(state)
    ready = [t for t in _ordered(state) if _is_ready(state, t)]
    ready.sort(key=lambda t: (-path.get(t.task_id, 1), not t.required, t.order, t.task_id))
    return [t.task_id for t in ready]


def critical_path(state: GraphState) -> dict[str, int]:
    """For every open task, the length of the longest chain of open tasks
    that (transitively) depend on it, counting itself.

    Closed tasks (VERIFIED, CANCELLED) are left out and do not carry a chain.
    Linear in tasks plus dependency edges."""
    open_tasks = [t for t in _ordered(state) if t.state not in CLOSED]
    open_ids = {t.task_id for t in open_tasks}
    dependents: dict[str, list[str]] = {tid: [] for tid in open_ids}
    indegree = dict.fromkeys(open_ids, 0)
    for task in open_tasks:
        for dep in dict.fromkeys(task.depends_on):
            if dep in open_ids and dep != task.task_id:
                dependents[dep].append(task.task_id)
                indegree[task.task_id] += 1
    queue = [t.task_id for t in open_tasks if indegree[t.task_id] == 0]
    topo: list[str] = []
    while queue:
        tid = queue.pop()
        topo.append(tid)
        for nxt in dependents[tid]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    result: dict[str, int] = {}
    for tid in reversed(topo):
        result[tid] = 1 + max((result[d] for d in dependents[tid]), default=0)
    # Defensive: a cycle (rejected at validation) leaves nodes unsorted.
    for task in open_tasks:
        if task.task_id not in result:
            result[task.task_id] = 1 + max((result.get(d, 0) for d in dependents[task.task_id]), default=0)
    return {t.task_id: result[t.task_id] for t in open_tasks}


def _active_count(state: GraphState, participant_id: str) -> int:
    return sum(1 for t in state.tasks.values() if t.owner == participant_id and t.state in ACTIVE)


def _completed_count(state: GraphState, participant_id: str) -> int:
    return sum(1 for t in state.tasks.values() if t.owner == participant_id and t.state == DONE)


def _eligible(person: ParticipantView, task: GraphTask) -> bool:
    if task.kind in WRITE_KINDS and not person.is_writer:
        return False
    if task.kind in ISOLATED_KINDS and person.is_writer:
        return False  # isolated work is the non-writer's; the writer integrates it
    if task.owner is not None and task.owner != person.participant_id:
        return False
    return True


def _matching_size(people: list[str], options: dict[str, list[str]], taken: set[str]) -> int:
    """Maximum bipartite matching (augmenting paths) of people to tasks."""
    match: dict[str, str] = {}

    def augment(person: str, seen: set[str]) -> bool:
        for tid in options[person]:
            if tid in taken or tid in seen:
                continue
            seen.add(tid)
            if tid not in match or augment(match[tid], seen):
                match[tid] = person
                return True
        return False

    return sum(1 for person in people if augment(person, set()))


def assign(state: GraphState) -> dict[str, str | None]:
    """A non-duplicating suggestion: each available participant gets at most
    one ready task it is eligible for, no task goes to two participants.

    Eligibility: code needs the writer; a CHANGES_REQUESTED task goes back to
    its owner only; a participant holding MAX_ACTIVE_CLAIMS active tasks gets
    None. Participants are served in balance order: fewer active tasks
    first, then fewer completed tasks, then non-writers before the writer
    (only the writer can take code, so shared work goes to the other side on
    a tie), then participant id. Each takes its highest-priority task, except
    that no one takes a task whose loss would leave another participant idle
    while it has an alternative: the result is the maximum number of busy
    participants, chosen in that order. Unavailable participants are not in
    the result."""
    ready = ready_tasks(state)
    people = sorted(
        (p for p in state.participants.values() if p.available),
        key=lambda p: (_active_count(state, p.participant_id), _completed_count(state, p.participant_id), p.is_writer, p.participant_id),
    )
    options: dict[str, list[str]] = {}
    for person in people:
        pid = person.participant_id
        if _active_count(state, pid) >= MAX_ACTIVE_CLAIMS:
            options[pid] = []
        else:
            options[pid] = [tid for tid in ready if _eligible(person, state.tasks[tid])]
    ids = [p.participant_id for p in people]
    result: dict[str, str | None] = dict.fromkeys(ids)
    taken: set[str] = set()
    remaining = _matching_size(ids, options, taken)
    for i, pid in enumerate(ids):
        if remaining == 0:
            break
        for tid in options[pid]:
            if tid in taken:
                continue
            if 1 + _matching_size(ids[i + 1:], options, taken | {tid}) == remaining:
                result[pid] = tid
                taken.add(tid)
                remaining -= 1
                break
    return result


# --- Recommendations -----------------------------------------------------------------


def recommend(state: GraphState, participant_id: str) -> list[Recommendation]:
    """What this participant should do next, highest priority first.

    answer (100): open peer questions or review requests addressed to it;
    continue (80): each task it owns in an active state;
    claim (60 + critical path, at most 79): the task `assign` gives it and
    its own CHANGES_REQUESTED tasks, within its free claim slots;
    wait (10): only when there is nothing else, naming what blocks the work.
    Never a task owned by someone else, never code for a non-writer."""
    me = state.participants.get(participant_id)
    if me is None:
        raise ValidationError(f"unknown participant {participant_id!r}")
    if not me.available:
        return [
            Recommendation(
                "wait",
                f"{participant_id} is unavailable; its tasks wait until it reconnects or the user reassigns them",
                priority=PRIORITY_WAIT,
            )
        ]
    recs: list[Recommendation] = []
    pending = state.pending_requests.get(participant_id, 0)
    if isinstance(pending, int) and pending > 0:
        recs.append(
            Recommendation(
                "answer",
                f"answer {pending} open peer request(s) (questions or review requests) before anything else",
                priority=PRIORITY_ANSWER,
            )
        )
    mine = [t for t in _ordered(state) if t.owner == participant_id and t.state in ACTIVE]
    for task in mine:
        reason = f"continue your {task.state} {task.kind} task {task.task_id}"
        if task.task_id in state.waiting_on:
            reason += f"; it waits on {state.waiting_on[task.task_id]}"
        recs.append(Recommendation("continue", reason, task_id=task.task_id, priority=PRIORITY_CONTINUE))
    free = MAX_ACTIVE_CLAIMS - len(mine)
    if free > 0:
        path = critical_path(state)
        suggested = assign(state).get(participant_id)
        claims: list[str] = []
        for tid in ready_tasks(state):
            task = state.tasks[tid]
            own_repair = task.state == CHANGES_REQUESTED and task.owner == participant_id
            if (tid == suggested or own_repair) and _eligible(me, task):
                claims.append(tid)
        for tid in claims[:free]:
            task = state.tasks[tid]
            length = path.get(tid, 1)
            if task.state == CHANGES_REQUESTED:
                reason = f"changes were requested on your {task.kind} task {tid}; claim it again and repair it"
            else:
                reason = f"claim {task.kind} task {tid} ({'required' if task.required else 'optional'}; critical path {length})"
            recs.append(Recommendation("claim", reason, task_id=tid, priority=min(PRIORITY_CLAIM + length, PRIORITY_CONTINUE - 1)))
    if not recs:
        recs.append(Recommendation("wait", _wait_reason(state, me), priority=PRIORITY_WAIT))
    recs.sort(key=lambda r: -r.priority)
    return recs


def _wait_reason(state: GraphState, me: ParticipantView) -> str:
    if not state.tasks:
        return NO_TASKS_YET
    open_tasks = [t for t in _ordered(state) if t.state not in CLOSED]
    if not open_tasks:
        return NOTHING_LEFT
    ready = set(ready_tasks(state))
    suggested = {tid: pid for pid, tid in assign(state).items() if tid is not None}
    blocked: list[str] = []
    other: list[str] = []
    for task in open_tasks:
        tid = task.task_id
        unmet = _unmet(state, task)
        owner = state.participants.get(task.owner) if task.owner else None
        owner_note = f" ({task.owner} is unavailable; the task can be released for reassignment)" if owner and not owner.available else ""
        if tid in state.waiting_on:
            blocked.append(f"{tid} waits on {state.waiting_on[tid]}")
        elif unmet:
            blocked.append(f"{tid} waits for {', '.join(unmet)}")
        elif task.state in ACTIVE:
            other.append(f"{tid} is {task.state} with {task.owner}{owner_note}")
        elif task.state == "REVIEW_REQUIRED":
            other.append(f"{tid} awaits review")
        elif task.state == "BLOCKED":
            other.append(f"{tid} is BLOCKED")
        elif task.state == "PROPOSED":
            other.append(f"{tid} awaits acceptance")
        elif tid in ready and task.kind in WRITE_KINDS and not me.is_writer:
            other.append(f"{tid} is {task.kind} work for the writer")
        elif tid in ready and task.kind in ISOLATED_KINDS and me.is_writer:
            other.append(f"{tid} is isolated code work for your peer; you integrate it when you accept it")
        elif tid in ready and task.owner is not None and task.owner != me.participant_id:
            other.append(f"{tid} is {task.owner}'s to repair{owner_note}")
        elif tid in suggested and suggested[tid] != me.participant_id:
            other.append(f"{tid} is suggested for {suggested[tid]}")
        else:
            other.append(f"{tid} is {task.state}")
    items = blocked + other
    shown = "; ".join(items[:_MAX_REASON_ITEMS])
    if len(items) > _MAX_REASON_ITEMS:
        shown += f"; and {len(items) - _MAX_REASON_ITEMS} more"
    return "nothing to claim now: " + shown


# --- Progress and loop detection -------------------------------------------------------


def _rows(items: object, name: str) -> list[dict]:
    if items is None:
        return []
    if not isinstance(items, (list, tuple)) or not all(isinstance(i, dict) for i in items):
        raise ValidationError(f"{name} must be a list of objects")
    return list(items)


def _unique_sorted(items: list) -> list:
    return sorted({canonical_json(i): i for i in items}.values(), key=canonical_json)


def progress_fingerprint(tasks: list[dict], snapshots: list[dict], evidence: list[dict], findings: list[dict]) -> str:
    """Hash of the state that counts as progress.

    Only task (id, state, revision, owner), snapshot tree hashes, evidence
    (snapshot, check, status) and the ids of open findings are read; every
    other key (message text, summaries, timestamps) is ignored, so talk alone
    never looks like progress. Order and exact duplicates do not matter."""
    body = {
        "v": 1,
        "tasks": _unique_sorted(
            [[t.get("task_id"), t.get("state"), t.get("revision"), t.get("owner")] for t in _rows(tasks, "tasks")]
        ),
        "trees": _unique_sorted([s.get("tree_hash") for s in _rows(snapshots, "snapshots")]),
        "evidence": _unique_sorted(
            [[e.get("snapshot_id"), e.get("check_id"), e.get("status")] for e in _rows(evidence, "evidence")]
        ),
        "open_findings": _unique_sorted([f.get("finding_id") for f in _rows(findings, "findings") if f.get("status") == "open"]),
    }
    return content_hash(body)


def detect_loop(
    samples: list[ProgressSample],
    failures: list[FailureRecord],
    *,
    replans_done: int,
    max_repairs: int,
    max_idle_messages: int,
) -> LoopVerdict:
    """Decide whether the run is progressing, stalled, needs a focused
    re-plan, or must pause honestly.

    `failures` are those under the current approach (since the last
    re-plan). A task whose same failure repeats `max_repairs` times, or that
    fails `max_repairs + 1` times in any way, needs a re-plan; after a
    re-plan it needs an honest pause instead. When several tasks qualify, the
    one that crossed the limit first is reported. A discussion is stalled when
    the last `max_idle_messages` samples are all messages and none changed
    the fingerprint of the sample before them. Failures take precedence."""
    for name, value, minimum in (("replans_done", replans_done, 0), ("max_repairs", max_repairs, 1), ("max_idle_messages", max_idle_messages, 1)):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValidationError(f"{name} must be an integer >= {minimum}")

    by_task: dict[str, list[FailureRecord]] = {}
    for failure in sorted(failures, key=lambda f: (f.index, f.task_id, f.signature)):
        by_task.setdefault(failure.task_id, []).append(failure)
    crossed: list[tuple[int, str]] = []
    for task_id, records in by_task.items():
        counts: dict[str, int] = {}
        for n, record in enumerate(records, 1):
            counts[record.signature] = counts.get(record.signature, 0) + 1
            if counts[record.signature] >= max_repairs or n >= max_repairs + 1:
                crossed.append((record.index, task_id))
                break
    if crossed:
        _, task_id = min(crossed)
        records = by_task[task_id]
        signatures = tuple(r.signature for r in records)
        repeated = max(signatures.count(s) for s in set(signatures))
        what = (
            f"task {task_id} failed {len(records)} times under the current approach "
            f"({repeated} with the same signature)"
        )
        evidence = (task_id,) + signatures
        if replans_done == 0:
            return LoopVerdict(
                "replan",
                what + "; stop repairing and make a focused re-plan with the peer. "
                "The objective and the acceptance contract stay as they are.",
                task_id=task_id,
                evidence=evidence,
            )
        return LoopVerdict(
            "pause",
            what + f" after {replans_done} re-plan(s); pause and report honestly instead of retrying.",
            task_id=task_id,
            evidence=evidence,
        )

    ordered = sorted(samples, key=lambda s: s.index)
    if len(ordered) > max_idle_messages:
        base = ordered[-max_idle_messages - 1]
        tail = ordered[-max_idle_messages:]
        if all(s.kind == "message" and s.fingerprint == base.fingerprint for s in tail):
            return LoopVerdict(
                "stalled",
                f"{max_idle_messages} messages in a row changed no task, snapshot, evidence or finding; "
                "stop discussing and decide, propose a plan, or pause",
                evidence=(base.fingerprint,),
            )
    return LoopVerdict("ok", "no repeated failure or stalled discussion")
