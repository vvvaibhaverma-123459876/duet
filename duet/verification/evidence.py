"""Snapshot-keyed evidence, reviews and findings.

Rules enforced here (spec 9.3/9.4):
- Only the controller records check evidence, and only for the run's
  *current* acceptance-contract hash and a snapshot whose tree matches the
  inputs the check actually ran on.
- A review applies to one snapshot and one contract version. It must come
  from a participant other than the snapshot's author; in pair mode, from the
  other provider. Two messages from one provider are never a Claude-Codex
  review.
- A blocking finding is closed by the reviewer who raised it (or the
  controller, with a recorded resolution); the author cannot wave it away."""
from __future__ import annotations

import hashlib
import json

from ..runtime.api import Runtime
from ..runtime.artifacts import ArtifactStore
from ..runtime.contracts import (
    MAX_TEXT,
    Conflict,
    Event,
    NotFound,
    Principal,
    Unauthorized,
    ValidationError,
    check_list,
    check_optional_text,
    check_text,
    new_id,
)
from ..workspaces.snapshots import Snapshot
from .acceptance import AcceptanceContract
from .runner import CheckOutcome

DISPOSITIONS = ("approve", "changes_requested", "comment", "acknowledge")
SEVERITIES = ("blocking", "non_blocking")


def snapshot_id_for(run_id: str, tree_hash: str) -> str:
    return "snap_" + hashlib.sha256(f"{run_id}\n{tree_hash}".encode()).hexdigest()[:32]


class EvidenceService:
    def __init__(self, runtime: Runtime, artifacts: ArtifactStore) -> None:
        self.runtime = runtime
        self.artifacts = artifacts

    def _event(self, type_: str, payload: dict, principal: Principal, run_id: str) -> Event:
        return self.runtime._event(type_, payload, principal, run_id)

    # -- snapshots ---------------------------------------------------------------------

    def record_snapshot(self, principal: Principal, run_id: str, snapshot: Snapshot, *, author: str | None) -> dict:
        """Record an immutable snapshot. `author` is the participant whose
        writes produced it (None for a base snapshot)."""
        if principal.kind != "controller":
            raise Unauthorized("only the controller records snapshots")
        manifest_ref = snapshot.manifest_ref or self.artifacts.put_text(json.dumps(snapshot.manifest(), sort_keys=True))
        snapshot_id = snapshot_id_for(run_id, snapshot.tree_hash)
        with self.runtime.store.transaction() as tx:
            self.runtime._live_run(tx, run_id)
            if author is not None:
                row = tx.require("participants", author)
                if row["run_id"] != run_id:
                    raise Unauthorized("snapshot author belongs to another run")
            tx.emit(
                self._event(
                    "snapshot.recorded",
                    {
                        "snapshot_id": snapshot_id, "run_id": run_id, "tree_hash": snapshot.tree_hash,
                        "base_sha": snapshot.base_sha, "author": author, "file_count": len(snapshot.files),
                        "changed": list(snapshot.changed), "excluded": list(snapshot.excluded), "manifest_ref": manifest_ref,
                    },
                    principal,
                    run_id,
                )
            )
            return dict(tx.require("snapshots", snapshot_id))

    def snapshot(self, snapshot_id: str) -> dict:
        row = self.runtime.store.read().get("snapshots", snapshot_id)
        if row is None:
            raise NotFound(f"snapshot {snapshot_id} not found")
        return row

    def latest_snapshot(self, run_id: str) -> dict | None:
        rows = self.runtime.store.read().query("SELECT * FROM snapshots WHERE run_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (run_id,))
        return dict(rows[0]) if rows else None

    # -- check evidence ------------------------------------------------------------------

    def record_check(self, principal: Principal, run_id: str, snapshot_id: str, contract: AcceptanceContract, outcome: CheckOutcome) -> dict:
        if principal.kind != "controller":
            raise Unauthorized("only the controller records check evidence")
        contract.check(outcome.check_id)  # must be a check of this contract
        artifact = self.artifacts.put_text(outcome.output) if outcome.output else None
        with self.runtime.store.transaction() as tx:
            run = self.runtime._run(tx, run_id)
            if run["acceptance_hash"] != contract.hash():
                raise Conflict("evidence must be recorded against the run's current acceptance contract")
            snapshot = tx.require("snapshots", snapshot_id)
            if snapshot["run_id"] != run_id:
                raise Unauthorized("snapshot belongs to another run")
            if outcome.snapshot_before != snapshot["tree_hash"]:
                raise Conflict("the check ran on different inputs than this snapshot; evidence would be misattributed")
            evidence_id = new_id("evd")
            tx.emit(
                self._event(
                    "evidence.recorded",
                    {
                        "evidence_id": evidence_id, "run_id": run_id, "check_id": outcome.check_id,
                        "snapshot_id": snapshot_id, "acceptance_hash": run["acceptance_hash"], "argv": list(outcome.argv),
                        "cwd": outcome.cwd, "env_fingerprint": outcome.env_fingerprint, "status": outcome.status,
                        "exit_code": outcome.exit_code, "started_at": outcome.started_at, "ended_at": outcome.ended_at,
                        "output_hash": outcome.output_hash, "artifact_ref": artifact, "producer": "controller",
                        "trust": "controller_executed", "detail": outcome.detail,
                    },
                    principal,
                    run_id,
                )
            )
            return dict(tx.require("evidence", evidence_id))

    def evidence_for(self, run_id: str, snapshot_id: str, acceptance_hash: str) -> dict[str, dict]:
        """Latest controller-executed evidence per check for exactly this
        snapshot and contract version."""
        rows = self.runtime.store.read().query(
            "SELECT * FROM evidence WHERE run_id = ? AND snapshot_id = ? AND acceptance_hash = ? AND trust = 'controller_executed' "
            "ORDER BY ended_at, rowid",
            (run_id, snapshot_id, acceptance_hash),
        )
        latest: dict[str, dict] = {}
        for row in rows:
            latest[row["check_id"]] = dict(row)
        return latest

    # -- reviews -------------------------------------------------------------------------

    def submit_review(
        self,
        principal: Principal,
        snapshot_id: str,
        *,
        disposition: str,
        summary: str,
        scope: list[str] | None = None,
        findings: list[dict] | None = None,
    ) -> dict:
        if not principal.is_participant:
            raise Unauthorized("reviews come from participants")
        if disposition not in DISPOSITIONS:
            raise ValidationError(f"disposition must be one of {DISPOSITIONS}")
        check_text(summary, "summary", limit=MAX_TEXT)
        scope = check_list(scope or ["all"], "scope")
        clean = []
        for item in findings or []:
            if not isinstance(item, dict):
                raise ValidationError("finding must be an object")
            severity = item.get("severity", "blocking")
            if severity not in SEVERITIES:
                raise ValidationError(f"finding severity must be one of {SEVERITIES}")
            clean.append(
                {
                    "finding_id": new_id("fnd"),
                    "severity": severity,
                    "summary": check_text(item.get("summary"), "finding summary", limit=MAX_TEXT),
                    "location": check_optional_text(item.get("location"), "finding location", limit=1024),
                }
            )
        if disposition == "acknowledge" and clean:
            raise ValidationError("an acknowledgement carries no findings; use changes_requested")
        if disposition == "approve" and any(f["severity"] == "blocking" for f in clean):
            raise ValidationError("an approval cannot carry blocking findings; use changes_requested")
        with self.runtime.store.transaction() as tx:
            snapshot = tx.require("snapshots", snapshot_id)
            self.runtime._bind_run(principal, snapshot["run_id"])
            run = self.runtime._live_run(tx, snapshot["run_id"])
            author = snapshot["author"]
            explicit = scope != ["all"]
            if author == principal.id:
                # The submitter may acknowledge the snapshot (joint work, D10)
                # or review files it did not write, named explicitly; the gate
                # decides per file whether that review counts.
                if disposition != "acknowledge" and not explicit:
                    raise Unauthorized("the author of a change cannot review it; name the files you did not write in review.scope")
            elif author is not None and disposition != "acknowledge":
                author_provider = tx.require("participants", author)["provider"]
                if author_provider == principal.provider:
                    raise Unauthorized("a review must come from the other provider")
            review_id = new_id("rvw")
            tx.emit(
                self._event(
                    "review.submitted",
                    {
                        "review_id": review_id, "run_id": snapshot["run_id"], "reviewer": principal.id,
                        "reviewer_provider": principal.provider, "snapshot_id": snapshot_id,
                        "acceptance_hash": run["acceptance_hash"], "scope": scope, "disposition": disposition,
                        "summary": summary, "findings": clean,
                    },
                    principal,
                    snapshot["run_id"],
                )
            )
            return {**dict(tx.require("reviews", review_id)), "findings": [f["finding_id"] for f in clean]}

    def resolve_finding(self, principal: Principal, finding_id: str, *, resolution: str, status: str = "resolved") -> dict:
        check_text(resolution, "resolution", limit=MAX_TEXT)
        with self.runtime.store.transaction() as tx:
            finding = tx.require("findings", finding_id)
            review = tx.require("reviews", finding["review_id"])
            if principal.is_participant:
                self.runtime._bind_run(principal, finding["run_id"])
                if review["reviewer"] != principal.id:
                    raise Unauthorized("only the reviewer who raised a finding can close it")
            elif principal.kind != "controller":
                raise Unauthorized("not allowed")
            tx.emit(
                self._event(
                    "finding.resolved",
                    {"finding_id": finding_id, "status": status, "resolution": resolution, "resolved_by": f"{principal.kind}:{principal.id}"},
                    principal,
                    finding["run_id"],
                )
            )
            return dict(tx.require("findings", finding_id))

    def record_baseline(self, principal: Principal, run_id: str, contract: AcceptanceContract, base_sha: str, outcome: CheckOutcome) -> dict:
        """What a required check said on the base commit (D10)."""
        if principal.kind != "controller":
            raise Unauthorized("only the controller records baselines")
        contract.check(outcome.check_id)
        baseline_id = f"{run_id}:{outcome.check_id}:{contract.hash()}"
        with self.runtime.store.transaction() as tx:
            tx.emit(self._event(
                "baseline.recorded",
                {"baseline_id": baseline_id, "run_id": run_id, "check_id": outcome.check_id, "acceptance_hash": contract.hash(), "base_sha": base_sha,
                 "status": outcome.status, "exit_code": outcome.exit_code, "output_hash": outcome.output_hash or None, "detail": outcome.detail},
                principal, run_id,
            ))
            return dict(tx.require("baselines", baseline_id))

    def baselines_for(self, run_id: str, acceptance_hash: str) -> dict[str, dict]:
        rows = self.runtime.store.read().query("SELECT * FROM baselines WHERE run_id = ? AND acceptance_hash = ?", (run_id, acceptance_hash))
        return {row["check_id"]: dict(row) for row in rows}

    def reviews_for(self, run_id: str, snapshot_id: str, acceptance_hash: str) -> list[dict]:
        rows = self.runtime.store.read().query(
            "SELECT * FROM reviews WHERE run_id = ? AND snapshot_id = ? AND acceptance_hash = ? ORDER BY created_at, rowid",
            (run_id, snapshot_id, acceptance_hash),
        )
        return [dict(row) for row in rows]

    def open_blocking_findings(self, run_id: str) -> list[dict]:
        rows = self.runtime.store.read().query(
            "SELECT * FROM findings WHERE run_id = ? AND severity = 'blocking' AND status = 'open' ORDER BY created_at", (run_id,)
        )
        return [dict(row) for row in rows]
