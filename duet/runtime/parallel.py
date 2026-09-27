"""Selective parallel implementation (D11).

The run keeps one workspace and one writer. Independent code work
(`code_isolated` tasks) is done by the other participant in a worktree of
its own, under a lease of its own, so two agents never write to one
checkout. Its result is a patch against the run's base commit plus a
snapshot authored by that participant (D10 then attributes its files to
them).

Integration has one owner: the writer. When the writer accepts the result,
DUET applies the patch to the run's workspace inside that call, checking
the writer's fence first (a superseded writer cannot integrate), as a
durable action. The patch is checked before anything is written: a patch
that does not apply cleanly writes nothing and becomes an explicit conflict
task with the patch as evidence, never a blind retry or a file full of
conflict markers. After integration the workspace differs from every
earlier snapshot, so the next submission is a new snapshot and earlier
evidence and approvals do not apply to it.

After a crash, an integration left in doubt is settled by inspecting the
files: the patch reverse-applies (it is there) or applies (it is not); only
the second case integrates again, because the evidence says it is safe."""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ..workspaces.manager import StrictWorkspace
from ..workspaces.repo import resolve_repo
from ..workspaces.snapshots import capture_snapshot
from .contracts import CONTROLLER, ActionState, DomainError, MessageKind, PolicyDenied, Principal, ValidationError

if TYPE_CHECKING:
    from .pairing import PairCoordinator

log = logging.getLogger("duet.parallel")


class ParallelWork:
    def __init__(self, coordinator: "PairCoordinator") -> None:
        self.co = coordinator
        self.rt = coordinator.runtime

    # -- worktrees ---------------------------------------------------------------------

    def record(self, task_id: str) -> dict | None:
        row = self.rt.store.read().get("task_workspaces", task_id)
        return dict(row) if row else None

    def workspace(self, task_id: str) -> StrictWorkspace:
        row = self.record(task_id)
        if row is None:
            raise ValidationError(f"task {task_id} has no isolated workspace")
        return StrictWorkspace(run_id=row["run_id"], repo=resolve_repo(self.co.settings(row["run_id"]).repo_path), path=Path(row["path"]),
                               branch=row["branch"], base_sha=row["base_sha"])

    def ensure(self, principal: Principal, task: dict) -> StrictWorkspace:
        """The task's own worktree (created at the first claim) and its write
        lease for the claimant."""
        run_id = task["run_id"]
        row = self.record(task["task_id"])
        if row is None:
            settings = self.co.settings(run_id)
            short = task["task_id"].removeprefix("tsk_")[:12]
            ws = self.co.workspaces.create(run_id, settings.repo_path, base=settings.base_sha, branch=f"{settings.branch}-task-{short}", subdir=f"task-{short}")
            with self.rt.store.transaction() as tx:
                tx.emit(self.rt._event("task_workspace.created", {
                    "task_id": task["task_id"], "run_id": run_id, "owner": principal.id, "path": str(ws.path), "branch": ws.branch, "base_sha": ws.base_sha,
                }, CONTROLLER, run_id))
        elif row["owner"] != principal.id:
            with self.rt.store.transaction() as tx:
                tx.emit(self.rt._event("task_workspace.state", {"task_id": task["task_id"], "state": "ACTIVE", "owner": principal.id}, CONTROLLER, run_id))
        ws = self.workspace(task["task_id"])
        self.fence(ws, principal.id)
        return ws

    def fence(self, ws: StrictWorkspace, owner: str) -> int:
        value = self.rt.store.read().scalar("SELECT fencing_token FROM leases WHERE resource = ? AND owner = ? AND released_at IS NULL", (ws.lease_resource, owner))
        if value is None:
            return int(self.co.workspaces.acquire_writer(ws, owner)["fencing_token"])
        return int(value)

    # -- results -----------------------------------------------------------------------

    def capture(self, principal: Principal, task: dict) -> dict:
        """Snapshot and patch of the isolated work, authored by its owner."""
        ws = self.workspace(task["task_id"])
        self.co.workspaces.check_writer(ws, principal.id, self.fence(ws, principal.id))
        snap = capture_snapshot(ws.path, base_sha=ws.base_sha, store=self.co.artifacts)
        row = self.co.evidence.record_snapshot(CONTROLLER, task["run_id"], snap, author=principal.id)
        changed = list(snap.changed)
        if not changed:
            raise ValidationError("the isolated workspace has no changes against the base commit")
        subprocess.run(["git", "add", "-A", "--", *changed], cwd=ws.path, capture_output=True, check=True)
        diff = subprocess.run(["git", "diff", "--cached", "--binary", ws.base_sha, "--", *changed], cwd=ws.path, capture_output=True, check=True)
        patch_ref = self.co.artifacts.put_bytes(diff.stdout)
        with self.rt.store.transaction() as tx:
            tx.emit(self.rt._event("task_workspace.state", {"task_id": task["task_id"], "state": "SUBMITTED", "patch_ref": patch_ref,
                                                            "snapshot_id": row["snapshot_id"], "detail": ", ".join(changed)[:2000]}, CONTROLLER, task["run_id"]))
        return row

    # -- integration -------------------------------------------------------------------

    def _patch(self, record: dict) -> bytes:
        return self.co.artifacts.get_bytes(record["patch_ref"])

    @staticmethod
    def _apply(ws_path: Path, patch: bytes, *extra: str) -> subprocess.CompletedProcess:
        with tempfile.NamedTemporaryFile(suffix=".patch", delete=False) as handle:
            handle.write(patch)
            name = handle.name
        try:
            return subprocess.run(["git", "apply", "--binary", *extra, name], cwd=ws_path, capture_output=True, text=True)
        finally:
            Path(name).unlink(missing_ok=True)

    def integrate(self, writer: Principal, task: dict) -> dict:
        """Apply an accepted isolated result to the run's workspace. Called
        inside the writer's own tool call (duet_decide_task), after its
        fence is checked."""
        run_id = task["run_id"]
        record = self.record(task["task_id"])
        if record is None or record["state"] != "SUBMITTED":
            raise ValidationError(f"task {task['task_id']} has no submitted isolated result to integrate")
        main = self.co.workspace(run_id)
        if self.co._writer_id(run_id) != writer.id:
            raise PolicyDenied("only the run's writer integrates isolated work into its workspace")
        self.co.workspaces.check_writer(main, writer.id, self.co._writer_fence(main, writer.id))
        planned = self.rt.plan_action(CONTROLLER, run_id=run_id, type="integration", task_id=task["task_id"],
                                      input={"task_id": task["task_id"], "patch_ref": record["patch_ref"], "workspace": str(main.path)})
        action_id = planned["action"]["action_id"]
        fence = self.rt.claim_action(CONTROLLER, action_id)["lease"]["fencing_token"]
        self.rt.record_action(CONTROLLER, action_id, ActionState.RUNNING, fence=fence)
        return self._finish(run_id, task, record, main, action_id, fence)

    def _finish(self, run_id: str, task: dict, record: dict, main: StrictWorkspace, action_id: str, fence: int) -> dict:
        patch = self._patch(record)
        check = self._apply(main.path, patch, "--check")
        if check.returncode != 0:
            self.rt.record_action(CONTROLLER, action_id, ActionState.FAILED, fence=fence, result={"kind": "conflict", "error": check.stderr[-2000:]})
            return self._conflict(run_id, task, record, check.stderr)
        applied = self._apply(main.path, patch)
        if applied.returncode != 0:  # checked a moment ago: the workspace changed underneath
            self.rt.record_action(CONTROLLER, action_id, ActionState.FAILED, fence=fence, result={"kind": "conflict", "error": applied.stderr[-2000:]})
            return self._conflict(run_id, task, record, applied.stderr)
        self.rt.record_action(CONTROLLER, action_id, ActionState.SUCCEEDED, fence=fence, result={"integrated": record["detail"]})
        self._integrated(run_id, task, record)
        return {"integrated": True, "files": record["detail"]}

    def _integrated(self, run_id: str, task: dict, record: dict) -> None:
        with self.rt.store.transaction() as tx:
            tx.emit(self.rt._event("task_workspace.state", {"task_id": task["task_id"], "state": "INTEGRATED"}, CONTROLLER, run_id))
        note = (f"Task {task['task_id']} was integrated into the run's workspace ({record['detail']}). The next submission is a new "
                "snapshot: checks run again and earlier approvals do not apply to it.")
        for part in self.rt.participants(CONTROLLER, run_id):
            self.co._status(run_id, part["participant_id"], note)
        self._release(task)

    def _conflict(self, run_id: str, task: dict, record: dict, stderr: str) -> dict:
        with self.rt.store.transaction() as tx:
            tx.emit(self.rt._event("task_workspace.state", {"task_id": task["task_id"], "state": "CONFLICT", "detail": stderr[-2000:]}, CONTROLLER, run_id))
        original = self.co._task(task["task_id"], run_id)
        conflict = self.rt.propose_task(
            CONTROLLER, run_id=run_id, kind="code", required=bool(original["required"]),
            description=f"Integrate {task['task_id']} by hand: its patch does not apply to the workspace. {stderr.strip()[:600]}",
            acceptance_ids=json.loads(original["acceptance_ids_json"] or "[]"),
        )
        writer = self.co._writer_id(run_id)
        if writer:
            self.rt.send_message(CONTROLLER, kind=MessageKind.BLOCKER, recipient=writer, run_id=run_id, task_id=conflict["task_id"],
                                 body=(f"Integration conflict: {task['task_id']}'s patch ({record['detail']}) does not apply to your workspace:\n"
                                       f"{stderr.strip()[:1500]}\nNothing was written. Task {conflict['task_id']} asks you to integrate it by hand; "
                                       f"the patch is artifact {record['patch_ref']}."),
                                 artifact_refs=[record["patch_ref"]])
        self._release(task)
        return {"integrated": False, "conflict_task": conflict["task_id"], "error": stderr.strip()[:600]}

    def _release(self, task: dict) -> None:
        try:
            ws = self.workspace(task["task_id"])
            row = self.rt.store.read().query("SELECT owner, fencing_token FROM leases WHERE resource = ? AND released_at IS NULL", (ws.lease_resource,))
            if row:
                self.co.workspaces.release_writer(ws, int(row[0]["fencing_token"]))
        except DomainError:
            log.info("isolated workspace lease for %s already released", task["task_id"])

    # -- recovery ----------------------------------------------------------------------

    def settle_in_doubt(self, action_ids: list[str]) -> list[str]:
        """An integration the service lost mid-way: inspect the workspace.
        Reverse-applies: it happened. Applies: it did not, so integrating
        now is safe. Neither: the files changed in some other way, and a
        conflict task hands it to the writer. Never a blind repeat."""
        settled = []
        tx = self.rt.store.read()
        for action_id in action_ids:
            action = tx.get("actions", action_id)
            if action is None or action["type"] != "integration" or action["state"] != "IN_DOUBT":
                continue
            task = dict(tx.require("tasks", action["task_id"]))
            record = self.record(task["task_id"])
            main = self.co.workspace(action["run_id"])
            patch = self._patch(record)
            if self._apply(main.path, patch, "--check", "--reverse").returncode == 0:
                self.rt.resolve_in_doubt(CONTROLLER, action_id, "SUCCEEDED", reconciliation="the patch is present in the workspace (reverse-applies)")
                self._integrated(action["run_id"], task, record)
            elif self._apply(main.path, patch, "--check").returncode == 0:
                self.rt.resolve_in_doubt(CONTROLLER, action_id, "FAILED", reconciliation="the patch is absent (applies cleanly): integrating again")
                planned = self.rt.plan_action(CONTROLLER, run_id=action["run_id"], type="integration", task_id=task["task_id"],
                                              input={"task_id": task["task_id"], "patch_ref": record["patch_ref"], "workspace": str(main.path), "after": action_id})
                again = planned["action"]["action_id"]
                fence = self.rt.claim_action(CONTROLLER, again)["lease"]["fencing_token"]
                self.rt.record_action(CONTROLLER, again, ActionState.RUNNING, fence=fence)
                self._finish(action["run_id"], task, record, main, again, fence)
            else:
                self.rt.resolve_in_doubt(CONTROLLER, action_id, "FAILED", reconciliation="the workspace matches neither side of the patch")
                self._conflict(action["run_id"], task, record, "after a restart the workspace matches neither the integrated nor the original files")
            settled.append(action_id)
        return settled

    def status(self, run_id: str) -> list[dict]:
        rows = self.rt.store.read().query("SELECT task_id, owner, branch, state, snapshot_id, detail FROM task_workspaces WHERE run_id = ? ORDER BY created_at", (run_id,))
        return [dict(r) for r in rows]
