"""Per-user runtime state location (DECISIONS.md D-009).

The runtime database, endpoint socket and credentials live outside any
agent-editable checkout, in a directory only the user can access. This is not
a security boundary against other processes running as the same OS user; it
keeps state out of repositories and away from other users."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from .contracts import SchemaError


def state_dir(create: bool = True) -> Path:
    override = os.environ.get("DUET_STATE_DIR")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "duet"
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        path = Path(base).expanduser() / "duet"
    if create:
        ensure_private_dir(path)
    return path


def ensure_private_dir(path: Path) -> Path:
    """Create `path` as 0700 if missing; refuse an existing directory that
    other users can write to, or one owned by someone else."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":  # pragma: no cover - POSIX permission model only
        return path
    info = path.stat()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise SchemaError(f"state directory {path} is owned by uid {info.st_uid}, not the current user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SchemaError(
            f"state directory {path} is writable by group or others (mode {oct(info.st_mode & 0o777)}); "
            f"run `chmod 700 {path}`"
        )
    if info.st_mode & 0o077:
        # Readable by others is not fatal for pre-existing dirs we did not
        # create, but tighten it: tokens and transcripts live here.
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def runtime_dir(create: bool = True) -> Path:
    path = state_dir(create) / "v2"
    if create:
        ensure_private_dir(path)
    return path
