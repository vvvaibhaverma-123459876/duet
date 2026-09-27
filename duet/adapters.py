from __future__ import annotations

import functools
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .providers.process import DEFAULT_MAX_STDERR, DEFAULT_MAX_STDOUT, run_bounded

SESSION_ID_PLACEHOLDER = "{session_id}"
WORKSPACE_PLACEHOLDER = "{workspace}"

# Claude Code >= 2.1.277 reports a resumed session's whole spend as the call's
# `total_cost_usd`; older versions report the call's own cost. Shared with
# duet.providers.claude_cli so both paths read the version the same way.
CUMULATIVE_RESUME_COST_SINCE = (2, 1, 277)
# cost_json_scope values. "auto" resolves per binary from `<cmd> --version`.
COST_SCOPES = ("call", "session_cumulative_on_resume", "auto")
# What "auto" resolves to when the version cannot be read: a resumed turn's
# reported cost may be cumulative or not, so it is unknown.
UNKNOWN_RESUME_SCOPE = "unknown_on_resume"
# Quota/rate-limit suspicion markers. Matching is a last-resort heuristic over
# a *failed* CLI's output, never a reading of the account balance. "billing"
# and a bare "429" were removed: billing blocks are not retryable quota (see
# BillingError) and a bare number matches line numbers and ids.
DEFAULT_QUOTA_MARKERS = ("rate limit", "rate-limit", "rate_limit", "quota", "usage limit", "too many requests")

_AUTH_PATTERNS = (
    r"not logged in",
    r"please run /login",
    r"please log ?in",
    r"login required",
    r"\bunauthori[sz]ed\b",
    r"invalid (?:api|x-api)[ -]?key",
    r"authentication (?:failed|error|required)",
    r"(?:status|http|error|code)[^0-9a-z]{0,3}401\b",
    r"codex login",
)
_BILLING_PATTERNS = (
    r"credit balance is too low",
    r"insufficient (?:credit|funds|balance)",
    r"payment required",
    r"(?:status|http|error|code)[^0-9a-z]{0,3}402\b",
    r"billing (?:issue|error|problem|hard limit|required)",
)
_MODEL_PATTERNS = (
    r"model[_ ]not[_ ]found",
    r"unknown model",
    r"invalid model",
    r"model .{0,80} (?:does not exist|is not available|not supported)",
    r"do(?:es)? not have access to (?:the )?model",
)
_RATE_STATUS_PATTERN = r"(?:status|http|error|code)[^0-9a-z]{0,3}429\b|\b429\s+too many"
_OVERLOAD_PATTERNS = (r"\boverloaded\b", r"(?:status|http|error|code)[^0-9a-z]{0,3}529\b", r"server is busy")


class AgentError(RuntimeError):
    """Raised when an agent CLI cannot produce a usable response.

    `kind` classifies the failure so policies can react precisely instead of
    treating every failure the same: error, not_found, timeout, output_limit,
    empty_output, auth, billing, model_unavailable, quota, rate_limit,
    overloaded."""

    kind = "error"
    retryable = False

    def __init__(self, message: str, kind: str | None = None) -> None:
        super().__init__(message)
        if kind is not None:
            self.kind = kind


class QuotaError(AgentError):
    """The CLI failed in a way that looks like an exhausted usage limit, a rate
    limit, or provider overload. Suspected, not confirmed: the detection reads
    error text. Retrying immediately would only burn more quota."""

    kind = "quota"
    retryable = True


class AuthError(AgentError):
    kind = "auth"


class BillingError(AgentError):
    """Billing blocks (no credit, payment required) are not retryable and must
    never trigger a paid fallback."""

    kind = "billing"


class ModelUnavailableError(AgentError):
    kind = "model_unavailable"


class AgentTimeoutError(AgentError):
    kind = "timeout"


class OutputLimitError(AgentError):
    kind = "output_limit"


@dataclass(frozen=True)
class AgentResult:
    text: str
    exit_code: int
    duration_s: float
    raw_stdout: str
    raw_stderr: str
    session_id: str | None = None
    # None means the agent reported no usable cost. Unknown is never zero.
    cost_usd: float | None = None


class Agent(Protocol):
    name: str
    display_name: str

    def send(self, prompt: str, workspace: Path) -> AgentResult:
        ...


@dataclass
class CLIAgent:
    name: str
    display_name: str
    command: list[str]
    prompt_via: str
    workspace_flag: str
    output_format: str
    timeout_seconds: int
    result_json_path: str = ""
    session_json_path: str = ""
    model: str = ""
    stdin_sentinel: str = "-"
    resume_command: list[str] = field(default_factory=list)
    session_id: str = ""
    chain_sessions: bool = False
    last_session_id: str = ""
    cost_json_path: str = ""
    quota_markers: list[str] = field(default_factory=lambda: list(DEFAULT_QUOTA_MARKERS))
    extra_env: dict[str, str] = field(default_factory=dict)
    max_output_bytes: int = DEFAULT_MAX_STDOUT
    max_stderr_bytes: int = DEFAULT_MAX_STDERR
    # Monotonic deadline set by the broker so a turn cannot outlive the
    # session's wallclock budget. None means only timeout_seconds applies.
    deadline: float | None = None
    # "call": the reported cost is this invocation's own spend.
    # "session_cumulative_on_resume": a resumed session reports the whole
    # session's spend so far (Claude Code >= 2.1.277); the turn's own cost is
    # the delta against the last value seen for that session, or unknown.
    # "auto": decided once per process from `<command> --version`: >= 2.1.277
    # is session_cumulative_on_resume, older is call, and when the version
    # cannot be read a resumed turn's cost is unknown.
    cost_json_scope: str = "call"
    _session_cost_seen: dict = field(default_factory=dict, repr=False)

    def git_identity_env(self) -> dict[str, str]:
        """Attribute commits the agent makes itself to the agent, not to whoever
        happens to own the shell. Set in the subprocess env; this is provenance
        by cooperation, since the agent can still override it."""
        email = f"{self.name}@duet.local"
        return {
            "GIT_AUTHOR_NAME": self.display_name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": self.display_name,
            "GIT_COMMITTER_EMAIL": email,
        }

    def build_command(self, prompt: str, workspace: Path) -> tuple[list[str], str | None]:
        template = self.resume_command if self.session_id and self.resume_command else self.command
        # Some CLIs only accept the workspace flag before a subcommand
        # (codex exec resume), so templates may place it via {workspace}
        # instead of relying on the appended workspace_flag.
        inline_workspace = any(WORKSPACE_PLACEHOLDER in part for part in template)
        cmd = [
            part.replace(SESSION_ID_PLACEHOLDER, self.session_id).replace(WORKSPACE_PLACEHOLDER, str(workspace))
            for part in template
        ]
        if self.model:
            cmd.extend(["-m", self.model])
        if self.workspace_flag and not inline_workspace:
            cmd.extend([self.workspace_flag, str(workspace)])

        stdin_data: str | None = None
        if self.prompt_via == "stdin":
            stdin_data = prompt
        elif self.prompt_via == "stdin-sentinel":
            cmd.append(self.stdin_sentinel)
            stdin_data = prompt
        elif self.prompt_via == "arg":
            cmd.append(prompt)
        else:
            raise AgentError(f"{self.name}: unsupported prompt_via={self.prompt_via!r}")
        return cmd, stdin_data

    def effective_timeout(self) -> float:
        """The configured per-turn timeout, clamped to the broker's deadline."""
        timeout = float(self.timeout_seconds)
        if self.deadline is not None:
            timeout = min(timeout, max(1.0, self.deadline - time.monotonic()))
        return timeout

    def send(self, prompt: str, workspace: Path) -> AgentResult:
        resumed_from = self.session_id if (self.session_id and self.resume_command) else None
        cmd, stdin_data = self.build_command(prompt, workspace)
        env = None
        if self.extra_env:
            env = os.environ.copy()
            env.update(self.extra_env)
        timeout = self.effective_timeout()
        try:
            proc = run_bounded(
                cmd,
                cwd=workspace,
                stdin_data=stdin_data,
                env=env,
                timeout=timeout,
                max_stdout=self.max_output_bytes,
                max_stderr=self.max_stderr_bytes,
            )
        except FileNotFoundError as exc:
            raise AgentError(
                f"{self.name}: executable not found: {cmd[0]}. Command: {_redacted_cmd(cmd)}", kind="not_found"
            ) from exc
        except PermissionError as exc:
            raise AgentError(f"{self.name}: cannot execute {cmd[0]}: {exc}", kind="not_found") from exc
        if proc.timed_out:
            raise AgentTimeoutError(
                f"{self.name}: timed out after {timeout:.0f}s; terminated its process tree. "
                f"Command: {_redacted_cmd(cmd)}. If this was Codex approval blocking, set non-interactive approval/sandbox flags in duet.toml."
            )
        stdout, stderr = proc.stdout, proc.stderr
        if proc.stdout_truncated and self.output_format == "json":
            raise OutputLimitError(
                f"{self.name}: output exceeded the {self.max_output_bytes}-byte capture limit "
                f"({proc.stdout_bytes} bytes); structured output was not parsed. Command: {_redacted_cmd(cmd)}"
            )
        text, session_id, cost_usd, warnings = self._parse_output(stdout)
        if proc.returncode != 0:
            detail = text or _tail(stderr) or _tail(stdout)
            raise classify_failure(self.name, proc.returncode, _redacted_cmd(cmd), detail, stderr, self.quota_markers)
        if not text.strip():
            raise AgentError(
                f"{self.name}: produced empty output. Command: {_redacted_cmd(cmd)}. stderr: {_tail(stderr)}",
                kind="empty_output",
            )
        if cost_usd is not None and self.cost_json_scope != "call":
            scope = detect_cost_scope(cmd[0]) if self.cost_json_scope == "auto" else self.cost_json_scope
            problem = ""
            if scope == "session_cumulative_on_resume":
                cost_usd, problem = self._own_cost(cost_usd, resumed_from, session_id)
            elif scope == UNKNOWN_RESUME_SCOPE and resumed_from is not None:
                # A new session's first figure is the call's own cost under
                # either reading; a resumed one may be cumulative or not.
                cost_usd, problem = None, (
                    f"could not read the {cmd[0]} version, so it is unknown whether the reported cost of a resumed "
                    "session is cumulative; this turn's own cost is unknown"
                )
            if problem:
                warnings.append(problem)
        if proc.stdout_truncated:
            warnings.append(f"output exceeded {self.max_output_bytes} bytes and was truncated at capture")
        if proc.output_incomplete:
            warnings.append("a descendant process kept the output pipe open; output may be incomplete")
        if warnings:
            text = f"{text.strip()}\n\n" + "\n".join(f"[Duet warning: {w}]" for w in warnings)
        if session_id:
            # Always remember the newest id so a later `duet resume` can
            # re-attach this conversation even from a non-chained run.
            self.last_session_id = session_id
            if self.chain_sessions:
                # Resumed sessions may return a fresh id on each turn; adopt it so
                # the next send() continues the same conversation, not a stale fork.
                self.session_id = session_id
        return AgentResult(
            text=text.strip(),
            exit_code=proc.returncode if proc.returncode is not None else -1,
            duration_s=proc.duration_s,
            raw_stdout=stdout,
            raw_stderr=stderr,
            session_id=session_id,
            cost_usd=cost_usd,
        )

    def _own_cost(self, reported: float, resumed_from: str | None, session_id: str | None) -> tuple[float | None, str]:
        """Turn a session-cumulative figure into this call's own cost."""
        problem = ""
        if resumed_from is None:
            own: float | None = reported  # a new session: everything so far is this call
        elif resumed_from in self._session_cost_seen:
            own = reported - self._session_cost_seen[resumed_from]
            if own < 0:
                own, problem = None, "session cost went backwards; this turn's cost is unknown"
        else:
            own = None
            problem = (
                f"resumed session {resumed_from} reports its cumulative spend (${reported:.4f}) and Duet has no "
                "earlier figure for it; this turn's own cost is unknown"
            )
        if session_id:
            self._session_cost_seen[session_id] = reported
        return own, problem

    def _parse_output(self, stdout: str) -> tuple[str, str | None, float | None, list[str]]:
        if self.output_format == "text":
            return stdout.strip(), None, None, []
        if self.output_format == "text-last-line":
            section = _extract_cli_speaker_section(stdout, self.name)
            if section:
                return section, None, None, []
            return _last_block(stdout), None, None, []
        if self.output_format == "json":
            try:
                payload = json.loads(stdout)
                text = _get_dotted(payload, self.result_json_path)
                session_id = _get_dotted(payload, self.session_json_path) if self.session_json_path else None
                if not isinstance(text, str):
                    raise AgentError(f"{self.name}: JSON path {self.result_json_path!r} did not resolve to text")
            except (json.JSONDecodeError, AgentError) as exc:
                return stdout.strip(), None, None, [f"expected JSON output but fell back to raw stdout: {exc}"]
            warnings: list[str] = []
            cost: float | None = None
            if self.cost_json_path:
                try:
                    raw_cost = _get_dotted(payload, self.cost_json_path)
                except AgentError:
                    raw_cost = None  # absent: unknown, not zero
                cost, problem = parse_cost(raw_cost)
                if problem:
                    warnings.append(problem)
            return text, str(session_id) if session_id is not None else None, cost, warnings
        raise AgentError(f"{self.name}: unsupported output_format={self.output_format!r}")


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def cumulative_resume_cost(version: tuple[int, ...] | None) -> bool:
    return bool(version and version >= CUMULATIVE_RESUME_COST_SINCE)


@functools.lru_cache(maxsize=None)
def detect_cost_scope(binary: str) -> str:
    """Resolve cost_json_scope="auto" for a Claude Code binary: runs
    `<binary> --version` once per process (per binary) and returns
    "session_cumulative_on_resume" (>= 2.1.277), "call" (older) or
    UNKNOWN_RESUME_SCOPE when no version could be read."""
    try:
        proc = run_bounded([binary, "--version"], timeout=30, max_stdout=64 * 1024, max_stderr=64 * 1024)
    except OSError:
        return UNKNOWN_RESUME_SCOPE
    version = parse_version(proc.stdout) if proc.returncode == 0 and not proc.timed_out else None
    if version is None:
        return UNKNOWN_RESUME_SCOPE
    return "session_cumulative_on_resume" if cumulative_resume_cost(version) else "call"


def parse_cost(value: object) -> tuple[float | None, str]:
    """Validate a provider-reported cost. Returns (cost or None, problem).
    Missing is unknown (None, no problem). Booleans, strings, negative and
    non-finite numbers are rejected as invalid: unknown, with a warning."""
    if value is None:
        return None, ""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"ignored non-numeric reported cost {value!r}; cost is unknown for this turn"
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None, f"ignored invalid reported cost {value!r}; cost is unknown for this turn"
    return number, ""


def classify_failure(
    name: str,
    returncode: int | None,
    command: str,
    detail: str,
    stderr: str,
    quota_markers: list[str] | tuple[str, ...] = DEFAULT_QUOTA_MARKERS,
) -> AgentError:
    """Map a failed CLI invocation to the most specific error class. Order
    matters: authentication and billing problems are checked before quota so
    that "log in again" or "add credit" is never mistaken for a retryable limit."""
    haystack = f"{detail}\n{stderr}".lower()
    base = f"{name}: exited {returncode}. Command: {command}. Output: {detail}"
    if _matches(haystack, _AUTH_PATTERNS):
        return AuthError(f"{base}\nAuthentication problem suspected; run `{name} login` or the CLI's auth command.")
    if _matches(haystack, _BILLING_PATTERNS):
        return BillingError(f"{base}\nBilling block suspected; Duet will not switch to paid usage on its own.")
    if _matches(haystack, _MODEL_PATTERNS):
        return ModelUnavailableError(f"{base}\nThe configured model appears unavailable to this account.")
    if any(marker.lower() in haystack for marker in quota_markers) or re.search(_RATE_STATUS_PATTERN, haystack):
        kind = "rate_limit" if re.search(r"rate[ _-]?limit|too many requests|" + _RATE_STATUS_PATTERN, haystack) else "quota"
        return QuotaError(f"{name}: exited {returncode}. quota/rate-limit suspected. Command: {command}. Output: {detail}", kind=kind)
    if _matches(haystack, _OVERLOAD_PATTERNS):
        return QuotaError(f"{name}: exited {returncode}. provider overload suspected. Command: {command}. Output: {detail}", kind="overloaded")
    return AgentError(base)


def _matches(haystack: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, haystack) for pattern in patterns)


def _get_dotted(payload: object, path: str) -> object:
    cur = payload
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise AgentError(f"missing JSON path {path!r}")
    return cur


def _extract_cli_speaker_section(stdout: str, speaker: str) -> str:
    lines = stdout.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip().lower() == speaker.lower()]
    if not starts:
        return ""
    start = starts[-1] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].strip().lower() == "tokens used":
            end = index
            break
    section = "\n".join(lines[start:end]).strip()
    if section:
        return section
    return ""


def _last_block(stdout: str) -> str:
    """The final contiguous run of non-empty lines. Used when no speaker
    section is found; never the whole stdout, which may echo the prompt."""
    block: list[str] = []
    for line in reversed(stdout.splitlines()):
        if line.strip():
            block.append(line.rstrip())
        elif block:
            break
    return "\n".join(reversed(block)).strip()


def _redacted_cmd(cmd: list[str]) -> str:
    redacted = []
    secret_next = False
    for part in cmd:
        if secret_next:
            redacted.append("<redacted>")
            secret_next = False
            continue
        if len(part) > 400:
            # Prompts passed as an argument can be huge and may carry context
            # that does not belong in error messages or logs.
            redacted.append(f"<{len(part)} chars>")
            continue
        redacted.append(part)
        if part.lower() in {"--api-key", "--token", "--auth-token"}:
            secret_next = True
    return " ".join(redacted)


def _tail(text: str, limit: int = 1200) -> str:
    stripped = text.strip()
    return stripped[-limit:] if len(stripped) > limit else stripped


def _looks_like_quota(text: str, markers: tuple[str, ...] | list[str] = DEFAULT_QUOTA_MARKERS) -> bool:
    """Compatibility helper: True when `text` carries a quota/rate-limit marker."""
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers) or bool(re.search(_RATE_STATUS_PATTERN, lowered))
