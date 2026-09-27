"""D04: capability-aware Claude and Codex adapters against protocol emulators
seeded with recorded fixtures. No real provider is contacted here."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

from duet.adapters import AgentTimeoutError, AuthError, BillingError, ModelUnavailableError, OutputLimitError, QuotaError
from duet.providers.base import TurnRequest, UnsupportedSetting, lineage_for
from duet.providers.claude_cli import ClaudeCLIAdapter, discover_from_help
from duet.providers.codex_appserver import CodexAppServerAdapter
from duet.providers.codex_exec import CodexExecAdapter
from duet.providers.process import stream_process
from duet.runtime.contracts import Control, ValidationError

HERE = Path(__file__).resolve().parent
EMU = HERE / "emulators"
FIXTURES = HERE.parent / "fixtures" / "provider_protocols"


def shim(tmp_path: Path, name: str, script: str) -> str:
    path = tmp_path / name
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{EMU / script}" "$@"\n')
    path.chmod(0o755)
    return str(path)


@pytest.fixture()
def claude(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.log"))
    return ClaudeCLIAdapter(shim(tmp_path, "claude", "fake_claude.py"))


def claude_argv(tmp_path) -> list[list[str]]:
    return [json.loads(line)["argv"] for line in (tmp_path / "claude.log").read_text().splitlines()]


@pytest.fixture()
def codex(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.log"))
    adapter = CodexAppServerAdapter(shim(tmp_path, "codex", "fake_codex_appserver.py"))
    yield adapter
    adapter.close()


def req(tmp_path, **kw) -> TurnRequest:
    return TurnRequest(prompt=kw.pop("prompt", "implement mul()"), cwd=tmp_path, **kw)


# --- Claude ---------------------------------------------------------------------------


class TestClaudeCapabilities:
    def test_discovered_from_recorded_help(self):
        caps = discover_from_help((FIXTURES / "claude" / "2.1.283-help.txt").read_text(), "2.1.283 (Claude Code)")
        assert caps.efforts == ("low", "medium", "high", "xhigh", "max")
        assert caps.session_fork and caps.session_id_preassign and caps.provider_budget_cap
        assert caps.model_control == Control.SUPPORTED and caps.models is None  # no catalogue
        assert "session-cumulative" in caps.notes[0]

    def test_older_version_is_not_assumed_cumulative(self):
        caps = discover_from_help((FIXTURES / "claude" / "2.1.283-help.txt").read_text(), "2.1.200 (Claude Code)")
        assert not any("cumulative" in note for note in caps.notes)

    def test_missing_flags_disable_controls(self):
        caps = discover_from_help("Usage: claude [options]\n  -p, --print\n", "2.0.1")
        assert caps.effort_control == Control.UNSUPPORTED and not caps.session_fork and caps.efforts is None


class TestClaudeTurns:
    def test_success_records_settings_usage_and_lineage(self, claude, tmp_path):
        result = claude.run_turn(req(tmp_path, model="claude-opus-5-5", effort="high"))
        assert result.ok and result.text.startswith("done: implement mul()")
        assert result.lineage == "new" and result.session_id
        assert result.settings.requested["effort"] == "high"
        assert result.settings.accepted == {"model": "claude-opus-5-5", "effort": "high", "permission_mode": "acceptEdits", "permission_prompts": "none"}
        assert result.settings.observed["model"] == "claude-opus-5-5"
        assert "effort" not in result.settings.observed  # not reported by the CLI: unobserved, not assumed
        cost = [u for u in result.usage if u.metric == "cost_usd" and u.key == ""]
        assert cost[0].value == Decimal("0.25") and cost[0].scope == "call" and cost[0].quality == "estimated"
        argv = claude_argv(tmp_path)[-1]
        assert "--bare" not in argv and "--dangerously-skip-permissions" not in argv

    def test_unsupported_effort_rejected_before_dispatch(self, claude, tmp_path):
        with pytest.raises(UnsupportedSetting):
            claude.run_turn(req(tmp_path, effort="ultra"))
        assert not (tmp_path / "claude.log").exists() or all("-p" not in a for a in claude_argv(tmp_path))

    def test_resume_is_session_cumulative(self, claude, tmp_path, monkeypatch):  # cost semantics (W10)
        monkeypatch.setenv("FAKE_CLAUDE_PRIOR_COST", "1.0")
        result = claude.run_turn(req(tmp_path, session_id="sess-1"))
        assert result.lineage == "resumed_same" and result.session_id == "sess-1"
        cost = [u for u in result.usage if u.metric == "cost_usd" and u.key == ""][0]
        assert cost.scope == "session_cumulative" and cost.value == Decimal("1.25")

    def test_fork_gets_new_id(self, claude, tmp_path):
        result = claude.run_turn(req(tmp_path, session_id="sess-1", fork=True))
        assert result.lineage == "forked" and result.session_id != "sess-1"
        assert "--fork-session" in claude_argv(tmp_path)[-1]

    def test_preassigned_session_id(self, claude, tmp_path):
        result = claude.run_turn(req(tmp_path, new_session_id="11111111-2222-3333-4444-555555555555"))
        assert result.session_id == "11111111-2222-3333-4444-555555555555"

    def test_read_only_profile(self, claude, tmp_path):
        claude.run_turn(req(tmp_path, permission_profile="read_only"))
        argv = claude_argv(tmp_path)[-1]
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"

    def test_mcp_config_is_strict(self, claude, tmp_path):
        claude.run_turn(req(tmp_path, mcp_config={"mcpServers": {"duet": {"command": "duet", "args": ["mcp", "serve"]}}}))
        argv = claude_argv(tmp_path)[-1]
        assert "--strict-mcp-config" in argv and json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["duet"]

    @pytest.mark.parametrize(
        "mode,cls,kind",
        [("auth", AuthError, "auth"), ("billing", BillingError, "billing"), ("model", ModelUnavailableError, "model_unavailable"), ("rate_limit", QuotaError, "rate_limit")],
    )
    def test_typed_failures(self, claude, tmp_path, monkeypatch, mode, cls, kind):
        monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
        result = claude.run_turn(req(tmp_path))
        assert result.status == "failed" and type(result.error) is cls and result.error.kind == kind

    def test_permission_denials_are_reported_not_hidden(self, claude, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "denied")
        result = claude.run_turn(req(tmp_path))
        assert result.ok and result.permission_denials[0]["tool_name"] == "Bash"

    def test_crash_with_zeroed_costs_is_unknown_not_zero(self, claude, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash_zero")
        result = claude.run_turn(req(tmp_path))
        assert result.status == "failed"
        assert result.usage == ()

    def test_provider_budget_cap(self, claude, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "budget")
        result = claude.run_turn(req(tmp_path, max_budget_usd=Decimal("0.50")))
        assert result.error.kind == "provider_budget_cap"
        assert "--max-budget-usd" in claude_argv(tmp_path)[-1]

    def test_no_result_is_a_failure(self, claude, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_result")
        assert claude.run_turn(req(tmp_path)).status == "failed"

    def test_malformed_lines_are_tolerated(self, claude, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "malformed")
        result = claude.run_turn(req(tmp_path))
        assert result.ok and any("non-JSON" in w for w in result.warnings)

    def test_flood_is_bounded(self, tmp_path, monkeypatch):  # AT34
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "flood")
        adapter = ClaudeCLIAdapter(shim(tmp_path, "claude", "fake_claude.py"), max_total_bytes=2 * 1024 * 1024)
        started = time.monotonic()
        result = adapter.run_turn(req(tmp_path, timeout_seconds=60))
        assert result.status == "failed" and isinstance(result.error, OutputLimitError)
        assert time.monotonic() - started < 60

    def test_cancel_interrupts_the_turn(self, claude, tmp_path, monkeypatch):  # AT31
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        started = time.monotonic()
        result = claude.run_turn(req(tmp_path, timeout_seconds=60), cancel=cancel)
        assert result.status == "cancelled" and time.monotonic() - started < 15
        assert result.session_id  # init was seen; lineage is known even for a cancelled turn

    def test_timeout(self, claude, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
        result = claude.run_turn(req(tmp_path, timeout_seconds=1))
        assert result.status == "timeout" and isinstance(result.error, AgentTimeoutError)


# --- Codex app-server ------------------------------------------------------------------


class TestCodexAppServer:
    def test_handshake_against_recorded_responses(self, codex):
        assert codex.version() == "0.157.1"
        caps = codex.capabilities()
        assert caps.protocol == "codex-app-server/v2"
        assert "gpt-6-astra" in caps.models and len(caps.models) == 7  # both pages collected
        assert "ultra" in caps.efforts
        assert codex.account()["requiresOpenaiAuth"] is True

    def test_rate_limits_unavailable_is_not_zero(self, codex):
        observations, reason = codex.read_rate_limits()
        assert observations == () and "authentication required" in reason

    def test_rate_limits_when_available(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CODEX_MODE", "ratelimited")
        adapter = CodexAppServerAdapter(shim(tmp_path, "codex", "fake_codex_appserver.py"))
        try:
            observations, reason = adapter.read_rate_limits()
            assert reason is None
            assert {o.value for o in observations} == {Decimal(42), Decimal(7)}
            assert all(o.metric == "quota.used_percent" and o.scope == "window" for o in observations)
        finally:
            adapter.close()

    def test_turn_success_with_settings_and_usage(self, codex, tmp_path):
        result = codex.run_turn(req(tmp_path, model="gpt-6-sol", effort="high"))
        assert result.ok and "model=gpt-6-sol effort=high" in result.text
        assert result.lineage == "new" and result.session_id == "thr-1"
        assert result.settings.accepted["sandbox"] == "workspace-write" and result.settings.accepted["approval_policy"] == "never"
        turn_tokens = {u.metric: u.value for u in result.usage if u.scope == "turn"}
        cumulative = {u.metric: u.value for u in result.usage if u.scope == "thread_cumulative"}
        assert turn_tokens["tokens.input"] == 100 and cumulative["tokens.input"] == 300
        assert not any(u.metric == "cost_usd" for u in result.usage)  # codex reports no cost
        assert any(u.metric == "quota.used_percent" for u in result.usage)

    def test_effort_not_advertised_by_model_is_rejected(self, codex, tmp_path):
        with pytest.raises(UnsupportedSetting, match="not supported by gpt-6-luna"):
            codex.run_turn(req(tmp_path, model="gpt-6-luna", effort="ultra"))
        with pytest.raises(UnsupportedSetting, match="not in this account"):
            codex.run_turn(req(tmp_path, model="gpt-imaginary"))

    def test_resume_keeps_thread_and_fork_changes_it(self, codex, tmp_path):
        first = codex.run_turn(req(tmp_path))
        again = codex.run_turn(req(tmp_path, session_id=first.session_id))
        forked = codex.run_turn(req(tmp_path, session_id=first.session_id, fork=True))
        assert again.lineage == "resumed_same" and again.session_id == first.session_id
        assert forked.lineage == "forked" and forked.session_id != first.session_id

    def test_failed_turn_is_classified(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CODEX_MODE", "fail")
        adapter = CodexAppServerAdapter(shim(tmp_path, "codex", "fake_codex_appserver.py"))
        try:
            result = adapter.run_turn(req(tmp_path))
            assert result.status == "failed" and isinstance(result.error, QuotaError)
        finally:
            adapter.close()

    def test_approval_requests_are_declined(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CODEX_MODE", "approval")
        monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.log"))
        adapter = CodexAppServerAdapter(shim(tmp_path, "codex", "fake_codex_appserver.py"))
        try:
            result = adapter.run_turn(req(tmp_path))
            answers = [json.loads(l)["answer"] for l in (tmp_path / "codex.log").read_text().splitlines() if '"answer"' in l]
            assert answers == [{"id": 9001, "result": {"decision": "decline"}}]
            assert any("declined" in w for w in result.warnings)
        finally:
            adapter.close()

    def test_cancel_uses_turn_interrupt(self, tmp_path, monkeypatch):  # AT31
        monkeypatch.setenv("FAKE_CODEX_MODE", "hang")
        adapter = CodexAppServerAdapter(shim(tmp_path, "codex", "fake_codex_appserver.py"))
        try:
            cancel = threading.Event()
            threading.Timer(0.5, cancel.set).start()
            result = adapter.run_turn(req(tmp_path, timeout_seconds=60), cancel=cancel)
            assert result.status == "cancelled"
            assert adapter._rpc is not None and adapter._rpc.alive()  # clean interrupt keeps the server
        finally:
            adapter.close()

    def test_unacknowledged_interrupt_stops_the_server(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CODEX_MODE", "ignore_interrupt")
        adapter = CodexAppServerAdapter(shim(tmp_path, "codex", "fake_codex_appserver.py"))
        import duet.providers.codex_appserver as mod

        monkeypatch.setattr(mod, "INTERRUPT_GRACE_SECONDS", 1.0)
        result = adapter.run_turn(req(tmp_path, timeout_seconds=1))
        assert result.status == "timeout" and adapter._rpc is None

    def test_server_death_is_a_structured_failure(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CODEX_MODE", "die")
        adapter = CodexAppServerAdapter(shim(tmp_path, "codex", "fake_codex_appserver.py"))
        result = adapter.run_turn(req(tmp_path, timeout_seconds=10))
        assert result.status == "failed" and "exited" in str(result.error)

    def test_malformed_lines_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CODEX_MODE", "malformed")
        adapter = CodexAppServerAdapter(shim(tmp_path, "codex", "fake_codex_appserver.py"))
        try:
            result = adapter.run_turn(req(tmp_path))
            assert result.ok and any("malformed" in w for w in result.warnings)
        finally:
            adapter.close()

    def test_unsupported_requests_rejected(self, codex, tmp_path):
        with pytest.raises(UnsupportedSetting):
            codex.run_turn(req(tmp_path, new_session_id="x"))
        with pytest.raises(UnsupportedSetting):
            codex.run_turn(req(tmp_path, max_budget_usd=Decimal("1")))


# --- Codex exec fallback --------------------------------------------------------------------


class TestCodexExec:
    def _adapter(self, tmp_path, monkeypatch, mode="success"):
        monkeypatch.setenv("FAKE_CODEX_EXEC_MODE", mode)
        monkeypatch.setenv("FAKE_CODEX_EXEC_LOG", str(tmp_path / "exec.log"))
        return CodexExecAdapter(shim(tmp_path, "codex", "fake_codex_exec.py"))

    def test_success_and_lower_capability_labels(self, tmp_path, monkeypatch):
        adapter = self._adapter(tmp_path, monkeypatch)
        result = adapter.run_turn(req(tmp_path, effort="high", model="gpt-6-sol"))
        assert result.ok and result.text.startswith("exec reply to")
        assert adapter.capabilities().effort_control == Control.ADVISORY
        assert result.settings.accepted["effort"] == "high" and "effort" not in result.settings.observed
        assert {u.metric for u in result.usage} >= {"tokens.input", "tokens.output"}
        assert any("Reconnecting" in w for w in result.warnings)  # transient errors are warnings

    def test_resume_puts_global_flags_before_exec(self, tmp_path, monkeypatch):
        adapter = self._adapter(tmp_path, monkeypatch)
        result = adapter.run_turn(req(tmp_path, session_id="01a0e15b-aaaa"))
        argv = json.loads((tmp_path / "exec.log").read_text().splitlines()[-1])["argv"]
        assert argv.index("--sandbox") < argv.index("exec") < argv.index("resume")
        assert result.lineage == "resumed_same"

    def test_recorded_network_denied_failure(self, tmp_path, monkeypatch):
        result = self._adapter(tmp_path, monkeypatch, "network_denied").run_turn(req(tmp_path))
        assert result.status == "failed" and result.session_id  # thread started before the failure

    def test_turn_failed_classified(self, tmp_path, monkeypatch):
        result = self._adapter(tmp_path, monkeypatch, "failed").run_turn(req(tmp_path))
        assert isinstance(result.error, BillingError)

    def test_fork_unsupported(self, tmp_path, monkeypatch):
        with pytest.raises(UnsupportedSetting):
            self._adapter(tmp_path, monkeypatch).run_turn(req(tmp_path, session_id="s", fork=True))


# --- shared contract ----------------------------------------------------------------------------


def test_turn_request_validation(tmp_path):
    with pytest.raises(ValidationError):
        TurnRequest(prompt="x", cwd=tmp_path, fork=True)
    with pytest.raises(ValidationError):
        TurnRequest(prompt="x", cwd=tmp_path, session_id="a", new_session_id="b")
    with pytest.raises(ValidationError):
        TurnRequest(prompt="x", cwd=tmp_path, permission_profile="root")


def test_lineage_labels():
    assert lineage_for(None, False, "a") == "new"
    assert lineage_for("a", False, "a") == "resumed_same"
    assert lineage_for("a", False, "b") == "resumed_new_id"
    assert lineage_for("a", True, "b") == "forked"
    assert lineage_for("a", False, None) == "unknown"


def test_stream_process_truncates_long_lines(tmp_path):
    lines = []
    code = "import sys; sys.stdout.write('a' * 5000 + '\\n' + 'short\\n')"
    result = stream_process([sys.executable, "-c", code], on_line=lines.append, cwd=tmp_path, timeout=30, max_line_bytes=100)
    assert result.truncated_lines == 1 and lines == ["a" * 100, "short"]
