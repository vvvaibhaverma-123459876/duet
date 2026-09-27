"""D07: the shared usage ledger. Each test names the exit criterion or
acceptance test it proves. Fixtures: tests/fixtures/telemetry (see
SOURCES.json for which are verbatim from official docs and which are
constructed against documented or generated schemas)."""
from __future__ import annotations

import copy
import itertools
import json
import random
from decimal import Decimal
from pathlib import Path

import pytest

from duet.integrations.claude_statusline import observe_status_line
from duet.providers.base import SettingsRecord, TurnRequest, TurnResult, UsageObservation
from duet.providers.claude_cli import ClaudeCLIAdapter, _StreamState
from duet.usage import Baseline, Freshness, Ingest, Ledger, Observation, ObservationError, Quality, Scope
from duet.usage.observations import (
    from_claude_assistant_messages,
    from_codex_rate_limits,
    from_codex_token_usage,
    from_turn_result,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "telemetry"
NOW = 1_790_400_000_000
COST = "cost.estimated_usd"


def docs_statusline() -> dict:
    return json.loads((FIXTURES / "claude" / "statusline-docs-full-schema.json").read_text())


def statusline(session: str, *, cost=None, context_input=None, rate=None, drop_rate_limits=False) -> dict:
    payload = docs_statusline()
    payload["session_id"] = session
    if cost is not None:
        payload["cost"]["total_cost_usd"] = Decimal(cost)
    if context_input is not None:
        payload["context_window"]["total_input_tokens"] = context_input
    if rate is not None:
        payload["rate_limits"]["five_hour"]["used_percentage"] = Decimal(rate)
    if drop_rate_limits:
        payload.pop("rate_limits")
    return payload


def claude_turn(session, cost, *, lineage, status="completed", requested=None, model_costs=None, tokens=None) -> TurnResult:
    scope = "call" if lineage == "new" else "session_cumulative"
    usage = []
    if cost is not None:
        usage.append(UsageObservation("cost_usd", Decimal(cost), scope, "claude.result.total_cost_usd", quality="estimated", unit="USD"))
    for model, value in (model_costs or {}).items():
        usage.append(UsageObservation("cost_usd", Decimal(value), scope, "claude.result.modelUsage", quality="estimated", unit="USD", key=model))
    for model, (inp, out) in (tokens or {}).items():
        usage.append(UsageObservation("tokens.input", inp, scope, "claude.result.modelUsage", unit="tokens", key=model))
        usage.append(UsageObservation("tokens.output", out, scope, "claude.result.modelUsage", unit="tokens", key=model))
        usage.append(UsageObservation("tokens.cache_read", 0, scope, "claude.result.modelUsage", unit="tokens", key=model))
        usage.append(UsageObservation("tokens.cache_write", 0, scope, "claude.result.modelUsage", unit="tokens", key=model))
    return TurnResult(status=status, text="", session_id=session, lineage=lineage,
                      settings=SettingsRecord(requested or ({} if lineage == "new" else {"resume": session}), {}, {}),
                      usage=tuple(usage), duration_s=1.0)


def ingest_turn(ledger, result, turn_id, at, **kw):
    return ledger.ingest_all(from_turn_result(result, provider="claude", turn_id=turn_id, received_at_ms=at, **kw))


def codex_updates() -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / "codex" / "token-usage-updates.jsonl").read_text().splitlines()]


def codex_ledger(messages, **kw) -> Ledger:
    ledger = Ledger()
    for message in messages:
        ledger.ingest_all(from_codex_token_usage(message, received_at_ms=NOW, baseline=Baseline.ZERO, pool_id="codex:acct-7f3a", **kw))
    return ledger


def cumulative(session, value, at, *, provider="codex", pool="codex:a", baseline=Baseline.ZERO, epoch="", metric="tokens.total", event=None):
    unit = "USD" if metric.startswith("cost") else "tokens"
    quality = Quality.ESTIMATED if metric.startswith("cost") else Quality.OBSERVED
    return Observation(provider=provider, session_id=session, metric=metric, value=value, unit=unit,
                       scope=Scope.THREAD_CUMULATIVE if provider == "codex" else Scope.SESSION_CUMULATIVE,
                       source="codex.thread.tokenUsage.total" if provider == "codex" else "claude.result.total_cost_usd",
                       source_event_id=event or f"ev-{session}-{at}-{value}", observed_at_ms=at, quality=quality, pool_id=pool,
                       epoch=epoch, baseline=baseline)


class TestSameWorkSeenTwice:
    """Exit: the same work seen by multiple telemetry paths is not counted twice."""

    def test_statusline_and_cli_result_count_the_same_turn_once(self):
        session, ledger = "0b3f2d9e-5c1a-4e77-a0d4-9f8e7d6c5b4a", Ledger()
        for i, cost in enumerate(("0", "0.0300", "0.0421")):
            ledger.ingest_all(observe_status_line(statusline(session, cost=cost), observed_at_ms=NOW + i * 4_000))
        ingest_turn(ledger, claude_turn(session, "0.0421", lineage="new", model_costs={"claude-opus-5-5": "0.0421"}), "act-1", NOW + 10_000)
        cost = ledger.consumption(session_id=session).get("claude", COST)
        assert cost.value == Decimal("0.0421")  # not 0.0421 (result) + 0.0421 (status line) + 0.0421 (per-model)
        assert cost.primary == {session: "claude.result.total_cost_usd"}
        assert {(v.source, v.status) for v in cost.validations} == {("claude.statusline", "agree"), ("claude.result.modelUsage", "agree")}

    def test_a_newer_status_line_validates_but_never_adds(self):
        session, ledger = "s-ahead", Ledger()
        ingest_turn(ledger, claude_turn(session, "0.0421", lineage="new"), "act-1", NOW)
        ledger.ingest_all(observe_status_line(statusline(session, cost="0.0600"), observed_at_ms=NOW + 5_000))
        cost = ledger.consumption(session_id=session).get("claude", COST)
        assert cost.value == Decimal("0.0421")
        assert [(v.status, v.value) for v in cost.validations] == [("ahead", Decimal("0.0600"))]

    def test_disagreement_is_preserved_not_resolved(self):
        session, ledger = "s-disagree", Ledger()
        ledger.ingest_all(observe_status_line(statusline(session, cost="0.0500"), observed_at_ms=NOW))
        ingest_turn(ledger, claude_turn(session, "0.0421", lineage="new"), "act-1", NOW + 5_000)
        cost = ledger.consumption(session_id=session).get("claude", COST)
        assert cost.value == Decimal("0.0421") and cost.disputed
        assert [(v.primary_value, v.value, v.status) for v in cost.validations] == [(Decimal("0.0421"), Decimal("0.0500"), "disagree")]

    def test_status_line_alone_is_the_fallback_primary(self):
        session, ledger = "native-1", Ledger()
        for i, cost in enumerate(("0", "0.10", "0.25")):
            ledger.ingest_all(observe_status_line(statusline(session, cost=cost), observed_at_ms=NOW + i))
        cost = ledger.consumption(session_id=session).get("claude", COST)
        assert cost.value == Decimal("0.25") and cost.primary == {session: "claude.statusline"}

    def test_parallel_tool_messages_sharing_an_id_count_once(self, tmp_path):
        events = [json.loads(line) for line in (FIXTURES / "claude" / "stream-parallel-tool-use.jsonl").read_text().splitlines()]
        state = _StreamState()
        for event in events:
            state.feed(json.dumps(event))
        usage = ClaudeCLIAdapter._usage(state, TurnRequest(prompt="x", cwd=tmp_path), True)
        session = state.session_id
        result = TurnResult(status="completed", text="", session_id=session, lineage="new", settings=SettingsRecord({}, {}, {}), usage=usage)
        ledger = Ledger()
        outcomes = ledger.ingest_all(from_claude_assistant_messages(events, received_at_ms=NOW))
        assert outcomes.count(Ingest.DUPLICATE) == 9  # three repeats of msg_01STEP1 x three token fields
        ledger.ingest_all(from_turn_result(result, provider="claude", turn_id="act-1", received_at_ms=NOW + 1))
        report = ledger.consumption(session_id=session)
        assert report.get("claude", "tokens.input").value == 1200  # not 900 x 4 + 300 + 1200
        assert report.get("claude", "tokens.cache_read").value == 9200
        assert {v.status for m in ("tokens.input", "tokens.cache_read", "tokens.cache_write") for v in report.get("claude", m).validations} == {"agree"}
        assert report.get("claude", "tokens.output").value == 410  # the placeholder output_tokens=1 is never used

    def test_replayed_codex_updates_count_once(self):  # AT10
        updates = codex_updates()
        ledger = Ledger()
        outcomes = [ledger.ingest_all(from_codex_token_usage(m, received_at_ms=NOW, baseline=Baseline.ZERO)) for m in updates]
        tokens_outcomes = [[o for o, ob in zip(out, from_codex_token_usage(m, received_at_ms=NOW)) if ob.dimension.value == "tokens"]
                           for out, m in zip(outcomes, updates)]
        assert all(o == Ingest.DUPLICATE for o in tokens_outcomes[2] + tokens_outcomes[3])  # verbatim and re-emitted replays
        report = ledger.consumption()
        assert report.get("codex", "tokens.total").value == 6100  # naive: 1150 + 2800 * 3 + 6100
        assert report.get("codex", "tokens.input").value == 5400
        assert [(v.source, v.status) for v in report.get("codex", "tokens.total").validations] == [("codex.thread.tokenUsage.last", "agree")]


class TestCumulativeCounters:
    def test_cumulative_resume_totals_become_deltas(self):
        ledger = Ledger()
        ingest_turn(ledger, claude_turn("s", "0.25", lineage="new"), "t1", NOW)
        ingest_turn(ledger, claude_turn("s", "0.40", lineage="resumed_same"), "t2", NOW + 1_000)
        ingest_turn(ledger, claude_turn("s", "0.70", lineage="resumed_same"), "t3", NOW + 2_000)
        cost = ledger.consumption(session_id="s").get("claude", COST)
        assert cost.value == Decimal("0.70")
        assert [d.value for d in cost.deltas] == [Decimal("0.25"), Decimal("0.15"), Decimal("0.30")]

    def test_attached_session_history_is_unknown_but_bounded(self):
        ledger = Ledger()
        ingest_turn(ledger, claude_turn("attached", "1.20", lineage="resumed_same"), "t1", NOW)
        ingest_turn(ledger, claude_turn("attached", "1.50", lineage="resumed_same"), "t2", NOW + 1_000)
        cost = ledger.consumption(session_id="attached").get("claude", COST)
        assert cost.value is None and cost.unknown_segments == 1
        assert (cost.lower_bound, cost.upper_bound) == (Decimal("0.30"), Decimal("1.50"))

    def test_fork_does_not_recount_the_parent_spend(self):
        ledger = Ledger()
        ingest_turn(ledger, claude_turn("parent", "0.40", lineage="new"), "t1", NOW)
        ingest_turn(ledger, claude_turn("child", "0.55", lineage="forked", requested={"resume": "parent", "fork": True}), "t2", NOW + 1_000)
        assert ledger.consumption().get("claude", COST).value == Decimal("0.55")  # not 0.40 + 0.55

    def test_a_decrease_is_a_reset_not_a_negative_or_absolute_delta(self):
        ledger = Ledger()
        ledger.ingest_all([cumulative("thr", v, NOW + i) for i, v in enumerate((500, 100, 300))])
        total = ledger.consumption().get("codex", "tokens.total")
        assert [d.value for d in total.deltas] == [500, None, 200]
        assert all(d.value is None or d.value >= 0 for d in total.deltas)
        assert total.value is None and total.lower_bound == 700 and total.upper_bound is None  # pre-reset tail is unknown
        [reset] = ledger.resets()
        assert (reset.before, reset.after) == (500, 100) and total.resets == (reset,)

    def test_declared_epochs_are_never_diffed_against_each_other(self):
        ledger = Ledger()
        ledger.ingest_all([cumulative("thr", 900, NOW, epoch="proc-1"),
                           cumulative("thr", 1000, NOW + 5, epoch="proc-2", baseline=Baseline.UNKNOWN)])
        total = ledger.consumption().get("codex", "tokens.total")
        assert [d.value for d in total.deltas] == [900, None]  # 1000 - 900 would assume the counter survived a restart
        assert total.upper_bound == 1900 and not ledger.resets()

    def test_out_of_order_updates_give_the_same_answer(self):
        updates = codex_updates()
        expected = None
        for order in itertools.permutations(range(len(updates))):
            report = codex_ledger([updates[i] for i in order]).consumption()
            answer = {k: (m.value, m.lower_bound, m.upper_bound) for k, m in report.metrics.items()}
            expected = expected or answer
            assert answer == expected
        assert expected[("codex", "tokens.total")] == (6100, 6100, 6100)


class TestUnknownsAndStaleness:
    def test_unknown_usage_is_never_zero(self):  # AT08
        ledger = Ledger()
        ingest_turn(ledger, claude_turn("s", None, lineage="new", status="cancelled"), "t1", NOW)
        cost = ledger.consumption(session_id="s").get("claude", COST)
        assert cost.value is None and cost.lower_bound is None and cost.quality is Quality.UNKNOWN and cost.unknown_segments == 1
        assert ledger.consumption().total_tokens("claude").value is None
        assert ledger.consumption().get("codex", "tokens.total") is None  # nothing observed: absent, not zero

    def test_a_later_cumulative_value_resolves_the_unknown(self):
        ledger = Ledger()
        ingest_turn(ledger, claude_turn("s", None, lineage="new", status="cancelled"), "t1", NOW)
        ingest_turn(ledger, claude_turn("s", "0.40", lineage="resumed_same"), "t2", NOW + 1_000)
        cost = ledger.consumption(session_id="s").get("claude", COST)
        assert cost.value == Decimal("0.40")  # counted from the session's zero, covering the cancelled turn

    def test_absent_final_usage_after_cancellation_stays_unknown(self):
        ledger = Ledger()
        ledger.ingest_all(from_turn_result(
            TurnResult(status="cancelled", text="", session_id="thr", lineage="new", settings=SettingsRecord({}, {}, {}),
                       usage=tuple(UsageObservation(m, v, "thread_cumulative", "codex.thread.tokenUsage", unit="tokens", observed_at_ms=NOW)
                                   for m, v in (("tokens.input", 1000), ("tokens.output", 150), ("tokens.total", 1150),
                                                ("tokens.cache_read", 200), ("tokens.reasoning_output", 60))),
                       provider_invocation_id="turn-1"),
            provider="codex", turn_id="act-1", received_at_ms=NOW + 5_000))
        total = ledger.consumption().get("codex", "tokens.total")
        assert total.value is None and total.lower_bound == 1150 and total.upper_bound is None

    def test_undesignated_sources_are_not_counted(self):
        ledger = Ledger()
        ledger.ingest_all(from_claude_assistant_messages(
            [{"type": "assistant", "session_id": "s", "message": {"id": "m1", "usage": {"input_tokens": 50}}}], received_at_ms=NOW))
        tokens = ledger.consumption().get("claude", "tokens.input")
        assert tokens.value is None and tokens.primary == {"s": None}

    def test_stale_observations_are_flagged(self):
        ledger = Ledger()
        ingest_turn(ledger, claude_turn("s", "0.10", lineage="new"), "t1", NOW)
        ledger.ingest_all(observe_status_line(statusline("s"), observed_at_ms=NOW))
        assert ledger.consumption(now_ms=NOW + 60_000).get("claude", COST).stale is False
        assert ledger.consumption(now_ms=NOW + 16 * 60_000).get("claude", COST).stale is True
        assert ledger.context("s", now_ms=NOW + 10 * 60_000)["context.input_tokens"].freshness is Freshness.STALE
        windows = {w.key: w for w in ledger.capacity(now_ms=NOW + 11 * 60_000)}
        assert windows["five_hour"].freshness is Freshness.EXPIRED  # the docs example's resets_at is long past
        assert windows["five_hour"].current_used_percent is None and windows["five_hour"].used_percent == Decimal("23.5")


class TestPools:
    def test_two_models_and_two_runs_share_one_pool_without_double_counting(self):  # AT09
        ledger, pool = Ledger(), "claude:acct-1"
        ingest_turn(ledger, claude_turn("a", "0.30", lineage="new", model_costs={"opus": "0.25", "haiku": "0.05"},
                                        tokens={"opus": (1000, 100), "haiku": (4000, 50)}), "t1", NOW, pool_id=pool, run_id="run-1")
        ingest_turn(ledger, claude_turn("b", "0.10", lineage="new", model_costs={"opus": "0.10"}, tokens={"opus": (300, 30)}),
                    "t2", NOW + 1, pool_id=pool, run_id="run-2")
        ingest_turn(ledger, claude_turn("c", "9.99", lineage="new"), "t3", NOW + 2, pool_id="claude:other", run_id="run-2")
        report = ledger.pool_consumption(pool)
        assert report.get("claude", COST).value == Decimal("0.40")  # not 0.80 with the per-model breakdown added
        tokens = report.get("claude", "tokens.input")
        assert tokens.value == 5300 and dict(tokens.by_key) == {"haiku": 4000, "opus": 1300}
        assert ledger.run_consumption("run-1").get("claude", COST).value == Decimal("0.30")
        assert ledger.run_consumption("run-2").get("claude", COST).value == Decimal("10.09")

    def test_sessions_on_one_pool_see_one_window(self):
        ledger = Ledger()
        ledger.ingest_all(observe_status_line(statusline("a", rate="30"), observed_at_ms=NOW, pool_id="claude:acct-1"))
        ledger.ingest_all(observe_status_line(statusline("b", rate="34.5"), observed_at_ms=NOW + 1_000, pool_id="claude:acct-1"))
        ledger.ingest_all(observe_status_line(statusline("c", rate="90"), observed_at_ms=NOW + 2_000))  # unknown pool
        five = [w for w in ledger.capacity(now_ms=NOW + 3_000) if w.key == "five_hour"]
        assert [(w.pool_id, w.used_percent) for w in five] == [(None, Decimal("90")), ("claude:acct-1", Decimal("34.5"))]

    def test_an_account_change_leaves_the_delta_unattributed(self):
        ledger = Ledger()
        ledger.ingest_all([cumulative("thr", 100, NOW, pool="codex:a"), cumulative("thr", 250, NOW + 1, pool="codex:b")])
        assert ledger.pool_consumption("codex:a").get("codex", "tokens.total").value == 100
        assert ledger.pool_consumption("codex:b").get("codex", "tokens.total") is None
        assert ledger.unattributed().get("codex", "tokens.total").value == 150
        assert ledger.consumption(session_id="thr").get("codex", "tokens.total").value == 250
        [change] = ledger.pool_changes()
        assert (change.before, change.after) == ("codex:a", "codex:b")


class TestGauges:
    def test_context_occupancy_never_enters_consumption(self):  # AT11
        ledger = Ledger()
        ingest_turn(ledger, claude_turn("s", "0.10", lineage="new", tokens={"opus": (1000, 100)}), "t1", NOW)
        ledger.ingest_all(observe_status_line(statusline("s", context_input=150_000), observed_at_ms=NOW + 1))
        ledger.ingest_all(observe_status_line(statusline("s", context_input=20_000), observed_at_ms=NOW + 2))  # after /compact
        report = ledger.consumption()
        assert not any(metric.startswith("context.") for _, metric in report.metrics)
        assert report.get("claude", "tokens.input").value == 1000
        assert ledger.context("s", now_ms=NOW + 3)["context.input_tokens"].value == 20_000
        assert not ledger.resets()  # a smaller context is not a counter reset

    def test_quota_percentages_never_become_tokens_or_an_average(self):
        data = json.loads((FIXTURES / "codex" / "rate-limits.json").read_text())
        ledger = Ledger()
        ledger.ingest_all(from_codex_rate_limits(data["read_result"], received_at_ms=NOW))
        ledger.ingest_all(from_codex_rate_limits(data["updated"], received_at_ms=NOW, pool_id="codex:acct-7f3a"))
        ledger.ingest_all(observe_status_line(statusline("s", rate="23.5", cost="0"), observed_at_ms=NOW, pool_id="claude:acct-1"))
        now = NOW + 61_000
        report = ledger.consumption()
        assert all(not metric.startswith(("tokens", "quota")) for _, metric in report.metrics)
        assert report.total_tokens("codex").value is None
        by_pool = {}
        for w in ledger.capacity(now_ms=now):
            by_pool.setdefault(w.pool_id, {})[w.key] = w.used_percent
        assert by_pool["codex:acct-7f3a"] == {"codex:primary": Decimal(45), "codex:secondary": Decimal(7), "codex_bengalfox:primary": Decimal(12)}
        assert by_pool["claude:acct-1"]["five_hour"] == Decimal("23.5")  # separate, never averaged with Codex
        assert ledger.binding_window("codex:acct-7f3a", now_ms=now).key == "codex:primary"

    def test_a_pool_mixing_providers_is_refused(self):
        ledger = Ledger()
        ledger.ingest_all(from_codex_rate_limits(json.loads((FIXTURES / "codex" / "rate-limits.json").read_text())["read_result"],
                                                 received_at_ms=NOW, pool_id="shared"))
        ledger.ingest_all(observe_status_line(statusline("s", rate="23.5"), observed_at_ms=NOW, pool_id="shared"))
        with pytest.raises(ObservationError, match="not comparable"):
            ledger.binding_window("shared", now_ms=NOW + 1)

    def test_latest_gauge_wins_whatever_the_arrival_order(self):
        data = json.loads((FIXTURES / "codex" / "rate-limits.json").read_text())
        newer = from_codex_rate_limits(data["updated"], received_at_ms=NOW, pool_id="codex:acct-7f3a")
        older = from_codex_rate_limits(data["read_result"], received_at_ms=NOW)
        ledger = Ledger()
        ledger.ingest_all(newer + older)
        primary = next(w for w in ledger.capacity(now_ms=NOW + 61_000) if w.key == "codex:primary")
        assert primary.used_percent == Decimal(45)


class TestTokenAlgebra:
    def test_reasoning_and_cached_tokens_are_not_double_counted(self):
        report = codex_ledger(codex_updates()).consumption()
        total = report.total_tokens("codex")
        assert total.value == 6100 and total.derivation == "reported tokens.total"
        assert report.get("codex", "tokens.reasoning_output").value == 280 and report.get("codex", "tokens.output").value == 700
        naive = sum(report.get("codex", m).value for m in ("tokens.input", "tokens.cache_read", "tokens.output", "tokens.reasoning_output"))
        assert naive == 9880 and "tokens.reasoning_output" in total.excluded and "tokens.cache_read" in total.excluded

    def test_derived_total_uses_only_disjoint_components(self):
        updates = codex_updates()
        for update in updates:
            for part in ("total", "last"):
                update["params"]["tokenUsage"][part].pop("totalTokens")
        total = codex_ledger(updates).consumption().total_tokens("codex")
        assert total.value == 5400 + 700 and total.derivation == "tokens.input + tokens.output"
        claude = Ledger()
        ingest_turn(claude, claude_turn("s", "0.1", lineage="new", tokens={"opus": (1000, 100)}), "t1", NOW)
        assert claude.consumption().total_tokens("claude").value == 1100  # Anthropic fields are disjoint


class TestLedgerMechanics:
    def test_conflicting_duplicate_is_kept_once_and_reported(self):
        ledger = Ledger()
        first = cumulative("thr", 100, NOW, event="same")
        assert ledger.ingest(first) == Ingest.ACCEPTED
        assert ledger.ingest(first) == Ingest.DUPLICATE
        assert ledger.ingest(cumulative("thr", 120, NOW, event="same")) == Ingest.CONFLICT
        assert ledger.consumption().get("codex", "tokens.total").value == 100 and len(ledger.conflicts) == 1

    @staticmethod
    def native_ledger() -> Ledger:
        ledger = Ledger()
        for i, cost in enumerate(("1.00", "1.20", "1.50")):
            ledger.ingest_all(observe_status_line(statusline("native", cost=cost), observed_at_ms=NOW + i * 1_000))
        return ledger

    def test_native_session_attributed_to_a_run_from_its_start(self):
        ledger = self.native_ledger()
        ledger.attribute_session("native", "run-1", since_ms=NOW)
        assert ledger.run_consumption("run-1").get("claude", COST).value == Decimal("0.50")  # 1.00 predates the run
        ledger = self.native_ledger()
        ledger.attribute_session("native", "run-1", since_ms=NOW + 500)
        cost = ledger.run_consumption("run-1").get("claude", COST)
        assert cost.value is None and (cost.lower_bound, cost.upper_bound) == (Decimal("0.30"), Decimal("0.50"))

    def test_a_native_session_can_serve_runs_in_turn(self):
        ledger = self.native_ledger()
        ledger.attribute_session("native", "run-1", since_ms=NOW, until_ms=NOW + 1_000)
        ledger.attribute_session("native", "run-2", since_ms=NOW + 1_000)
        assert ledger.run_consumption("run-1").get("claude", COST).value == Decimal("0.20")
        assert ledger.run_consumption("run-2").get("claude", COST).value == Decimal("0.30")
        with pytest.raises(ObservationError):
            ledger.attribute_session("native", "run-3", since_ms=NOW, until_ms=NOW)

    def test_answers_do_not_depend_on_arrival_order(self):
        observations = []
        for message in codex_updates():
            observations += from_codex_token_usage(message, received_at_ms=NOW, baseline=Baseline.ZERO, pool_id="codex:p")
        for i, cost in enumerate(("0.25", "0.40", "0.30", "0.70")):
            observations += from_turn_result(claude_turn("s", cost, lineage="new" if i == 0 else "resumed_same"), provider="claude",
                                             turn_id=f"t{i}", received_at_ms=NOW + i, pool_id="claude:p")
        observations += observe_status_line(statusline("s", cost="0.70"), observed_at_ms=NOW + 9)

        def answer(obs):
            ledger = Ledger()
            ledger.ingest_all(obs)
            report = ledger.consumption(now_ms=NOW + 10)
            return repr(sorted((k, m.lower_bound, m.upper_bound, m.unknown_segments, [v.status for v in m.validations])
                               for k, m in report.metrics.items())), repr(ledger.resets())

        expected = answer(observations)
        rng = random.Random(7)
        for _ in range(20):
            shuffled = copy.copy(observations)
            rng.shuffle(shuffled)
            assert answer(shuffled) == expected
