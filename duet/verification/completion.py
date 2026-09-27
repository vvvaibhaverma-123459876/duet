"""The controller-only completion predicate (spec 9.4).

    all required tasks satisfied
    AND all required checks passed on the final snapshot
    AND required non-author reviews valid for that snapshot
    AND no unresolved blocking findings
    AND both provider contributions evidenced
    AND authorised scope and policy respected
    AND no unobserved active actions affecting the result
    AND deliverables and checkpoint exported

COMPLETED_VERIFIED is reported only when every item holds, and only the
controller can move a run there. Everything else gets a precise label and
the list of missing obligations; no second LLM is needed to write it."""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path

from ..runtime.api import Runtime
from ..runtime.artifacts import ArtifactStore
from ..runtime.contracts import (
    CONTROLLER,
    ActionState,
    Collaboration,
    InvalidTransition,
    Principal,
    RunLifecycle,
    TaskState,
    Unauthorized,
    canonical_json,
    content_hash,
    new_id,
    utc_now,
)
from ..workspaces.snapshots import Snapshot, protected_changes
from .acceptance import AcceptanceContract
from .evidence import EvidenceService

UNOBSERVED_ACTION_STATES = (ActionState.DISPATCHING.value, ActionState.RUNNING.value, ActionState.IN_DOUBT.value)

COMPLETED_VERIFIED = "COMPLETED_VERIFIED"
IMPLEMENTED_REVIEW_PENDING = "IMPLEMENTED_REVIEW_PENDING"
REPORTED_UNVERIFIED = "REPORTED_UNVERIFIED"


@dataclass(frozen=True)
class PredicateItem:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class CompletionReport:
    run_id: str
    snapshot_id: str
    acceptance_hash: str
    items: tuple[PredicateItem, ...]
    outcome: str

    @property
    def satisfied(self) -> bool:
        return all(item.ok for item in self.items)

    @property
    def missing(self) -> list[PredicateItem]:
        return [item for item in self.items if not item.ok]

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "acceptance_hash": self.acceptance_hash,
            "outcome": self.outcome,
            "satisfied": self.satisfied,
            "items": [{"name": i.name, "ok": i.ok, "detail": i.detail} for i in self.items],
        }


class CompletionGate:
    def __init__(self, runtime: Runtime, evidence: EvidenceService, artifacts: ArtifactStore) -> None:
        self.runtime = runtime
        self.evidence = evidence
        self.artifacts = artifacts

    def evaluate(self, run_id: str, snapshot_id: str, *, workspace: Path | None = None, snapshot: Snapshot | None = None) -> CompletionReport:
        tx = self.runtime.store.read()
        run = self.runtime._run(tx, run_id)
        contract = AcceptanceContract.from_dict(json.loads(run["acceptance_json"]))
        acceptance_hash = run["acceptance_hash"]
        snap = tx.require("snapshots", snapshot_id)
        items = [
            self._tasks(tx, run_id),
            self._checks(run_id, snapshot_id, acceptance_hash, contract),
            self._reviews(tx, run, snap, contract),
            self._findings(run_id),
            self._contributions(tx, run, snap),
            self._scope(tx, run, snap, contract, workspace, snapshot),
            self._actions(tx, run_id),
            self._checkpoint(tx, run_id, snapshot_id, acceptance_hash),
        ]
        return CompletionReport(run_id, snapshot_id, acceptance_hash, tuple(items), _classify(items))

    # -- predicate items -------------------------------------------------------------

    @staticmethod
    def _tasks(tx, run_id: str) -> PredicateItem:
        rows = tx.query("SELECT task_id, state, description FROM tasks WHERE run_id = ? AND required = 1", (run_id,))
        open_tasks = [r for r in rows if r["state"] != TaskState.VERIFIED.value]
        if open_tasks:
            listed = ", ".join(f"{r['task_id']} ({r['state']})" for r in open_tasks[:5])
            return PredicateItem("required_tasks", False, f"{len(open_tasks)} required task(s) not verified: {listed}")
        return PredicateItem("required_tasks", True, f"{len(rows)} required task(s) verified")

    def _checks(self, run_id: str, snapshot_id: str, acceptance_hash: str, contract: AcceptanceContract) -> PredicateItem:
        required = contract.required_checks()
        if not required:
            return PredicateItem("required_checks", False, "the acceptance contract names no required checks; nothing can verify completion")
        evidence = self.evidence.evidence_for(run_id, snapshot_id, acceptance_hash)
        problems = []
        for check_id in required:
            record = evidence.get(check_id)
            if record is None:
                problems.append(f"{check_id}: not run on this snapshot")
            elif record["status"] != "passed":
                problems.append(f"{check_id}: {record['status']} ({record['detail']})")
        if problems:
            return PredicateItem("required_checks", False, "; ".join(problems))
        return PredicateItem("required_checks", True, f"{len(required)} required check(s) passed on {snapshot_id}")

    def _reviews(self, tx, run: dict, snap: dict, contract: AcceptanceContract) -> PredicateItem:
        if not contract.review_required():
            return PredicateItem("non_author_review", True, "the contract requires no review")
        reviews = self.evidence.reviews_for(run["run_id"], snap["snapshot_id"], run["acceptance_hash"])
        author = snap["author"]
        author_provider = tx.require("participants", author)["provider"] if author else None
        latest_by_reviewer: dict[str, dict] = {}
        for review in reviews:
            if review["reviewer"] == author or (author_provider and review["reviewer_provider"] == author_provider):
                continue
            if review["disposition"] in ("approve", "changes_requested"):
                latest_by_reviewer[review["reviewer"]] = review
        approvals = [r for r in latest_by_reviewer.values() if r["disposition"] == "approve"]
        blocked = [r for r in latest_by_reviewer.values() if r["disposition"] == "changes_requested"]
        if blocked:
            return PredicateItem("non_author_review", False, f"changes requested on this snapshot by {', '.join(r['reviewer_provider'] for r in blocked)}")
        if not approvals:
            stale = tx.scalar("SELECT COUNT(*) FROM reviews WHERE run_id = ? AND disposition = 'approve'", (run["run_id"],))
            hint = f" ({stale} approval(s) exist for other snapshots or contract versions and no longer apply)" if stale else ""
            return PredicateItem("non_author_review", False, f"no non-author approval for {snap['snapshot_id']}{hint}")
        return PredicateItem("non_author_review", True, f"approved by {', '.join(r['reviewer_provider'] for r in approvals)}")

    def _findings(self, run_id: str) -> PredicateItem:
        open_blocking = self.evidence.open_blocking_findings(run_id)
        if open_blocking:
            return PredicateItem("blocking_findings", False, f"{len(open_blocking)} unresolved blocking finding(s): " + "; ".join(f["summary"][:80] for f in open_blocking[:3]))
        return PredicateItem("blocking_findings", True, "no unresolved blocking findings")

    @staticmethod
    def _contributions(tx, run: dict, snap: dict) -> PredicateItem:
        participants = {row["participant_id"]: row["provider"] for row in tx.query("SELECT participant_id, provider FROM participants WHERE run_id = ?", (run["run_id"],))}
        if run["collaboration"] == Collaboration.SOLO_EXPLICIT.value:
            return PredicateItem("both_contributions", True, "explicit solo run (user decision); pair contribution not required")
        contributed: set[str] = set()
        for row in tx.query("SELECT DISTINCT author FROM snapshots WHERE run_id = ? AND author IS NOT NULL AND changed_json != '[]'", (run["run_id"],)):
            contributed.add(participants.get(row["author"], ""))
        for row in tx.query("SELECT DISTINCT reviewer_provider FROM reviews WHERE run_id = ?", (run["run_id"],)):
            contributed.add(row["reviewer_provider"])
        # Contribution records come only from evidence (accepted task results,
        # accepted plans, authored changes, reviews); messages never count (R01).
        for row in tx.query("SELECT DISTINCT provider FROM contributions WHERE run_id = ?", (run["run_id"],)):
            contributed.add(row["provider"])
        providers = set(participants.values())
        needed = {"claude", "codex"}
        missing_participants = needed - providers
        if missing_participants:
            return PredicateItem("both_contributions", False, f"no registered {', '.join(sorted(missing_participants))} participant")
        missing = needed - contributed
        if missing:
            return PredicateItem("both_contributions", False, f"no substantive contribution evidenced from {', '.join(sorted(missing))} (messages alone do not count)")
        return PredicateItem("both_contributions", True, "claude and codex both contributed")

    def _scope(self, tx, run: dict, snap: dict, contract: AcceptanceContract, workspace: Path | None, snapshot: Snapshot | None) -> PredicateItem:
        problems = []
        policy = self.runtime._policy(tx, run)
        changed = json.loads(snap["changed_json"])
        if policy.repo_roots:
            outside = [p for p in changed if not any(p == root or p.startswith(root.rstrip("/") + "/") or fnmatch.fnmatch(p, root) for root in policy.repo_roots)]
            if outside:
                problems.append(f"changes outside the authorised roots: {', '.join(outside[:5])}")
        if contract.protected_paths:
            if workspace is None or snapshot is None:
                problems.append("protected paths could not be checked (no workspace snapshot supplied)")
            elif snapshot.tree_hash != snap["tree_hash"]:
                problems.append("supplied snapshot does not match the recorded one")
            else:
                touched = protected_changes(snapshot, workspace, snap["base_sha"], contract.protected_paths)
                if touched:
                    problems.append(f"protected acceptance inputs were modified: {', '.join(touched[:5])}")
        if problems:
            return PredicateItem("scope_and_policy", False, "; ".join(problems))
        return PredicateItem("scope_and_policy", True, "changes stay within the authorised scope; protected inputs untouched")

    @staticmethod
    def _actions(tx, run_id: str) -> PredicateItem:
        states = ",".join("?" * len(UNOBSERVED_ACTION_STATES))
        rows = tx.query(f"SELECT action_id, state FROM actions WHERE run_id = ? AND state IN ({states})", (run_id, *UNOBSERVED_ACTION_STATES))
        if rows:
            return PredicateItem("no_unobserved_actions", False, "actions without an observed outcome: " + ", ".join(f"{r['action_id']} ({r['state']})" for r in rows[:5]))
        return PredicateItem("no_unobserved_actions", True, "no actions in flight or in doubt")

    @staticmethod
    def _checkpoint(tx, run_id: str, snapshot_id: str, acceptance_hash: str) -> PredicateItem:
        found = tx.scalar(
            "SELECT checkpoint_id FROM checkpoints WHERE run_id = ? AND snapshot_id = ? AND acceptance_hash = ? ORDER BY created_at DESC LIMIT 1",
            (run_id, snapshot_id, acceptance_hash),
        )
        if not found:
            return PredicateItem("checkpoint_exported", False, "no checkpoint exported for this snapshot and contract version")
        return PredicateItem("checkpoint_exported", True, f"checkpoint {found}")

    # -- export and finalisation ------------------------------------------------------

    def export_checkpoint(self, principal: Principal, report: CompletionReport) -> dict:
        """Persist the report (with evidence references) as an immutable
        artifact and record it. The export is what a later session or a human
        reviewer reads to continue or accept the work."""
        if principal.kind != "controller":
            raise Unauthorized("only the controller exports checkpoints")
        tx = self.runtime.store.read()
        evidence = self.evidence.evidence_for(report.run_id, report.snapshot_id, report.acceptance_hash)
        reviews = self.evidence.reviews_for(report.run_id, report.snapshot_id, report.acceptance_hash)
        body = {
            "schema": "duet.checkpoint/1",
            "exported_at": utc_now(),
            "report": report.to_dict(),
            "snapshot": dict(tx.require("snapshots", report.snapshot_id)),
            "evidence": {k: {"status": v["status"], "evidence_id": v["evidence_id"], "artifact_ref": v["artifact_ref"], "output_hash": v["output_hash"]} for k, v in evidence.items()},
            "reviews": [{"review_id": r["review_id"], "reviewer_provider": r["reviewer_provider"], "disposition": r["disposition"]} for r in reviews],
            "open_findings": self.evidence.open_blocking_findings(report.run_id),
        }
        text = canonical_json(body)
        ref = self.artifacts.put_text(text)
        with self.runtime.store.transaction() as wtx:
            checkpoint_id = new_id("chk")
            wtx.emit(
                self.runtime._event(
                    "checkpoint.exported",
                    {
                        "checkpoint_id": checkpoint_id, "run_id": report.run_id, "snapshot_id": report.snapshot_id,
                        "acceptance_hash": report.acceptance_hash, "report_hash": content_hash(body), "artifact_ref": ref,
                    },
                    principal,
                    report.run_id,
                )
            )
            return dict(wtx.require("checkpoints", checkpoint_id))

    def finalize(self, run_id: str, snapshot_id: str, *, workspace: Path | None = None, snapshot: Snapshot | None = None, principal: Principal = CONTROLLER) -> CompletionReport:
        """Evaluate; if everything but the checkpoint holds, export it,
        re-evaluate, and only then move the run to COMPLETED_VERIFIED."""
        if principal.kind != "controller":
            raise Unauthorized("only the controller finalises runs")
        report = self.evaluate(run_id, snapshot_id, workspace=workspace, snapshot=snapshot)
        pending = [item for item in report.items if not item.ok and item.name != "checkpoint_exported"]
        if pending:
            return report
        # Export the report as it stands once this export exists, so the
        # artifact never claims its own checkpoint is missing; then re-evaluate
        # from the store to confirm rather than trusting the in-memory copy.
        items = tuple(
            PredicateItem(item.name, True, "exported with this report") if item.name == "checkpoint_exported" else item
            for item in report.items
        )
        self.export_checkpoint(principal, CompletionReport(run_id, snapshot_id, report.acceptance_hash, items, _classify(list(items))))
        report = self.evaluate(run_id, snapshot_id, workspace=workspace, snapshot=snapshot)
        if not report.satisfied:
            return report
        run = self.runtime.get_run(principal, run_id)
        lifecycle = RunLifecycle(run["lifecycle"])
        if lifecycle in (RunLifecycle.EXECUTING, RunLifecycle.REVIEWING, RunLifecycle.REPAIRING):
            self.runtime.transition_run(principal, run_id, RunLifecycle.VERIFYING, reason="completion predicate evaluation")
        elif lifecycle != RunLifecycle.VERIFYING:
            raise InvalidTransition(f"run is {lifecycle.value}; completion is established from EXECUTING/REVIEWING/REPAIRING/VERIFYING")
        self.runtime.transition_run(principal, run_id, RunLifecycle.COMPLETED_VERIFIED, reason=f"predicate satisfied for {snapshot_id}")
        return report


def _classify(items: list[PredicateItem]) -> str:
    status = {item.name: item.ok for item in items}
    if all(status.values()):
        return COMPLETED_VERIFIED
    if status["required_checks"] and status["required_tasks"] and status["scope_and_policy"] and not (
        status["non_author_review"] and status["blocking_findings"] and status["both_contributions"]
    ):
        return IMPLEMENTED_REVIEW_PENDING
    return REPORTED_UNVERIFIED
