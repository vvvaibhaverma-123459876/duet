from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .logging_setup import get_logger

log = get_logger()


@dataclass(frozen=True)
class VerificationResult:
    status: str
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


class CommandVerifier:
    """Run an arbitrary shell command in the workspace; exit 0 is a pass.
    This is how non-Python stacks (jest, cargo, go test, tsc, lint) verify."""

    def __init__(self, command: str, timeout_seconds: int = 600) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.name = f"cmd:{command}"

    def verify(self, workspace: Path) -> VerificationResult:
        try:
            proc = subprocess.run(
                self.command,
                shell=True,
                cwd=workspace,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            captured = (exc.stdout or "") + (exc.stderr or "")
            return VerificationResult(
                "failed", False, f"{self.command!r} timed out after {self.timeout_seconds}s.\n{captured}".strip()
            )
        except OSError as exc:
            return VerificationResult("unknown", False, f"{self.command!r} could not be executed: {exc}")
        output = (proc.stdout + proc.stderr).strip()
        return VerificationResult("passed" if proc.returncode == 0 else "failed", proc.returncode == 0, output)


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
            elif result.status == "unknown" and worst != "failed":
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
        try:
            proc = subprocess.run(
                ["pytest", "-q"],
                cwd=workspace,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            log.warning("pytest timed out after %ss", self.timeout_seconds)
            captured = (exc.stdout or "") + (exc.stderr or "")
            return VerificationResult(
                "failed",
                False,
                f"pytest timed out after {self.timeout_seconds}s (treated as failing).\n{captured}".strip(),
            )
        except OSError as exc:
            log.warning("pytest could not be executed: %s", exc)
            return VerificationResult("unknown", False, f"pytest could not be executed: {exc}")
        output = (proc.stdout + proc.stderr).strip()
        return VerificationResult("passed" if proc.returncode == 0 else "failed", proc.returncode == 0, output)
