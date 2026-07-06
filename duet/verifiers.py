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
