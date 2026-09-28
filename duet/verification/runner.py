"""Bounded execution of acceptance checks.

A check runs against a workspace whose inputs were snapshotted immediately
before; the inputs are snapshotted again afterwards. If they differ, the
check's result describes a state that no longer exists, so the outcome is
`invalidated`, never `passed` (R10, AT36). Checks run with an allowlisted
environment: provider API keys, tokens and other secrets in Duet's own
environment are not passed through unless the user named them."""
from __future__ import annotations

from ..runtime.hygiene import redact

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..providers.process import run_bounded
from ..runtime.artifacts import ArtifactStore
from ..runtime.contracts import content_hash, utc_now
from ..runtime.paths import ensure_private_dir
from ..workspaces.snapshots import Snapshot, capture_snapshot, materialize
from .acceptance import CheckSpec

BASE_ENV_ALLOW = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TERM", "SYSTEMROOT", "COMSPEC",
    # Windows: what programs need to start, find their launchers, write
    # temporary files and locate the user's profile. Names absent on POSIX,
    # so POSIX environment fingerprints are unchanged.
    "PATHEXT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE", "USERNAME", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
)
MAX_CHECK_OUTPUT = 1024 * 1024
_NO_TESTS = (
    re.compile(r"\bno tests ran\b", re.I),
    re.compile(r"\bcollected 0 items\b", re.I),
    re.compile(r"\bno tests found\b", re.I),
    re.compile(r"\bno test files\b", re.I),
    re.compile(r"\b0 tests? (?:passed|ran|run)\b", re.I),
)
PYTEST_NO_TESTS_EXIT = 5


@dataclass(frozen=True)
class CheckOutcome:
    check_id: str
    status: str  # passed | failed | unknown | invalidated
    exit_code: int | None
    argv: tuple[str, ...]
    cwd: str
    env_fingerprint: str
    started_at: str
    ended_at: str
    output: str
    output_hash: str
    snapshot_before: str
    snapshot_after: str
    detail: str


def build_env(spec: CheckSpec, parent: dict[str, str] | None = None) -> tuple[dict[str, str], str]:
    """The check's environment and a fingerprint of it. The fingerprint hashes
    names and values, so evidence records *which* environment produced it
    without storing any value."""
    source = dict(os.environ if parent is None else parent)
    env = {name: source[name] for name in (*BASE_ENV_ALLOW, *spec.env_allow) if name in source}
    env.update(dict(spec.env))
    env["DUET_VERIFY"] = "1"
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env, content_hash(sorted(env.items()))


def run_check(
    spec: CheckSpec,
    workspace: Path | str,
    *,
    snapshot_before: Snapshot | None = None,
    untracked: str = "include",
    parent_env: dict[str, str] | None = None,
) -> CheckOutcome:
    root = Path(workspace).resolve()
    before = snapshot_before or capture_snapshot(root, untracked=untracked)
    cwd = (root / spec.cwd).resolve()
    try:
        cwd.relative_to(root)
    except ValueError:
        return _outcome(spec, "unknown", None, "", "", before.tree_hash, before.tree_hash, "check cwd escapes the workspace", "", "")
    env, fingerprint = build_env(spec, parent_env)
    started = utc_now()
    try:
        proc = run_bounded(
            spec.command,
            cwd=cwd,
            env=env,
            timeout=spec.timeout_seconds,
            max_stdout=MAX_CHECK_OUTPUT,
            max_stderr=MAX_CHECK_OUTPUT,
        )
    except OSError as exc:
        ended = utc_now()
        return _outcome(spec, "unknown", None, f"could not execute: {exc}", fingerprint, before.tree_hash, before.tree_hash, "check could not be executed", started, ended)
    ended = utc_now()
    # Redacted before it is hashed and stored: evidence never keeps a secret (D13).
    output = redact((proc.stdout + ("\n" if proc.stdout and proc.stderr else "") + proc.stderr).strip())
    after = capture_snapshot(root, untracked=untracked)
    if proc.timed_out:
        status, detail = "failed", f"timed out after {spec.timeout_seconds}s (treated as failing)"
    elif proc.returncode == 0:
        status, detail = "passed", "exit 0"
    else:
        status, detail = "failed", f"exit {proc.returncode}"
    # The no-tests policy applies only when the run itself did not fail:
    # "no test files" in one package must not hide a failure in another.
    if not proc.timed_out and proc.returncode in (0, PYTEST_NO_TESTS_EXIT) and _no_tests(proc.returncode, output):
        status = {"unknown": "unknown", "fail": "failed", "pass": "passed"}[spec.no_tests]
        detail = f"no tests were collected (policy: {spec.no_tests})"
    if after.tree_hash != before.tree_hash:
        changed = sorted({f.key()[0] for f in set(after.files) ^ set(before.files)})
        status = "invalidated"
        detail = f"inputs changed while the check ran: {', '.join(changed[:10])}{' ...' if len(changed) > 10 else ''}"
    return _outcome(spec, status, proc.returncode, output, fingerprint, before.tree_hash, after.tree_hash, detail, started, ended)


def run_check_on_snapshot(
    spec: CheckSpec,
    manifest: dict,
    store: ArtifactStore,
    scratch_root: Path | str,
    *,
    expected_tree_hash: str,
    parent_env: dict[str, str] | None = None,
) -> CheckOutcome:
    """Run a check on a fresh copy of exactly the snapshot's files.

    The live workspace can hold things the snapshot deliberately left out
    (ignored files, links that escape, stale bytecode) and can change while a
    check runs. A private copy built from the stored blobs has neither
    problem: if the copy does not reproduce the snapshot's tree hash, the
    check is not run and the outcome is `unknown`."""
    scratch = Path(tempfile.mkdtemp(prefix=f"check-{spec.id}-", dir=ensure_private_dir(Path(scratch_root))))
    try:
        root = scratch / "tree"
        try:
            materialize(manifest, store, root)
        except Exception as exc:  # a snapshot that cannot be rebuilt cannot be verified
            return _outcome(spec, "unknown", None, "", "", expected_tree_hash, expected_tree_hash, f"snapshot could not be materialised: {exc}", "", "")
        for argv in (["init", "-q"], ["add", "-A"], ["-c", "user.name=duet", "-c", "user.email=duet@localhost.invalid", "commit", "-q", "--no-verify", "--allow-empty", "-m", "snapshot"]):
            subprocess.run(["git", *argv], cwd=root, capture_output=True, check=False)
        before = capture_snapshot(root)
        if before.tree_hash != expected_tree_hash:
            return _outcome(spec, "unknown", None, "", "", expected_tree_hash, before.tree_hash, "the materialised copy does not reproduce the snapshot (e.g. submodules); check not run", "", "")
        return run_check(spec, root, snapshot_before=before, parent_env=parent_env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _no_tests(returncode: int | None, output: str) -> bool:
    if returncode == PYTEST_NO_TESTS_EXIT and "pytest" in output.lower():
        return True
    some_passed = re.search(r"\b[1-9]\d* (?:tests? )?passed\b", output, re.I)
    return any(pattern.search(output) for pattern in _NO_TESTS) and not some_passed


def _outcome(spec: CheckSpec, status, exit_code, output, fingerprint, before, after, detail, started, ended) -> CheckOutcome:
    return CheckOutcome(
        check_id=spec.id,
        status=status,
        exit_code=exit_code,
        argv=tuple(spec.command),
        cwd=spec.cwd,
        env_fingerprint=fingerprint,
        started_at=started or utc_now(),
        ended_at=ended or utc_now(),
        output=output,
        output_hash="sha256:" + hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest(),
        snapshot_before=before,
        snapshot_after=after,
        detail=detail,
    )
