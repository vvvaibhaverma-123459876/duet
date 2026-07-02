from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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

    def verify(self, workspace: Path) -> VerificationResult:
        proc = subprocess.run(["pytest", "-q"], cwd=workspace, text=True, capture_output=True)
        output = (proc.stdout + proc.stderr).strip()
        return VerificationResult("passed" if proc.returncode == 0 else "failed", proc.returncode == 0, output)
