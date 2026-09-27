"""Bounded execution of acceptance checks.

A check runs against a workspace whose inputs were snapshotted immediately
before; the inputs are snapshotted again afterwards. If they differ, the
check's result describes a state that no longer exists, so the outcome is
`invalidated`, never `passed` (R10, AT36). Checks run with an allowlisted
environment: provider API keys, tokens and other secrets in Duet's own
environment are not passed through unless the user named them."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..providers.process import run_bounded
from ..runtime.contracts import content_hash, utc_now
from ..workspaces.snapshots import Snapshot, capture_snapshot
from .acceptance import CheckSpec

BASE_ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TERM", "SYSTEMROOT", "COMSPEC")
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
    output = (proc.stdout + ("\n" if proc.stdout and proc.stderr else "") + proc.stderr).strip()
    after = capture_snapshot(root, untracked=untracked)
    if proc.timed_out:
        status, detail = "failed", f"timed out after {spec.timeout_seconds}s (treated as failing)"
    elif proc.returncode == 0:
        status, detail = "passed", "exit 0"
    else:
        status, detail = "failed", f"exit {proc.returncode}"
    if _no_tests(proc.returncode, output) and not proc.timed_out:
        status = {"unknown": "unknown", "fail": "failed", "pass": "passed"}[spec.no_tests]
        detail = f"no tests were collected (policy: {spec.no_tests})"
    if after.tree_hash != before.tree_hash:
        changed = sorted({f.key()[0] for f in set(after.files) ^ set(before.files)})
        status = "invalidated"
        detail = f"inputs changed while the check ran: {', '.join(changed[:10])}{' ...' if len(changed) > 10 else ''}"
    return _outcome(spec, status, proc.returncode, output, fingerprint, before.tree_hash, after.tree_hash, detail, started, ended)


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
