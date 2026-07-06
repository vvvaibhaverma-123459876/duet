from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .logging_setup import get_logger

log = get_logger()


class WorkspaceError(RuntimeError):
    """Raised when a workspace path is unsafe or a git operation fails."""


def create_workspace(path: str | None = None, reset: bool = False) -> Path:
    workspace = Path(path).expanduser() if path else Path(tempfile.mkdtemp(prefix="duet-"))
    if reset and workspace.exists():
        assert_safe_workspace(workspace)
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    assert_safe_workspace(workspace)
    # Prepare the git repo first; only take the lock once the workspace is
    # actually usable, so a failed init never leaves an orphan lock behind.
    _run(["git", "init"], workspace)
    _run(["git", "config", "user.name", "Duet Broker"], workspace)
    _run(["git", "config", "user.email", "duet@example.invalid"], workspace)
    _exclude_duet_dir(workspace)
    acquire_lock(workspace)
    return workspace.resolve()


def _exclude_duet_dir(workspace: Path) -> None:
    """Keep Duet's own bookkeeping (`.duet/`, incl. the session lock) out of
    `git add -A` via `.git/info/exclude`. This is local-only and never itself
    committed, so it does not touch the user's tracked `.gitignore`."""
    exclude = workspace / ".git" / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".duet/" not in existing.split():
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write(("\n" if existing and not existing.endswith("\n") else "") + ".duet/\n")
    except OSError as exc:
        log.warning("could not update git exclude in %s: %s", workspace, exc)


# --- System-path safety -----------------------------------------------------

# Prefix roots: anything *under* these is refused. /var and /sbin are omitted
# on purpose because macOS scratch dirs resolve under /private/var; they are
# still refused as exact paths via _SYSTEM_EXACT below.
_SYSTEM_PARENTS = (Path("/System"), Path("/bin"), Path("/usr"), Path("/etc"))
_SYSTEM_EXACT = (Path("/System"), Path("/bin"), Path("/sbin"), Path("/usr"), Path("/etc"), Path("/var"))


def _is_under_system_root(resolved: Path) -> bool:
    for parent in _SYSTEM_PARENTS:
        try:
            if parent.resolve() in resolved.parents or parent.resolve() == resolved:
                return True
        except FileNotFoundError:
            continue
    return False


def assert_safe_workspace(path: Path) -> None:
    """Guard for scratch workspaces: refuse the user's home, cwd, the repo
    itself, and anything system-owned. Scratch workspaces are disposable, so we
    are deliberately strict."""
    resolved = path.expanduser().resolve()
    home = Path.home().resolve()
    cwd = Path.cwd().resolve()
    repo_root = Path(__file__).resolve().parent.parent
    denied = {Path("/").resolve(), home, cwd, repo_root.resolve()} | {r.resolve() for r in _SYSTEM_EXACT if r.exists()}
    if resolved in denied:
        raise WorkspaceError(f"refusing unsafe workspace path: {resolved}")
    if _is_under_system_root(resolved):
        raise WorkspaceError(f"refusing system workspace path: {resolved}")


def assert_safe_live_repo(path: Path) -> None:
    """Guard for an existing repo the user explicitly targets. Unlike a scratch
    workspace this may legitimately be the cwd or under home, so we only refuse
    the filesystem root and system-owned locations."""
    resolved = path.expanduser().resolve()
    if resolved == Path("/").resolve() or _is_under_system_root(resolved):
        raise WorkspaceError(f"refusing to operate on system path: {resolved}")


# --- Locking ----------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


def _read_lock_pid(lock: Path) -> int | None:
    try:
        for line in lock.read_text(encoding="utf-8").splitlines():
            if line.startswith("pid="):
                return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        return None
    return None


def acquire_lock(workspace: Path) -> Path:
    duet_dir = workspace / ".duet"
    duet_dir.mkdir(exist_ok=True)
    lock = duet_dir / "session.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        holder = _read_lock_pid(lock)
        if holder is not None and not _pid_alive(holder):
            log.warning("reclaiming stale lock from dead pid %s: %s", holder, lock)
            lock.unlink(missing_ok=True)
            return acquire_lock(workspace)
        raise WorkspaceError(
            f"workspace is already locked by another Duet session (pid={holder}): {lock}"
        ) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"pid={os.getpid()}\ncreated={time.time()}\n")
    return lock


def release_lock(workspace: Path) -> None:
    lock = workspace / ".duet" / "session.lock"
    try:
        lock.unlink()
    except FileNotFoundError:
        return


# --- Live (existing) repository support -------------------------------------


@dataclass
class LiveRepo:
    workspace: Path
    branch: str
    original_ref: str
    base_commit: str | None
    preexisting_stash: str | None = None


def _git_out(args: list[str], cwd: Path) -> str:
    return _run(["git", *args], cwd).stdout.strip()


def is_git_worktree(path: Path) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=path,
        text=True,
        capture_output=True,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def prepare_live_repo(path: str, branch: str | None = None, allow_dirty: bool = False) -> LiveRepo:
    """Prepare an existing git repo for a Duet session on an isolated branch."""
    repo = Path(path).expanduser()
    if not repo.exists():
        raise WorkspaceError(f"repo path does not exist: {repo}")
    assert_safe_live_repo(repo)
    repo = repo.resolve()
    if not is_git_worktree(repo):
        raise WorkspaceError(f"not a git work tree: {repo}. Run `git init` there first, or use a scratch workspace.")

    top = Path(_git_out(["rev-parse", "--show-toplevel"], repo))
    dirty = bool(_git_out(["status", "--porcelain"], repo))
    if dirty and not allow_dirty:
        raise WorkspaceError(
            f"repo has uncommitted changes: {top}. Commit/stash them, or pass --allow-dirty to include them."
        )

    # Base commit is None for a repo with no commits yet.
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=top, text=True, capture_output=True)
    base_commit = head.stdout.strip() if head.returncode == 0 else None
    original = subprocess.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=top, text=True, capture_output=True)
    original_ref = original.stdout.strip() or base_commit or "HEAD"

    # In --allow-dirty mode the user's pre-existing changes get committed onto
    # the Duet branch. Capture them as a stash object first so that a later
    # rollback (which deletes the branch) can restore them instead of destroying
    # the only copy of the user's work.
    preexisting_stash: str | None = None
    if dirty and allow_dirty:
        _run(["git", "stash", "push", "-u", "-m", "duet-preexisting"], top)
        preexisting_stash = _git_out(["rev-parse", "stash@{0}"], top)

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    branch = branch or f"duet/session-{stamp}"
    _run(["git", "checkout", "-b", branch], top)
    if preexisting_stash:
        # Bring the dirty changes back onto the fresh branch, then drop the
        # stack entry; the captured object id keeps them recoverable.
        _run(["git", "stash", "pop"], top)
    _exclude_duet_dir(top)
    acquire_lock(top)
    log.info("prepared live repo %s on branch %s (base=%s)", top, branch, base_commit)
    return LiveRepo(
        workspace=top,
        branch=branch,
        original_ref=original_ref,
        base_commit=base_commit,
        preexisting_stash=preexisting_stash,
    )


def rollback_live_repo(live: LiveRepo) -> None:
    """Discard the Duet branch and return the repo to where it started,
    restoring any pre-existing uncommitted work that was captured at prepare."""
    try:
        _run(["git", "checkout", "--force", live.original_ref], live.workspace)
        _run(["git", "branch", "-D", live.branch], live.workspace)
        if live.preexisting_stash:
            restore = subprocess.run(
                ["git", "stash", "apply", live.preexisting_stash],
                cwd=live.workspace,
                text=True,
                capture_output=True,
            )
            if restore.returncode != 0:
                log.error("could not restore pre-existing changes (%s): %s", live.preexisting_stash, restore.stderr.strip())
            else:
                log.info("restored pre-existing uncommitted changes from %s", live.preexisting_stash)
        log.info("rolled back live repo %s to %s", live.workspace, live.original_ref)
    except WorkspaceError as exc:
        log.error("rollback failed for %s: %s", live.workspace, exc)


# --- Demo seeding -----------------------------------------------------------


def seed_demo(workspace: Path) -> None:
    (workspace / ".gitignore").write_text(
        "__pycache__/\n*.py[cod]\n.pytest_cache/\n",
        encoding="utf-8",
    )
    (workspace / "roman.py").write_text(
        '''def roman_to_int(s: str) -> int:
    """Convert a valid Roman numeral string to an integer.

    Supports the standard subtractive pairs IV, IX, XL, XC, CD, and CM.
    The input is guaranteed to be a valid, non-empty uppercase Roman numeral.
    """
    raise NotImplementedError("roman_to_int is not implemented yet")
''',
        encoding="utf-8",
    )
    (workspace / "test_roman.py").write_text(
        '''from roman import roman_to_int


def test_basic_roman_numerals():
    assert roman_to_int("I") == 1
    assert roman_to_int("III") == 3
    assert roman_to_int("LVIII") == 58


def test_subtractive_roman_numerals():
    assert roman_to_int("IV") == 4
    assert roman_to_int("IX") == 9
    assert roman_to_int("XL") == 40
    assert roman_to_int("XC") == 90
    assert roman_to_int("CD") == 400
    assert roman_to_int("CM") == 900
    assert roman_to_int("MCMXCIV") == 1994
''',
        encoding="utf-8",
    )
    _run(["git", "add", "."], workspace)
    _run(["git", "commit", "-m", "Seed demo fixture"], workspace)


def commit_after_turn(workspace: Path, agent: str, display_name: str) -> bool:
    _run(["git", "add", "-A"], workspace)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=workspace)
    if diff.returncode == 0:
        return False
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": display_name,
            "GIT_AUTHOR_EMAIL": f"{agent}@duet.local",
            "GIT_COMMITTER_NAME": "Duet Broker",
            "GIT_COMMITTER_EMAIL": "duet@example.invalid",
        }
    )
    proc = subprocess.run(
        ["git", "commit", "-m", f"{display_name} turn"],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise WorkspaceError(
            f"git commit failed after {display_name} turn: {(proc.stderr or proc.stdout).strip()}"
        )
    return True


def workspace_state(workspace: Path) -> str:
    files = _run(["git", "ls-files"], workspace).stdout.strip()
    status = _run(["git", "status", "--short"], workspace).stdout.strip()
    diff = _run(["git", "diff", "--", "."], workspace).stdout.strip()
    return "\n".join(
        [
            "Files:",
            files or "(none tracked)",
            "",
            "Git status:",
            status or "(clean)",
            "",
            "Uncommitted diff:",
            diff or "(none)",
        ]
    )


def git_log_summary(workspace: Path) -> str:
    try:
        return _run(["git", "log", "--format=%h %an <%ae> %s"], workspace).stdout.strip()
    except WorkspaceError:
        return "(no commits)"


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise WorkspaceError(f"command failed ({' '.join(cmd)}) in {cwd}: {detail}")
    return proc
