"""D11: selective parallel implementation and recovery. Two agents write at
once without sharing a checkout (AT28); the writer integrates isolated work
under its fence; a conflict becomes an explicit task and writes nothing; an
integration interrupted by a crash is settled from the files, never blindly
repeated (AT29/AT30); stopping DUET leaves unrelated sessions alone (AT32)."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
from pairkit import MUL, PY, coordinator, live_host, make_repo

from duet.runtime.contracts import CONTROLLER, PolicyDenied, StaleLease
from duet.runtime.identity import ProcessIdentity

CHECK = [PY, "check_feature.py"]
UTIL = "def double(x):\n    return 2 * x\n"


class Pair:
    def __init__(self, tmp_path):
        self.repo = make_repo(tmp_path / "repo")
        self.co = coordinator(tmp_path / "state")
        started = self.co.join(None, provider="claude", objective="add mul", repo=str(self.repo), checks=[CHECK], peer="invite", host=live_host())
        self.run_id = started["run_id"]
        self.claude = self.co.runtime.authenticate(started["token"])  # the writer
        joined = self.co.join(None, provider="codex", run_id=self.run_id, invite=started["invite"]["code"], host=str(ProcessIdentity.of(os.getppid())))
        self.codex = self.co.runtime.authenticate(joined["token"])
        self.main = self.co.workspace(self.run_id)

    def isolated_task(self, description="add util.double"):
        plan = self.co.graph.propose_plan(self.codex, tasks=[{"key": "u", "description": description, "kind": "code_isolated"}])
        self.co.graph.decide_plan(self.claude, plan_id=plan["plan_id"], decision="accept")
        rows = self.co.runtime.store.read().query("SELECT task_id FROM tasks WHERE run_id = ? AND kind = 'code_isolated' ORDER BY created_at DESC", (self.run_id,))
        return rows[0]["task_id"]

    def lifecycle(self):
        return self.co.runtime.get_run(CONTROLLER, self.run_id)["lifecycle"]


@pytest.fixture()
def pair(tmp_path):
    return Pair(tmp_path)


def test_two_agents_write_at_once_in_separate_checkouts(pair):
    """AT28: codex works in its own worktree while claude edits the run's
    workspace; neither can write the other's; the writer integrates."""
    co = pair.co
    task = pair.isolated_task()
    with pytest.raises(PolicyDenied):
        co.claim(pair.claude, task_id=task)  # isolated work is the non-writer's
    own = co.claim(pair.codex, task_id=task)
    main = co.claim(pair.claude)
    assert own["workspace"] != main["workspace"] and os.path.isdir(own["workspace"])
    with open(os.path.join(own["workspace"], "util.py"), "w") as handle:
        handle.write(UTIL)
    (pair.main.path / "calc.py").write_text(MUL)  # the writer, concurrently, in the run's workspace
    with pytest.raises(StaleLease):
        co.workspaces.check_writer(pair.main, pair.codex.id, 1)  # codex holds no lease on the run's workspace
    done = co.graph.complete_task(pair.codex, task_id=task, summary="util.double added")
    assert done["snapshot_id"]
    decided = co.graph.decide_task(pair.claude, task_id=task, decision="accept", reason="looks right")
    assert decided["integration"]["integrated"] and (pair.main.path / "util.py").read_text() == UTIL
    assert (pair.main.path / "calc.py").read_text() == MUL  # the writer's own edit is untouched
    with pytest.raises(Exception):
        co.graph.complete_task(pair.codex, task_id=task, summary="again")  # finished: no stale second result
    snap = co.submit(pair.claude, request_review=False)["snapshot_id"]
    assert co.wait_for_checks(snap, 60)
    authors = {path: who["author"] for path, who in co.gate.authorship(co.runtime.store.read(), dict(co.runtime.store.read().require("snapshots", snap))).items()}
    assert authors == {"calc.py": pair.claude.id, "util.py": pair.codex.id}
    co.send(pair.codex, kind="REVIEW_RESULT", body="mul ok", snapshot_id=snap, review={"disposition": "approve", "scope": ["calc.py"]})
    assert pair.lifecycle() != "COMPLETED_VERIFIED"  # util.py still needs a non-author: claude
    co.send(pair.claude, kind="REVIEW_RESULT", body="util ok", snapshot_id=snap, review={"disposition": "approve", "scope": ["util.py"]})
    co.try_complete(pair.run_id)
    assert pair.lifecycle() == "COMPLETED_VERIFIED"
    assert co.runtime.store.verify_replay() == []


def test_a_conflicting_integration_writes_nothing_and_becomes_a_task(pair):
    co = pair.co
    task = pair.isolated_task("rewrite calc.add")
    own = co.claim(pair.codex, task_id=task)
    with open(os.path.join(own["workspace"], "calc.py"), "w") as handle:
        handle.write("def add(a, b):\n    return b + a  # codex\n")
    co.claim(pair.claude)
    (pair.main.path / "calc.py").write_text("def add(a, b):\n    return a + b  # claude\n")
    co.graph.complete_task(pair.codex, task_id=task, summary="reordered")
    decided = co.graph.decide_task(pair.claude, task_id=task, decision="accept", reason="ok")
    assert decided["integration"]["integrated"] is False and decided["integration"]["conflict_task"]
    assert (pair.main.path / "calc.py").read_text() == "def add(a, b):\n    return a + b  # claude\n"  # nothing written, no markers
    conflict = co.runtime.store.read().require("tasks", decided["integration"]["conflict_task"])
    assert conflict["kind"] == "code" and "does not apply" in conflict["description"]
    blockers = [m for m in co.runtime.messages(CONTROLLER, pair.run_id) if m["kind"] == "BLOCKER" and m["recipient"] == pair.claude.id]
    assert blockers and "Integration conflict" in blockers[-1]["body"]
    assert co.parallel.record(task)["state"] == "CONFLICT"


def _in_doubt_integration(pair, apply_before_crash: bool):
    co = pair.co
    task = pair.isolated_task()
    own = co.claim(pair.codex, task_id=task)
    with open(os.path.join(own["workspace"], "util.py"), "w") as handle:
        handle.write(UTIL)
    co.graph.complete_task(pair.codex, task_id=task, summary="util")
    record = co.parallel.record(task)
    # The integration action is planned and started, then the service "dies".
    action = co.runtime.plan_action(CONTROLLER, run_id=pair.run_id, type="integration", task_id=task, input={"task_id": task})["action"]["action_id"]
    fence = co.runtime.claim_action(CONTROLLER, action, lease_seconds=1)["lease"]["fencing_token"]
    co.runtime.record_action(CONTROLLER, action, "RUNNING", fence=fence)
    if apply_before_crash:
        assert co.parallel._apply(pair.main.path, co.parallel._patch(record)).returncode == 0
    time.sleep(1.2)
    report = co.runtime.reconcile(CONTROLLER)
    assert action in report["in_doubt"]
    return task, action, report


def test_an_integration_done_before_a_crash_is_not_repeated(pair):
    task, action, report = _in_doubt_integration(pair, apply_before_crash=True)
    assert pair.co.parallel.settle_in_doubt(report["in_doubt"]) == [action]
    assert pair.co.runtime.store.read().require("actions", action)["state"] == "SUCCEEDED"
    assert (pair.main.path / "util.py").read_text() == UTIL  # applied once, not twice
    assert pair.co.parallel.record(task)["state"] == "INTEGRATED"


def test_an_integration_lost_before_it_wrote_is_redone_from_evidence(pair):
    task, action, report = _in_doubt_integration(pair, apply_before_crash=False)
    pair.co.parallel.settle_in_doubt(report["in_doubt"])
    settled = pair.co.runtime.store.read().require("actions", action)
    assert settled["state"] == "FAILED" and "absent" in settled["reconciliation"]
    assert (pair.main.path / "util.py").read_text() == UTIL and pair.co.parallel.record(task)["state"] == "INTEGRATED"


def test_stopping_duet_leaves_unrelated_sessions_alone(tmp_path):
    """AT32: a process that merely looks like a Claude session is not
    DUET's to stop; DUET stops only the peers it started."""
    from test_runtime_service import ScriptedProvider, factory_for, start
    from duet.runtime.service import ServiceClient, ServicePaths

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude = fake_bin / "claude"
    claude.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    claude.chmod(0o755)
    unrelated = subprocess.Popen([str(claude)])
    paths = ServicePaths.for_root(tmp_path / "state")
    peers: dict = {}
    service = start(paths, peer_factory=factory_for({"codex": lambda tools, request: "noted"}, peers))
    try:
        client = ServiceClient(paths)
        joined = client.call("join", provider="claude", objective="x", repo=str(make_repo(tmp_path / "repo")), checks=[CHECK], peer="managed")
        ServiceClient.as_controller(paths).call("cancel", run_id=joined["run_id"], reason="user stop")
        peer = peers["codex"][0]
        deadline = time.monotonic() + 10
        while peer.alive and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not peer.alive  # DUET's own peer stopped
    finally:
        service.close()
    try:
        assert unrelated.poll() is None  # the look-alike "claude" was never touched
    finally:
        unrelated.kill()
        unrelated.wait()
    assert ScriptedProvider  # imported for the factory


def test_an_integration_after_submission_voids_the_tested_tree(pair):
    """AT47: work integrated after a snapshot was checked and approved makes
    that evidence stale; the integrated revision is checked and reviewed
    again before the run can complete."""
    co = pair.co
    co.claim(pair.claude)
    (pair.main.path / "calc.py").write_text(MUL)
    first = co.submit(pair.claude, request_review=False)["snapshot_id"]
    assert co.wait_for_checks(first, 60)
    task = pair.isolated_task()
    own = co.claim(pair.codex, task_id=task)
    with open(os.path.join(own["workspace"], "util.py"), "w") as handle:
        handle.write(UTIL)
    co.graph.complete_task(pair.codex, task_id=task, summary="util.double added")
    assert co.graph.decide_task(pair.claude, task_id=task, decision="accept", reason="ok")["integration"]["integrated"]
    co.send(pair.codex, kind="REVIEW_RESULT", body="mul ok", snapshot_id=first, review={"disposition": "approve", "scope": ["calc.py"]})
    co.try_complete(pair.run_id)
    assert pair.lifecycle() != "COMPLETED_VERIFIED"  # the approved tree is no longer the files
    co.claim(pair.claude)
    second = co.submit(pair.claude, request_review=False)["snapshot_id"]
    assert second != first and co.wait_for_checks(second, 60)
    co.send(pair.codex, kind="REVIEW_RESULT", body="mul ok", snapshot_id=second, review={"disposition": "approve", "scope": ["calc.py"]})
    co.try_complete(pair.run_id)
    assert pair.lifecycle() != "COMPLETED_VERIFIED"  # util.py, integrated, still needs its non-author
    co.send(pair.claude, kind="REVIEW_RESULT", body="util ok", snapshot_id=second, review={"disposition": "approve", "scope": ["util.py"]})
    co.try_complete(pair.run_id)
    assert pair.lifecycle() == "COMPLETED_VERIFIED"
