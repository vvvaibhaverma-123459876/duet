"""Shared fixtures for D05 pairing tests: a small repo with a feature check,
a coordinator over a private state dir, and host identities."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from duet.runtime.api import Runtime
from duet.runtime.artifacts import ArtifactStore
from duet.runtime.identity import ProcessIdentity
from duet.runtime.pairing import PairCoordinator
from duet.runtime.store import Store

PY = sys.executable


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "u@example.invalid", cwd=path)
    git("config", "user.name", "User", cwd=path)
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (path / "check_feature.py").write_text(
        "import calc\nassert hasattr(calc, 'mul'), 'mul() missing'\nassert calc.mul(3, 4) == 12\nprint('feature ok')\n"
    )
    git("add", "-A", cwd=path)
    git("commit", "-qm", "base", cwd=path)
    return path


MUL = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
BROKEN_MUL = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a + b\n"


def dead_host() -> str:
    proc = subprocess.Popen([PY, "-c", "pass"])
    identity = ProcessIdentity.of(proc.pid)
    proc.wait()
    return str(identity)


def live_host() -> str:
    return str(ProcessIdentity.current())


def coordinator(state: Path, **kw) -> PairCoordinator:
    runtime = Runtime(Store(state / "duet.db"))
    return PairCoordinator(runtime, ArtifactStore(state / "artifacts"), state_root=state, **kw)
