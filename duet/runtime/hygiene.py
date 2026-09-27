"""Untrusted-text hygiene (D13, spec 9.2).

Peer messages, tool output and test logs are untrusted. Before they are
persisted, recognisable secrets are redacted (provider API keys, GitHub
and AWS credentials, private keys, bearer tokens, DUET's own participant
tokens). Before text is shown to a person, terminal control sequences are
stripped, so a message cannot rewrite the user's terminal or hide text.

Redaction is pattern-based and conservative: it catches the common
credential formats, not every secret, and is not a substitute for keeping
secrets out of the workspace (D03 already excludes sensitive files from
snapshots)."""
from __future__ import annotations

import re

SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("duet_token", re.compile(r"\bduet_pt_[A-Za-z0-9_\-]{16,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{20,}=*")),
    ("assignment", re.compile(r"(?i)\b([A-Z0-9_]*(?:API_KEY|SECRET|PASSWORD|AUTH_TOKEN|ACCESS_TOKEN))\s*[=:]\s*['\"]?[^\s'\"]{8,}")),
)

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def redact(text: str) -> str:
    """Replace recognisable secrets with a marker naming their kind."""
    if not text:
        return text
    for kind, pattern in SECRET_PATTERNS:
        if kind == "assignment":
            text = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED:{kind}]", text)
        else:
            text = pattern.sub(f"[REDACTED:{kind}]", text)
    return text


def for_display(text: str | None) -> str:
    """Terminal-safe: escape sequences and control characters removed
    (newlines and tabs kept)."""
    if not text:
        return text or ""
    return _CONTROL.sub("", _ANSI.sub("", text))
