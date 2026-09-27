"""Shared task graph for a pair run (D06): plans, task results, roles,
contributions and loop control. The decisions come from the pure
`scheduler` module; this module turns them into commands and events.

- Plans are shared: one participant proposes, the *other* accepts or rejects.
  A plan only adds tasks; it cannot touch the objective, the acceptance
  contract or existing tasks.
- A finished non-main task goes to the peer for a decision (non-author
  acceptance), then the controller marks it VERIFIED. The main task keeps the
  D05 path: DUET's checks plus a cross-provider snapshot review.
- Contributions are recorded only from evidence (an authored change, a
  review of an exact snapshot, an accepted task result, an accepted plan),
  never from message counts (R01).
- Loop control: repeated failed hypotheses lead first to a focused re-plan
  (the task is blocked until the peer accepts a new plan), then to an honest
  pause (PAUSED_APPROVAL: the user decides). Messages that change no task or
  evidence state are a stall; a stall that continues ends in a pause too."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import deque
from dataclasses import asdict
from typing import Any

from . import scheduler
from .contracts import (
    CONTROLLER,
    MAX_ID,
    MAX_TEXT,
    TERMINAL_RUN,
    Conflict,
    DomainError,
    InvalidTransition,
    Liveness,
    MessageKind,
    NotFound,
    PolicyDenied,
    Principal,
    RunLifecycle,
    TaskState,
    Unauthorized,
    ValidationError,
    check_text,
    new_id,
)
from .taskplan import (
    ACTIVE,
    MAX_ACTIVE_CLAIMS,
    MAX_TASKS_PER_RUN,
    ISOLATED_KINDS,
    WRITE_KINDS,
    FailureRecord,
    GraphState,
    GraphTask,
    ParticipantView,
    ProgressSample,
    TaskSpec,
)

STALL_MESSAGES = 12  # peer messages without any task/evidence change
SAMPLE_WINDOW = 200
OPEN_REQUEST_STATES = ("QUEUED", "TRANSPORT_DELIVERED", "PARTICIPANT_ACKNOWLEDGED")


def contribution_id(run_id: str, participant_id: str, kind: str, ref: str) -> str:
    return "ctb_" + hashlib.sha256(f"{run_id}|{participant_id}|{kind}|{ref}".encode()).hexdigest()[:32]


class TaskGraph:
    def __init__(self, coordinator: Any) -> None:
        self.co = coordinator
        self.rt = coordinator.runtime
        self._lock = threading.RLock()
        self._samples: dict[str, deque] = {}
        self._counter = 0
        self._nudged: set[tuple[str, str, str, int]] = set()
        self._claim_locks: dict[str, threading.Lock] = {}

    # ------------------------------------------------------------------ state

    def graph_state(self, run_id: str) -> GraphState:
        tx = self.rt.store.read()
        rows = tx.query("SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at, rowid", (run_id,))
        tasks = {
            r["task_id"]: GraphTask(
                task_id=r["task_id"], kind=r["kind"], state=r["state"], owner=r["owner"], required=bool(r["required"]),
                depends_on=tuple(json.loads(r["depends_on_json"])), parent_id=r["parent_id"],
                acceptance_ids=tuple(json.loads(r["acceptance_ids_json"])), attempts=r["attempts"], proposed_by=r["proposed_by"], order=i,
            )
            for i, r in enumerate(rows)
        }
        writer = self.co._writer_id(run_id)
        participants = {
            p["participant_id"]: ParticipantView(
                participant_id=p["participant_id"], provider=p["provider"], is_writer=p["participant_id"] == writer,
                available=p["liveness"] in (Liveness.CONNECTED.value, Liveness.IDLE.value),
            )
            for p in self.rt.participants(CONTROLLER, run_id)
        }
        pending: dict[str, int] = {}
        for row in tx.query(
            f"SELECT recipient, COUNT(*) AS n FROM messages WHERE run_id = ? AND kind IN ('QUESTION', 'REVIEW_REQUEST') "
            f"AND state IN ({','.join('?' * len(OPEN_REQUEST_STATES))}) AND recipient IS NOT NULL GROUP BY recipient",
            (run_id, *OPEN_REQUEST_STATES),
        ):
            pending[row["recipient"]] = row["n"]
        waiting = {
            t.task_id: ("a peer review" if t.state == TaskState.REVIEW_REQUIRED.value else (tx.get("tasks", t.task_id)["blocked_reason"] or "blocked"))
            for t in tasks.values()
            if t.state in (TaskState.REVIEW_REQUIRED.value, TaskState.BLOCKED.value)
        }
        run = tx.require("runs", run_id)
        criteria = json.loads(run["acceptance_json"]).get("criteria", [])
        return GraphState(
            tasks=tasks, participants=participants, acceptance_ids=frozenset(c.get("id") for c in criteria if isinstance(c, dict)),
            pending_requests=pending, waiting_on=waiting,
        )

    def next_for(self, principal: Principal) -> list[dict]:
        state = self.graph_state(principal.run_id or "")
        return [asdict(r) for r in scheduler.recommend(state, principal.id)]

    def fingerprint(self, run_id: str) -> str:
        tx = self.rt.store.read()
        tasks = [dict(r) for r in tx.query("SELECT task_id, state, revision, owner FROM tasks WHERE run_id = ?", (run_id,))]
        snapshots = [dict(r) for r in tx.query("SELECT tree_hash FROM snapshots WHERE run_id = ?", (run_id,))]
        evidence = [dict(r) for r in tx.query("SELECT snapshot_id, check_id, status FROM evidence WHERE run_id = ?", (run_id,))]
        findings = [dict(r) for r in tx.query("SELECT finding_id, status FROM findings WHERE run_id = ?", (run_id,))]
        return scheduler.progress_fingerprint(tasks, snapshots, evidence, findings)

    # ------------------------------------------------------------------ plans

    def propose_plan(self, principal: Principal, *, tasks: list, rationale: str = "") -> dict:
        self.co._require_participant(principal)
        run_id = principal.run_id or ""
        check_text(rationale, "rationale", limit=MAX_TEXT, allow_empty=True)
        if not isinstance(tasks, list):
            raise ValidationError("tasks must be a list of task objects")
        specs = [TaskSpec.from_dict(t) for t in tasks]
        peer = self.co._peer_id(principal)  # a shared plan needs someone to accept it
        plan_id = new_id("pln")
        # The one-pending-plan rule and the bounds are checked in the same
        # write transaction that records the plan (BEGIN IMMEDIATE serialises
        # writers across threads and processes), so two concurrent proposals
        # cannot both pass them.
        with self.rt.store.transaction() as wtx:
            self.rt._live_run(wtx, run_id)
            if wtx.scalar("SELECT COUNT(*) FROM plans WHERE run_id = ? AND state = 'PROPOSED'", (run_id,)):
                raise PolicyDenied("another plan is waiting for a decision; decide or withdraw it first")
            scheduler.validate_plan(self.graph_state(run_id), specs, proposer=principal.id)
            wtx.emit(self.rt._event("plan.proposed", {"plan_id": plan_id, "run_id": run_id, "proposer": principal.id, "rationale": rationale, "tasks": [s.to_dict() for s in specs]}, principal, run_id))
        lines = [f"Plan {plan_id} proposed by {principal.provider}: {rationale or '(no rationale given)'}"]
        for spec in specs:
            deps = f" after {', '.join(spec.depends_on)}" if spec.depends_on else ""
            lines.append(f"- [{spec.key}] ({spec.kind}{deps}) {spec.description}")
        lines.append(f"Accept or reject with duet_decide_plan(plan_id='{plan_id}', decision='accept'|'reject', reason=...).")
        self.rt.send_message(principal, kind=MessageKind.PLAN_PROPOSAL, body="\n".join(lines)[:MAX_TEXT], recipient=peer)
        self.co.notify()
        return {"plan_id": plan_id, "state": "PROPOSED", "tasks": [s.key for s in specs], "note": "your peer must accept the plan before its tasks exist"}

    def decide_plan(self, principal: Principal, *, plan_id: str, decision: str, reason: str = "") -> dict:
        self.co._require_participant(principal)
        run_id = principal.run_id or ""
        check_text(plan_id, "plan_id", limit=MAX_ID)
        check_text(reason, "reason", limit=MAX_TEXT, allow_empty=True)
        if decision not in ("accept", "reject", "withdraw"):
            raise ValidationError("decision must be accept, reject or withdraw")
        plan = self.rt.store.read().get("plans", plan_id)
        if plan is None or plan["run_id"] != run_id:
            raise NotFound(f"no plan {plan_id} in this run")
        if plan["state"] != "PROPOSED":
            raise InvalidTransition(f"plan {plan_id} is already {plan['state']}")
        if decision == "withdraw" and plan["proposer"] != principal.id:
            raise Unauthorized("only the proposer can withdraw a plan")
        if decision != "withdraw" and plan["proposer"] == principal.id:
            raise Unauthorized("a plan is accepted or rejected by the other participant, not its proposer")
        state = {"accept": "ACCEPTED", "reject": "REJECTED", "withdraw": "WITHDRAWN"}[decision]
        ids = self._decide(run_id, plan_id, state, principal, reason)
        if decision == "withdraw":
            return {"plan_id": plan_id, "state": state}
        if decision == "reject":
            # While a task waits for a re-plan the proposer has to act on a
            # rejection (a managed peer takes a turn for a BLOCKER only).
            kind = MessageKind.BLOCKER if self._replan_blocked(run_id) else MessageKind.STATUS
            self.co._status(run_id, plan["proposer"], f"Plan {plan_id} was rejected by {principal.provider}: {reason or '(no reason)'}", kind=kind)
            self.co.notify()
            self.observe(run_id, "review")
            return {"plan_id": plan_id, "state": state}
        self._unblock_after_replan(run_id, plan_id)
        # Actionable for the proposer: its plan's tasks now exist, and after a
        # re-plan the blocked task is claimable again. A STATUS would not wake
        # a managed proposer, and nothing else would.
        self.co._status(
            run_id, plan["proposer"],
            f"Plan {plan_id} accepted by {principal.provider}; tasks: " + ", ".join(f"{k}={v}" for k, v in ids.items())
            + ". duet_status lists what to do next.",
            kind=MessageKind.TASK_PROPOSAL,
        )
        self.co.notify()
        self.co.try_complete(run_id)  # the plan is a contribution; the predicate may hold now
        self.observe(run_id, "review")
        self.nudge(run_id)
        return {"plan_id": plan_id, "state": state, "tasks": ids}

    def _decide(self, run_id: str, plan_id: str, state: str, principal: Principal, reason: str) -> dict[str, str]:
        """Record a plan decision, and for an acceptance create the plan's
        tasks and the proposer's contribution, in one write transaction that
        re-checks the plan is still PROPOSED and re-validates it against the
        graph as it is now. Two racing decisions therefore cannot both take
        effect (no duplicated tasks), and a plan withdrawn or rejected
        meanwhile adds nothing; this holds across processes, not only threads.
        Returns plan key -> created task id."""
        ids: dict[str, str] = {}
        with self.rt.store.transaction() as tx:
            self.rt._live_run(tx, run_id)
            plan = tx.require("plans", plan_id)
            if plan["state"] != "PROPOSED":
                raise InvalidTransition(f"plan {plan_id} is already {plan['state']}")
            if state == "ACCEPTED":
                ids = self._create_plan_tasks(tx, run_id, plan, principal)
                self._contribution(tx, run_id, plan["proposer"], "plan", plan_id, f"accepted plan with {len(ids)} task(s)")
            tx.emit(self.rt._event("plan.decided", {"plan_id": plan_id, "state": state, "decided_by": principal.id, "reason": reason, "created_task_ids": list(ids.values())}, principal, run_id))
        return ids

    def _create_plan_tasks(self, tx: Any, run_id: str, plan: dict, decider: Principal) -> dict[str, str]:
        """The events `Runtime.propose_task` and `transition_task` would emit
        (a participant's PROPOSED task, then the controller's move to READY),
        emitted inside the caller's transaction; those methods open their own.
        `validate_plan` has already checked what they would: acceptance ids,
        dependencies and parents within this run, bounds and cycles."""
        specs = [TaskSpec.from_dict(t) for t in json.loads(plan["tasks_json"])]
        resolved = scheduler.validate_plan(self.graph_state(run_id), specs, proposer=plan["proposer"])  # the graph may have changed
        proposer_row = tx.require("participants", plan["proposer"])
        proposer = Principal("participant", proposer_row["participant_id"], run_id=run_id, provider=proposer_row["provider"])
        ids: dict[str, str] = {}
        for entry in resolved:
            spec = entry.spec
            task_id = new_id("tsk")
            tx.emit(self.rt._event("task.proposed", {
                "task_id": task_id, "run_id": run_id, "parent_id": ids.get(entry.parent, entry.parent) if entry.parent else None,
                "description": spec.description, "deliverables": list(spec.deliverables), "acceptance_ids": list(spec.acceptance_ids),
                "depends_on": [ids.get(d, d) for d in entry.depends_on], "required": bool(spec.acceptance_ids),
                "state": TaskState.PROPOSED.value, "proposed_by": proposer.id, "kind": spec.kind,
            }, proposer, run_id))
            tx.emit(self.rt._event("task.transition", {
                "task_id": task_id, "from": TaskState.PROPOSED.value, "to": TaskState.READY.value,
                "reason": f"plan {plan['plan_id']} accepted by {decider.provider}", "blocked_reason": None, "next_action": None,
                "bump_revision": False, "owner": None,
            }, CONTROLLER, run_id))
            ids[spec.key] = task_id
        return ids

    def _replan_blocked(self, run_id: str) -> bool:
        return any(
            t["state"] == TaskState.BLOCKED.value and (t.get("blocked_reason") or "").startswith("re-plan required")
            for t in self.rt.tasks(CONTROLLER, run_id)
        )

    # ------------------------------------------------------------------ claims and results

    def claim(self, principal: Principal, task: dict) -> dict:
        """`check_claim`, then the runtime claim, under the run's claim lock:
        the active-claim bound is a count read before the claim commits, so
        two concurrent claims by one participant could otherwise both pass
        it. Participant operations run only in the DUET service (one per
        state directory, held by a file lock), so a process lock suffices."""
        with self._claim_lock(principal.run_id or ""):
            self.check_claim(principal, task)
            return self.rt.claim_task(principal, task["task_id"], expected_version=task["state_version"])["task"]

    def _claim_lock(self, run_id: str) -> threading.Lock:
        with self._lock:
            return self._claim_locks.setdefault(run_id, threading.Lock())

    def check_claim(self, principal: Principal, task: dict) -> None:
        """Invalid claims are rejected here, before the runtime's own checks
        (state version, dependencies, exclusive lease)."""
        run_id = principal.run_id or ""
        writer = self.co._writer_id(run_id)
        if task["kind"] in WRITE_KINDS and writer != principal.id:
            raise PolicyDenied("this task changes files and the run has one writer: your peer. Take a non-code task or review instead")
        if task["kind"] in ISOLATED_KINDS and writer == principal.id:
            raise PolicyDenied("isolated code tasks are for your peer, in a worktree of their own; you integrate the result when you accept it")
        if task["state"] == TaskState.CHANGES_REQUESTED.value and task["owner"] not in (None, principal.id):
            if not (task["kind"] in WRITE_KINDS and writer == principal.id):
                raise PolicyDenied("changes were requested from the task's owner; it is theirs to fix")
        active = self.rt.store.read().scalar(
            f"SELECT COUNT(*) FROM tasks WHERE run_id = ? AND owner = ? AND state IN ({','.join('?' * len(ACTIVE))})",
            (run_id, principal.id, *sorted(ACTIVE)),
        ) or 0
        if active >= MAX_ACTIVE_CLAIMS and not (task["owner"] == principal.id and task["state"] in ACTIVE):
            raise PolicyDenied(f"you already hold {active} active task(s) (limit {MAX_ACTIVE_CLAIMS}); finish one first")

    def complete_task(self, principal: Principal, *, task_id: str, summary: str, artifact: str | None = None) -> dict:
        self.co._require_participant(principal)
        run_id = principal.run_id or ""
        check_text(summary, "summary", limit=MAX_TEXT)
        if artifact is not None:
            check_text(artifact, "artifact", limit=256 * 1024, allow_empty=True)
        if task_id == self.co._main_task_id(run_id):
            raise ValidationError("the main task is finished with duet_submit: DUET runs the acceptance checks on it")
        task = self.co._task(task_id, run_id)
        if task["owner"] != principal.id:
            raise Unauthorized("only the task's owner can complete it")
        fence = self.co._task_fence(task_id, principal.id)
        if task["state"] == TaskState.CLAIMED.value:
            task = self.rt.transition_task(principal, task_id, TaskState.RUNNING, expected_version=task["state_version"], fence=fence, reason="work started")
        if task["state"] != TaskState.RUNNING.value:
            raise InvalidTransition(f"task {task_id} is {task['state']}; claim it before completing it")
        snapshot_id = None
        if task["kind"] in ISOLATED_KINDS:
            row = self.co.parallel.capture(principal, task)
            snapshot_id = row["snapshot_id"]
            self.credit_code(run_id, principal.id, row)
        elif task["kind"] in WRITE_KINDS:
            from ..workspaces.snapshots import capture_snapshot

            ws = self.co.workspace(run_id)
            self.co.workspaces.check_writer(ws, principal.id, self.co._writer_fence(ws, principal.id))
            snap = capture_snapshot(ws.path, base_sha=ws.base_sha, store=self.co.artifacts)
            row = self.co.evidence.record_snapshot(CONTROLLER, run_id, snap, author=principal.id)
            snapshot_id = row["snapshot_id"]
            self.credit_code(run_id, principal.id, row)
        artifact_ref = self.co.artifacts.put_text(artifact) if artifact else None
        result_id = new_id("res")
        with self.rt.store.transaction() as tx:
            self.rt._live_run(tx, run_id)
            tx.emit(self.rt._event("task.result", {"result_id": result_id, "task_id": task_id, "run_id": run_id, "author": principal.id, "summary": summary, "artifact_ref": artifact_ref, "snapshot_id": snapshot_id}, principal, run_id))
        self.rt.transition_task(principal, task_id, TaskState.REVIEW_REQUIRED, expected_version=task["state_version"], fence=fence, reason=summary[:500])
        peer = self.co._peer_id(principal)
        body = f"Task {task_id} ({task['kind']}) is done and needs your decision: {task['description'][:300]}\nResult: {summary}\n"
        if artifact:
            body += f"\nAttached result (excerpt):\n{artifact[:6000]}\n"
        body += f"Decide with duet_decide_task(task_id='{task_id}', decision='accept'|'reject', reason=...)."
        self.rt.send_message(principal, kind=MessageKind.REVIEW_REQUEST, body=body[:MAX_TEXT], recipient=peer, task_id=task_id, snapshot_ref=snapshot_id, artifact_refs=[artifact_ref] if artifact_ref else None)
        self.co.notify()
        self.observe(run_id, "submission")
        return {"task_id": task_id, "result_id": result_id, "state": TaskState.REVIEW_REQUIRED.value, "snapshot_id": snapshot_id}

    def decide_task(self, principal: Principal, *, task_id: str, decision: str, reason: str = "") -> dict:
        self.co._require_participant(principal)
        run_id = principal.run_id or ""
        check_text(reason, "reason", limit=MAX_TEXT, allow_empty=True)
        if decision not in ("accept", "reject"):
            raise ValidationError("decision must be accept or reject")
        if task_id == self.co._main_task_id(run_id):
            raise ValidationError("the main task is decided by DUET's checks and a snapshot review (duet_send REVIEW_RESULT), not by duet_decide_task")
        task = self.co._task(task_id, run_id)
        if task["state"] != TaskState.REVIEW_REQUIRED.value:
            raise InvalidTransition(f"task {task_id} is {task['state']}, not waiting for a decision")
        if task["owner"] == principal.id:
            raise Unauthorized("the author of a task result cannot accept it")
        tx = self.rt.store.read()
        rows = tx.query("SELECT * FROM task_results WHERE task_id = ? AND decision IS NULL ORDER BY created_at DESC, rowid DESC LIMIT 1", (task_id,))
        if not rows:
            raise NotFound(f"task {task_id} has no result waiting for a decision (the main task is decided by review and checks)")
        result = dict(rows[0])
        with self.rt.store.transaction() as wtx:
            self.rt._live_run(wtx, run_id)
            wtx.emit(self.rt._event("task.result.decided", {"result_id": result["result_id"], "decision": "accepted" if decision == "accept" else "rejected", "decided_by": principal.id, "reason": reason}, principal, run_id))
        for message in tx.query(
            f"SELECT message_id FROM messages WHERE run_id = ? AND task_id = ? AND recipient = ? AND kind = 'REVIEW_REQUEST' AND state IN ({','.join('?' * len(OPEN_REQUEST_STATES))})",
            (run_id, task_id, principal.id, *OPEN_REQUEST_STATES),
        ):
            self.rt.mark_handled(principal, message["message_id"])
        integration = None
        if decision == "accept" and task["kind"] in ISOLATED_KINDS:
            # The writer accepts, and integrates, inside this call (D11).
            integration = self.co.parallel.integrate(principal, task)
        fresh = self.co._task(task_id, run_id)
        if decision == "accept":
            self.rt.transition_task(CONTROLLER, task_id, TaskState.VERIFIED, expected_version=fresh["state_version"], reason=f"accepted by {principal.provider}: {reason}"[:500])
            self.record_contribution(run_id, result["author"], task["kind"], task_id, result["summary"][:300])
            self.record_contribution(run_id, principal.id, "review", result["result_id"], f"accepted {task_id}")
            self.co._status(run_id, result["author"], f"Task {task_id} accepted by {principal.provider}.")
            # A required task verified after the main snapshot was approved
            # may be the last missing obligation; nothing else re-evaluates.
            self.co.try_complete(run_id)
        else:
            self.rt.transition_task(CONTROLLER, task_id, TaskState.CHANGES_REQUESTED, expected_version=fresh["state_version"], reason=f"rejected by {principal.provider}: {reason}"[:500])
            self.record_contribution(run_id, principal.id, "review", result["result_id"], f"rejected {task_id}: {reason[:200]}")
            self.co._status(run_id, result["author"], f"Task {task_id} was rejected by {principal.provider}: {reason or '(no reason)'}. Claim it again to rework it.", kind=MessageKind.BLOCKER)
        self.co.notify()
        self.observe(run_id, "review")
        self.nudge(run_id)
        out = {"task_id": task_id, "decision": decision, "state": self.co._task(task_id, run_id)["state"]}
        if integration is not None:
            out["integration"] = integration
        return out

    # ------------------------------------------------------------------ roles

    def handoff(self, principal: Principal, *, reason: str = "") -> dict:
        """Move the writer role. The writer hands it to the peer; the peer can
        take it over only when the writer is unavailable or gone. Evidence
        stays keyed to snapshots, so nothing already reviewed is lost."""
        self.co._require_participant(principal)
        run_id = principal.run_id or ""
        check_text(reason, "reason", limit=MAX_TEXT, allow_empty=True)
        writer = self.co._writer_id(run_id)
        peer = self.co._peer_of(run_id, principal.id)
        if writer == principal.id:
            if peer is None or peer["liveness"] not in (Liveness.CONNECTED.value, Liveness.IDLE.value):
                raise PolicyDenied("your peer is not available to take the writer role")
            outgoing, incoming = principal.id, peer["participant_id"]
        elif writer is None or self.co._participant(writer)["liveness"] in (Liveness.UNAVAILABLE.value, Liveness.GONE.value):
            outgoing, incoming = writer, principal.id
        else:
            raise PolicyDenied("only the writer can hand the role over; you can take it over only if the writer is unavailable")
        settings = self.co.settings(run_id)
        if outgoing is not None:
            self._capture_outgoing(run_id, outgoing)
            for task in self.rt.tasks(CONTROLLER, run_id):
                if task["owner"] == outgoing and task["kind"] in WRITE_KINDS and task["state"] in ACTIVE:
                    self.rt.transition_task(CONTROLLER, task["task_id"], TaskState.READY, expected_version=task["state_version"], reason="writer role handed over")
            self.co._release_writer(run_id)
            self.rt.update_participant(CONTROLLER, outgoing, workspace=None)
        self.rt.update_participant(CONTROLLER, incoming, workspace=settings.workspace_path)
        self.co.workspaces.acquire_writer(self.co.workspace(run_id), incoming)
        self.co._set_writer_provider(run_id, self.co._participant(incoming)["provider"])
        note = f"Writer role moved to {self.co._participant(incoming)['provider']}" + (f": {reason}" if reason else "") + f". Workspace: {settings.workspace_path}"
        for part in self.rt.participants(CONTROLLER, run_id):
            self.co._status(run_id, part["participant_id"], note)
        self.co.notify()
        self.nudge(run_id)
        return {"writer": incoming, "workspace": settings.workspace_path}

    def _capture_outgoing(self, run_id: str, outgoing: str) -> None:
        """Record the workspace as the outgoing writer leaves it, authored by
        them. Snapshot ids are per tree and a tree's first recorder is its
        author, so without this the next writer submitting the same files
        would become their author: it would get the code contribution, and
        the real author's approval would pass as a non-author review."""
        from ..verification.evidence import snapshot_id_for
        from ..workspaces.snapshots import capture_snapshot

        ws = self.co.workspace(run_id)
        snap = capture_snapshot(ws.path, base_sha=ws.base_sha, store=self.co.artifacts)
        if not snap.changed or self.rt.store.read().get("snapshots", snapshot_id_for(run_id, snap.tree_hash)) is not None:
            return  # nothing authored, or this tree is already recorded (with its author)
        self.credit_code(run_id, outgoing, self.co.evidence.record_snapshot(CONTROLLER, run_id, snap, author=outgoing))

    # ------------------------------------------------------------------ contributions

    def credit_code(self, run_id: str, participant_id: str, snapshot: dict) -> None:
        """A `code` contribution for a recorded snapshot, only for its stored
        author: the participant whose workspace first produced that tree
        (every change of writer records the outgoing writer's tree first).
        Submitting files someone else produced earns nothing; a tree its
        author recorded first differs from every earlier capture, so the
        credit is always for changes of their own."""
        changed = json.loads(snapshot["changed_json"])
        if snapshot["author"] != participant_id or not changed:
            return
        self.record_contribution(run_id, participant_id, "code", snapshot["snapshot_id"], f"changed {', '.join(changed[:5])}")

    def record_contribution(self, run_id: str, participant_id: str, kind: str, ref: str, summary: str) -> None:
        with self.rt.store.transaction() as tx:
            self._contribution(tx, run_id, participant_id, kind, ref, summary)

    def _contribution(self, tx: Any, run_id: str, participant_id: str, kind: str, ref: str, summary: str) -> None:
        cid = contribution_id(run_id, participant_id, kind, ref)
        if tx.get("contributions", cid) is not None:
            return
        provider = tx.require("participants", participant_id)["provider"]
        tx.emit(self.rt._event("contribution.recorded", {"contribution_id": cid, "run_id": run_id, "participant_id": participant_id, "provider": provider, "kind": kind, "ref": ref, "summary": summary[:500]}, CONTROLLER, run_id))

    def contributions(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self.rt.store.read().query("SELECT * FROM contributions WHERE run_id = ? ORDER BY created_at, rowid", (run_id,))]

    # ------------------------------------------------------------------ nudges

    def nudge(self, run_id: str) -> None:
        """Tell idle participants which ready task the scheduler assigns them
        (a controller TASK_PROPOSAL), once per participant and task version:
        a task that becomes claimable again (unblocked after a re-plan, put
        back by a handoff, reopened) has a new state version and is
        suggested again, otherwise a managed peer would never be woken for it."""
        try:
            state = self.graph_state(run_id)
            if RunLifecycle(self.rt.get_run(CONTROLLER, run_id)["lifecycle"]) in TERMINAL_RUN:
                return
            plan = scheduler.assign(state)
        except DomainError:
            return
        for pid, task_id in plan.items():
            if task_id is None:
                continue
            if any(t.owner == pid and t.state in ACTIVE for t in state.tasks.values()):
                continue
            task = state.tasks[task_id]
            row = self.rt.store.read().require("tasks", task_id)
            key = (run_id, pid, task_id, row["state_version"])
            with self._lock:
                if key in self._nudged:
                    continue
                self._nudged.add(key)
            self.co._status(
                run_id, pid,
                f"Suggested next task for you: {task_id} ({task.kind}): {row['description'][:300]}\nClaim it with duet_claim(task_id='{task_id}').",
                kind=MessageKind.TASK_PROPOSAL,
            )

    # ------------------------------------------------------------------ progress and loops

    def observe(self, run_id: str, kind: str) -> dict:
        """Record a progress sample and act on the loop verdict."""
        try:
            run = self.rt.get_run(CONTROLLER, run_id)
        except DomainError:
            return {"status": "ok"}
        if RunLifecycle(run["lifecycle"]) in TERMINAL_RUN or run["lifecycle"].startswith("PAUSED_"):
            return {"status": "ok"}
        fingerprint = self.fingerprint(run_id)
        with self._lock:
            self._counter += 1
            samples = self._samples.setdefault(run_id, deque(maxlen=SAMPLE_WINDOW))
            samples.append(ProgressSample(index=self._counter, kind=kind, fingerprint=fingerprint))
            sample_list = list(samples)
        interventions = self._interventions(run_id)
        replans = [i for i in interventions if i["status"] == "replan"]
        since = replans[-1]["created_at"] if replans else ""
        policy = self.rt.policy_for(run_id)
        verdict = scheduler.detect_loop(
            sample_list, self.failures(run_id, since=since), replans_done=len(replans),
            max_repairs=policy.max_repair_attempts, max_idle_messages=STALL_MESSAGES,
        )
        if verdict.status == "ok":
            return {"status": "ok"}
        if verdict.status == "stalled":
            stalls = [i for i in interventions if i["status"] == "stalled" and i["fingerprint"] == fingerprint]
            if stalls:
                # Already told once; if the stall has continued another full
                # window since then, stop instead of ping-ponging forever.
                recent = [s for s in sample_list if s.kind == "message"]
                if len(recent) >= 2 * STALL_MESSAGES and all(s.fingerprint == fingerprint for s in recent[-2 * STALL_MESSAGES:]):
                    return self._pause(run_id, "the peers kept messaging without changing any task or evidence state, after being told to decide", fingerprint)
                return {"status": "stalled"}
            self._intervene(run_id, "stalled", verdict.reason, verdict.task_id, verdict.evidence, fingerprint)
            for part in self.rt.participants(CONTROLLER, run_id):
                self.co._status(run_id, part["participant_id"], f"No progress: {verdict.reason} Decide, propose a plan (duet_propose_plan), or stop.", kind=MessageKind.BLOCKER)
            self.co.notify()
            return {"status": "stalled"}
        if verdict.status == "replan":
            if replans and replans[-1]["task_id"] == verdict.task_id and not self._plan_accepted_since(run_id, replans[-1]["created_at"]):
                return {"status": "replan"}  # already waiting for the new plan
            count = len(self.graph_state(run_id).tasks)
            if count >= MAX_TASKS_PER_RUN:
                # Only an accepted plan lifts a re-plan block, and a full graph
                # accepts no plan (validate_plan): the block would be permanent.
                reason = (
                    f"task {verdict.task_id} keeps failing and needs a re-plan, but the run already holds {count} tasks "
                    f"(the limit is {MAX_TASKS_PER_RUN}), so no new plan can be accepted"
                )
                return self._pause(run_id, reason, fingerprint, task_id=verdict.task_id, evidence=verdict.evidence)
            self._intervene(run_id, "replan", verdict.reason, verdict.task_id, verdict.evidence, fingerprint)
            if verdict.task_id:
                self._block(run_id, verdict.task_id, f"re-plan required: {verdict.reason}")
            for part in self.rt.participants(CONTROLLER, run_id):
                self.co._status(
                    run_id, part["participant_id"],
                    f"Re-plan needed: {verdict.reason} Stop repeating the same approach. Propose a different one with duet_propose_plan; "
                    f"the task stays blocked until your peer accepts a new plan.",
                    kind=MessageKind.BLOCKER,
                )
            self.co.notify()
            return {"status": "replan"}
        return self._pause(run_id, verdict.reason, fingerprint, task_id=verdict.task_id, evidence=verdict.evidence)

    def _pause(self, run_id: str, reason: str, fingerprint: str, *, task_id: str | None = None, evidence: tuple = ()) -> dict:
        self._intervene(run_id, "pause", reason, task_id, evidence, fingerprint)
        try:
            self.rt.transition_run(CONTROLLER, run_id, RunLifecycle.PAUSED_APPROVAL, reason=f"no progress: {reason}"[:500])
        except DomainError:
            return {"status": "pause"}
        if self.co.peer_stopper is not None:
            self.co.peer_stopper(run_id)
        for part in self.rt.participants(CONTROLLER, run_id):
            self.co._status_terminal(run_id, part["participant_id"], f"Run paused, not failed and not complete: {reason}. The user decides how to continue.")
        self.co.notify()
        return {"status": "pause"}

    def _intervene(self, run_id: str, status: str, reason: str, task_id: str | None, evidence: tuple, fingerprint: str) -> None:
        with self.rt.store.transaction() as tx:
            tx.emit(self.rt._event("intervention.recorded", {"intervention_id": new_id("itv"), "run_id": run_id, "status": status, "task_id": task_id, "reason": reason[:2000], "evidence": list(evidence)[:20], "fingerprint": fingerprint}, CONTROLLER, run_id))

    def _interventions(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self.rt.store.read().query("SELECT * FROM interventions WHERE run_id = ? ORDER BY created_at, rowid", (run_id,))]

    def _plan_accepted_since(self, run_id: str, since: str) -> bool:
        return bool(self.rt.store.read().scalar("SELECT COUNT(*) FROM plans WHERE run_id = ? AND state = 'ACCEPTED' AND updated_at > ?", (run_id, since)))

    def _block(self, run_id: str, task_id: str, reason: str) -> None:
        task = self.co._task(task_id, run_id)
        if task["state"] in (TaskState.VERIFIED.value, TaskState.CANCELLED.value, TaskState.BLOCKED.value):
            return
        try:
            self.rt.transition_task(
                CONTROLLER, task_id, TaskState.BLOCKED, expected_version=task["state_version"], reason=reason[:500], blocked_reason=reason[:2000],
                next_action="propose a different approach with duet_propose_plan; the task unblocks when the peer accepts it",
            )
        except (InvalidTransition, Conflict):
            pass

    def _unblock_after_replan(self, run_id: str, plan_id: str) -> None:
        replans = [i for i in self._interventions(run_id) if i["status"] == "replan"]
        if not replans:
            return
        for task in self.rt.tasks(CONTROLLER, run_id):
            if task["state"] == TaskState.BLOCKED.value and (task.get("blocked_reason") or "").startswith("re-plan required"):
                self.rt.transition_task(CONTROLLER, task["task_id"], TaskState.READY, expected_version=task["state_version"], reason=f"new plan {plan_id} accepted")

    def failures(self, run_id: str, *, since: str = "") -> list[FailureRecord]:
        """Failed attempts since `since` (the latest re-plan): failed required
        checks, blocking review verdicts and rejected task results."""
        tx = self.rt.store.read()
        main = self.co._main_task_id(run_id)
        records: list[tuple[str, FailureRecord]] = []
        for row in tx.query(
            "SELECT e.snapshot_id, e.check_id, e.status, e.exit_code, e.ended_at, e.output_hash, e.artifact_ref FROM evidence e "
            "JOIN snapshots s ON s.snapshot_id = e.snapshot_id "
            "WHERE e.run_id = ? AND e.status != 'passed' AND s.author IS NOT NULL AND e.ended_at > ? ORDER BY e.ended_at, e.rowid",
            (run_id, since),
        ):
            signature = f"check:{row['check_id']}:{row['status']}:{row['exit_code']}:{self._output_digest(row)}"
            records.append((row["ended_at"], FailureRecord(main, row["snapshot_id"], signature, 0)))
        for row in tx.query(
            "SELECT review_id, snapshot_id, summary, created_at FROM reviews WHERE run_id = ? AND disposition = 'changes_requested' AND created_at > ? ORDER BY created_at, rowid",
            (run_id, since),
        ):
            findings = tx.query("SELECT summary FROM findings WHERE review_id = ? AND severity = 'blocking' ORDER BY rowid", (row["review_id"],))
            text = findings[0]["summary"] if findings else row["summary"]
            records.append((row["created_at"], FailureRecord(main, row["snapshot_id"], "review:" + _normalise(text), 0)))
        for row in tx.query(
            "SELECT task_id, result_id, decision_reason, updated_at FROM task_results WHERE run_id = ? AND decision = 'rejected' AND updated_at > ? ORDER BY updated_at, rowid",
            (run_id, since),
        ):
            records.append((row["updated_at"], FailureRecord(row["task_id"], row["result_id"], "result:" + _normalise(row["decision_reason"] or ""), 0)))
        records.sort(key=lambda item: item[0])
        return [FailureRecord(r.task_id, r.snapshot_id, r.signature, i + 1) for i, (_, r) in enumerate(records)]

    def _output_digest(self, evidence: Any) -> str:
        """Which failure a check reported, from its recorded output. Each check
        runs in a fresh temporary copy of the snapshot, so the raw output (and
        its `output_hash`) names a different directory in every traceback,
        and test runners print timings: the same failure would never repeat.
        Those parts are normalised away before hashing; the stored hash is
        the fallback when the output cannot be read."""
        output = ""  # an empty output is stored without an artifact
        if evidence["artifact_ref"]:
            try:
                output = self.co.artifacts.get_bytes(evidence["artifact_ref"]).decode("utf-8", errors="replace")
            except Exception:  # a missing artifact must not break loop control
                return str(evidence["output_hash"]).removeprefix("sha256:")[:16]
        return hashlib.sha256(_normalise_output(output).encode("utf-8")).hexdigest()[:16]


_SCRATCH_TREE = re.compile(r"""[^\s"'()]*/check-[^/\s"']+/tree(?=[/\s"')]|$)""")
_DURATION = re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|secs|seconds)\b")
_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]{6,}\b")


def _normalise_output(text: str) -> str:
    return _ADDRESS.sub("0x?", _DURATION.sub("<t>", _SCRATCH_TREE.sub("<snapshot>", text)))


def _normalise(text: str) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())[:120]
