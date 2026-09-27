"""Repository identity.

Identity comes from the canonical *common* git directory, not from a remote
URL: two clones of the same origin are different repositories (different
object stores, refs and working trees), while linked worktrees of one clone
share an identity because they share refs."""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..runtime.contracts import ValidationError


@dataclass(frozen=True)
class RepoIdentity:
    repo_id: str
    toplevel: Path
    common_dir: Path
    git_dir: Path
    is_linked_worktree: bool
    head: str | None
    branch: str | None
    remotes: tuple[tuple[str, str], ...]


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise ValidationError(f"git {' '.join(args)} failed in {cwd}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def _absolute(path_text: str, cwd: Path) -> Path:
    path = Path(path_text)
    return (path if path.is_absolute() else cwd / path).resolve()


def resolve_repo(path: str | Path) -> RepoIdentity:
    start = Path(path).expanduser()
    if not start.exists():
        raise ValidationError(f"repository path does not exist: {start}")
    start = start.resolve()
    if _git(["rev-parse", "--is-inside-work-tree"], start) != "true":
        raise ValidationError(f"not a git work tree: {start}")
    toplevel = Path(_git(["rev-parse", "--show-toplevel"], start)).resolve()
    common = _absolute(_git(["rev-parse", "--git-common-dir"], toplevel), toplevel)
    git_dir = _absolute(_git(["rev-parse", "--git-dir"], toplevel), toplevel)
    head_proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=toplevel, text=True, capture_output=True)
    head = head_proc.stdout.strip() or None
    branch_proc = subprocess.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=toplevel, text=True, capture_output=True)
    branch = branch_proc.stdout.strip() or None
    remotes = []
    listing = subprocess.run(["git", "remote", "-v"], cwd=toplevel, text=True, capture_output=True).stdout
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] in ("(fetch)",):
            remotes.append((parts[0], parts[1]))
    repo_id = "repo_" + hashlib.sha256(str(common).encode("utf-8")).hexdigest()[:24]
    return RepoIdentity(
        repo_id=repo_id,
        toplevel=toplevel,
        common_dir=common,
        git_dir=git_dir,
        is_linked_worktree=git_dir != common,
        head=head,
        branch=branch,
        remotes=tuple(sorted(set(remotes))),
    )
