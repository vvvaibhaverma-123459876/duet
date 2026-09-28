"""Claude Code CLI adapter (managed peer path, spec 6.3).

Runs `claude -p --output-format stream-json --verbose` with the prompt on
stdin, using the user's existing Claude Code login. `--bare` is never used:
it switches authentication to ANTHROPIC_API_KEY, i.e. to paid API billing,
which Duet must not do on its own (R13).

Behaviour taken from the installed CLI's help (fixture
tests/fixtures/provider_protocols/claude/2.1.283-help.txt) and the Claude Code
docs for programmatic use and cost tracking:
- stream-json emits `system/init` (session_id, model, permissionMode,
  capabilities), `system/api_retry` (typed error categories), assistant/user
  messages, and a final `result` (subtype, is_error, result, session_id,
  total_cost_usd, usage, modelUsage, permission_denials);
- a resumed session's `total_cost_usd`/`modelUsage` include the session's
  earlier spend (Claude Code >= 2.1.277), so they are *session cumulative*,
  not this call's cost; `--max-budget-usd` caps only the call's own spend;
- costs are client-side estimates, not bills;
- SIGINT ends the current turn cleanly; SIGTERM leaves it unfinished.

In non-interactive mode Claude Code prefers ANTHROPIC_API_KEY (API billing)
over the user's login when it is set. The child environment therefore never
carries provider credential variables (process.PROVIDER_CREDENTIAL_ENV) unless
the adapter is constructed with `allow_api_key_env=True`, an explicit opt-in
for a user who deliberately wants API billing; nothing in DUET sets it. The
removed names (never their values) are reported in the turn's warnings."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time

from .. import oscompat
from ..adapters import (
    CUMULATIVE_RESUME_COST_SINCE,
    AgentError,
    AgentTimeoutError,
    AuthError,
    BillingError,
    ModelUnavailableError,
    OutputLimitError,
    QuotaError,
    classify_failure,
    cumulative_resume_cost,
    parse_version,
)
from ..runtime.contracts import Control, UsageCapability
from .base import (
    EventCallback,
    ProviderCapabilities,
    SettingsRecord,
    TurnRequest,
    TurnResult,
    UnsupportedSetting,
    UsageObservation,
    decimal_or_none,
    int_or_none,
    lineage_for,
)
from .process import credential_env_warning, provider_child_env, stream_process

RETRY_CATEGORY_ERRORS = {
    "authentication_failed": AuthError,
    "oauth_org_not_allowed": AuthError,
    "cloud_credential_error": AuthError,
    "account_on_hold": BillingError,
    "billing_error": BillingError,
    "model_not_found": ModelUnavailableError,
}
RETRY_CATEGORY_QUOTA = {"rate_limit": "rate_limit", "overloaded": "overloaded"}


def help_flags(help_text: str) -> frozenset[str]:
    return frozenset(re.findall(r"(--[a-z][a-z0-9-]+)", help_text or ""))


def discover_from_help(help_text: str, version_text: str) -> ProviderCapabilities:
    """Derive capabilities from the installed CLI's own help and version
    output. Nothing is assumed from the version number alone except the two
    behaviour changes the docs date by version."""
    version = parse_version(version_text)
    flags = help_flags(help_text)
    text = " ".join((help_text or "").split())
    efforts = None
    match = re.search(r"--effort <level>.*?\(([a-z, ]+)\)", text)
    if match:
        efforts = tuple(part.strip() for part in match.group(1).split(",") if part.strip())
    notes = []
    if cumulative_resume_cost(version):
        notes.append("resumed sessions report session-cumulative cost")
    if "--permission-prompts" not in flags:
        notes.append("no --permission-prompts: a permission request could wait for a host")
    return ProviderCapabilities(
        provider="claude",
        protocol="claude-cli-stream-json/1",
        version=".".join(map(str, version)) if version else None,
        streaming="stream-json" in text,
        session_resume="--resume" in flags,
        session_fork="--fork-session" in flags,
        session_id_preassign="--session-id" in flags,
        cancel="interrupt",
        model_control=Control.SUPPORTED if "--model" in flags else Control.UNSUPPORTED,
        effort_control=Control.SUPPORTED if efforts else Control.UNSUPPORTED,
        usage=UsageCapability.PARTIAL,  # per-call estimates and tokens; no quota windows
        efforts=efforts,
        models=None,  # the CLI exposes no model catalogue
        provider_budget_cap="--max-budget-usd" in flags,
        notes=tuple(notes),
    )


class ClaudeCLIAdapter:
    name = "claude"

    def __init__(
        self,
        binary: str = "claude",
        *,
        allowed_tools: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        help_text: str | None = None,
        version_text: str | None = None,
        max_line_bytes: int = 4 * 1024 * 1024,
        max_total_bytes: int = 64 * 1024 * 1024,
        allow_api_key_env: bool = False,
    ) -> None:
        """`allow_api_key_env=True` keeps provider API-key/cloud credential
        variables in the child environment, i.e. lets Claude Code bill the API
        instead of the user's login. Off by default (R13: no silent paid
        fallback); only a user who explicitly wants API billing should set it."""
        self.binary = binary
        self.allow_api_key_env = allow_api_key_env
        self.allowed_tools = tuple(allowed_tools)
        self.env = env
        self._help_text = help_text
        self._version_text = version_text
        self._caps: ProviderCapabilities | None = None
        self._flags: frozenset[str] = frozenset()
        self.max_line_bytes = max_line_bytes
        self.max_total_bytes = max_total_bytes

    def capabilities(self) -> ProviderCapabilities:
        if self._caps is None:
            if self._help_text is None or self._version_text is None:
                self._version_text = subprocess.run([self.binary, "--version"], capture_output=True, text=True, timeout=30).stdout
                self._help_text = subprocess.run([self.binary, "--help"], capture_output=True, text=True, timeout=30).stdout
            self._caps = discover_from_help(self._help_text, self._version_text)
            self._flags = help_flags(self._help_text)
        return self._caps

    def supports(self, flag: str) -> bool:
        self.capabilities()
        return flag in self._flags

    @property
    def cumulative_resume_cost(self) -> bool:
        return cumulative_resume_cost(parse_version(self.capabilities().version or ""))

    def build_command(self, request: TurnRequest) -> tuple[list[str], dict, dict]:
        caps = self.capabilities()
        cmd = [self.binary, "-p", "--output-format", "stream-json", "--verbose"]
        requested = {
            "model": request.model, "effort": request.effort, "permission_profile": request.permission_profile,
            "resume": request.session_id, "fork": request.fork, "new_session_id": request.new_session_id,
        }
        accepted: dict = {}
        if request.model:
            if caps.model_control == Control.UNSUPPORTED:
                raise UnsupportedSetting("this Claude Code version has no --model flag")
            cmd += ["--model", request.model]
            accepted["model"] = request.model
        if request.effort:
            if not caps.efforts or request.effort not in caps.efforts:
                raise UnsupportedSetting(f"effort {request.effort!r} is not supported; this Claude Code accepts {caps.efforts}")
            cmd += ["--effort", request.effort]
            accepted["effort"] = request.effort
        if request.permission_profile == "read_only":
            # dontAsk: reads within the working dirs still run; anything that
            # would prompt is denied unless it is in the allowlist below.
            cmd += ["--permission-mode", "dontAsk"]
            accepted["permission_mode"] = "dontAsk"
        else:
            cmd += ["--permission-mode", "acceptEdits"]
            accepted["permission_mode"] = "acceptEdits"
        if self.allowed_tools:
            # The allowlist names tools that need no prompt in either
            # profile (DUET's own MCP tools for a managed peer). Write tools
            # must not be listed for a read-only session.
            cmd += ["--allowed-tools", ",".join(self.allowed_tools)]
            accepted["allowed_tools"] = list(self.allowed_tools)
        if self.supports("--permission-prompts"):
            # Nobody answers prompts in a managed turn: deny instead of waiting.
            cmd += ["--permission-prompts", "none"]
            accepted["permission_prompts"] = "none"
        if request.session_id:
            if not caps.session_resume:
                raise UnsupportedSetting("this Claude Code version cannot resume sessions")
            cmd += ["--resume", request.session_id]
            accepted["resume"] = request.session_id
            if request.fork:
                if not caps.session_fork:
                    raise UnsupportedSetting("this Claude Code version cannot fork sessions")
                cmd.append("--fork-session")
                accepted["fork"] = True
        elif request.new_session_id:
            if not caps.session_id_preassign:
                raise UnsupportedSetting("this Claude Code version cannot pre-assign a session id")
            cmd += ["--session-id", request.new_session_id]
            accepted["session_id"] = request.new_session_id
        if request.extra_instructions:
            cmd += ["--append-system-prompt", request.extra_instructions]
            accepted["append_system_prompt"] = True
        if request.mcp_config is not None:
            cmd += ["--mcp-config", json.dumps(request.mcp_config), "--strict-mcp-config"]
            accepted["mcp_servers"] = sorted((request.mcp_config.get("mcpServers") or {}).keys())
        if request.max_budget_usd is not None:
            if not caps.provider_budget_cap:
                raise UnsupportedSetting("this Claude Code version has no provider-enforced budget cap")
            cmd += ["--max-budget-usd", str(request.max_budget_usd)]
            accepted["max_budget_usd"] = str(request.max_budget_usd)
        if oscompat.is_batch_launcher(shutil.which(self.binary) or self.binary):
            cmd = _batch_safe(cmd)
            accepted["batch_launcher"] = True
        return cmd, requested, accepted

    def run_turn(self, request: TurnRequest, *, on_event: EventCallback | None = None, cancel: threading.Event | None = None) -> TurnResult:
        caps = self.capabilities()
        cmd, requested, accepted = self.build_command(request)
        state = _StreamState()

        def on_line(line: str) -> None:
            state.feed(line)
            if on_event and state.last_event is not None:
                on_event(state.last_event)

        env, removed_env = provider_child_env(self.env, request.env, allow_api_key_env=self.allow_api_key_env)
        started = time.monotonic()
        try:
            proc = stream_process(
                cmd,
                on_line=on_line,
                cwd=request.cwd,
                env=env,
                stdin_data=_batch_prompt(request) if accepted.get("batch_launcher") else request.prompt,
                timeout=request.timeout_seconds,
                cancel_event=cancel,
                max_line_bytes=self.max_line_bytes,
                max_total_bytes=self.max_total_bytes,
                interrupt_first=True,
            )
        except FileNotFoundError as exc:
            raise AgentError(f"claude: executable not found: {self.binary}", kind="not_found") from exc
        observed = {k: v for k, v in (("model", state.init.get("model")), ("permission_mode", state.init.get("permissionMode")), ("session_id", state.session_id)) if v}
        settings = SettingsRecord(requested, accepted, observed)
        warnings = list(state.warnings)
        if removed_env:
            warnings.append(credential_env_warning(removed_env))
        if proc.truncated_lines:
            warnings.append(f"{proc.truncated_lines} over-long stream line(s) were truncated")
        usage = self._usage(state, request, self.cumulative_resume_cost)
        common = dict(
            session_id=state.session_id,
            lineage=lineage_for(request.session_id, request.fork, state.session_id),
            settings=settings,
            usage=usage,
            duration_s=time.monotonic() - started,
            exit_code=proc.returncode,
            permission_denials=tuple(state.result.get("permission_denials") or ()) if state.result else (),
            provider_invocation_id=state.session_id,
            events=state.events,
        )
        text = (state.result or {}).get("result") if isinstance((state.result or {}).get("result"), str) else state.last_text
        if proc.over_limit:
            error = OutputLimitError(f"claude: stream exceeded {self.max_total_bytes} bytes; remaining events were not parsed")
            return TurnResult(status="failed", text=text or "", error=error, warnings=tuple(warnings), **common)
        if proc.timed_out:
            return TurnResult(status="timeout", text=text or "", error=AgentTimeoutError(f"claude: turn timed out after {request.timeout_seconds:.0f}s; interrupted"), warnings=tuple(warnings), **common)
        if proc.cancelled:
            return TurnResult(status="cancelled", text=text or "", warnings=tuple(warnings), **common)
        result = state.result
        if result is not None and not result.get("is_error") and result.get("subtype") == "success" and proc.returncode == 0:
            return TurnResult(status="completed", text=(text or "").strip(), warnings=tuple(warnings), **common)
        error = self._classify(state, proc.returncode, proc.stderr)
        return TurnResult(status="failed", text=text or "", error=error, warnings=tuple(warnings), **common)

    @staticmethod
    def _classify(state: "_StreamState", returncode: int | None, stderr: str) -> AgentError:
        category = state.retry_category
        detail = ""
        if state.result:
            detail = str(state.result.get("result") or state.result.get("subtype") or "")
            if state.result.get("subtype") == "error_max_budget_usd":
                return AgentError("claude: the provider-enforced budget cap for this call was reached", kind="provider_budget_cap")
        if category in RETRY_CATEGORY_ERRORS:
            return RETRY_CATEGORY_ERRORS[category](f"claude: {category}: {detail or stderr[-400:]}")
        if category in RETRY_CATEGORY_QUOTA:
            return QuotaError(f"claude: {category}: {detail or stderr[-400:]}", kind=RETRY_CATEGORY_QUOTA[category])
        return classify_failure("claude", returncode, "claude -p", detail or state.last_text or "", stderr)

    @staticmethod
    def _usage(state: "_StreamState", request: TurnRequest, cumulative_on_resume: bool) -> tuple[UsageObservation, ...]:
        result = state.result
        if not result:
            return ()
        scope = "session_cumulative" if (request.session_id is not None and cumulative_on_resume) else "call"
        crashed = result.get("subtype") == "error_during_execution"
        out: list[UsageObservation] = []
        cost = decimal_or_none(result.get("total_cost_usd"))
        if cost is not None and not (crashed and cost == 0):
            out.append(UsageObservation("cost_usd", cost, scope, "claude.result.total_cost_usd", quality="estimated", unit="USD"))
        for model, entry in (result.get("modelUsage") or {}).items():
            if not isinstance(entry, dict):
                continue
            for field_name, metric in (
                ("inputTokens", "tokens.input"), ("outputTokens", "tokens.output"),
                ("cacheReadInputTokens", "tokens.cache_read"), ("cacheCreationInputTokens", "tokens.cache_write"),
            ):
                value = int_or_none(entry.get(field_name))
                if value is not None and not (crashed and value == 0):
                    out.append(UsageObservation(metric, value, scope, "claude.result.modelUsage", unit="tokens", key=str(model)))
            model_cost = decimal_or_none(entry.get("costUSD"))
            if model_cost is not None and not (crashed and model_cost == 0):
                out.append(UsageObservation("cost_usd", model_cost, scope, "claude.result.modelUsage", quality="estimated", unit="USD", key=str(model)))
        return tuple(out)


class _StreamState:
    """Incremental stream-json parser. Unknown event types and fields are
    tolerated; malformed lines are counted, never fatal on their own."""

    def __init__(self) -> None:
        self.init: dict = {}
        self.result: dict | None = None
        self.session_id: str | None = None
        self.retry_category: str | None = None
        self.last_text = ""
        self.events = 0
        self.malformed = 0
        self.last_event: dict | None = None
        self.warnings: list[str] = []

    def feed(self, line: str) -> None:
        self.last_event = None
        stripped = line.strip()
        if not stripped:
            return
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            self.malformed += 1
            if self.malformed == 1:
                self.warnings.append("claude emitted non-JSON output on the event stream")
            return
        if not isinstance(event, dict):
            self.malformed += 1
            return
        self.events += 1
        self.last_event = event
        kind, subtype = event.get("type"), event.get("subtype")
        if isinstance(event.get("session_id"), str):
            self.session_id = event["session_id"]
        if kind == "system" and subtype == "init":
            self.init = event
        elif kind == "system" and subtype == "api_retry":
            if isinstance(event.get("error"), str):
                self.retry_category = event["error"]
        elif kind == "assistant" and not event.get("parent_tool_use_id"):
            message = event.get("message") or {}
            texts = [block.get("text", "") for block in message.get("content") or [] if isinstance(block, dict) and block.get("type") == "text"]
            if any(texts):
                self.last_text = "\n".join(t for t in texts if t)
        elif kind == "result":
            self.result = event



# parse_version, cumulative_resume_cost and CUMULATIVE_RESUME_COST_SINCE live in
# duet.adapters (shared with the legacy CLI agent's cost_json_scope = "auto").
__all__ = ["CUMULATIVE_RESUME_COST_SINCE", "ClaudeCLIAdapter", "cumulative_resume_cost", "discover_from_help", "help_flags", "parse_version"]


# cmd.exe re-parses the arguments of a .cmd/.bat launcher (npm installs
# `claude.cmd`): %VAR% expands and & | < > ^ " change meaning. Free text never
# goes there as an argument: extra instructions move to stdin and the MCP
# config to a file, and anything else with such characters is refused.
_BATCH_UNSAFE = set('%!^&|<>"\r\n')


def _batch_safe(cmd: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for index, arg in enumerate(cmd):
        if skip:
            skip = False
            continue
        if arg == "--append-system-prompt":
            skip = True  # delivered on stdin instead (_batch_prompt)
            continue
        if arg == "--mcp-config" and index + 1 < len(cmd):
            out += [arg, _config_file(cmd[index + 1])]
            skip = True
            continue
        if index and _BATCH_UNSAFE & set(arg):
            raise UnsupportedSetting(
                f"cannot pass {arg[:40]!r} safely to a .cmd launcher; install Claude Code's native claude.exe"
            )
        out.append(arg)
    return out


def _batch_prompt(request: "TurnRequest") -> str:
    if not request.extra_instructions:
        return request.prompt
    return f"<instructions>\n{request.extra_instructions}\n</instructions>\n\n{request.prompt}"


def _config_file(text: str) -> str:
    """Write an inline MCP config to a private file and return its path (the
    config names a token file, never the token itself)."""
    from ..runtime.paths import ensure_private_dir, state_dir

    folder = ensure_private_dir(state_dir() / "tmp")
    path = folder / f"mcp-{hashlib.sha256(text.encode()).hexdigest()[:16]}.json"
    path.write_text(text, encoding="utf-8")
    return str(path)
