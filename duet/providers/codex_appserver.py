"""Codex App Server adapter (managed Codex peers, spec 6.2).

Implements the subset of the app-server v2 protocol Duet needs, built against
the schema generated from the installed CLI (`codex app-server
generate-json-schema`, codex-cli 0.157.1; subset in
tests/fixtures/provider_protocols/codex/0.157.1/schema-subset.json):

  initialize / initialized, model/list (paginated), account/read,
  account/rateLimits/read, thread/start, thread/resume, thread/fork,
  turn/start, turn/interrupt
  notifications: turn/started, turn/completed, item/completed,
  item/agentMessage/delta, thread/tokenUsage/updated, error,
  account/rateLimits/updated (unknown notifications are ignored)
  server requests: approval / user-input requests are declined by default.

Threads persist across turns in one app-server process. Model and effort are
applied per turn (turn/start) after validation against model/list: an effort
the chosen model does not advertise is refused before dispatch, not sent and
hoped for. Codex reports tokens and rate-limit windows but no cost.

The app-server's environment is fixed when the process starts, so provider
credential variables (OPENAI_API_KEY, CODEX_API_KEY, ...; see
process.PROVIDER_CREDENTIAL_ENV) are removed there: Codex runs on the user's
login, never on API billing, unless the adapter is constructed with
`allow_api_key_env=True` (explicit opt-in; DUET never sets it). The removed
names, never values, are reported in every turn's warnings."""
from __future__ import annotations

import re
import threading
import time
from decimal import Decimal

from ..adapters import AgentError, AgentTimeoutError, classify_failure
from ..runtime.contracts import Control, UsageCapability
from .base import (
    EventCallback,
    ProviderCapabilities,
    SettingsRecord,
    TurnRequest,
    TurnResult,
    UnsupportedSetting,
    UsageObservation,
    int_or_none,
    lineage_for,
)
from .jsonrpc import JsonRpcError, JsonRpcProcess, ProcessGone
from .process import credential_env_warning, provider_child_env

CLIENT_NAME = "duet"
INTERRUPT_GRACE_SECONDS = 10.0
SANDBOX_FOR_PROFILE = {"workspace_write": "workspace-write", "read_only": "read-only"}
DECLINES = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
    "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
    "item/tool/requestUserInput": {"answers": {}},
}


class CodexAppServerAdapter:
    name = "codex"

    def __init__(
        self,
        binary: str = "codex",
        *,
        config_overrides: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        client_version: str = "0.1.0",
        request_timeout: float = 60.0,
        allow_api_key_env: bool = False,
    ) -> None:
        """`allow_api_key_env=True` keeps provider API keys in the app-server's
        environment (API billing); off by default, never set by DUET."""
        self.binary = binary
        self.allow_api_key_env = allow_api_key_env
        self.removed_env: tuple[str, ...] = ()
        self.config_overrides = tuple(config_overrides)
        self.env = env
        self.client_version = client_version
        self.request_timeout = request_timeout
        self._rpc: JsonRpcProcess | None = None
        self._init: dict = {}
        self._models: list[dict] | None = None
        self._collector: _TurnCollector | None = None
        self.declined_requests: list[str] = []

    # -- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        if self._rpc is not None and self._rpc.alive():
            return
        argv = [self.binary]
        for override in self.config_overrides:
            argv += ["-c", override]
        argv.append("app-server")
        env, self.removed_env = provider_child_env(self.env, allow_api_key_env=self.allow_api_key_env)
        try:
            self._rpc = JsonRpcProcess(argv, env=env, on_notification=self._on_notification, on_server_request=self._on_server_request)
        except FileNotFoundError as exc:
            raise AgentError(f"codex: executable not found: {self.binary}", kind="not_found") from exc
        self._init = self._rpc.request(
            "initialize",
            {"clientInfo": {"name": CLIENT_NAME, "version": self.client_version}, "capabilities": {"experimentalApi": False}},
            timeout=self.request_timeout,
        )
        self._rpc.notify("initialized")
        self._models = None

    def close(self) -> None:
        if self._rpc is not None:
            self._rpc.close()
            self._rpc = None

    @property
    def rpc(self) -> JsonRpcProcess:
        self.start()
        assert self._rpc is not None
        return self._rpc

    # -- discovery ----------------------------------------------------------------------

    def version(self) -> str | None:
        self.start()
        match = re.match(r"[^/]+/(\d+\.\d+\.\d+)", str(self._init.get("userAgent", "")))
        return match.group(1) if match else None

    def models(self) -> list[dict]:
        """The full model catalogue, following pagination."""
        if self._models is None:
            models: list[dict] = []
            cursor = None
            for _ in range(50):  # bounded pagination
                params: dict = {"limit": 50}
                if cursor:
                    params["cursor"] = cursor
                page = self.rpc.request("model/list", params, timeout=self.request_timeout)
                models += [m for m in page.get("data") or [] if isinstance(m, dict)]
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            self._models = models
        return self._models

    def capabilities(self) -> ProviderCapabilities:
        models = self.models()
        efforts = sorted({e.get("reasoningEffort") for m in models for e in m.get("supportedReasoningEfforts") or [] if isinstance(e, dict) and e.get("reasoningEffort")})
        return ProviderCapabilities(
            provider="codex",
            protocol="codex-app-server/v2",
            version=self.version(),
            streaming=True,
            session_resume=True,
            session_fork=True,
            session_id_preassign=False,
            cancel="interrupt",
            model_control=Control.SUPPORTED,
            effort_control=Control.SUPPORTED,
            usage=UsageCapability.STRUCTURED,
            efforts=tuple(efforts) or None,
            models=tuple(m.get("id") for m in models if m.get("id") and not m.get("hidden")),
            provider_budget_cap=False,
            notes=("no cost reporting; tokens and rate-limit windows only",),
        )

    def account(self) -> dict:
        return self.rpc.request("account/read", {}, timeout=self.request_timeout)

    def read_rate_limits(self) -> tuple[tuple[UsageObservation, ...], str | None]:
        """Structured quota windows, or (), reason when unavailable (for
        example an unauthenticated account). Unavailable is not zero."""
        try:
            result = self.rpc.request("account/rateLimits/read", None, timeout=self.request_timeout)
        except JsonRpcError as exc:
            return (), exc.message
        return _rate_limit_observations(result.get("rateLimits") or {}, "codex.account.rateLimits"), None

    def check_settings(self, model: str | None, effort: str | None) -> None:
        if model is None and effort is None:
            return
        catalogue = {m.get("id"): m for m in self.models()}
        chosen = None
        if model is not None:
            chosen = catalogue.get(model)
            if chosen is None:
                raise UnsupportedSetting(f"model {model!r} is not in this account's model list")
        else:
            chosen = next((m for m in catalogue.values() if m.get("isDefault")), None)
        if effort is not None:
            supported = [e.get("reasoningEffort") for e in (chosen or {}).get("supportedReasoningEfforts") or []]
            if effort not in supported:
                raise UnsupportedSetting(f"effort {effort!r} is not supported by {(chosen or {}).get('id', 'the default model')}; supported: {supported}")

    # -- turns ---------------------------------------------------------------------------

    def run_turn(self, request: TurnRequest, *, on_event: EventCallback | None = None, cancel: threading.Event | None = None) -> TurnResult:
        requested = {
            "model": request.model, "effort": request.effort, "permission_profile": request.permission_profile,
            "resume": request.session_id, "fork": request.fork,
        }
        if request.new_session_id:
            raise UnsupportedSetting("codex app-server assigns thread ids itself")
        if request.max_budget_usd is not None:
            raise UnsupportedSetting("codex enforces no per-call spend cap")
        thread_config = codex_mcp_config(request.mcp_config) if request.mcp_config is not None else None
        accepted: dict = {}
        self.check_settings(request.model, request.effort)
        started = time.monotonic()
        rpc = self.rpc
        sandbox = SANDBOX_FOR_PROFILE[request.permission_profile]
        common = {"cwd": str(request.cwd), "approvalPolicy": "never", "sandbox": sandbox}
        if request.extra_instructions:
            common["developerInstructions"] = request.extra_instructions
        if thread_config is not None:
            # Observed on 0.157.1: thread/start|resume|fork `config.mcp_servers`
            # launches the server for that thread (with its `env`). Unknown keys
            # are accepted silently, so acceptance is not proof of effect.
            common["config"] = thread_config
            accepted["mcp_servers"] = sorted(thread_config["mcp_servers"])
        try:
            if request.session_id and request.fork:
                opened = rpc.request("thread/fork", {"threadId": request.session_id, **common}, timeout=self.request_timeout)
            elif request.session_id:
                opened = rpc.request("thread/resume", {"threadId": request.session_id, **common}, timeout=self.request_timeout)
            else:
                opened = rpc.request("thread/start", common, timeout=self.request_timeout)
        except JsonRpcError as exc:
            raise classify_failure("codex", None, "thread open", exc.message, "") from exc
        accepted.update(approval_policy="never", sandbox=sandbox)
        thread = opened.get("thread") or {}
        thread_id = thread.get("id")
        observed = {"session_id": thread_id}
        if opened.get("model"):
            observed["model"] = opened["model"]
        if opened.get("reasoningEffort"):
            observed["effort"] = opened["reasoningEffort"]
        params: dict = {"threadId": thread_id, "input": [{"type": "text", "text": request.prompt}]}
        if request.model:
            params["model"] = request.model
            accepted["model"] = request.model
        if request.effort:
            params["effort"] = request.effort
            accepted["effort"] = request.effort
        # The collector is installed before turn/start is sent: the server may
        # emit the turn's notifications (a fast `error` + `turn/completed`)
        # before it answers the request. Until the turn id is known, events for
        # this thread are accepted; the id is then bound from the response (or
        # from turn/started, whichever arrives first).
        collector = _TurnCollector(thread_id, None, on_event)
        self._collector = collector
        status = "completed"
        interrupted_by_us = False
        try:
            try:
                started_turn = rpc.request("turn/start", params, timeout=self.request_timeout)
            except JsonRpcError as exc:
                raise classify_failure("codex", None, "turn/start", exc.message, "") from exc
            collector.bind_turn((started_turn.get("turn") or {}).get("id"))
            deadline = started + request.timeout_seconds
            try:
                while collector.completed is None:
                    if cancel is not None and cancel.is_set() or time.monotonic() >= deadline:
                        status = "timeout" if time.monotonic() >= deadline else "cancelled"
                        interrupted_by_us = True
                        self._interrupt(thread_id, collector.turn_id, collector)
                        break
                    rpc.pump(timeout=0.5, stop=lambda: collector.completed is not None)
            except ProcessGone as exc:
                self._rpc = None
                error = AgentError(f"codex: app-server exited during the turn: {exc}", kind="error")
                return self._result("failed", collector, requested, accepted, observed, request, started, error)
        finally:
            self._collector = None
        if interrupted_by_us:
            error = AgentTimeoutError(f"codex: turn timed out after {request.timeout_seconds:.0f}s; interrupted") if status == "timeout" else None
            return self._result(status, collector, requested, accepted, observed, request, started, error)
        turn = collector.completed or {}
        turn_status = turn.get("status")
        if turn_status == "completed":
            return self._result("completed", collector, requested, accepted, observed, request, started, None)
        if turn_status == "interrupted":
            return self._result("interrupted", collector, requested, accepted, observed, request, started, None)
        message = ((turn.get("error") or {}).get("message")) or collector.last_error or "turn failed"
        return self._result("failed", collector, requested, accepted, observed, request, started, classify_failure("codex", None, "turn", message, ""))

    def _interrupt(self, thread_id: str, turn_id: str | None, collector: "_TurnCollector") -> None:
        try:
            if turn_id:
                self.rpc.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=self.request_timeout)
            self.rpc.pump(timeout=INTERRUPT_GRACE_SECONDS, stop=lambda: collector.completed is not None)
        except (JsonRpcError, TimeoutError, ProcessGone):
            pass
        if collector.completed is None:
            # The turn did not acknowledge the interrupt: stop the server; its
            # effects are unknown and the caller must reconcile, not retry.
            self.close()

    def _result(self, status, collector, requested, accepted, observed, request, started, error) -> TurnResult:
        usage = list(collector.usage)
        warnings = list(collector.warnings)
        if self.removed_env:
            warnings.append(credential_env_warning(self.removed_env))
        if self.declined_requests:
            warnings.append(f"declined {len(self.declined_requests)} approval/input request(s): {sorted(set(self.declined_requests))}")
            self.declined_requests = []
        if self._rpc is not None and (self._rpc.malformed or self._rpc.oversized):
            warnings.append(f"app-server stream: {self._rpc.malformed} malformed and {self._rpc.oversized} oversized line(s) skipped")
        thread_id = observed.get("session_id")
        return TurnResult(
            status=status,
            text=collector.text.strip(),
            session_id=thread_id,
            lineage=lineage_for(request.session_id, request.fork, thread_id),
            settings=SettingsRecord(requested, accepted, observed),
            usage=tuple(usage),
            duration_s=time.monotonic() - started,
            exit_code=None,
            error=error,
            warnings=tuple(warnings),
            provider_invocation_id=collector.turn_id,
            events=collector.events,
        )

    # -- incoming ---------------------------------------------------------------------------

    def _on_notification(self, message: dict) -> None:
        collector = self._collector
        if collector is not None:
            collector.feed(message)

    def _on_server_request(self, message: dict):
        method = message.get("method", "")
        self.declined_requests.append(method)
        if method in DECLINES:
            return DECLINES[method]
        return JsonRpcError(-32601, f"duet does not answer {method} in a managed turn")


class _TurnCollector:
    def __init__(self, thread_id: str, turn_id: str | None, on_event: EventCallback | None) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.on_event = on_event
        self.completed: dict | None = None
        self.text = ""
        self.usage: list[UsageObservation] = []
        self.warnings: list[str] = []
        self.last_error: str | None = None
        self.events = 0

    def bind_turn(self, turn_id: str | None) -> None:
        """Adopt the id from the turn/start response. Events that arrived
        earlier were accepted for the whole thread; a completion recorded for
        a different turn is not this turn's and is dropped."""
        if not turn_id:
            return
        self.turn_id = turn_id
        if self.completed is not None and self.completed.get("id") not in (None, turn_id):
            self.completed = None

    def _mine(self, params: dict) -> bool:
        if params.get("threadId") not in (None, self.thread_id):
            return False
        turn = params.get("turnId") or (params.get("turn") or {}).get("id")
        return self.turn_id is None or turn in (None, self.turn_id)

    def feed(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "account/rateLimits/updated":
            self.usage.extend(_rate_limit_observations(params.get("rateLimits") or {}, "codex.account.rateLimits.updated", message.get("emittedAtMs")))
            return
        if not self._mine(params):
            return
        self.events += 1
        if self.on_event:
            self.on_event(message)
        if method == "turn/started" and self.turn_id is None:
            self.turn_id = (params.get("turn") or {}).get("id")
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                self.text = item["text"]
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            self.usage = [u for u in self.usage if u.source != "codex.thread.tokenUsage"]
            # ThreadTokenUsage in the 0.157.1 schema has only `last` (the most
            # recent model request) and `total` (thread cumulative); there is
            # no per-turn field. A turn can make several requests, so `last`
            # is a per-call figure, never the turn's total.
            self.usage += _token_observations(usage.get("last") or {}, "call", message.get("emittedAtMs"))
            self.usage += _token_observations(usage.get("total") or {}, "thread_cumulative", message.get("emittedAtMs"))
            window = int_or_none(usage.get("modelContextWindow"))
            if window is not None:
                self.usage.append(UsageObservation("context.window", window, "thread_cumulative", "codex.thread.tokenUsage", unit="tokens"))
        elif method == "error":
            error = params.get("error") or {}
            self.last_error = str(error.get("message") or "error")
            if params.get("willRetry"):
                self.warnings.append(f"provider error, retrying: {self.last_error[:200]}")
        elif method == "turn/completed":
            self.completed = params.get("turn") or {"status": "completed"}


def _token_observations(breakdown: dict, scope: str, at_ms: int | None) -> list[UsageObservation]:
    out = []
    for field_name, metric in (
        ("inputTokens", "tokens.input"), ("cachedInputTokens", "tokens.cache_read"), ("cacheWriteInputTokens", "tokens.cache_write"),
        ("outputTokens", "tokens.output"), ("reasoningOutputTokens", "tokens.reasoning_output"), ("totalTokens", "tokens.total"),
    ):
        value = int_or_none(breakdown.get(field_name))
        if value is not None:
            out.append(UsageObservation(metric, value, scope, "codex.thread.tokenUsage", unit="tokens", observed_at_ms=at_ms))
    return out


def _rate_limit_observations(snapshot: dict, source: str, at_ms: int | None = None) -> tuple[UsageObservation, ...]:
    out = []
    limit = str(snapshot.get("limitId") or snapshot.get("limitName") or "default")
    for name in ("primary", "secondary"):
        window = snapshot.get(name)
        if not isinstance(window, dict):
            continue
        used = int_or_none(window.get("usedPercent"))
        if used is None:
            continue
        minutes = int_or_none(window.get("windowDurationMins"))
        resets = int_or_none(window.get("resetsAt"))
        key = f"{limit}:{name}:{minutes or '?'}m:resets={resets or '?'}"
        out.append(UsageObservation("quota.used_percent", Decimal(used), "window", source, unit="percent", key=key, observed_at_ms=at_ms))
    return tuple(out)


__all__ = ["CodexAppServerAdapter"]


def codex_mcp_config(mcp_config: dict) -> dict:
    """Translate the Claude-style `{"mcpServers": {name: {command, args, env}}}`
    shape into Codex thread config. Only stdio servers are passed through."""
    servers = mcp_config.get("mcpServers") if isinstance(mcp_config, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise UnsupportedSetting("mcp_config needs an mcpServers table with at least one stdio server")
    out: dict = {}
    for name, spec in servers.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("command"), str):
            raise UnsupportedSetting(f"MCP server {name!r} needs a command (only stdio servers are supported)")
        entry: dict = {"command": spec["command"], "args": [str(a) for a in spec.get("args", [])]}
        if spec.get("env"):
            entry["env"] = {str(k): str(v) for k, v in spec["env"].items()}
        for key in ("tool_timeout_sec", "startup_timeout_sec"):
            if key in spec:
                entry[key] = spec[key]
        out[name] = entry
    return {"mcp_servers": out}
