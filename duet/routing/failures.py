"""Failure classification (spec 8.4): what kind of help a failure needs.

- environment: a tool, module, file or permission is missing: repair the
  environment; a stronger model would not help (AT19).
- requirements: the task is unclear: state an assumption or ask.
- hypothesis: the code ran and was wrong (assertions, failing tests,
  changes requested): more reasoning or a different approach may help.
- provider: quota, rate limits, auth, billing, overload: admission (D08)
  owns these; routing ignores them.
- timeout, unknown.

Classes are tried in the order provider, environment, timeout,
requirements, hypothesis: a ModuleNotFoundError inside pytest output is an
environment failure, not a wrong hypothesis."""
from __future__ import annotations

import re

from .contracts import FAILURE_CLASSES, FailureRecord, Reason

_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("provider", re.compile(r"rate.?limit|usage limit|quota|overloaded|billing|payment required|not logged in|authenticat|unauthori[sz]ed|model.{0,20}(not found|unavailable)", re.I)),
    ("environment", re.compile(
        r"command not found|: not found\b|No such file or directory|ModuleNotFoundError|ImportError|No module named|cannot find package|"
        r"Permission denied|Network is unreachable|Connection refused|Name or service not known|Temporary failure in name resolution|"
        r"No space left on device|exit code 12[67]\b", re.I)),
    ("timeout", re.compile(r"timed out|timeout expired|deadline exceeded", re.I)),
    ("requirements", re.compile(r"ambiguous|unclear requirement|requirement is unclear|not specified|which of (these|the)|clarif", re.I)),
    ("hypothesis", re.compile(r"AssertionError|assert |FAILED|failures?=|expected .{0,80} got|Traceback|changes requested|result rejected|Error", re.I)),
)
_PROVIDER_KINDS = {"quota", "rate_limit", "overloaded", "auth", "billing", "model_unavailable"}


def classify(record: FailureRecord) -> tuple[str, Reason]:
    if record.kind in _PROVIDER_KINDS:
        return "provider", Reason("failure:provider", f"{record.source} failed: {record.kind}")
    if record.kind == "timeout":
        return "timeout", Reason("failure:timeout", f"{record.source} timed out")
    text = record.text or ""
    for cls, pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            return cls, Reason(f"failure:{cls}", f"{record.source}: {match.group(0).strip()[:80]}")
    if record.source == "review":
        return "hypothesis", Reason("failure:hypothesis", "a review requested changes")
    return "unknown", Reason("failure:unknown", f"{record.source} failed without a recognisable cause")


def summarise(failures) -> dict[str, int]:
    counts = {cls: 0 for cls in FAILURE_CLASSES}
    for record in failures:
        counts[classify(record)[0]] += 1
    return counts


def consecutive(failures, cls: str) -> int:
    count = 0
    for record in reversed(list(failures)):
        if classify(record)[0] != cls:
            break
        count += 1
    return count
