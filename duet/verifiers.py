from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import oscompat
from .logging_setup import get_logger
from .providers.process import run_bounded

log = get_logger()

# Verifier output is evidence, not an archive: keep a bounded window.
MAX_VERIFIER_OUTPUT = 1024 * 1024


@dataclass(frozen=True)
class VerificationResult:
    status: str  # passed | failed | unknown
    success: bool
    output: str


class Verifier(Protocol):
    name: str

    def verify(self, workspace: Path) -> VerificationResult:
        ...


class AlwaysUnknown:
    name = "none"

    def verify(self, workspace: Path) -> VerificationResult:
        return VerificationResult("unknown", False, "No verifier configured.")


def _run_check(argv: list[str], workspace: Path, timeout_seconds: int, label: str) -> VerificationResult:
    """Run one check with bounded output and whole-tree termination on timeout,
    so a hanging test (or a grandchild holding the pipes) cannot wedge the
    session."""
    try:
        proc = run_bounded(
            argv,
            cwd=workspace,
            timeout=timeout_seconds,
            max_stdout=MAX_VERIFIER_OUTPUT,
            max_stderr=MAX_VERIFIER_OUTPUT,
        )
    except OSError as exc:
        log.warning("%s could not be executed: %s", label, exc)
        return VerificationResult("unknown", False, f"{label} could not be executed: {exc}")
    output = (proc.stdout + proc.stderr).strip()
    if proc.timed_out:
        log.warning("%s timed out after %ss", label, timeout_seconds)
        return VerificationResult(
            "failed", False, f"{label} timed out after {timeout_seconds}s (treated as failing).\n{output}".strip()
        )
    return VerificationResult("passed" if proc.returncode == 0 else "failed", proc.returncode == 0, output)


class CommandVerifier:
    """Run a user-approved shell command in the workspace; exit 0 is a pass.
    This is how non-Python stacks (jest, cargo, go test, tsc, lint) verify.
    The command comes from the user's CLI/config, never from agent output."""

    def __init__(self, command: str, timeout_seconds: int = 600) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.name = f"cmd:{command}"

    def verify(self, workspace: Path) -> VerificationResult:
        shell = oscompat.shell_argv(self.command)
        return _run_check(shell, workspace, self.timeout_seconds, repr(self.command))


class CompositeVerifier:
    """All member verifiers must pass. A single failure fails the gate; if
    nothing fails but any member is unknown, the composite stays unknown."""

    def __init__(self, verifiers: list[Verifier]) -> None:
        self.verifiers = list(verifiers)
        self.name = "all(" + ", ".join(v.name for v in verifiers) + ")"

    def verify(self, workspace: Path) -> VerificationResult:
        outputs = []
        worst = "passed"
        for verifier in self.verifiers:
            result = verifier.verify(workspace)
            outputs.append(f"[{verifier.name}: {result.status}]\n{result.output}")
            if result.status == "failed":
                worst = "failed"
            elif result.status != "passed" and worst != "failed":
                worst = "unknown"
        return VerificationResult(worst, worst == "passed", "\n\n".join(outputs))


def build_verifier(specs: list[str]) -> Verifier:
    """Build a verifier from CLI specs: 'pytest', 'none', or 'cmd:<shell command>'.
    Multiple specs compose into an all-must-pass gate."""
    chosen: list[Verifier] = []
    for spec in specs:
        if spec == "none":
            continue
        if spec == "pytest":
            chosen.append(PytestVerifier())
        elif spec.startswith("cmd:"):
            command = spec[len("cmd:"):].strip()
            if not command:
                raise ValueError("empty command in 'cmd:' verifier spec")
            chosen.append(CommandVerifier(command))
        else:
            raise ValueError(f"unknown verifier spec {spec!r}: use pytest, none, or cmd:<shell command>")
    if not chosen:
        return AlwaysUnknown()
    if len(chosen) == 1:
        return chosen[0]
    return CompositeVerifier(chosen)


class PytestVerifier:
    name = "pytest"

    def __init__(self, timeout_seconds: int = 600) -> None:
        self.timeout_seconds = timeout_seconds

    def verify(self, workspace: Path) -> VerificationResult:
        if shutil.which("pytest") is None:
            log.warning("pytest not found on PATH; cannot verify")
            return VerificationResult("unknown", False, "pytest not found on PATH; install it to enable verification.")
        result = _run_check(["pytest", "-q"], workspace, self.timeout_seconds, "pytest")
        if result.status == "failed" and _no_tests_collected(result.output):
            # pytest exits 5 when nothing was collected: that is not a pass, and
            # not evidence of a failure either.
            return VerificationResult("unknown", False, f"pytest collected no tests.\n{result.output}".strip())
        return result


def _no_tests_collected(output: str) -> bool:
    lowered = output.lower()
    return "no tests ran" in lowered or "collected 0 items" in lowered
