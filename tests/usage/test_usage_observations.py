"""D07: normalised observations and their adapters from D04 provider output.
No real provider is contacted; the end-to-end cases drive the real D04
adapters against the D04 protocol emulators."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from exeshim import make_exe

from duet.providers.base import SettingsRecord, TurnRequest, TurnResult, UsageObservation
from duet.providers.claude_cli import ClaudeCLIAdapter
from duet.providers.codex_appserver import CodexAppServerAdapter, _TurnCollector
from duet.usage import Baseline, Freshness, Ledger, Observation, ObservationError, Quality, Scope
from duet.usage.observations import (
    CODEX_TOTAL_SOURCE,
    from_codex_rate_limits,
    from_codex_token_usage,
    from_turn_result,
    pool_id_for,
)

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures" / "telemetry"
EMU = HERE.parent / "providers" / "emulators"
NOW = 1_790_400_000_000


def obs(**kw) -> Observation:
    base = dict(provider="claude", session_id="s1", metric="tokens.input", value=10, unit="tokens", scope=Scope.CALL,
                source="test", source_event_id="e1", observed_at_ms=NOW)
    base.update(kw)
    return Observation(**base)


def turn(usage=(), *, status="completed", session="s1", lineage="new", requested=None, duration=2.5, invocation=None) -> TurnResult:
    return TurnResult(status=status, text="", session_id=session, lineage=lineage, settings=SettingsRecord(requested or {}, {}, {}),
                      usage=tuple(usage), duration_s=duration, provider_invocation_id=invocation)


def codex_updates() -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / "codex" / "token-usage-updates.jsonl").read_text().splitlines()]


class TestObservationInvariants:
    def test_money_is_never_float(self):
        with pytest.raises(ObservationError, match="float"):
            obs(metric="cost.estimated_usd", value=0.25, unit="USD", quality=Quality.ESTIMATED)
        with pytest.raises(ObservationError, match="Decimal"):
            obs(metric="cost.estimated_usd", value=1, unit="USD", quality=Quality.ESTIMATED)
        assert obs(metric="cost.estimated_usd", value=Decimal("0.25"), unit="USD", quality=Quality.ESTIMATED).value == Decimal("0.25")

    @pytest.mark.parametrize("value", [-1, True, Decimal("NaN")])
    def test_invalid_numbers_are_rejected_not_zeroed(self, value):
        with pytest.raises(ObservationError):
            obs(value=value)

    def test_unknown_has_no_value_and_a_value_is_not_unknown(self):
        assert obs(value=None, quality=Quality.UNKNOWN).known is False
        with pytest.raises(ObservationError):
            obs(value=None)  # quality OBSERVED without a value
        with pytest.raises(ObservationError):
            obs(value=0, quality=Quality.UNKNOWN)

    def test_context_occupancy_cannot_be_cumulative_usage(self):  # AT11
        with pytest.raises(ObservationError, match="point-in-time"):
            obs(metric="context.input_tokens", scope=Scope.SESSION_CUMULATIVE)
        with pytest.raises(ObservationError, match="counter"):
            obs(metric="tokens.input", scope=Scope.SNAPSHOT)
        with pytest.raises(ObservationError, match="window"):
            obs(metric="quota.used_percent", value=Decimal(40), unit="percent", scope=Scope.SNAPSHOT)

    def test_cost_quality_matches_its_dimension(self):
        with pytest.raises(ObservationError, match="estimated"):
            obs(metric="cost.estimated_usd", value=Decimal("1"), unit="USD", quality=Quality.OBSERVED)
        with pytest.raises(ObservationError, match="authoritative"):
            obs(metric="cost.billed_usd", value=Decimal("1"), unit="USD", quality=Quality.ESTIMATED)

    def test_unknown_metric_unit_and_baseline_rules(self):
        with pytest.raises(ObservationError, match="unknown metric"):
            obs(metric="tokens.everything")
        with pytest.raises(ObservationError, match="measured in"):
            obs(unit="percent")
        with pytest.raises(ObservationError, match="parent_session_id"):
            obs(scope=Scope.SESSION_CUMULATIVE, baseline=Baseline.INHERITED)

    def test_time_is_utc_and_freshness_is_explicit(self):
        o = obs(metric="quota.used_percent", value=Decimal(40), unit="percent", scope=Scope.WINDOW, resets_at_ms=NOW + 60_000)
        assert o.observed_at == datetime.fromtimestamp(NOW / 1000, tz=timezone.utc)
        assert o.freshness(NOW + 1_000, 10_000) is Freshness.FRESH
        assert o.freshness(NOW + 20_000, 10_000) is Freshness.STALE
        assert o.freshness(NOW + 60_000, 10_000) is Freshness.EXPIRED

    def test_pool_identity_is_unknown_without_an_account(self):
        assert pool_id_for("codex", "acct-1") == "codex:acct-1"
        assert pool_id_for("codex", None) is None and pool_id_for("codex", "  ") is None


class TestClaudeTurnResults:
    def test_new_session_call_is_the_session_counter_from_zero(self):
        usage = [UsageObservation("cost_usd", Decimal("0.25"), "call", "claude.result.total_cost_usd", quality="estimated", unit="USD"),
                 UsageObservation("tokens.input", 1200, "call", "claude.result.modelUsage", unit="tokens", key="claude-opus-5-5")]
        out = from_turn_result(turn(usage), provider="claude", turn_id="act-1", received_at_ms=NOW)
        cost = next(o for o in out if o.metric == "cost.estimated_usd" and o.known)
        assert cost.scope is Scope.SESSION_CUMULATIVE and cost.baseline is Baseline.ZERO and cost.quality is Quality.ESTIMATED
        assert next(o for o in out if o.metric == "tokens.input").key == "claude-opus-5-5"
        assert [o.value for o in out if o.metric == "attempts.turns"] == [1]
        assert [o.value for o in out if o.metric == "time.active_ms"] == [2500]

    def test_missing_usage_is_an_unknown_marker_not_zero(self):  # AT08
        out = from_turn_result(turn(status="cancelled", lineage="resumed_same"), provider="claude", turn_id="act-2", received_at_ms=NOW)
        markers = [o for o in out if not o.known]
        assert {o.metric for o in markers} == {"cost.estimated_usd", "tokens.input", "tokens.output", "tokens.cache_read", "tokens.cache_write"}
        assert all(o.quality is Quality.UNKNOWN and o.value is None for o in markers)
        assert not any(o.value == 0 for o in out if o.dimension.value.startswith(("tokens", "cost")))

    def test_fork_inherits_the_parent_level(self):
        usage = [UsageObservation("cost_usd", Decimal("0.55"), "session_cumulative", "claude.result.total_cost_usd", quality="estimated", unit="USD")]
        out = from_turn_result(turn(usage, session="child", lineage="forked", requested={"resume": "parent", "fork": True}),
                               provider="claude", turn_id="act-3", received_at_ms=NOW)
        cost = next(o for o in out if o.metric == "cost.estimated_usd")
        assert cost.baseline is Baseline.INHERITED and cost.parent_session_id == "parent"

    def test_turn_identity_is_required(self):
        with pytest.raises(ObservationError):
            from_turn_result(turn(), provider="claude", turn_id="", received_at_ms=NOW)


class TestCodexObservations:
    def test_turn_result_totals_window_and_quota(self):
        collector = _TurnCollector("019a7c3e-thr-a", "turn-2", None)
        collector.feed(codex_updates()[4])
        collector.feed({"method": "account/rateLimits/updated", "params": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 43, "windowDurationMins": 300, "resetsAt": 1790500000}}}})
        result = turn(collector.usage, session="019a7c3e-thr-a", lineage="resumed_same", invocation="turn-2")
        out = from_turn_result(result, provider="codex", turn_id="act-9", received_at_ms=NOW + 5_000, pool_id="codex:acct-7f3a")
        totals = {o.metric: o.value for o in out if o.source == CODEX_TOTAL_SOURCE}
        assert totals == {"tokens.input": 5400, "tokens.cache_read": 3500, "tokens.cache_write": 0, "tokens.output": 700,
                          "tokens.reasoning_output": 280, "tokens.total": 6100}
        assert not any(o.source.endswith(".last") for o in out)  # a TurnResult keeps only the final update
        window = next(o for o in out if o.metric == "context.window_size")
        assert window.scope is Scope.SNAPSHOT and window.value == 400000
        quota = next(o for o in out if o.metric == "quota.used_percent")
        assert (quota.key, quota.window_minutes, quota.resets_at_ms, quota.value) == ("codex:primary", 300, 1790500000000, Decimal(43))

    def test_the_same_update_via_turn_result_and_raw_stream_has_one_identity(self):  # AT10
        message = codex_updates()[4]
        collector = _TurnCollector("019a7c3e-thr-a", "turn-2", None)
        collector.feed(message)
        via_result = from_turn_result(turn(collector.usage, session="019a7c3e-thr-a", lineage="resumed_same", invocation="turn-2"),
                                      provider="codex", turn_id="act-9", received_at_ms=NOW)
        via_stream = from_codex_token_usage(message, received_at_ms=NOW + 999)
        ledger = Ledger()
        ledger.ingest_all(via_stream)
        outcomes = [ledger.ingest(o) for o in via_result if o.source == CODEX_TOTAL_SOURCE and o.known]
        assert outcomes and all(outcome == "duplicate" for outcome in outcomes)

    def test_interrupted_turn_marks_its_tail_unknown(self):
        collector = _TurnCollector("thr", "turn-1", None)
        collector.feed(codex_updates()[0] | {"params": codex_updates()[0]["params"] | {"threadId": "thr"}})
        out = from_turn_result(turn(collector.usage, status="interrupted", session="thr", invocation="turn-1"),
                               provider="codex", turn_id="act-1", received_at_ms=NOW + 10_000)
        tail = [o for o in out if not o.known]
        assert {o.metric for o in tail} == {"tokens.input", "tokens.output", "tokens.total", "tokens.cache_read", "tokens.reasoning_output"}
        assert all(o.scope is Scope.THREAD_CUMULATIVE and o.observed_at_ms == NOW + 10_000 for o in tail)

    def test_rate_limits_are_capacity_for_the_named_account(self):
        data = json.loads((FIXTURES / "codex" / "rate-limits.json").read_text())
        read = from_codex_rate_limits(data["read_result"], received_at_ms=NOW)
        assert {(o.pool_id, o.key, o.value) for o in read} == {
            ("codex:acct-7f3a", "codex:primary", Decimal(42)), ("codex:acct-7f3a", "codex:secondary", Decimal(7)),
            ("codex:acct-7f3a", "codex_bengalfox:primary", Decimal(12)),
        }
        assert all(o.session_id is None and o.scope is Scope.WINDOW and o.unit == "percent" for o in read)
        update = from_codex_rate_limits(data["updated"], received_at_ms=NOW + 1, pool_id="codex:acct-7f3a")
        assert [(o.key, o.observed_at_ms, o.source) for o in update] == [("codex:primary", 1790400060000, "codex.account.rateLimits.updated")]

    def test_malformed_updates_observe_nothing(self):
        assert from_codex_token_usage({"method": "thread/tokenUsage/updated", "params": {"threadId": "t"}}, received_at_ms=NOW) == ()
        bad = {"params": {"threadId": "t", "turnId": "u", "tokenUsage": {"total": {"inputTokens": -5, "outputTokens": True}, "last": {}}}}
        assert from_codex_token_usage(bad, received_at_ms=NOW) == ()


def shim(tmp_path: Path, name: str, script: str) -> str:
    return str(make_exe(tmp_path, name, script=EMU / script))


class TestEmulatedProviders:
    """The real D04 adapters, emulated providers, then the ledger."""

    def test_claude_resumed_session_is_counted_as_deltas(self, tmp_path):
        adapter = ClaudeCLIAdapter(shim(tmp_path, "claude", "fake_claude.py"))
        first = adapter.run_turn(TurnRequest(prompt="a", cwd=tmp_path))
        second = adapter.run_turn(TurnRequest(prompt="b", cwd=tmp_path, session_id=first.session_id, env={"FAKE_CLAUDE_PRIOR_COST": "0.25"}))
        assert second.lineage == "resumed_same"
        ledger = Ledger()
        for i, result in enumerate((first, second)):
            ledger.ingest_all(from_turn_result(result, provider="claude", turn_id=f"act-{i}", received_at_ms=NOW + i * 1000, pool_id="claude:me"))
        cost = ledger.consumption(session_id=first.session_id).get("claude", "cost.estimated_usd")
        naive = sum(u.value for r in (first, second) for u in r.usage if u.source == "claude.result.total_cost_usd")
        assert naive == Decimal("0.75")  # what summing results would claim
        assert cost.value == Decimal("0.5") and cost.quality is Quality.ESTIMATED
        assert [v.status for v in cost.validations] == ["agree"]  # per-model costUSD validates, never adds

    def test_codex_thread_totals_are_counted_as_deltas(self, tmp_path):
        adapter = CodexAppServerAdapter(shim(tmp_path, "codex", "fake_codex_appserver.py"))
        try:
            first = adapter.run_turn(TurnRequest(prompt="a", cwd=tmp_path, permission_profile="read_only"))
            second = adapter.run_turn(TurnRequest(prompt="b", cwd=tmp_path, session_id=first.session_id, permission_profile="read_only"))
        finally:
            adapter.close()
        ledger = Ledger()
        for i, result in enumerate((first, second)):
            ledger.ingest_all(from_turn_result(result, provider="codex", turn_id=f"act-{i}", received_at_ms=NOW + i * 1000,
                                               pool_id="codex:acct", epoch="appserver-1"))
        report = ledger.consumption(session_id=first.session_id)
        assert report.get("codex", "tokens.total").value == 660  # not 360 + 660
        assert report.total_tokens("codex").value == 660
        assert [w.used_percent for w in ledger.capacity(now_ms=NOW + 2000, pool_id="codex:acct")] == [Decimal(43)]
