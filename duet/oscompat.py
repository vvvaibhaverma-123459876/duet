"""Operating-system differences, in one place (docs/duet-v2/WINDOWS_PORT.md).

Everything that behaves differently on Windows goes through here: process
existence and identity, stopping a process tree, interrupting a turn, file
locks, detached launches, and command-line quoting. On POSIX the behaviour
is exactly what the callers did before; on Windows it uses the Win32
equivalents (through `psutil`, a Windows-only dependency).

One trap motivates this module: on Windows `os.kill(pid, 0)` does not test
for existence, it *terminates* the process. Never call it; use
`pid_exists`."""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from typing import IO, Any

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:  # pragma: no cover - exercised by the Windows CI job
    import msvcrt

    import psutil
else:
    import fcntl


# --- processes ----------------------------------------------------------------------------


def pid_exists(pid: int) -> bool:
    """True if a process with this pid exists (it may belong to another
    user). Never signals it."""
    if pid <= 0:
        return False
    if IS_WINDOWS:  # pragma: no cover
        return psutil.pid_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists but not ours (EPERM), or cannot tell: assume alive
    return True


def windows_process_start(pid: int) -> str | None:  # pragma: no cover - Windows only
    """Creation time of `pid` (exact FILETIME, so stable across calls), or
    None when there is no such process."""
    try:
        return f"{psutil.Process(pid).create_time():.6f}"
    except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError, OSError):
        return None


def windows_parent_pid(pid: int) -> int | None:  # pragma: no cover - Windows only
    try:
        return psutil.Process(pid).ppid()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return None


def parent_pid(pid: int) -> int | None:
    """The parent of `pid`, or None if unknown. Linux reads /proc; Windows
    asks the kernel; elsewhere (macOS) this is not available without a
    subprocess and returns None."""
    if IS_WINDOWS:  # pragma: no cover
        return windows_parent_pid(pid)
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            stat = handle.read()
    except OSError:
        return None
    try:
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (IndexError, ValueError):
        return None


def _windows_descendants(pid: int, since: float | None) -> list[Any]:  # pragma: no cover - Windows only
    """Every live process descending from `pid`, found by parent links. A
    Windows child keeps its dead parent's pid as its ppid, so descendants
    of an exited leader are still found; only processes created at or after
    `since` (the leader's creation) count, so a reused pid is not taken for
    a child."""
    by_parent: dict[int, list[Any]] = {}
    for proc in psutil.process_iter(["pid", "ppid", "create_time"]):
        info = proc.info
        if since is not None and (info.get("create_time") or 0) + 0.001 < since:
            continue
        by_parent.setdefault(info.get("ppid") or 0, []).append(proc)
    found: list[Any] = []
    frontier = [pid]
    seen = {pid}
    while frontier:
        parent = frontier.pop()
        for child in by_parent.get(parent, []):
            if child.pid not in seen:
                seen.add(child.pid)
                found.append(child)
                frontier.append(child.pid)
    return found


def kill_tree(pid: int, *, since: float | None = None, timeout: float = 5.0) -> None:  # pragma: no cover - Windows only
    """Windows: kill `pid` and all its descendants, children first."""
    try:
        leader = psutil.Process(pid)
        since = since if since is not None else leader.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        leader = None
    victims = list(reversed(_windows_descendants(pid, since)))
    if leader is not None:
        victims.append(leader)
    for proc in victims:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    psutil.wait_procs(victims, timeout=timeout)


def signal_group(pgid: int, sig: int) -> None:
    """POSIX: signal a whole process group (ignoring a vanished one).
    Windows: SIGINT becomes CTRL_BREAK_EVENT to the child's process group
    (effective only for console children started with
    CREATE_NEW_PROCESS_GROUP); anything else kills the tree."""
    if IS_WINDOWS:  # pragma: no cover
        if sig == signal.SIGINT:
            try:
                os.kill(pgid, signal.CTRL_BREAK_EVENT)
            except OSError:
                pass
        else:
            kill_tree(pgid)
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def group_exists(pgid: int) -> bool:
    """POSIX: whether any member of the group remains."""
    if IS_WINDOWS:  # pragma: no cover
        return pid_exists(pgid)
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def own_group_kwargs() -> dict:
    """Popen arguments that put a child in its own process group, so its
    whole tree can be stopped without touching ours."""
    if IS_WINDOWS:  # pragma: no cover
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def detached_kwargs() -> dict:
    """Popen arguments for a background service that outlives its launcher
    and has no console window."""
    if IS_WINDOWS:  # pragma: no cover
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        return {"creationflags": flags, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


# --- file locks ---------------------------------------------------------------------------


class LockBusy(OSError):
    """A non-blocking lock attempt found the lock held."""


def lock_file(handle: IO, *, blocking: bool = True) -> None:
    """Exclusive advisory lock on an open file. Raises LockBusy when
    `blocking` is false and another process holds it."""
    if IS_WINDOWS:  # pragma: no cover
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if not blocking:
                    raise LockBusy("the lock is held by another process") from None
                time.sleep(0.05)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        raise LockBusy("the lock is held by another process") from None


def unlock_file(handle: IO) -> None:
    if IS_WINDOWS:  # pragma: no cover
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# --- users and commands -------------------------------------------------------------------


def user_key() -> str:
    """A short, filesystem-safe identifier for the current user."""
    if hasattr(os, "getuid"):
        return str(os.getuid())
    name = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return "".join(c for c in name if c.isalnum() or c in "-_")[:32] or "user"


def split_command(text: str) -> list[str]:
    """Split a command line the way this platform's shell-less exec would:
    POSIX rules, or Windows command-line rules (backslashes in paths are
    kept, double quotes group)."""
    if IS_WINDOWS:  # pragma: no cover
        parts = shlex.split(text, posix=False)
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]
    return shlex.split(text)


def join_command(argv: list[str]) -> str:
    """Quote an argv as one command line for this platform (for commands
    written into client configuration, e.g. a Claude hook)."""
    if IS_WINDOWS:  # pragma: no cover
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def is_batch_launcher(path: str) -> bool:
    """A Windows .cmd/.bat file runs through cmd.exe, which re-parses its
    arguments (%VAR% expansion, & | < > ^). DUET must not pass free text to
    one as an argument."""
    return IS_WINDOWS and path.lower().endswith((".cmd", ".bat"))


__all__ = [
    "IS_WINDOWS", "LockBusy", "detached_kwargs", "group_exists", "is_batch_launcher", "join_command", "kill_tree", "lock_file",
    "own_group_kwargs", "parent_pid", "pid_exists", "signal_group", "split_command", "unlock_file", "user_key",
    "windows_parent_pid", "windows_process_start",
]

