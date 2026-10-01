"""Deterministic final report (D10): what a run produced and what it still
owes, built only from records. No model call writes it.

It names the exact final snapshot (tree hash), the deliverable commit if
one exists, the contract version, every required check with its command,
exit code and output hash (and whether it was reused or shown fail-to-pass
against the base commit), every review with its scope and basis, file
authorship, findings, contributions, routing and admission coverage, and
each missing obligation of the completion predicate with the next action
that would satisfy it."""
from __future__ import annotations

import json
import subprocess
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from ..verification.acceptance import AcceptanceContract
from .contracts import CONTROLLER
from .hygiene import for_display

if TYPE_CHECKING:
    from .pairing import PairCoordinator

SCHEMA = "duet.final-report/1"

NEXT_ACTION = {
    "required_tasks": "finish, or have the peer accept, the required tasks listed",
    "required_checks": "fix the change and submit again so the required checks pass on the new snapshot",
    "non_author_review": "ask the participant who did not write the named files to review them (or the named earlier version)",
    "criteria_demonstrated": "add a check that fails without the change, or have a non-author review attest the criterion (scope criterion:ID)",
    "blocking_findings": "address the findings and have the reviewer who raised them resolve them",
    "both_contributions": "both providers must contribute evidenced work (a change, a review, an accepted result or plan)",
    "scope_and_policy": "undo changes to protected inputs or outside the authorised scope and submit again",
    "no_unobserved_actions": "wait for running actions to finish, or reconcile the ones in doubt",
    "checkpoint_exported": "the checkpoint is exported automatically once everything else holds",
}

LIMITATIONS = (
    "Reviews and implementation were produced by AI agents; this is not a human review.",
    "DUET controls only the turns, checks and messages it schedules; work done outside its tools is not observed.",
    "Checks ran on DUET's private copy of the snapshot on this machine; other platforms (CI) are not covered unless they ran.",
)


def build(co: "PairCoordinator", run_id: str) -> dict:
    status = co.run_status(run_id)
    run = co.runtime.get_run(CONTROLLER, run_id)
    tx = co.runtime.store.read()
    contract = AcceptanceContract.from_dict(run["acceptance"])
    settings = co.settings(run_id)
    latest = co._latest_snapshot(run_id)
    report: dict = {
        "schema": SCHEMA, "run_id": run_id, "objective": run["objective"], "lifecycle": run["lifecycle"],
        "collaboration": run["collaboration"],
        "contract": {"hash": run["acceptance_hash"], "criteria": [c.to_dict() for c in contract.criteria],
                     "protected_paths": list(contract.protected_paths)},
        "repository": {"path": settings.repo_path, "base_sha": settings.base_sha, "branch": settings.branch, "deliverable_commit": _branch_head(settings)},
        "participants": [{k: p[k] for k in ("provider", "origin", "role", "liveness")} for p in status["participants"]],
        "snapshot": None, "checks": [], "reviews": [], "authorship": {}, "findings": [], "missing": [],
        "contributions": status["contributions"], "routing": [
            {k: r[k] for k in ("provider", "role", "profile", "model", "effort", "coverage", "floor_met")} for r in status["routing"]["latest"].values()
        ],
        "admission": {"holds": status["admission"]["holds"], "finishing": status["admission"].get("finishing", [])},
        "limitations": list(LIMITATIONS),
        "policy": _policy(co, run_id),
        "resources": _resources(tx, run_id),
    }
    if report["repository"]["deliverable_commit"] == settings.base_sha:
        report["repository"]["deliverable_commit"] = None
    if latest is None:
        report["missing"].append({"item": "snapshot", "detail": "nothing was submitted", "next": "the writer claims the task and submits"})
        report["outcome"] = run["lifecycle"]
        return report
    snap = dict(latest)
    report["snapshot"] = {"snapshot_id": snap["snapshot_id"], "tree_hash": snap["tree_hash"], "base_sha": snap["base_sha"],
                          "changed": json.loads(snap["changed_json"])}
    evidence = co.evidence.evidence_for(run_id, snap["snapshot_id"], run["acceptance_hash"])
    baselines = co.evidence.baselines_for(run_id, run["acceptance_hash"])
    for check_id in contract.required_checks():
        record = evidence.get(check_id)
        base = baselines.get(check_id)
        report["checks"].append({
            "check_id": check_id, "command": list(contract.check(check_id).command), "status": record["status"] if record else "not run",
            "exit_code": record["exit_code"] if record else None, "output_hash": record["output_hash"] if record else None,
            "evidence_id": record["evidence_id"] if record else None, "env_fingerprint": record["env_fingerprint"] if record else None,
            "baseline": base["status"] if base else "not recorded",
        })
    for review in tx.query("SELECT * FROM reviews WHERE run_id = ? ORDER BY created_at, rowid", (run_id,)):
        scope = json.loads(review["scope_json"]) if review["scope_json"] else ["all"]
        report["reviews"].append({
            "reviewer_provider": review["reviewer_provider"], "snapshot_id": review["snapshot_id"], "disposition": review["disposition"],
            "scope": scope, "summary": review["summary"][:500],
            "applies": review["snapshot_id"] == snap["snapshot_id"] and review["acceptance_hash"] == run["acceptance_hash"],
        })
    providers = {p["participant_id"]: p["provider"] for p in co.runtime.participants(CONTROLLER, run_id)}
    for path, who in co.gate.authorship(tx, snap).items():
        report["authorship"][path] = {"author": providers.get(who["author"]), "earlier_versions_by": sorted(providers.get(c) for c in who["contributors"])}
    report["findings"] = [
        {k: row[k] for k in ("severity", "summary", "status", "resolution")}
        for row in tx.query("SELECT * FROM findings WHERE run_id = ? ORDER BY created_at", (run_id,))
    ]
    main = co._main_task_id(run_id)
    waiting = tx.require("tasks", main)["state"] == "REVIEW_REQUIRED"
    # A main task waiting only for review counts as implemented, as in the completion path.
    completion = co._reported_completion(run_id, snap, assume_verified=(main,) if waiting else ())
    if run["lifecycle"] == "COMPLETED_VERIFIED" or run["lifecycle"] in ("CANCELLED", "FAILED") or run["lifecycle"].startswith("PAUSED_"):
        report["outcome"] = run["lifecycle"]
    else:
        report["outcome"] = completion.outcome  # e.g. IMPLEMENTED_REVIEW_PENDING, REPORTED_UNVERIFIED
    report["missing"] = [
        {"item": item.name, "detail": item.detail, "next": NEXT_ACTION.get(item.name, "")}
        for item in completion.items if not item.ok and not (run["lifecycle"] == "COMPLETED_VERIFIED" and item.name == "checkpoint_exported")
    ]
    return report


def _resources(tx, run_id: str) -> dict:
    """What the run recorded against its pools, per provider and metric, with
    unknown quantities counted rather than guessed; and the actions it ran."""
    usage: dict[tuple[str, str], dict] = {}
    rows = tx.query(
        "SELECT r.metric, r.quantity_json, r.quality, p.provider FROM usage_records r "
        "LEFT JOIN participants p ON p.participant_id = r.participant_id WHERE r.run_id = ? ORDER BY r.rowid", (run_id,))
    for row in rows:
        entry = usage.setdefault((row["provider"] or "unknown", row["metric"]),
                                 {"provider": row["provider"] or "unknown", "metric": row["metric"], "records": 0, "known_total": "0", "unknown": 0, "qualities": []})
        entry["records"] += 1
        if row["quality"] not in entry["qualities"]:
            entry["qualities"].append(row["quality"])
        try:
            entry["known_total"] = str(Decimal(entry["known_total"]) + Decimal(str(json.loads(row["quantity_json"]))))
        except (InvalidOperation, TypeError, ValueError):
            entry["unknown"] += 1
    actions: dict[str, dict[str, int]] = {}
    for row in tx.query("SELECT type, state, COUNT(*) AS n FROM actions WHERE run_id = ? GROUP BY type, state", (run_id,)):
        actions.setdefault(row["type"], {})[row["state"]] = row["n"]
    return {"usage": list(usage.values()), "actions": actions,
            "note": "recorded by DUET for the turns and checks it ran; estimated or unknown where the provider did not report"}


def _policy(co: "PairCoordinator", run_id: str) -> dict:
    """The resolved authorisation policy of the run and its hash (no secrets live in it)."""
    row = co.runtime.store.read().get("runs", run_id)
    policy = co.runtime.policy_for(run_id)
    body = policy.to_dict() if hasattr(policy, "to_dict") else dict(policy.__dict__)
    return {"hash": row["policy_hash"] if row is not None and "policy_hash" in row.keys() else None, "resolved": body}


def _branch_head(settings) -> str | None:
    proc = subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{settings.branch}"], cwd=settings.repo_path, capture_output=True, text=True)
    return (proc.stdout.strip() or None) if proc.returncode == 0 else None


def render_markdown(report: dict) -> str:
    return for_display(_render(report))


def _render(report: dict) -> str:
    lines = [f"# DUET final report: {report['run_id']}", "", f"**Outcome: {report['outcome']}** ({report['collaboration']})", "",
             f"Objective: {report['objective']}", ""]
    repo = report["repository"]
    lines.append(f"Repository `{repo['path']}`, base `{repo['base_sha'][:12]}`, branch `{repo['branch']}`"
                 + (f", deliverable commit `{repo['deliverable_commit'][:12]}`." if repo["deliverable_commit"] else ", no deliverable commit."))
    lines.append(f"Contract version `{report['contract']['hash'][:19]}`: " + "; ".join(f"{c['id']} ({c.get('kind', 'change')}): {c['description'][:80]}" for c in report["contract"]["criteria"]))
    snap = report["snapshot"]
    if snap:
        lines += ["", f"## Final snapshot `{snap['snapshot_id']}`", f"Tree `{snap['tree_hash']}`; changed: {', '.join(snap['changed']) or '(none)'}", "", "## Checks"]
        for c in report["checks"]:
            lines.append(f"- {c['check_id']}: **{c['status']}** (exit {c['exit_code']}, output {str(c['output_hash'])[:19]}, base commit: {c['baseline']}) `{' '.join(c['command'])}`")
        lines += ["", "## Reviews"]
        for r in report["reviews"]:
            lines.append(f"- {r['reviewer_provider']} {r['disposition']} on `{r['snapshot_id']}` scope {', '.join(r['scope'])}" + ("" if r["applies"] else " (does not apply to the final snapshot)"))
        lines += ["", "## Authorship"]
        for path, who in sorted(report["authorship"].items()):
            lines.append(f"- {path}: {who['author']}" + (f" (earlier versions by {', '.join(who['earlier_versions_by'])})" if who["earlier_versions_by"] else ""))
    if report["findings"]:
        lines += ["", "## Findings"] + [f"- [{f['severity']}] {f['status']}: {f['summary'][:120]}" for f in report["findings"]]
    lines += ["", "## Missing obligations"]
    lines += [f"- **{m['item']}**: {m['detail']}. Next: {m['next']}." for m in report["missing"]] or ["- none"]
    if report["routing"]:
        lines += ["", "## Control coverage"] + [f"- {r['provider']} ({r['role']}): {r['profile']} [{r['coverage']}]" + ("" if r["floor_met"] else ", below floor by user pin") for r in report["routing"]]
    resources = report.get("resources") or {}
    lines += ["", "## Resources"]
    lines += [f"- {u['provider']} {u['metric']}: {u['known_total']} over {u['records']} record(s)" + (f", {u['unknown']} unknown" if u["unknown"] else "")
              + f" ({', '.join(u['qualities'])})" for u in resources.get("usage", [])] or ["- no usage recorded against a pool"]
    lines += [f"- actions {kind}: " + ", ".join(f"{n} {state.lower()}" for state, n in sorted(states.items())) for kind, states in sorted(resources.get("actions", {}).items())]
    lines += ["", "## Limitations"] + [f"- {text}" for text in report["limitations"]]
    return "\n".join(lines) + "\n"
