"""Baseline checks (D10): what the required checks say before any change.

A check that already passes on the base commit cannot demonstrate a new
acceptance criterion: a green pre-existing suite says nothing about a
feature that is not there yet (AT21). Each required check runs once per run
and contract version on a private export of the base commit (git archive,
so no ignored or untracked local state leaks in). The completion gate then
counts a criterion as demonstrated only by fail-to-pass evidence, or by a
non-author review that explicitly attests it."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .. import oscompat
from ..runtime.paths import ensure_private_dir
from ..workspaces.snapshots import capture_snapshot
from .acceptance import CheckSpec
from .runner import CheckOutcome, _outcome, run_check


def run_baseline(spec: CheckSpec, repo_path: Path | str, base_sha: str, scratch_root: Path | str, *, parent_env: dict[str, str] | None = None) -> CheckOutcome:
    scratch = Path(tempfile.mkdtemp(prefix=f"baseline-{spec.id}-", dir=ensure_private_dir(Path(scratch_root))))
    try:
        root = scratch / "tree"
        root.mkdir()
        archive = subprocess.run(["git", "archive", "--format=tar", base_sha], cwd=repo_path, capture_output=True)
        if archive.returncode != 0:
            return _outcome(spec, "unknown", None, "", "", "", "", f"base commit could not be exported: {archive.stderr.decode(errors='replace')[:300]}", "", "")
        subprocess.run(["tar", "-x", "-C", str(root)], input=archive.stdout, capture_output=True, check=True)
        for argv in (["init", "-q"], ["add", "-A"], ["-c", "user.name=duet", "-c", "user.email=duet@localhost.invalid", "commit", "-q", "--no-verify", "--allow-empty", "-m", "base"]):
            subprocess.run(["git", *argv], cwd=root, capture_output=True, check=False)
        return run_check(spec, root, snapshot_before=capture_snapshot(root), parent_env=parent_env)
    except Exception as exc:  # a baseline that cannot run demonstrates nothing, and says so
        return _outcome(spec, "unknown", None, "", "", "", "", f"baseline could not run: {exc}", "", "")
    finally:
        oscompat.remove_tree(scratch)
