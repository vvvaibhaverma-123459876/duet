"""Strict-mode workspace allocation.

A strict workspace is a linked git worktree created under Duet's private state
directory from an explicit base commit, on a Duet-owned branch. The user's
checkout (branch, index, working tree, open editors) is never switched or
modified, and nothing untracked or ignored is copied in. Dirty user work
reaches the workspace only through `import_inputs`, an explicit list the user
approved, checked against the sensitive-file and symlink policy.

Exactly one writer holds a fenced `workspace:` lease at a time; reviewers get
an immutable materialised snapshot instead of a view of changing files.

Worktrees share the repository's refs and config with the user's checkout, so
this is isolation of the *working copy*, not a security boundary: an agent
running as the same user can still reach the common git directory."""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..runtime.api import Runtime
from ..runtime.contracts import CONTROLLER, Principal, StaleLease, ValidationError
from ..runtime.paths import ensure_private_dir, runtime_dir
from ..workspace import _exclude_duet_dir, _unique_branch, assert_safe_live_repo
from .repo import RepoIdentity, resolve_repo
from .snapshots import is_sensitive, is_transient

WRITER_LEASE_SECONDS = 1800
MAX_IMPORT_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class StrictWorkspace:
    run_id: str
    repo: RepoIdentity
    path: Path
    branch: str
    base_sha: str

    @property
    def lease_resource(self) -> str:
        return "workspace:" + hashlib.sha256(str(self.path).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class ImportReport:
    imported: tuple[str, ...]
    refused: tuple[tuple[str, str], ...]


class WorkspaceManager:
    def __init__(self, runtime: Runtime, root: Path | None = None) -> None:
        self.runtime = runtime
        self.root = ensure_private_dir(Path(root) if root else runtime_dir() / "worktrees")

    def create(self, run_id: str, repo_path: str | Path, *, base: str = "HEAD", branch: str | None = None) -> StrictWorkspace:
        repo = resolve_repo(repo_path)
        assert_safe_live_repo(repo.toplevel)
        base_sha = _git(["rev-parse", "--verify", f"{base}^{{commit}}"], repo.toplevel)
        name = branch or _unique_branch(repo.toplevel, f"duet/run-{run_id.removeprefix('run_')[:12]}")
        if _git_ok(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], repo.toplevel):
            raise ValidationError(f"branch {name} already exists; Duet never reuses or resets an existing branch")
        dest = self.root / run_id / (repo.toplevel.name or "repo")
        if dest.exists():
            raise ValidationError(f"workspace path already exists: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        # `git worktree add` from the common repo never touches the user's
        # current checkout: no branch switch, no index update, no stash.
        _git(["worktree", "add", "--quiet", "-b", name, str(dest), base_sha], repo.toplevel)
        _exclude_duet_dir(dest)
        return StrictWorkspace(run_id=run_id, repo=repo, path=dest.resolve(), branch=name, base_sha=base_sha)

    # -- one writer ---------------------------------------------------------------

    def acquire_writer(self, workspace: StrictWorkspace, participant_id: str, *, lease_seconds: int = WRITER_LEASE_SECONDS, principal: Principal = CONTROLLER) -> dict:
        """Grant exclusive write access to one participant. A second writer
        gets a Conflict until the lease is released or expires; a superseded
        writer's fence is rejected everywhere it is checked."""
        return self.runtime.acquire_lease(principal, workspace.lease_resource, owner=participant_id, lease_seconds=lease_seconds)

    def check_writer(self, workspace: StrictWorkspace, participant_id: str, fence: int) -> None:
        row = self.runtime.check_fence(workspace.lease_resource, fence)
        if row["owner"] != participant_id:
            raise StaleLease(f"{participant_id} does not hold the write lease for {workspace.path}")

    def release_writer(self, workspace: StrictWorkspace, fence: int, *, principal: Principal = CONTROLLER) -> None:
        self.runtime.release_lease(principal, workspace.lease_resource, fence=fence)

    # -- explicit inputs ------------------------------------------------------------

    def import_inputs(
        self,
        workspace: StrictWorkspace,
        source: str | Path,
        paths: list[str],
        *,
        allow_sensitive: bool = False,
    ) -> ImportReport:
        """Copy an explicit, user-approved list of files from the user's
        checkout into the workspace. Each path is checked: it must stay inside
        both trees, must not be under .git, must not be a symlink that escapes,
        and must not look like a secret unless `allow_sensitive` was approved."""
        src_root = Path(source).resolve()
        imported: list[str] = []
        refused: list[tuple[str, str]] = []
        for raw in paths:
            rel = PurePosixPath(raw)
            if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                refused.append((raw, "path must be relative and stay inside the repository"))
                continue
            if rel.parts[0] == ".git":
                refused.append((raw, "git internals are never imported"))
                continue
            if is_transient(str(rel)):
                refused.append((raw, "transient output, not an input"))
                continue
            if is_sensitive(str(rel)) and not allow_sensitive:
                refused.append((raw, "looks like a secret; needs explicit user approval"))
                continue
            src = src_root.joinpath(*rel.parts)
            try:
                info = os.lstat(src)
            except FileNotFoundError:
                refused.append((raw, "does not exist in the source checkout"))
                continue
            if stat.S_ISLNK(info.st_mode):
                target = (src.parent / os.readlink(src)).resolve()
                try:
                    target.relative_to(src_root)
                except ValueError:
                    refused.append((raw, "symlink escapes the source repository"))
                    continue
            elif not stat.S_ISREG(info.st_mode):
                refused.append((raw, "only regular files and in-tree symlinks are imported"))
                continue
            elif info.st_size > MAX_IMPORT_BYTES:
                refused.append((raw, f"larger than {MAX_IMPORT_BYTES} bytes"))
                continue
            dest = workspace.path.joinpath(*rel.parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(info.st_mode):
                if dest.exists() or dest.is_symlink():
                    dest.unlink()
                os.symlink(os.readlink(src), dest)
            else:
                shutil.copy2(src, dest, follow_symlinks=False)
            imported.append(str(rel))
        return ImportReport(tuple(imported), tuple(refused))

    # -- teardown ---------------------------------------------------------------------

    def remove(self, workspace: StrictWorkspace, *, delete_branch: bool = False) -> None:
        _git(["worktree", "remove", "--force", str(workspace.path)], workspace.repo.toplevel)
        if delete_branch:
            _git(["branch", "-D", workspace.branch], workspace.repo.toplevel)


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise ValidationError(f"git {' '.join(args)} failed in {cwd}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def _git_ok(args: list[str], cwd: Path) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0

