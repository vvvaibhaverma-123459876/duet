from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


class WorkspaceError(RuntimeError):
    """Raised when a workspace path is unsafe or cannot be prepared."""


def create_workspace(path: str | None = None, reset: bool = False) -> Path:
    workspace = Path(path).expanduser() if path else Path(tempfile.mkdtemp(prefix="duet-"))
    if reset and workspace.exists():
        assert_safe_workspace(workspace)
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    assert_safe_workspace(workspace)
    acquire_lock(workspace)
    _run(["git", "init"], workspace)
    _run(["git", "config", "user.name", "Duet Broker"], workspace)
    _run(["git", "config", "user.email", "duet@example.invalid"], workspace)
    return workspace.resolve()


def assert_safe_workspace(path: Path) -> None:
    resolved = path.expanduser().resolve()
    home = Path.home().resolve()
    cwd = Path.cwd().resolve()
    repo_root = Path(__file__).resolve().parent.parent
    denied = {
        Path("/").resolve(),
        home,
        cwd,
        repo_root.resolve(),
        Path("/System").resolve(),
        Path("/bin").resolve(),
        Path("/usr").resolve(),
        Path("/etc").resolve(),
        Path("/var").resolve(),
    }
    if resolved in denied:
        raise WorkspaceError(f"refusing unsafe workspace path: {resolved}")
    for parent in (Path("/System"), Path("/bin"), Path("/usr"), Path("/etc")):
        try:
            if parent.resolve() in resolved.parents:
                raise WorkspaceError(f"refusing system workspace path: {resolved}")
        except FileNotFoundError:
            continue


def acquire_lock(workspace: Path) -> Path:
    duet_dir = workspace / ".duet"
    duet_dir.mkdir(exist_ok=True)
    lock = duet_dir / "session.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise WorkspaceError(f"workspace is already locked by another Duet session: {lock}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"pid={os.getpid()}\ncreated={time.time()}\n")
    return lock


def release_lock(workspace: Path) -> None:
    lock = workspace / ".duet" / "session.lock"
    try:
        lock.unlink()
    except FileNotFoundError:
        return


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
    subprocess.run(["git", "commit", "-m", f"{display_name} turn"], cwd=workspace, env=env, check=True, capture_output=True, text=True)
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
    return _run(["git", "log", "--format=%h %an <%ae> %s"], workspace).stdout.strip()


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=True)
