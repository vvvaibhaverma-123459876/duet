"""`codex exec --json` fallback adapter (explicitly lower capability).

Used when the app-server path is unavailable. Event shapes are those of the
exec JSONL stream (codex-rs/exec/src/exec_events.rs; failure path observed
with codex-cli 0.157.1 in tests/fixtures/.../exec-json-network-denied.jsonl):
thread.started{thread_id}, turn.started, item.started/updated/completed
{item: {id, type, ...}}, turn.completed{usage}, turn.failed{error}, error
{message}. Top-level `error` events are frequently transient ("Reconnecting
...") and are warnings unless the turn fails.

Limits, reported honestly: no model catalogue (model is accepted but not
observable), effort only through `-c model_reasoning_effort=...` (advisory),
per-turn tokens only, no cost, no rate-limit windows, cancellation by
signal only."""
from __future__ import annotations

import json
import os
import threading
import time

from ..adapters import AgentError, AgentTimeoutError, OutputLimitError, classify_failure
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
from .process import stream_process

SANDBOX_FOR_PROFILE = {"workspace_write": "workspace-write", "read_only": "read-only"}


class CodexExecAdapter:
    name = "codex"

    def __init__(self, binary: str = "codex", *, env: dict[str, str] | None = None, version: str | None = None) -> None:
        self.binary = binary
        self.env = env
        self._version = version

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="codex",
            protocol="codex-exec-jsonl/1",
            version=self._version,
            streaming=True,
            session_resume=True,
            session_fork=False,
            session_id_preassign=False,
            cancel="terminate",
            model_control=Control.SUPPORTED,
            effort_control=Control.ADVISORY,
            usage=UsageCapability.PARTIAL,
            efforts=None,
            models=None,
            provider_budget_cap=False,
            notes=("fallback path: no model discovery, effort not observable, no cost or quota windows",),
        )

    def build_command(self, request: TurnRequest) -> tuple[list[str], dict, dict]:
        if request.fork:
            raise UnsupportedSetting("codex exec cannot fork a session")
        if request.new_session_id:
            raise UnsupportedSetting("codex exec assigns session ids itself")
        if request.max_budget_usd is not None:
            raise UnsupportedSetting("codex enforces no per-call spend cap")
        if request.mcp_config is not None:
            raise UnsupportedSetting("per-turn MCP config is not supported by codex exec")
        sandbox = SANDBOX_FOR_PROFILE[request.permission_profile]
        # Global options go before the subcommand: `exec resume` does not
        # accept -C/--sandbox itself (codex-cli 0.157.1 help).
        cmd = [self.binary, "--ask-for-approval", "never", "--sandbox", sandbox, "-C", str(request.cwd)]
        accepted: dict = {"approval_policy": "never", "sandbox": sandbox}
        if request.effort:
            cmd += ["-c", f'model_reasoning_effort="{request.effort}"']
            accepted["effort"] = request.effort  # advisory: exec reports no effective effort
        cmd += ["exec"]
        if request.session_id:
            cmd += ["resume", request.session_id]
            accepted["resume"] = request.session_id
        cmd += ["--json", "--skip-git-repo-check"]
        if request.model:
            cmd += ["-m", request.model]
            accepted["model"] = request.model
        cmd.append("-")
        requested = {"model": request.model, "effort": request.effort, "permission_profile": request.permission_profile, "resume": request.session_id}
        return cmd, requested, accepted

    def run_turn(self, request: TurnRequest, *, on_event: EventCallback | None = None, cancel: threading.Event | None = None) -> TurnResult:
        if request.extra_instructions:
            prompt = f"{request.extra_instructions}\n\n{request.prompt}"
        else:
            prompt = request.prompt
        cmd, requested, accepted = self.build_command(request)
        state = _ExecState(on_event)
        env = None
        if self.env or request.env:
            env = dict(os.environ)
            env.update(self.env or {})
            env.update(request.env or {})
        started = time.monotonic()
        try:
            proc = stream_process(cmd, on_line=state.feed, cwd=request.cwd, env=env, stdin_data=prompt, timeout=request.timeout_seconds, cancel_event=cancel)
        except FileNotFoundError as exc:
            raise AgentError(f"codex: executable not found: {self.binary}", kind="not_found") from exc
        observed = {"session_id": state.thread_id} if state.thread_id else {}
        common = dict(
            session_id=state.thread_id,
            lineage=lineage_for(request.session_id, False, state.thread_id),
            settings=SettingsRecord(requested, accepted, observed),
            usage=tuple(state.usage),
            duration_s=time.monotonic() - started,
            exit_code=proc.returncode,
            provider_invocation_id=state.thread_id,
            events=state.events,
        )
        warnings = tuple(state.warnings)
        if proc.over_limit:
            return TurnResult(status="failed", text=state.text, error=OutputLimitError("codex: event stream exceeded the capture limit"), warnings=warnings, **common)
        if proc.timed_out:
            return TurnResult(status="timeout", text=state.text, error=AgentTimeoutError(f"codex: timed out after {request.timeout_seconds:.0f}s"), warnings=warnings, **common)
        if proc.cancelled:
            return TurnResult(status="cancelled", text=state.text, warnings=warnings, **common)
        if state.completed and proc.returncode == 0:
            return TurnResult(status="completed", text=state.text.strip(), warnings=warnings, **common)
        detail = state.failure or state.last_error or proc.stderr[-800:] or "codex exec failed"
        return TurnResult(status="failed", text=state.text, error=classify_failure("codex", proc.returncode, "codex exec", detail, proc.stderr), warnings=warnings, **common)


class _ExecState:
    def __init__(self, on_event: EventCallback | None) -> None:
        self.on_event = on_event
        self.thread_id: str | None = None
        self.text = ""
        self.usage: list[UsageObservation] = []
        self.completed = False
        self.failure: str | None = None
        self.last_error: str | None = None
        self.warnings: list[str] = []
        self.events = 0
        self.malformed = 0

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self.malformed += 1
            if self.malformed == 1:
                self.warnings.append("codex exec emitted non-JSON output on the event stream")
            return
        if not isinstance(event, dict):
            return
        self.events += 1
        if self.on_event:
            self.on_event(event)
        kind = event.get("type")
        if kind == "thread.started" and isinstance(event.get("thread_id"), str):
            self.thread_id = event["thread_id"]
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                self.text = item["text"]
            elif item.get("type") == "error":
                self.warnings.append(f"item error: {str(item.get('message'))[:200]}")
        elif kind == "turn.completed":
            self.completed = True
            usage = event.get("usage") or {}
            for field_name, metric in (
                ("input_tokens", "tokens.input"), ("cached_input_tokens", "tokens.cache_read"),
                ("cache_write_input_tokens", "tokens.cache_write"), ("output_tokens", "tokens.output"),
                ("reasoning_output_tokens", "tokens.reasoning_output"),
            ):
                value = int_or_none(usage.get(field_name))
                if value is not None:
                    self.usage.append(UsageObservation(metric, value, "turn", "codex.exec.turn.completed", unit="tokens"))
        elif kind == "turn.failed":
            self.failure = str((event.get("error") or {}).get("message") or "turn failed")
        elif kind == "error":
            self.last_error = str(event.get("message") or "")
            if len(self.warnings) < 20:
                self.warnings.append(f"stream error: {self.last_error[:200]}")
