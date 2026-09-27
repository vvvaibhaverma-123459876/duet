"""D08: pure admission decisions. Each test names the spec rule or
acceptance test it pins down."""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from duet.usage.admission import (
    ActionRequest,
    AdmissionPolicy,
    Gauge,
    Hold,
    PoolView,
    ReserveView,
    backoff_ms,
    classify_enforcement,
    decide,
    plan_finishing,
)
from duet.usage.estimation import estimate_turn

NOW = 1_800_000_000_000
MIN = 60_000
TURN = {"turns": estimate_turn("turns", [])}


def pool(pool_id="codex-turns", provider="codex", metric="turns", allowance="10", used="0", held="0", enforcement="local_bound", unit=None, unknown=0):
    return PoolView(pool_id, provider, metric, unit or ("turns" if metric == "turns" else "USD"),
                    None if allowance is None else D(allowance), D(used), D(held), enforcement, unknown)


def request(action_class="required", purpose="implement", provider="codex", estimates=None, **kw):
    return ActionRequest(provider, action_class, purpose, TURN if estimates is None else estimates, **kw)


def admit(req, pools=(), reserves=(), gauges=(), hold=None, policy=AdmissionPolicy()):
    return decide(req, list(pools), list(reserves), list(gauges), hold, now_ms=NOW, policy=policy)


# -- estimates ---------------------------------------------------------------------------------


def test_estimates_are_explainable_ranges_or_unknown():
    assert estimate_turn("turns", []).high == 1
    unknown = estimate_turn("cost.estimated_usd", [])
    assert unknown.high is None and unknown.quality == "unknown"  # never an invented number
    few = estimate_turn("cost.estimated_usd", [D("0.10"), D("0.30")])
    assert (few.low, few.high) == (D("0.10"), D("0.45")) and "x1.5" in few.basis
    many = estimate_turn("cost.estimated_usd", [D("0.20")] * 5)
    assert many.high == D("0.25")


# -- local pools and finishing reserves --------------------------------------------------------


def test_optional_work_cannot_spend_the_finishing_reserve():
    """AT13: 10 turns, 8 used, 2 held for the review: optional work defers."""
    p = pool(used="8", held="2")
    decision = admit(request("optional", "investigate"), [p])
    assert decision.verdict == "defer" and "including finishing reserves" in decision.reason
    required = admit(request("required", "implement"), [p])
    assert required.verdict == "pause" and required.pause_kind == "budget"  # the user decides, no silent overspend


def test_a_finishing_action_draws_its_reserve_once():
    """AT14: the review turn is funded by its earmarked reserve, not reserved again."""
    p = pool(used="8", held="2")
    reserve = ReserveView("rsv_review", "review", "codex-turns", D(2), 2)
    decision = admit(request("finishing", "review"), [p], [reserve])
    assert decision.verdict == "admit"
    assert [(line.quantity, line.draw_from) for line in decision.lines] == [(D(1), "rsv_review")]
    # A repair is not a review: it cannot use the review's reserve.
    assert admit(request("finishing", "repair"), [p], [reserve]).verdict == "pause"


def test_what_the_reserve_does_not_cover_needs_free_capacity():
    cost = pool("claude-cost", "claude", "cost.estimated_usd", allowance="1.00", used="0.50", held="0.20")
    estimates = {"turns": TURN["turns"], "cost.estimated_usd": estimate_turn("cost.estimated_usd", [D("0.20")])}  # high 0.30
    reserve = ReserveView("rsv_r", "review", "claude-cost", D("0.20"), 1)
    decision = admit(request("finishing", "review", "claude", estimates), [cost], [reserve])
    assert [(line.quantity, line.draw_from) for line in decision.lines] == [(D("0.20"), "rsv_r"), (D("0.10"), None)]
    tight = pool("claude-cost", "claude", "cost.estimated_usd", allowance="0.75", used="0.50", held="0.20")
    assert admit(request("finishing", "review", "claude", estimates), [tight], [reserve]).verdict == "pause"


def test_a_fully_reserved_review_runs_even_when_the_pool_is_overdrawn():
    over = pool(used="11", held="1")  # other work overshot the allowance
    decision = admit(request("finishing", "review"), [over], [ReserveView("rsv", "review", "codex-turns", D(1), 1)])
    assert decision.verdict == "admit" and any("overdrawn" in w for w in decision.warnings)


def test_capacity_of_one_provider_never_funds_another():
    """Exit criterion: a required Claude review cannot be funded from Codex capacity."""
    with pytest.raises(ValueError, match="cannot be funded"):
        admit(request("finishing", "review", "claude"), [pool(provider="codex")])


def test_unknown_record_sizes_add_a_margin():
    cost = pool("c", "codex", "cost.estimated_usd", allowance="1.00", used="0.40", unknown=2)
    estimates = {"cost.estimated_usd": estimate_turn("cost.estimated_usd", [D("0.20")])}  # high 0.30; margin 2 x 0.30
    assert admit(request(estimates=estimates), [cost]).verdict == "pause"
    certain = pool("c", "codex", "cost.estimated_usd", allowance="1.00", used="0.40")
    assert admit(request(estimates=estimates), [certain]).verdict == "admit"


def test_unknown_turn_size_is_bounded_not_imagined():
    """7.3: no fictional inequality: one unknown-size turn at a time while
    the pool has room; optional work waits for a measured turn."""
    cost = pool("c", "codex", "cost.estimated_usd", allowance="1.00", used="0.40")
    estimates = {"cost.estimated_usd": estimate_turn("cost.estimated_usd", [])}
    first = admit(request(estimates=estimates), [cost])
    assert first.verdict == "admit" and first.lines[0].unknown_size and first.lines[0].quantity == 0
    assert any("can overshoot" in w for w in first.warnings)
    second = admit(request(estimates=estimates, unknown_in_flight={"c": 1}), [cost])
    assert second.verdict == "defer" and second.transient
    assert admit(request("optional", "investigate", estimates=estimates), [cost]).verdict == "defer"
    empty = pool("c", "codex", "cost.estimated_usd", allowance="1.00", used="1.00")
    assert admit(request(estimates=estimates), [empty]).verdict == "pause"


def test_best_effort_and_tracking_pools_never_refuse():
    tracked = [pool("t", allowance=None), pool("b", allowance="1", used="5", enforcement="best_effort")]
    decision = admit(request("optional", "investigate"), tracked)
    assert decision.verdict == "admit" and dict(decision.enforcement) == {"t": "best_effort", "b": "best_effort"}


# -- enforcement labels ------------------------------------------------------------------------


def test_a_provider_cap_is_used_only_where_the_provider_enforces_it():
    """AT15: disclosed before any paid work; never a false cap."""
    capped = pool("claude-usd", "claude", "cost.estimated_usd", allowance="2.00", used="0.50", enforcement="provider_cap")
    estimates = {"cost.estimated_usd": estimate_turn("cost.estimated_usd", [D("0.20")])}
    ok = admit(request(provider="claude", estimates=estimates, per_call_budget_cap=True), [capped])
    assert ok.verdict == "admit" and dict(ok.enforcement) == {"claude-usd": "provider_cap"} and ok.per_call_budget == D("1.50")
    codex = pool("codex-usd", "codex", "cost.estimated_usd", allowance="2.00", enforcement="provider_cap")
    refused = admit(request(estimates=estimates), [codex])
    assert refused.verdict == "pause" and refused.pause_kind == "approval" and "cannot enforce" in refused.reason
    turns = pool("claude-turns", "claude", "turns", allowance="5", enforcement="provider_cap")
    assert admit(request(provider="claude", per_call_budget_cap=True), [turns]).pause_kind == "approval"  # a money cap is not a turn cap
    assert classify_enforcement(pool(), False) == "local_bound"


# -- provider quota ----------------------------------------------------------------------------


def gauge(percent, *, age_min=1, resets_in_min=60, provider="codex", window="codex:primary:300m"):
    return Gauge(provider, window, D(percent), NOW + resets_in_min * MIN if resets_in_min is not None else None, NOW - age_min * MIN)


def test_an_exhausted_window_pauses_until_its_reset():
    decision = admit(request("finishing", "review"), gauges=[gauge(100, resets_in_min=30)])
    assert (decision.verdict, decision.pause_kind, decision.hold) == ("pause", "quota", "place")
    assert decision.resume_at_ms == NOW + 30 * MIN + AdmissionPolicy().reset_grace_ms
    unknown_reset = admit(request(), gauges=[gauge(100, resets_in_min=None)])
    assert unknown_reset.resume_at_ms == NOW + backoff_ms(0, AdmissionPolicy())


def test_pressure_defers_optional_work_only():
    """7.4: under pressure, optional work waits; the critical path continues."""
    g = [gauge(92)]
    assert admit(request("optional", "investigate"), gauges=g).verdict == "defer"
    required = admit(request("required", "implement"), gauges=g)
    assert required.verdict == "admit" and required.quota == "pressure"


def test_percentages_are_not_averaged_across_windows_or_providers():
    g = [gauge(20, window="codex:secondary"), gauge(100, window="codex:primary"), gauge(100, provider="claude", window="five_hour")]
    assert admit(request(), gauges=g).verdict == "pause"  # the binding window decides, not a mean of 60%
    assert admit(request(), gauges=[gauge(100, provider="claude", window="five_hour")]).verdict == "admit"  # another provider's window


def test_stale_or_reset_telemetry_is_not_trusted():
    past_reset = gauge(100, age_min=5, resets_in_min=-1)
    old = gauge(100, age_min=60)
    for g in (past_reset, old):
        decision = admit(request(), gauges=[g])
        assert decision.verdict == "admit" and decision.quota == "stale"
        assert any("best effort" in w for w in decision.warnings)


def test_unknown_quota_allows_bounded_optional_work():
    """7.3: missing telemetry does not make every task impossible, nor unlimited."""
    policy = AdmissionPolicy(unknown_quota_optional_limit=3)
    assert admit(request("optional", "investigate", optional_so_far=2), policy=policy).verdict == "admit"
    assert admit(request("optional", "investigate", optional_so_far=3), policy=policy).verdict == "defer"
    assert admit(request("required", optional_so_far=99), policy=policy).verdict == "admit"
    assert admit(request("optional", "investigate", optional_so_far=99), gauges=[gauge(10)], policy=policy).verdict == "admit"


def test_after_a_quota_pause_one_probe_goes_first():
    """AT40: reset with stale telemetry: one real turn probes, the rest wait
    for its outcome; a fresh reading below the threshold ends the pause."""
    waiting = Hold("codex", "HELD", "quota", NOW + 5 * MIN, NOW - MIN)
    assert admit(request(), hold=waiting).verdict == "pause"
    due = Hold("codex", "HELD", "quota", NOW - MIN, NOW - 10 * MIN)
    probe = admit(request(), hold=due)
    assert (probe.verdict, probe.hold, probe.quota) == ("admit", "probe", "probe")
    probing = Hold("codex", "PROBING", "quota", NOW - MIN, NOW - 10 * MIN)
    blocked = admit(request(), hold=probing)
    assert blocked.verdict == "defer" and blocked.transient
    fresh = Gauge("codex", "codex:primary", D(3), NOW + 290 * MIN, NOW - 1000)
    released = admit(request(), gauges=[fresh], hold=probing)
    assert (released.verdict, released.hold) == ("admit", "release")
    older = Gauge("codex", "codex:primary", D(3), NOW + 290 * MIN, NOW - 11 * MIN)  # taken before the hold
    assert admit(request(), gauges=[older], hold=probing).verdict == "defer"


def test_backoff_is_bounded():
    policy = AdmissionPolicy()
    assert [backoff_ms(n, policy) // MIN for n in range(6)] == [5, 10, 20, 40, 60, 60]


def test_decisions_never_name_another_provider():
    """Exit criterion: no paid fallback or rotation: a refusal is a pause or
    deferral of this provider's work, never a substitute."""
    decisions = [
        admit(request("finishing", "review"), gauges=[gauge(100)]),
        admit(request("required"), [pool(used="10")]),
        admit(request("optional", "investigate"), [pool(used="8", held="2")]),
    ]
    for decision in decisions:
        assert decision.verdict in ("pause", "defer") and not decision.lines
        assert "claude" not in decision.reason


# -- finishing plan ----------------------------------------------------------------------------


def test_finishing_reserves_go_to_the_provider_that_must_do_the_work():
    pools = [pool("codex-turns", "codex"), pool("claude-turns", "claude"), pool("claude-usd", "claude", "cost.estimated_usd", allowance="5"),
             pool("codex-tracked", "codex", allowance=None), pool("codex-be", "codex", enforcement="best_effort")]
    estimates = {("claude", "cost.estimated_usd"): estimate_turn("cost.estimated_usd", [D("0.40")])}
    lines = plan_finishing(writer_provider="codex", reviewer_provider="claude", pools=pools, estimates=estimates)
    got = {(l.purpose, l.provider, l.pool_id, l.units, l.quantity) for l in lines}
    assert got == {
        ("review", "claude", "claude-turns", 2, D(0)),  # turns: no estimate passed -> unknown size per unit
        ("review", "claude", "claude-usd", 2, D("1.20")),
        ("repair", "codex", "codex-turns", 2, D(0)),
    }
    native_reviewer = plan_finishing(writer_provider="codex", reviewer_provider=None, pools=pools, estimates={})
    assert {l.purpose for l in native_reviewer} == {"repair"}  # a native session's turns are not DUET's to reserve
