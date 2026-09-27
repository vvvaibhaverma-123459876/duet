"""D06 in the runtime: shared plans, bounded task ownership, contributions
from evidence, role reassignment, and loop control (re-plan, then pause)."""
from __future__ import annotations

import os
import time

import pytest
from pairkit import BROKEN_MUL, MUL, PY, coordinator, git, live_host, make_repo

from duet.runtime.contracts import CONTROLLER, Conflict, InvalidTransition, PolicyDenied, Unauthorized, ValidationError
from duet.runtime.identity import ProcessIdentity
from duet.runtime.taskplan import MAX_PLAN_TASKS

CHECK = [PY, "check_feature.py"]


class Pair:
    def __init__(self, tmp_path):
        self.repo = make_repo(tmp_path / "repo")
        self.co = coordinator(tmp_path / "state")
        started = self.co.join(None, provider="claude", objective="add mul(a, b) to calc.py", repo=str(self.repo), checks=[CHECK], peer="invite", host=live_host())
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
