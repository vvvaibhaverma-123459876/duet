"""Fake executables for tests, on every platform.

POSIX runs a script with a `#!` line; Windows runs `name.cmd`, a batch
wrapper around the Python interpreter (as npm installs `claude.cmd`). Both
forward all arguments, stdin and the exit code."""
from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"


def make_exe(bin_dir: Path, name: str, *, source: str | None = None, script: Path | None = None, python: str | None = None) -> Path:
    """Create `name` in `bin_dir` running either Python `source` or an
    existing Python `script`, and return the path to invoke."""
    if (source is None) == (script is None):
        raise ValueError("give exactly one of source or script")
    py = python or sys.executable
    bin_dir.mkdir(parents=True, exist_ok=True)
    if source is not None:
        target = bin_dir / f"{name}.py" if IS_WINDOWS else None
        if IS_WINDOWS:
            target.write_text(source, encoding="utf-8")
            script = target
        else:
            path = bin_dir / name
            path.write_text(f"#!{py}\n{source}", encoding="utf-8")
            path.chmod(0o755)
            return path
    if IS_WINDOWS:
        path = bin_dir / f"{name}.cmd"
        path.write_text(f'@"{py}" "{script}" %*\r\n', encoding="utf-8")
        return path
    path = bin_dir / name
    path.write_text(f'#!/bin/sh\nexec "{py}" "{script}" "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def fake_agent(bin_dir: Path, name: str = "fake", *, stdout: str = "", stderr: str = "", code: int = 0) -> Path:
    """An agent that reads its whole prompt, prints fixed output and exits
    with `code` (the portable form of `cat >/dev/null; echo ...; exit N`)."""
    source = (
        "import sys\nsys.stdin.read()\n"
        f"sys.stdout.buffer.write({(stdout + chr(10) if stdout else '').encode()!r})\n"
        f"sys.stderr.buffer.write({(stderr + chr(10) if stderr else '').encode()!r})\n"
        f"sys.exit({code})\n"
    )
    return make_exe(bin_dir, name, source=source)
