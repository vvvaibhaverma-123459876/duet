"""D06 in the runtime: shared plans, bounded task ownership, contributions
from evidence, role reassignment, and loop control (re-plan, then pause)."""
from __future__ import annotations

import os
import threading
import time

import pytest
from pairkit import BROKEN_MUL, MUL, PY, coordinator, git, live_host, make_repo

from duet.runtime.contracts import CONTROLLER, Conflict, InvalidTransition, PolicyDenied, Unauthorized, ValidationError
from duet.runtime.identity import ProcessIdentity
from duet.runtime.taskplan import MAX_PLAN_TASKS

CHECK = [PY, "check_feature.py"]


class Pair:
    def __init__(self, tmp_path, protected=None):
        self.repo = make_repo(tmp_path / "repo")
        self.co = coordinator(tmp_path / "state")
        started = self.co.join(None, provider="claude", objective="add mul(a, b) to calc.py", repo=str(self.repo), checks=[CHECK], protected=protected, peer="invite", host=live_host())
        self.run_id = started["run_id"]
        self.claude = self.co.runtime.authenticate(started["token"])
        joined = self.co.join(None, provider="codex", run_id=self.run_id, invite=started["invite"]["code"], host=str(ProcessIdentity.of(os.getppid())))
        self.codex = self.co.runtime.authenticate(joined["token"])
        self.main = started["task_id"]

    def task(self, task_id):
        return self.co.runtime.store.read().require("tasks", task_id)

    def lifecycle(self):
        return self.co.runtime.get_run(CONTROLLER, self.run_id)["lifecycle"]

    def messages(self, to, kind=None):
        rows = self.co.runtime.messages(CONTROLLER, self.run_id)
        return [m for m in rows if m["recipient"] == to.id and (kind is None or m["kind"] == kind)]

    def checks_done(self, snapshot_id, timeout=60):
        assert self.co.wait_for_checks(snapshot_id, timeout)

    def write(self, content, name="calc.py"):
        (self.co.workspace(self.run_id).path / name).write_text(content)

    def submit(self, who=None, content=None, **kw):
        who = who or self.claude
        self.co.claim(who)
        if content is not None:
            self.write(content)
        snap = self.co.submit(who, request_review=False, **kw)["snapshot_id"]
        self.checks_done(snap)
        return snap

    def approve(self, who, snap):
        return self.co.send(who, kind="REVIEW_RESULT", body="ok", snapshot_id=snap, review={"disposition": "approve"})

    def contributions(self):
        return {(c["provider"], c["kind"]) for c in self.co.graph.contributions(self.run_id)}

    def after(self, seq, to):
        """Messages to `to` after `seq` that make a managed peer take a turn
        (controller BLOCKER or TASK_PROPOSAL; see peers._actionable)."""
        return [
            m for m in self.co.runtime.messages(CONTROLLER, self.run_id)
            if m["seq"] > seq and m["recipient"] == to.id and m["sender"] == "controller" and m["kind"] in ("BLOCKER", "TASK_PROPOSAL")
        ]

    def last_seq(self):
        return max(m["seq"] for m in self.co.runtime.messages(CONTROLLER, self.run_id))


@pytest.fixture()
def pair(tmp_path):
    return Pair(tmp_path)


def wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


PLAN = [
    {"key": "rootcause", "description": "Find every caller of add() and note whether any would pass floats", "kind": "investigate"},
    {"key": "tests", "description": "Design the test cases mul() must satisfy", "kind": "test_design", "depends_on": ["rootcause"]},
]


class TestPlans:
    def test_plan_is_shared_and_only_adds_tasks(self, pair):
        co = pair.co
        before = co.runtime.get_run(CONTROLLER, pair.run_id)
        proposed = co.graph.propose_plan(pair.claude, tasks=PLAN, rationale="split investigation from coding")
        assert proposed["state"] == "PROPOSED" and len(co.runtime.tasks(CONTROLLER, pair.run_id)) == 1  # nothing exists yet
        assert pair.messages(pair.codex, "PLAN_PROPOSAL")
        with pytest.raises(Unauthorized, match="other participant"):
            co.graph.decide_plan(pair.claude, plan_id=proposed["plan_id"], decision="accept")
        accepted = co.graph.decide_plan(pair.codex, plan_id=proposed["plan_id"], decision="accept", reason="good split")
        ids = accepted["tasks"]
        assert pair.task(ids["rootcause"])["state"] == "READY" and pair.task(ids["rootcause"])["kind"] == "investigate"
        assert pair.task(ids["tests"])["depends_on_json"] == f'["{ids["rootcause"]}"]'
        after = co.runtime.get_run(CONTROLLER, pair.run_id)
        assert (after["objective"], after["acceptance_hash"]) == (before["objective"], before["acceptance_hash"])  # R04 intact
        kinds = {(c["provider"], c["kind"]) for c in co.graph.contributions(pair.run_id)}
        assert ("claude", "plan") in kinds

    def test_bad_plans_are_rejected(self, pair):
        propose = pair.co.graph.propose_plan
        with pytest.raises(ValidationError, match="cycle"):
            propose(pair.claude, tasks=[
                {"key": "a", "description": "a", "kind": "investigate", "depends_on": ["b"]},
                {"key": "b", "description": "b", "kind": "investigate", "depends_on": ["a"]},
            ])
        with pytest.raises(ValidationError):
            propose(pair.claude, tasks=[{"key": f"t{i}", "description": "x", "kind": "review"} for i in range(MAX_PLAN_TASKS + 1)])
        with pytest.raises(ValidationError):
            propose(pair.claude, tasks=[{"key": "x", "description": "x", "kind": "investigate", "acceptance_ids": ["AC9"]}])
        with pytest.raises(PolicyDenied):  # unbounded delegation: 4 ancestors
            propose(pair.claude, tasks=[
                {"key": "l0", "description": "x", "kind": "investigate"},
                {"key": "l1", "description": "x", "kind": "investigate", "parent": "l0"},
                {"key": "l2", "description": "x", "kind": "investigate", "parent": "l1"},
                {"key": "l3", "description": "x", "kind": "investigate", "parent": "l2"},
                {"key": "l4", "description": "x", "kind": "investigate", "parent": "l3"},
            ])
        with pytest.raises(ValidationError):
            propose(pair.claude, tasks=[{"key": "x", "description": "x", "kind": "deploy"}])
        propose(pair.claude, tasks=PLAN)
        with pytest.raises(PolicyDenied, match="waiting for a decision"):
            propose(pair.codex, tasks=PLAN)


class TestSharedWork:
    def test_both_contribute_without_duplicating_work(self, pair):
        co, claude, codex = pair.co, pair.claude, pair.codex
        plan = co.graph.propose_plan(claude, tasks=[PLAN[0]], rationale="codex investigates while claude codes")
        ids = co.graph.decide_plan(codex, plan_id=plan["plan_id"], decision="accept")["tasks"]
        investigate = ids["rootcause"]
        suggestions = pair.messages(codex, "TASK_PROPOSAL")
        assert suggestions and investigate in suggestions[-1]["body"]  # the scheduler routes the non-code task to codex
        assert co.graph.next_for(codex)[0]["task_id"] == investigate
        co.claim(codex, task_id=investigate)
        with pytest.raises((Conflict, PolicyDenied, InvalidTransition)):
            co.claim(claude, task_id=investigate)  # no duplicated work
        with pytest.raises(PolicyDenied, match="one writer"):
            co.claim(codex)  # the main task is code: writer only
        done = co.graph.complete_task(codex, task_id=investigate, summary="add() has two callers, both int-only", artifact="calc.add called from a.py:3 and b.py:9 with ints")
        with pytest.raises(Unauthorized):
            co.graph.decide_task(codex, task_id=investigate, decision="accept")
        co.graph.decide_task(claude, task_id=investigate, decision="accept", reason="matches what I see")
        assert pair.task(investigate)["state"] == "VERIFIED"
        assert done["result_id"]

        co.claim(claude)
        (co.workspace(pair.run_id).path / "calc.py").write_text(MUL)
        snap = co.submit(claude, summary="mul added")["snapshot_id"]
        pair.checks_done(snap)
        request = next(m for m in pair.messages(codex, "REVIEW_REQUEST") if m["snapshot_ref"] == snap)
        co.send(codex, kind="REVIEW_RESULT", body="ok", reply_to=request["message_id"], review={"disposition": "approve"})
        assert wait_for(lambda: pair.lifecycle() == "COMPLETED_VERIFIED"), co.run_status(pair.run_id)["verification"]
        kinds = {(c["provider"], c["kind"]) for c in co.graph.contributions(pair.run_id)}
        assert {("codex", "investigate"), ("codex", "review"), ("claude", "code"), ("claude", "plan"), ("claude", "review")} <= kinds

    def test_rejected_result_goes_back_to_its_owner(self, pair):
        co, claude, codex = pair.co, pair.claude, pair.codex
        plan = co.graph.propose_plan(codex, tasks=[{"key": "tp", "description": "test plan", "kind": "test_design"}])
        task = co.graph.decide_plan(claude, plan_id=plan["plan_id"], decision="accept")["tasks"]["tp"]
        co.claim(codex, task_id=task)
        co.graph.complete_task(codex, task_id=task, summary="one case")
        co.graph.decide_task(claude, task_id=task, decision="reject", reason="needs negative numbers")
        assert pair.task(task)["state"] == "CHANGES_REQUESTED"
        assert any("negative numbers" in m["body"] for m in pair.messages(codex, "BLOCKER"))
        with pytest.raises(PolicyDenied, match="theirs to fix"):
            co.claim(claude, task_id=task)
        co.claim(codex, task_id=task)

    def test_active_claims_are_bounded(self, pair):
        co, claude, codex = pair.co, pair.claude, pair.codex
        plan = co.graph.propose_plan(claude, tasks=[{"key": f"r{i}", "description": f"review area {i}", "kind": "review"} for i in range(3)])
        ids = co.graph.decide_plan(codex, plan_id=plan["plan_id"], decision="accept")["tasks"]
        co.claim(codex, task_id=ids["r0"])
        co.claim(codex, task_id=ids["r1"])
        with pytest.raises(PolicyDenied, match="limit"):
            co.claim(codex, task_id=ids["r2"])

    def test_messages_alone_are_not_a_contribution(self, pair):
        co, claude, codex = pair.co, pair.claude, pair.codex
        for i in range(3):
            co.send(codex, kind="FINDING", body=f"observation {i}")
        co.claim(claude)
        (co.workspace(pair.run_id).path / "calc.py").write_text(MUL)
        snap = co.submit(claude, request_review=False)["snapshot_id"]
        pair.checks_done(snap)
        report = co.gate.evaluate(pair.run_id, snap)
        item = next(i for i in report.items if i.name == "both_contributions")
        assert not item.ok and "codex" in item.detail and "messages alone do not count" in item.detail


class TestRoles:
    def test_writer_hands_off_and_peer_takes_the_workspace(self, pair):
        co, claude, codex = pair.co, pair.claude, pair.codex
        co.claim(claude)
        moved = co.graph.handoff(claude, reason="codex knows this module")
        assert moved["writer"] == codex.id
        assert pair.task(pair.main)["state"] == "READY"  # the outgoing writer's work went back to the pool
        with pytest.raises(PolicyDenied, match="one writer"):
            co.claim(claude)
        claimed = co.claim(codex)
        assert claimed["workspace"] == co.settings(pair.run_id).workspace_path
        assert co.settings(pair.run_id).writer_provider == "codex"

    def test_reviewer_takes_over_only_when_the_writer_is_unavailable(self, pair):
        co, claude, codex = pair.co, pair.claude, pair.codex
        with pytest.raises(PolicyDenied, match="only the writer"):
            co.graph.handoff(codex)
        co.disconnect(claude, reason="session closed")
        assert co.graph.handoff(codex, reason="writer went away")["writer"] == codex.id


class TestLoopControl:
    def _fail_once(self, pair):
        co = pair.co
        co.claim(pair.claude)
        (co.workspace(pair.run_id).path / "calc.py").write_text(BROKEN_MUL + f"# attempt {time.monotonic_ns()}\n")
        snap = co.submit(pair.claude, request_review=False)["snapshot_id"]
        pair.checks_done(snap)
        return snap

    def test_repeated_failures_replan_then_pause(self, pair):
        co = pair.co
        self._fail_once(pair)
        self._fail_once(pair)  # same failed check twice: the hypothesis is not working
        assert pair.task(pair.main)["state"] == "BLOCKED"
        interventions = co.run_status(pair.run_id)["interventions"]
        assert [i["status"] for i in interventions] == ["replan"]
        assert any("Re-plan needed" in m["body"] for m in pair.messages(pair.claude, "BLOCKER"))
        with pytest.raises((Conflict, PolicyDenied, InvalidTransition)):
            co.claim(pair.claude)  # blocked until a new plan is accepted

        plan = co.graph.propose_plan(pair.claude, tasks=[{"key": "alt", "description": "check the operator precedence first", "kind": "investigate"}], rationale="different approach")
        co.graph.decide_plan(pair.codex, plan_id=plan["plan_id"], decision="accept")
        assert pair.task(pair.main)["state"] == "READY"

        self._fail_once(pair)
        self._fail_once(pair)  # the new approach failed the same way: pause honestly
        assert pair.lifecycle() == "PAUSED_APPROVAL"
        assert [i["status"] for i in co.run_status(pair.run_id)["interventions"]] == ["replan", "pause"]
        assert co.runtime.store.verify_replay() == []

    def test_message_ping_pong_stalls_then_pauses(self, pair):
        co = pair.co
        from duet.runtime.taskgraph import STALL_MESSAGES

        sent = 0
        while not co.run_status(pair.run_id)["interventions"] and sent <= STALL_MESSAGES + 1:
            co.send(pair.claude if sent % 2 == 0 else pair.codex, kind="FINDING", body=f"I still think the other approach {sent}")
            sent += 1
        assert STALL_MESSAGES <= sent <= STALL_MESSAGES + 1  # detected within the window, not before
        assert [i["status"] for i in co.run_status(pair.run_id)["interventions"]] == ["stalled"]
        assert any("No progress" in m["body"] for m in pair.messages(pair.codex, "BLOCKER"))
        assert pair.lifecycle() == "EXECUTING"
        for i in range(STALL_MESSAGES + 1):
            if pair.lifecycle() != "EXECUTING":
                break
            co.send(pair.claude if i % 2 == 0 else pair.codex, kind="FINDING", body=f"no really {i}")
        assert pair.lifecycle() == "PAUSED_APPROVAL"  # bounded: no infinite ping-pong

    def test_real_progress_resets_the_stall_count(self, pair):
        co = pair.co
        from duet.runtime.taskgraph import STALL_MESSAGES

        for i in range(STALL_MESSAGES - 2):
            co.send(pair.codex, kind="FINDING", body=f"note {i}")
        co.claim(pair.claude)  # a task state change is progress
        for i in range(STALL_MESSAGES - 2):
            co.send(pair.codex, kind="FINDING", body=f"more {i}")
        assert co.run_status(pair.run_id)["interventions"] == []


class TestStatus:
    def test_next_puts_peer_questions_first(self, pair):
        co = pair.co
        co.send(pair.claude, kind="QUESTION", body="which file?")
        nxt = co.status(pair.codex)["next"]
        assert nxt[0]["action"] == "answer"
        assert co.status(pair.claude)["next"][0]["action"] in ("claim", "continue")

    def test_user_checkout_untouched_by_plans(self, pair):
        head = git("rev-parse", "HEAD", cwd=pair.repo)
        plan = pair.co.graph.propose_plan(pair.claude, tasks=PLAN)
        pair.co.graph.decide_plan(pair.codex, plan_id=plan["plan_id"], decision="accept")
        assert git("rev-parse", "HEAD", cwd=pair.repo) == head and git("status", "--porcelain", cwd=pair.repo) == ""


class TestReviewFindings:
    """Regression tests for the internal review of D06/D07 (findings 1-4 and
    9-11 in docs/duet-v2/D06_REPORT.md)."""

    # -- 1: the main task is marked VERIFIED only when the run can complete

    def test_a_failed_predicate_does_not_strand_the_main_task(self, tmp_path):
        pair = Pair(tmp_path, protected=["check_feature.py"])
        co, ws = pair.co, pair.co.workspace(pair.run_id).path
        original = (ws / "check_feature.py").read_text()
        pair.write(original + "# tweaked\n", "check_feature.py")  # a protected acceptance input
        snap = pair.submit(content=MUL)
        pair.approve(pair.codex, snap)
        assert pair.task(pair.main)["state"] == "CHANGES_REQUESTED"  # never VERIFIED: the writer can repair
        assert pair.lifecycle() == "REPAIRING"
        assert any("protected acceptance inputs were modified" in m["body"] for m in pair.messages(pair.claude, "BLOCKER"))
        pair.write(original, "check_feature.py")
        fixed = pair.submit()
        assert fixed != snap and pair.lifecycle() != "COMPLETED_VERIFIED"
        pair.approve(pair.codex, fixed)
        assert pair.lifecycle() == "COMPLETED_VERIFIED"
        assert co.runtime.store.verify_replay() == []

    def test_a_verified_main_task_can_still_be_reopened(self, pair, monkeypatch):
        """Marked VERIFIED, then an action starts before the run completes
        (the transient case): a stale workspace must still reopen the task."""
        co = pair.co
        snap = pair.submit(content=MUL)
        original, started = co.gate.finalize, []

        def action_in_flight(*args, **kwargs):
            if not started:
                action = co.runtime.plan_action(CONTROLLER, run_id=pair.run_id, type="check", input={"snapshot_id": snap, "check_id": "late"})["action"]
                fence = co.runtime.claim_action(CONTROLLER, action["action_id"])["lease"]["fencing_token"]
                co.runtime.record_action(CONTROLLER, action["action_id"], "RUNNING", fence=fence)
                started.append((action["action_id"], fence))
            return original(*args, **kwargs)

        monkeypatch.setattr(co.gate, "finalize", action_in_flight)
        pair.approve(pair.codex, snap)
        assert pair.task(pair.main)["state"] == "VERIFIED" and pair.lifecycle() == "REVIEWING"
        pair.write(MUL + "# edited after verification\n")
        action_id, fence = started[0]
        co.runtime.record_action(CONTROLLER, action_id, "SUCCEEDED", fence=fence)
        co.try_complete(pair.run_id)
        assert pair.task(pair.main)["state"] == "CHANGES_REQUESTED" and pair.lifecycle() == "REPAIRING"
        again = pair.submit()
        pair.approve(pair.codex, again)
        assert pair.lifecycle() == "COMPLETED_VERIFIED"

    # -- 2: accepting the last required task completes the run

    def test_a_required_task_accepted_last_completes_the_run(self, pair):
        co = pair.co
        plan = co.graph.propose_plan(pair.claude, tasks=[{"key": "tp", "description": "design tests for mul", "kind": "test_design", "acceptance_ids": ["AC1"]}])
        tp = co.graph.decide_plan(pair.codex, plan_id=plan["plan_id"], decision="accept")["tasks"]["tp"]
        assert pair.task(tp)["required"] == 1
        co.claim(pair.codex, task_id=tp)
        co.graph.complete_task(pair.codex, task_id=tp, summary="cases: 3*4, 0*x, negatives")
        snap = pair.submit(content=MUL)
        pair.approve(pair.codex, snap)
        assert pair.lifecycle() == "REVIEWING" and pair.task(pair.main)["state"] == "REVIEW_REQUIRED"
        co.graph.decide_task(pair.claude, task_id=tp, decision="accept", reason="good")
        assert pair.lifecycle() == "COMPLETED_VERIFIED"  # no further event was needed

    # -- 3: authorship survives a handoff; authors do not approve their own code

    def test_after_a_handoff_the_outgoing_writer_stays_the_author(self, pair):
        co = pair.co
        co.claim(pair.claude)
        pair.write(MUL)  # all of the work: claude
        co.graph.handoff(pair.claude, reason="done for now")
        assert ("claude", "code") in pair.contributions()
        co.claim(pair.codex)
        submitted = co.submit(pair.codex, summary="what is there")  # codex changed nothing
        snap = submitted["snapshot_id"]
        pair.checks_done(snap)
        assert co.runtime.store.read().require("snapshots", snap)["author"] == pair.claude.id
        assert "credited to claude" in submitted["note"] and "review_request" not in submitted
        assert not [m for m in pair.messages(pair.claude, "REVIEW_REQUEST") if m["snapshot_ref"] == snap]
        with pytest.raises(Unauthorized, match="author"):
            pair.approve(pair.claude, snap)  # claude cannot approve its own code
        assert pair.lifecycle() != "COMPLETED_VERIFIED"
        pair.approve(pair.codex, snap)  # codex authored none of it: a real cross-provider review
        assert pair.lifecycle() == "COMPLETED_VERIFIED"
        assert ("codex", "code") not in pair.contributions()
        assert {("claude", "code"), ("codex", "review")} <= pair.contributions()

    def test_an_approval_from_a_co_author_does_not_count(self, pair):
        co = pair.co
        co.claim(pair.claude)
        pair.write(MUL)
        co.graph.handoff(pair.claude)
        snap = pair.submit(pair.codex, content=MUL + "# codex's part\n")
        assert co.runtime.store.read().require("snapshots", snap)["author"] == pair.codex.id
        pair.approve(pair.claude, snap)  # claude wrote mul(): not a non-author of this diff
        assert pair.lifecycle() != "COMPLETED_VERIFIED"
        assert pair.task(pair.main)["state"] == "REVIEW_REQUIRED"  # not stranded as VERIFIED
        item = next(i for i in co.gate.evaluate(pair.run_id, snap).items if i.name == "non_author_review")
        assert not item.ok and "claude approved it but wrote part of this diff" in item.detail

    # -- 4: plan decisions, proposals and claims are atomic

    def test_concurrent_accepts_create_the_plan_once(self, pair, monkeypatch):
        co = pair.co
        plan = co.graph.propose_plan(pair.claude, tasks=PLAN)
        barrier, original, out = threading.Barrier(2), co.graph._decide, []
        monkeypatch.setattr(co.graph, "_decide", lambda *a, **kw: (barrier.wait(10), original(*a, **kw))[1])

        def accept():
            try:
                out.append(co.graph.decide_plan(pair.codex, plan_id=plan["plan_id"], decision="accept")["state"])
            except InvalidTransition:
                out.append("refused")

        threads = [threading.Thread(target=accept) for _ in range(2)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        assert sorted(out) == ["ACCEPTED", "refused"]
        assert len(co.runtime.tasks(CONTROLLER, pair.run_id)) == 1 + len(PLAN)
        assert co.runtime.store.verify_replay() == []

    def test_a_plan_withdrawn_before_the_accept_commits_adds_nothing(self, pair, monkeypatch):
        co = pair.co
        plan = co.graph.propose_plan(pair.claude, tasks=PLAN)
        original = co.graph._decide

        def withdrawn_first(run_id, plan_id, state, principal, reason):
            if state == "ACCEPTED":  # the proposer's withdraw lands after the accept's own checks
                original(run_id, plan_id, "WITHDRAWN", pair.claude, "changed my mind")
            return original(run_id, plan_id, state, principal, reason)

        monkeypatch.setattr(co.graph, "_decide", withdrawn_first)
        with pytest.raises(InvalidTransition, match="WITHDRAWN"):
            co.graph.decide_plan(pair.codex, plan_id=plan["plan_id"], decision="accept")
        assert co.runtime.store.read().require("plans", plan["plan_id"])["state"] == "WITHDRAWN"
        assert len(co.runtime.tasks(CONTROLLER, pair.run_id)) == 1

    def test_a_withdraw_during_the_accept_waits_and_is_refused(self, pair, monkeypatch):
        co = pair.co
        plan = co.graph.propose_plan(pair.claude, tasks=PLAN)
        original, out = co.graph._create_plan_tasks, []

        def withdraw():
            try:
                co.graph.decide_plan(pair.claude, plan_id=plan["plan_id"], decision="withdraw")
                out.append("withdrawn")
            except InvalidTransition:
                out.append("refused")

        thread = threading.Thread(target=withdraw)

        def racing(*args, **kwargs):
            thread.start()  # the withdraw needs the write lock the accept holds
            time.sleep(0.3)
            return original(*args, **kwargs)

        monkeypatch.setattr(co.graph, "_create_plan_tasks", racing)
        assert co.graph.decide_plan(pair.codex, plan_id=plan["plan_id"], decision="accept")["state"] == "ACCEPTED"
        thread.join(30)
        assert out == ["refused"]
        assert co.runtime.store.read().require("plans", plan["plan_id"])["state"] == "ACCEPTED"
        assert len(co.runtime.tasks(CONTROLLER, pair.run_id)) == 1 + len(PLAN)

    def test_concurrent_proposals_leave_one_plan_pending(self, pair, monkeypatch):
        co = pair.co
        barrier, original, out = threading.Barrier(2), co._peer_id, []
        monkeypatch.setattr(co, "_peer_id", lambda principal: (barrier.wait(10), original(principal))[1])

        def propose(who):
            try:
                out.append(co.graph.propose_plan(who, tasks=PLAN)["state"])
            except PolicyDenied:
                out.append("refused")

        threads = [threading.Thread(target=propose, args=(who,)) for who in (pair.claude, pair.codex)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        assert sorted(out) == ["PROPOSED", "refused"]
        assert co.runtime.store.read().scalar("SELECT COUNT(*) FROM plans WHERE run_id = ? AND state = 'PROPOSED'", (pair.run_id,)) == 1

    def test_concurrent_claims_respect_the_active_bound(self, pair, monkeypatch):
        co = pair.co
        plan = co.graph.propose_plan(pair.claude, tasks=[{"key": f"r{i}", "description": f"review area {i}", "kind": "review"} for i in range(3)])
        ids = co.graph.decide_plan(pair.codex, plan_id=plan["plan_id"], decision="accept")["tasks"]
        co.claim(pair.codex, task_id=ids["r0"])  # one of two slots taken
        barrier, original, out = threading.Barrier(2), co.graph.claim, []
        monkeypatch.setattr(co.graph, "claim", lambda principal, task: (barrier.wait(10), original(principal, task))[1])

        def claim(task_id):
            try:
                out.append(co.claim(pair.codex, task_id=task_id)["task"]["state"])
            except PolicyDenied:
                out.append("refused")

        threads = [threading.Thread(target=claim, args=(ids[k],)) for k in ("r1", "r2")]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        assert sorted(out) == ["RUNNING", "refused"]
        active = [t for t in co.runtime.tasks(CONTROLLER, pair.run_id) if t["owner"] == pair.codex.id and t["state"] in ("CLAIMED", "RUNNING")]
        assert len(active) == 2

    # -- 9: a managed writer is woken after a re-plan

    def _fail(self, pair, n):
        pair.submit(content=BROKEN_MUL + f"# attempt {n}\n")

    def test_the_writer_is_woken_after_a_replan(self, pair):
        co = pair.co
        first = co.graph.propose_plan(pair.claude, tasks=[{"key": "look", "description": "look at callers", "kind": "investigate"}])
        co.graph.decide_plan(pair.codex, plan_id=first["plan_id"], decision="accept")
        assert any(pair.main in m["body"] for m in pair.messages(pair.claude, "TASK_PROPOSAL"))  # the first suggestion
        self._fail(pair, 1)
        self._fail(pair, 2)
        assert pair.task(pair.main)["state"] == "BLOCKED"
        rejected = co.graph.propose_plan(pair.claude, tasks=[{"key": "same", "description": "try the same again", "kind": "code"}])
        seq = pair.last_seq()
        co.graph.decide_plan(pair.codex, plan_id=rejected["plan_id"], decision="reject", reason="that is the same approach")
        assert [m["kind"] for m in pair.after(seq, pair.claude)] == ["BLOCKER"]  # still blocked: the proposer must act
        alt = co.graph.propose_plan(pair.claude, tasks=[{"key": "alt", "description": "rewrite mul via repeated addition", "kind": "code"}])
        seq = pair.last_seq()
        co.graph.decide_plan(pair.codex, plan_id=alt["plan_id"], decision="accept")
        assert pair.task(pair.main)["state"] == "READY"
        woken = pair.after(seq, pair.claude)
        assert woken and woken[0]["kind"] == "TASK_PROPOSAL" and alt["plan_id"] in woken[0]["body"]
        assert any(pair.main in m["body"] for m in woken)  # suggested again although it was suggested before

    # -- 10: a re-plan that cannot happen pauses instead of blocking forever

    def test_a_replan_in_a_full_graph_pauses(self, pair):
        from duet.runtime.taskplan import MAX_TASKS_PER_RUN

        co = pair.co
        for batch, n in (("a", 12), ("b", 12), ("c", MAX_TASKS_PER_RUN - 25)):
            plan = co.graph.propose_plan(pair.claude, tasks=[{"key": f"{batch}{i}", "description": f"review area {batch}{i}", "kind": "review"} for i in range(n)])
            co.graph.decide_plan(pair.codex, plan_id=plan["plan_id"], decision="accept")
        assert len(co.runtime.tasks(CONTROLLER, pair.run_id)) == MAX_TASKS_PER_RUN
        self._fail(pair, 1)
        self._fail(pair, 2)
        assert pair.lifecycle() == "PAUSED_APPROVAL"
        assert pair.task(pair.main)["state"] != "BLOCKED"
        interventions = co.run_status(pair.run_id)["interventions"]
        assert [i["status"] for i in interventions] == ["pause"] and "no new plan can be accepted" in interventions[0]["reason"]

    # -- 11: different failures have different signatures

    def test_different_failures_are_not_one_repeated_failure(self, pair):
        co = pair.co
        pair.submit(content="def add(a, b):\n    return a + b\n# mul still missing\n")  # 'mul() missing'
        self._fail(pair, 1)  # mul() exists but is wrong
        signatures = [f.signature for f in co.graph.failures(pair.run_id)]
        assert len(signatures) == 2 and signatures[0] != signatures[1]
        assert pair.task(pair.main)["state"] == "CHANGES_REQUESTED" and co.run_status(pair.run_id)["interventions"] == []
        self._fail(pair, 2)  # the same failure as the last one: now it repeats
        signatures = [f.signature for f in co.graph.failures(pair.run_id)]
        assert signatures[1] == signatures[2]
        assert pair.task(pair.main)["state"] == "BLOCKED"

    def test_the_failure_signature_ignores_temporary_paths_and_timings(self):
        from duet.runtime.taskgraph import _normalise_output

        def output(scratch, seconds, line=3):
            return f'  File "/state/checkruns/check-check1-{scratch}/tree/test_calc.py", line {line}\nAssertionError\n1 failed in {seconds}s\n'

        assert _normalise_output(output("o709o16e", "0.12")) == _normalise_output(output("fnx5_572", "1.30"))
        assert _normalise_output(output("o709o16e", "0.12")) != _normalise_output(output("o709o16e", "0.12", line=4))
