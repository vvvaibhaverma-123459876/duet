"""Regression tests for review findings in the pairing coordinator: the
deliverable is exactly the verified snapshot, a stale submission can be
resubmitted, a revert makes the older snapshot current again, and check
leases outlive the check's timeout."""
from __future__ import annotations

import os
import time

import pytest
from pairkit import MUL, PY, coordinator, git, live_host, make_repo

from duet.runtime.contracts import CONTROLLER
from duet.runtime.identity import ProcessIdentity

CHECK = [PY, "check_feature.py"]


class Pair:
    def __init__(self, tmp_path):
        self.repo = make_repo(tmp_path / "repo")
        self.co = coordinator(tmp_path / "state")
        started = self.co.join(None, provider="claude", objective="add mul", repo=str(self.repo), checks=[CHECK], peer="invite", host=live_host())
        self.run_id = started["run_id"]
        self.claude = self.co.runtime.authenticate(started["token"])
        joined = self.co.join(None, provider="codex", run_id=self.run_id, invite=started["invite"]["code"], host=str(ProcessIdentity.of(os.getppid())))
        self.codex = self.co.runtime.authenticate(joined["token"])
        self.ws = self.co.workspace(self.run_id)

    def lifecycle(self):
        return self.co.runtime.get_run(CONTROLLER, self.run_id)["lifecycle"]

    def submit(self, content):
        self.co.claim(self.claude)
        (self.ws.path / "calc.py").write_text(content)
        snap = self.co.submit(self.claude, request_review=False)["snapshot_id"]
        assert self.co.wait_for_checks(snap, 60)
        return snap

    def approve(self, snap):
        self.co.send(self.codex, kind="REVIEW_RESULT", body="ok", snapshot_id=snap, review={"disposition": "approve"})

    def wait_done(self, timeout=30):
        deadline = time.monotonic() + timeout
        while self.lifecycle() != "COMPLETED_VERIFIED" and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.lifecycle() == "COMPLETED_VERIFIED"


@pytest.fixture()
def pair(tmp_path):
    return Pair(tmp_path)


def test_deliverable_ignores_work_the_writer_committed_but_the_snapshot_lacks(pair):
    ws = pair.ws.path
    (ws / "debug_dump.py").write_text("print('wip')\n")
    git("add", "debug_dump.py", cwd=ws)
    git("-c", "user.name=w", "-c", "user.email=w@example.invalid", "commit", "-qm", "wip", cwd=ws)
    (ws / "debug_dump.py").unlink()
    snap = pair.submit(MUL)
    pair.approve(snap)
    assert pair.wait_done(), pair.co.run_status(pair.run_id)["verification"]
    branch = pair.co.settings(pair.run_id).branch
    files = git("ls-tree", "-r", "--name-only", branch, cwd=pair.repo).splitlines()
    assert "debug_dump.py" not in files and git("show", f"{branch}:calc.py", cwd=pair.repo) == MUL.rstrip("\n")
    closing = [m["body"] for m in pair.co.runtime.messages(CONTROLLER, pair.run_id) if "COMPLETED_VERIFIED" in m["body"]]
    assert closing and all("is committed on branch" in body for body in closing)


def test_edits_after_verification_never_reach_the_commit(pair, monkeypatch):
    snap = pair.submit(MUL)
    original = pair.co.gate.finalize

    def racing_finalize(*args, **kwargs):
        report = original(*args, **kwargs)
        (pair.ws.path / "calc.py").write_text("def mul(a, b):\n    return a - b\n")  # lands after the tree check
        return report

    monkeypatch.setattr(pair.co.gate, "finalize", racing_finalize)
    pair.approve(snap)
    assert pair.wait_done()
    branch = pair.co.settings(pair.run_id).branch
    assert git("show", f"{branch}:calc.py", cwd=pair.repo) == MUL.rstrip("\n")


def test_a_stale_submission_can_be_resubmitted(pair):
    snap = pair.submit(MUL)
    (pair.ws.path / "calc.py").write_text(MUL + "# edited after submission\n")
    pair.approve(snap)
    main = pair.co._main_task_id(pair.run_id)
    assert pair.co.runtime.store.read().require("tasks", main)["state"] == "CHANGES_REQUESTED"
    again = pair.submit(MUL + "# edited after submission\n")  # "Submit again" is possible
    assert again != snap
    assert pair.lifecycle() != "COMPLETED_VERIFIED"  # the new snapshot needs its own review
    pair.approve(again)
    assert pair.wait_done()


def test_resubmitting_the_approved_tree_reuses_its_evidence(pair):
    """Evidence and approvals are keyed to the exact tree (R10): after a stale
    edit is undone, resubmitting the identical files is the same snapshot,
    whose passing checks and approval still apply."""
    snap = pair.submit(MUL)
    (pair.ws.path / "calc.py").write_text(MUL + "# stray edit\n")
    pair.approve(snap)
    assert pair.lifecycle() != "COMPLETED_VERIFIED"
    assert pair.submit(MUL) == snap
    assert pair.wait_done()


def test_reverting_to_an_earlier_tree_makes_it_current_again(pair):
    s1 = pair.submit(MUL)
    pair.co.send(pair.codex, kind="REVIEW_RESULT", body="try a docstring", snapshot_id=s1, review={"disposition": "changes_requested"})
    s2 = pair.submit(MUL + "# docstring attempt\n")
    assert pair.co._latest_snapshot(pair.run_id)["snapshot_id"] == s2
    pair.co.send(pair.codex, kind="REVIEW_RESULT", body="the first version was better; revert", snapshot_id=s2, review={"disposition": "changes_requested"})
    s3 = pair.submit(MUL)  # revert
    assert s3 == s1 and pair.co._latest_snapshot(pair.run_id)["snapshot_id"] == s1
    pair.approve(s1)
    assert pair.wait_done(), pair.co.run_status(pair.run_id)["verification"]


def test_check_leases_outlive_the_check_timeout(pair, monkeypatch):
    seen = []
    original = pair.co.runtime.claim_action

    def spy(principal, action_id, *, lease_seconds=900):
        seen.append(lease_seconds)
        return original(principal, action_id, lease_seconds=lease_seconds)

    monkeypatch.setattr(pair.co.runtime, "claim_action", spy)
    pair.submit(MUL)
    assert seen and all(lease >= 600 + 300 for lease in seen)  # default check timeout 600 s


def test_main_task_is_not_decided_by_task_acceptance(pair):
    from duet.runtime.contracts import ValidationError

    pair.submit(MUL)
    with pytest.raises(ValidationError, match="main task"):
        pair.co.graph.decide_task(pair.codex, task_id=pair.co._main_task_id(pair.run_id), decision="accept")
