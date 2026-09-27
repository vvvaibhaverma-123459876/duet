"""Failure classification (spec 8.4): what kind of help a failure needs.

classify(record: FailureRecord) -> tuple[str, Reason]
  Returns one of FAILURE_CLASSES and the evidence:
  - "environment": the tool or dependency is missing or broken, not the
    reasoning: command not found, No such file or directory (for an
    executable or interpreter), ModuleNotFoundError / ImportError / No
    module named, cannot find package, permission denied, network
    unreachable / connection refused / name resolution, disk full, exit
    code 126/127 when visible. Needs environment repair, never a stronger
    model (AT19).
  - "requirements": the task is unclear: the agent asked a question it
    could not answer itself, or text says ambiguous / unclear requirement /
    which of / not specified. Needs an explicit assumption or a question.
  - "hypothesis": the code ran and was wrong: assertion errors, test
    failures (FAILED, AssertionError, expected ... got), a review that
    requested changes. More reasoning or a different investigation may help.
  - "provider": quota, rate limit, auth, billing, overloaded, model
    unavailable (record.kind or text). Admission (D08) owns these.
  - "timeout": timed out / deadline.
  - "unknown": none of the above.
  Matching is case-insensitive on record.text and record.kind; the first
  matching class in the order provider, environment, timeout, requirements,
  hypothesis wins, so "ModuleNotFoundError" inside a pytest failure is
  environment, not hypothesis.

summarise(failures) -> dict[str, int]: counts per class, all classes present.
consecutive(failures, cls) -> int: how many of the newest failures in a row
  have class `cls`."""
from __future__ import annotations

from .contracts import FailureRecord, Reason


def classify(record: FailureRecord) -> tuple[str, Reason]:
    raise NotImplementedError


def summarise(failures: tuple[FailureRecord, ...] | list[FailureRecord]) -> dict[str, int]:
    raise NotImplementedError


def consecutive(failures: tuple[FailureRecord, ...] | list[FailureRecord], cls: str) -> int:
    raise NotImplementedError
