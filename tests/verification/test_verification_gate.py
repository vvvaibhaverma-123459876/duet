"""D03: bounded checks, snapshot-keyed evidence, non-author review and the
controller-only completion predicate."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from duet.runtime.api import Runtime
from duet.runtime.artifacts import ArtifactStore
from duet.runtime.contracts import CONTROLLER, USER, Conflict, InvalidTransition, Unauthorized, ValidationError
from duet.runtime.policy import AuthorisationPolicy
from duet.runtime.store import Store
from duet.verification.acceptance import AcceptanceContract, CheckSpec, Criterion
from duet.verification.completion import (
    COMPLETED_VERIFIED,
    IMPLEMENTED_REVIEW_PENDING,
    REPORTED_UNVERIFIED,
    CompletionGate,
)
from duet.verification.evidence import EvidenceService
from duet.verification.baseline import run_baseline
from duet.verification.runner import build_env, run_check
from duet.workspaces.manager import WorkspaceManager
from duet.workspaces.repo import resolve_repo
from duet.workspaces.snapshots import capture_snapshot

PY = sys.executable


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def make_repo(path: Path) -> Path:
    path.mkdir()
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "u@example.invalid", cwd=path)
    git("config", "user.name", "User", cwd=path)
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (path / "check_existing.py").write_text("from calc import add\nassert add(1, 2) == 3\nprint('existing ok')\n")
    (path / "check_feature.py").write_text(
        "import calc\nassert hasattr(calc, 'mul'), 'mul() missing'\nassert calc.mul(3, 4) == 12\nprint('feature ok')\n"
    )
    git("add", "-A", cwd=path)
    git("commit", "-qm", "base", cwd=path)
    return path


CONTRACT = AcceptanceContract(
    criteria=(
        Criterion("AC1", "mul(a, b) returns the product", checks=("feature",)),
        Criterion("AC2", "existing behaviour keeps working", checks=("existing",), kind="preserve"),
    ),
    checks=(
        CheckSpec("feature", argv=(PY, "check_feature.py")),
        CheckSpec("existing", argv=(PY, "check_existing.py")),
    ),
    protected_paths=("check_*.py",),
)


class World:
    """One run, both participants, a strict workspace, and the gate."""

    def __init__(self, tmp_path: Path, contract: AcceptanceContract = CONTRACT) -> None:
        state = tmp_path / "state"
        self.rt = Runtime(Store(state / "rt.db"))
        self.artifacts = ArtifactStore(state / "artifacts")
        self.evidence = EvidenceService(self.rt, self.artifacts)
        self.gate = CompletionGate(self.rt, self.evidence, self.artifacts)
        self.repo = make_repo(tmp_path / "repo")
        self.contract = contract
        self.run = self.rt.create_run(
            USER, repo_id=resolve_repo(self.repo).repo_id, objective="add mul()", policy=AuthorisationPolicy(), acceptance=contract.to_dict()
        )
        self.run_id = self.run["run_id"]
        self.claude = self.rt.authenticate(self.rt.register_participant(USER, self.run_id, provider="claude", origin="managed")["token"])
        self.codex = self.rt.authenticate(self.rt.register_participant(USER, self.run_id, provider="codex", origin="managed")["token"])
        self.ws = WorkspaceManager(self.rt, root=state / "worktrees").create(self.run_id, self.repo)
        for state_name in ("PREFLIGHT", "PLANNING", "EXECUTING"):
            self.rt.transition_run(CONTROLLER, self.run_id, state_name)

    def implement(self) -> None:
        (self.ws.path / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")

    def snapshot(self, author):
        snap = capture_snapshot(self.ws.path, base_sha=self.ws.base_sha)
        row = self.evidence.record_snapshot(CONTROLLER, self.run_id, snap, author=author.id if author else None)
        return snap, row["snapshot_id"]

    def verify(self, snap, snapshot_id, contract=None):
        contract = contract or self.contract
        done = self.evidence.baselines_for(self.run_id, contract.hash())
        for check_id in contract.required_checks():
            if check_id not in done:
                baseline = run_baseline(contract.check(check_id), self.repo, self.ws.base_sha, self.repo.parent / "baselines")
                self.evidence.record_baseline(CONTROLLER, self.run_id, contract, self.ws.base_sha, baseline)
        for check_id in contract.required_checks():
            outcome = run_check(contract.check(check_id), self.ws.path, snapshot_before=snap)
            self.evidence.record_check(CONTROLLER, self.run_id, snapshot_id, contract, outcome)

    def evaluate(self, snap, snapshot_id):
        return self.gate.evaluate(self.run_id, snapshot_id, workspace=self.ws.path, snapshot=snap)


@pytest.fixture()
def world(tmp_path):
    return World(tmp_path)


def item(report, name):
    return next(i for i in report.items if i.name == name)


# --- runner -----------------------------------------------------------------------


class TestRunner:
    def test_environment_is_allowlisted(self, tmp_path):
        spec = CheckSpec("env", argv=(PY, "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'absent'), os.environ.get('KEEP'))"), env_allow=("KEEP",))
        (tmp_path / "x").write_text("x")
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        outcome = run_check(spec, tmp_path, parent_env={"PATH": "/usr/bin:/bin", "ANTHROPIC_API_KEY": "sk-live", "KEEP": "yes"})
        assert outcome.status == "passed"
        assert "absent yes" in outcome.output and "sk-live" not in outcome.output
        env, fingerprint = build_env(spec, {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-live"})
        assert "ANTHROPIC_API_KEY" not in env and fingerprint.startswith("sha256:")

    def test_mutation_during_check_invalidates(self, world):  # AT36
        spec = CheckSpec("sneaky", argv=(PY, "-c", "open('calc.py','a').write('\\n# patched by the test\\n')"))
        outcome = run_check(spec, world.ws.path)
        assert outcome.status == "invalidated"
        assert "calc.py" in outcome.detail

    def test_transient_caches_do_not_invalidate(self, world):
        spec = CheckSpec("cache", argv=(PY, "-c", "import os; os.makedirs('.pytest_cache', exist_ok=True); open('.pytest_cache/x','w').write('1')"))
        assert run_check(spec, world.ws.path).status == "passed"

    def test_no_tests_is_not_a_pass(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        spec = CheckSpec("empty", argv=(PY, "-c", "print('collected 0 items'); print('no tests ran in 0.01s')"))
        assert run_check(spec, tmp_path).status == "unknown"
        strict = CheckSpec("empty2", argv=spec.argv, no_tests="fail")
        assert run_check(strict, tmp_path).status == "failed"

    def test_timeout_fails(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        outcome = run_check(CheckSpec("slow", argv=(PY, "-c", "import time; time.sleep(30)"), timeout_seconds=1), tmp_path)
        assert outcome.status == "failed" and "timed out" in outcome.detail

    def test_missing_executable_is_unknown(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        assert run_check(CheckSpec("gone", argv=(str(tmp_path / "nope"),)), tmp_path).status == "unknown"

    def test_shell_mode_is_explicit(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        # the platform's shell: /bin/sh, or cmd.exe on Windows (both know && and echo)
        outcome = run_check(CheckSpec("sh", shell=f'"{sys.executable}" -c "pass" && echo ok'), tmp_path)
        assert outcome.status == "passed" and "ok" in outcome.output


class TestAcceptanceContract:
    def test_roundtrip_and_stable_hash(self):
        again = AcceptanceContract.from_dict(CONTRACT.to_dict())
        assert again == CONTRACT and again.hash() == CONTRACT.hash()
        assert CONTRACT.required_checks() == ("feature", "existing")

    @pytest.mark.parametrize(
        "bad",
        [
            {"criteria": [{"id": "A", "description": "x", "checks": ["missing"]}]},
            {"criteria": [], "checks": [{"id": "c", "argv": ["x"], "shell": "x"}]},
            {"criteria": [], "checks": [{"id": "c"}]},
            {"criteria": [], "checks": [{"id": "c", "argv": ["x"], "cwd": "../outside"}]},
            {"criteria": [], "checks": [{"id": "c", "argv": ["x"], "timeout_seconds": 0}]},
            {"criteria": [], "checks": [{"id": "c", "argv": ["x"], "env": {"BAD NAME": "v"}}]},
            {"criteria": [], "checks": [{"id": "c", "argv": ["x"], "rm": "-rf"}]},
            {"criteria": [], "protected_paths": ["/etc/passwd"]},
            {"schema": "duet.acceptance/99", "criteria": []},
        ],
    )
    def test_invalid_contracts_rejected(self, bad):
        with pytest.raises(ValidationError):
            AcceptanceContract.from_dict(bad)


# --- evidence rules ---------------------------------------------------------------


class TestEvidenceRules:
    def test_participants_cannot_record_controller_evidence(self, world):
        snap, sid = world.snapshot(world.codex)
        outcome = run_check(CONTRACT.check("existing"), world.ws.path, snapshot_before=snap)
        with pytest.raises(Unauthorized):
            world.evidence.record_check(world.claude, world.run_id, sid, CONTRACT, outcome)
        with pytest.raises(Unauthorized):
            world.evidence.record_snapshot(world.codex, world.run_id, snap, author=world.codex.id)

    def test_evidence_must_match_snapshot_inputs(self, world):
        snap, sid = world.snapshot(world.codex)
        world.implement()
        later = capture_snapshot(world.ws.path)
        outcome = run_check(CONTRACT.check("feature"), world.ws.path, snapshot_before=later)
        with pytest.raises(Conflict, match="different inputs"):
            world.evidence.record_check(CONTROLLER, world.run_id, sid, CONTRACT, outcome)

    def test_evidence_must_use_current_contract(self, world):
        snap, sid = world.snapshot(world.codex)
        relaxed = AcceptanceContract(criteria=(Criterion("AC2", "existing", checks=("existing",)),), checks=(CheckSpec("existing", argv=(PY, "check_existing.py")),))
        outcome = run_check(relaxed.check("existing"), world.ws.path, snapshot_before=snap)
        with pytest.raises(Conflict, match="current acceptance contract"):
            world.evidence.record_check(CONTROLLER, world.run_id, sid, relaxed, outcome)

    def test_author_and_same_provider_cannot_review(self, world):
        snap, sid = world.snapshot(world.codex)
        with pytest.raises(Unauthorized, match="author"):
            world.evidence.submit_review(world.codex, sid, disposition="approve", summary="lgtm")
        assert world.evidence.submit_review(world.claude, sid, disposition="approve", summary="reviewed calc.py")["disposition"] == "approve"

    def test_approval_cannot_hide_blocking_findings(self, world):
        snap, sid = world.snapshot(world.codex)
        with pytest.raises(ValidationError):
            world.evidence.submit_review(world.claude, sid, disposition="approve", summary="x", findings=[{"severity": "blocking", "summary": "bug"}])

    def test_only_the_raiser_closes_a_finding(self, world):
        snap, sid = world.snapshot(world.codex)
        review = world.evidence.submit_review(world.claude, sid, disposition="changes_requested", summary="x", findings=[{"summary": "mul ignores negatives", "location": "calc.py:5"}])
        finding_id = review["findings"][0]
        with pytest.raises(Unauthorized):
            world.evidence.resolve_finding(world.codex, finding_id, resolution="it is fine")
        assert world.evidence.resolve_finding(world.claude, finding_id, resolution="fixed in the next snapshot")["status"] == "resolved"

    def test_participants_cannot_change_the_contract(self, world):  # AT24 (contract side)
        with pytest.raises(Unauthorized):
            world.rt.change_acceptance(world.codex, world.run_id, {"criteria": []}, reason="the feature check is flaky")


# --- the completion predicate --------------------------------------------------------


class TestCompletion:
    def _happy(self, world):
        world.implement()
        snap, sid = world.snapshot(world.codex)
        world.verify(snap, sid)
        world.evidence.submit_review(world.claude, sid, disposition="approve", summary="mul() correct; tests cover it")
        return snap, sid

    def test_happy_path_completes_verified(self, world):
        snap, sid = self._happy(world)
        report = world.gate.finalize(world.run_id, sid, workspace=world.ws.path, snapshot=snap)
        assert report.satisfied and report.outcome == COMPLETED_VERIFIED
        assert world.rt.get_run(USER, world.run_id)["lifecycle"] == "COMPLETED_VERIFIED"
        checkpoint = world.rt.store.read().query("SELECT artifact_ref FROM checkpoints")[0]["artifact_ref"]
        exported = json.loads(world.artifacts.get_text(checkpoint))
        assert exported["report"]["outcome"] == COMPLETED_VERIFIED and exported["report"]["satisfied"]
        assert set(exported["evidence"]) == {"feature", "existing"}
        assert all(e["status"] == "passed" for e in exported["evidence"].values())
        assert world.rt.store.verify_replay() == []

    def test_green_existing_suite_without_the_feature_is_not_complete(self, world):  # AT21
        snap, sid = world.snapshot(world.codex)
        world.verify(snap, sid)
        world.evidence.submit_review(world.claude, sid, disposition="approve", summary="nothing to see")
        report = world.gate.finalize(world.run_id, sid, workspace=world.ws.path, snapshot=snap)
        assert not report.satisfied
        assert "feature: failed" in item(report, "required_checks").detail
        assert world.rt.get_run(USER, world.run_id)["lifecycle"] == "EXECUTING"

    def test_approval_of_an_old_snapshot_does_not_count(self, world):  # AT23
        snap, sid = self._happy(world)
        (world.ws.path / "calc.py").write_text((world.ws.path / "calc.py").read_text() + "\n# follow-up edit\n")
        snap2, sid2 = world.snapshot(world.codex)
        world.verify(snap2, sid2)
        report = world.evaluate(snap2, sid2)
        review = item(report, "non_author_review")
        assert not review.ok and "do not apply" in review.detail
        assert report.outcome == IMPLEMENTED_REVIEW_PENDING

    def test_weakened_protected_check_blocks_completion(self, world):  # AT24
        world.implement()
        (world.ws.path / "check_feature.py").write_text("print('feature ok')\n")  # assertions gutted
        snap, sid = world.snapshot(world.codex)
        world.verify(snap, sid)
        world.evidence.submit_review(world.claude, sid, disposition="approve", summary="lgtm")
        report = world.gate.finalize(world.run_id, sid, workspace=world.ws.path, snapshot=snap)
        scope = item(report, "scope_and_policy")
        assert not scope.ok and "check_feature.py" in scope.detail
        assert world.rt.get_run(USER, world.run_id)["lifecycle"] != "COMPLETED_VERIFIED"

    def test_unknown_or_missing_required_check_blocks(self, world):  # AT35
        world.implement()
        snap, sid = world.snapshot(world.codex)
        outcome = run_check(CONTRACT.check("existing"), world.ws.path, snapshot_before=snap)
        world.evidence.record_check(CONTROLLER, world.run_id, sid, CONTRACT, outcome)
        report = world.evaluate(snap, sid)
        assert "feature: not run on this snapshot" in item(report, "required_checks").detail

    def test_contract_without_checks_can_never_verify(self, tmp_path):
        world = World(tmp_path, AcceptanceContract(criteria=(Criterion("AC1", "vibes"),)))
        snap, sid = world.snapshot(world.codex)
        report = world.evaluate(snap, sid)
        assert not item(report, "required_checks").ok and report.outcome == REPORTED_UNVERIFIED

    def test_open_blocking_finding_blocks(self, world):
        snap, sid = self._happy(world)
        world.evidence.submit_review(world.claude, sid, disposition="changes_requested", summary="wait", findings=[{"summary": "overflow"}])
        report = world.evaluate(snap, sid)
        assert not item(report, "blocking_findings").ok
        assert not item(report, "non_author_review").ok  # latest word from claude is changes_requested

    def test_both_providers_must_contribute(self, tmp_path):
        world = World(tmp_path)
        world.implement()
        snap, sid = world.snapshot(world.codex)
        world.verify(snap, sid)
        report = world.evaluate(snap, sid)
        assert "claude" in item(report, "both_contributions").detail

    def test_explicit_solo_is_a_user_decision_and_labelled(self, world):
        world.rt.set_collaboration(USER, world.run_id, "SOLO_EXPLICIT")
        world.implement()
        snap, sid = world.snapshot(world.codex)
        world.verify(snap, sid)
        report = world.evaluate(snap, sid)
        contrib = item(report, "both_contributions")
        assert contrib.ok and "explicit solo" in contrib.detail
        # Review is still required by the contract; solo does not waive it (R14).
        assert not item(report, "non_author_review").ok

    def test_in_doubt_action_blocks_completion(self, world):
        snap, sid = self._happy(world)
        planned = world.rt.plan_action(CONTROLLER, run_id=world.run_id, type="provider_turn", input={"x": 1})
        world.rt.claim_next_action(CONTROLLER)
        report = world.evaluate(snap, sid)
        assert not item(report, "no_unobserved_actions").ok
        assert planned["action"]["action_id"] in item(report, "no_unobserved_actions").detail

    def test_only_controller_finalises(self, world):
        snap, sid = self._happy(world)
        with pytest.raises(Unauthorized):
            world.gate.finalize(world.run_id, sid, workspace=world.ws.path, snapshot=snap, principal=USER)
        with pytest.raises(Unauthorized):
            world.rt.transition_run(USER, world.run_id, "COMPLETED_VERIFIED")

    def test_finalize_refuses_paused_runs(self, world):
        snap, sid = self._happy(world)
        world.rt.transition_run(CONTROLLER, world.run_id, "PAUSED_QUOTA")
        with pytest.raises(InvalidTransition):
            world.gate.finalize(world.run_id, sid, workspace=world.ws.path, snapshot=snap)

    def test_required_tasks_must_be_verified(self, world):
        snap, sid = self._happy(world)
        world.rt.propose_task(CONTROLLER, run_id=world.run_id, description="document mul()", acceptance_ids=["AC1"])
        report = world.evaluate(snap, sid)
        assert not item(report, "required_tasks").ok
