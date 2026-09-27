"""D10: final-revision review and completion. A green pre-existing suite does
not demonstrate a new criterion (AT21); approvals are stale after a change
unless a delta review names its basis (AT23); a new contract version voids
earlier evidence (AT24); jointly written work needs non-author review per
file and an acknowledgement from every writer (AT25); an identical snapshot
reuses its evidence (AT44); and the final report names revisions, checks and
every missing obligation."""
from __future__ import annotations

import os
import subprocess
import sys

from pairkit import MUL, PY, coordinator, live_host, make_repo

from duet.runtime.contracts import CONTROLLER, USER
from duet.runtime.final_report import build, render_markdown
from duet.runtime.identity import ProcessIdentity

CHECK = [PY, "check_feature.py"]
ALREADY_GREEN = [PY, "-c", "import calc; assert calc.add(1, 2) == 3"]


class Pair:
    def __init__(self, tmp_path, checks=(CHECK,)):
        self.repo = make_repo(tmp_path / "repo")
        self.co = coordinator(tmp_path / "state")
        started = self.co.join(None, provider="claude", objective="add mul", repo=str(self.repo), checks=list(checks), peer="invite", host=live_host())
        self.run_id = started["run_id"]
        self.claude = self.co.runtime.authenticate(started["token"])
        joined = self.co.join(None, provider="codex", run_id=self.run_id, invite=started["invite"]["code"], host=str(ProcessIdentity.of(os.getppid())))
        self.codex = self.co.runtime.authenticate(joined["token"])
        self.ws = self.co.workspace(self.run_id)

    def lifecycle(self):
        return self.co.runtime.get_run(CONTROLLER, self.run_id)["lifecycle"]

    def write(self, files):
        for rel, text in files.items():
            (self.ws.path / rel).write_text(text)

    def submit(self, who, files=None):
        self.co.claim(who)
        if files:
            self.write(files)
        snap = self.co.submit(who, request_review=False)["snapshot_id"]
        assert self.co.wait_for_checks(snap, 60)
        return snap

    def review(self, who, snap, disposition="approve", scope=None):
        review = {"disposition": disposition}
        if scope is not None:
            review["scope"] = scope
        self.co.send(who, kind="REVIEW_RESULT", body="reviewed", snapshot_id=snap, review=review)

    def item(self, snap, name):
        return next(i for i in self.co.gate.evaluate(self.run_id, snap).items if i.name == name)


def test_a_suite_green_before_the_change_does_not_demonstrate_the_criterion(tmp_path):
    """AT21: the only check already passed on the base commit."""
    pair = Pair(tmp_path, checks=(ALREADY_GREEN,))
    snap = pair.submit(pair.claude, {"calc.py": MUL})
    pair.review(pair.codex, snap)
    assert pair.lifecycle() != "COMPLETED_VERIFIED"
    criteria = pair.item(snap, "criteria_demonstrated")
    assert not criteria.ok and "already passed before the change" in criteria.detail
    pair.review(pair.codex, snap, scope=["all", "criterion:AC1"])  # an explicit, recorded attestation
    pair.co.try_complete(pair.run_id)
    assert pair.lifecycle() == "COMPLETED_VERIFIED"


def test_fail_to_pass_demonstrates_the_criterion(tmp_path):
    pair = Pair(tmp_path)
    snap = pair.submit(pair.claude, {"calc.py": MUL})
    assert pair.co.evidence.baselines_for(pair.run_id, pair.co.runtime.get_run(CONTROLLER, pair.run_id)["acceptance_hash"])["check1"]["status"] == "failed"
    pair.review(pair.codex, snap)
    assert pair.lifecycle() == "COMPLETED_VERIFIED"


def test_an_approval_is_stale_after_a_change_unless_a_delta_review_names_its_basis(tmp_path):
    """AT23: only what changed since the approved snapshot needs review, and
    only when the new review records that basis."""
    pair = Pair(tmp_path)
    first = pair.submit(pair.claude, {"calc.py": MUL, "notes.md": "v1\n"})
    pair.review(pair.codex, first, "changes_requested")
    second = pair.submit(pair.claude, {"notes.md": "v2\n"})
    pair.review(pair.codex, first)  # an approval of the old snapshot
    assert "do not apply" in pair.item(second, "non_author_review").detail
    pair.review(pair.codex, second, scope=["notes.md"])  # a partial review without a basis
    assert "calc.py" in pair.item(second, "non_author_review").detail
    pair.review(pair.codex, second, scope=["notes.md", f"basis:{first}"])  # the delta review
    pair.co.try_complete(pair.run_id)
    assert pair.lifecycle() == "COMPLETED_VERIFIED"


def test_a_new_contract_version_voids_earlier_evidence(tmp_path):
    """AT24: evidence and approvals belong to one contract version."""
    pair = Pair(tmp_path)
    snap = pair.submit(pair.claude, {"calc.py": MUL})
    run = pair.co.runtime.get_run(CONTROLLER, pair.run_id)
    contract = dict(run["acceptance"])
    contract["notes"] = "stricter wording from the user"
    pair.co.runtime.change_acceptance(USER, pair.run_id, contract, reason="clarified")
    pair.review(pair.codex, snap)
    assert pair.lifecycle() != "COMPLETED_VERIFIED"
    assert not pair.item(snap, "required_checks").ok  # the checks must run again under the new version


def test_joint_work_needs_per_file_review_and_both_acknowledgements(tmp_path):
    """AT25: claude writes calc.py, hands off; codex writes a second file.
    Each writer's file is reviewed by the other, and no relabelling helps."""
    pair = Pair(tmp_path)
    co = pair.co
    co.claim(pair.claude)
    pair.write({"calc.py": MUL})
    co.graph.handoff(pair.claude)
    snap = pair.submit(pair.codex, {"extra.py": "X = 1\n"})
    authorship = co.gate.authorship(co.runtime.store.read(), dict(co.runtime.store.read().require("snapshots", snap)))
    assert {path: who["author"] for path, who in authorship.items()} == {"calc.py": pair.claude.id, "extra.py": pair.codex.id}
    pair.review(pair.claude, snap, scope=["extra.py"])  # claude reviews codex's file
    detail = pair.item(snap, "non_author_review").detail
    assert "calc.py (written by claude)" in detail  # claude's own file still needs codex
    pair.review(pair.codex, snap, scope=["calc.py"])  # the submitter reviews the file it did not write
    co.try_complete(pair.run_id)
    assert pair.lifecycle() == "COMPLETED_VERIFIED"
    report = build(co, pair.run_id)
    assert report["authorship"] == {"calc.py": {"author": "claude", "earlier_versions_by": []}, "extra.py": {"author": "codex", "earlier_versions_by": []}}


def test_an_identical_snapshot_reuses_its_evidence(tmp_path):
    """AT44: resubmitting the same tree does not rerun matching checks."""
    pair = Pair(tmp_path)
    snap = pair.submit(pair.claude, {"calc.py": MUL})
    pair.review(pair.codex, snap, "changes_requested")
    again = pair.submit(pair.claude)  # nothing changed
    assert again == snap
    rows = pair.co.runtime.store.read().query("SELECT COUNT(*) AS n FROM evidence WHERE snapshot_id = ?", (snap,))
    assert rows[0]["n"] == 1
    notes = [m["body"] for m in pair.co.runtime.messages(CONTROLLER, pair.run_id) if m["kind"] == "STATUS" and "reused" in m["body"]]
    assert notes and "same tree, contract and environment" in notes[-1]


def test_the_final_report_names_revisions_checks_and_missing_obligations(tmp_path):
    pair = Pair(tmp_path)
    snap = pair.submit(pair.claude, {"calc.py": MUL})
    report = build(pair.co, pair.run_id)
    assert report["outcome"] == "IMPLEMENTED_REVIEW_PENDING" and report["snapshot"]["snapshot_id"] == snap
    assert report["checks"][0]["status"] == "passed" and report["checks"][0]["baseline"] == "failed" and report["checks"][0]["output_hash"]
    missing = {m["item"]: m for m in report["missing"]}
    assert "non_author_review" in missing and missing["non_author_review"]["next"]
    pair.review(pair.codex, snap)
    done = build(pair.co, pair.run_id)
    assert done["outcome"] == "COMPLETED_VERIFIED" and done["missing"] == [] and done["repository"]["deliverable_commit"]
    text = render_markdown(done)
    assert "**Outcome: COMPLETED_VERIFIED**" in text and snap in text and "none" in text.split("## Missing obligations")[1]


def test_report_cli_help():
    proc = subprocess.run([sys.executable, "-m", "duet", "report", "--help"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "--json" in proc.stdout and "--run" in proc.stdout
