"""D09: the router's deterministic policy, case by case (exit criteria and
AT16-AT20, AT41)."""
from __future__ import annotations

import pytest

from duet.routing.contracts import AgentAssessment, Candidate, FailureRecord, Pin, PriorDecision, ProviderControls, RoutingRequest, TaskFacts
from duet.routing.failures import classify
from duet.routing.policy import route
from duet.routing.profiles import candidates_for

CLAUDE = ProviderControls("claude", "unsupported", "supported", ("low", "medium", "high", "xhigh", "max"), None)
CODEX = ProviderControls("codex", "supported", "supported", ("high", "low", "medium", "minimal", "xhigh"), ("m1", "m2"))
BLIND = ProviderControls("claude", "unsupported", "unsupported", None, None)


def req(paths=(), description="add mul", *, purpose="implement", role="writer", controls=CLAUDE, origin="managed", failures=(),
        pins=(), history=(), pressure="none", agent=None, kind="code", has_checks=True, user_map=()):
    task = TaskFacts("tsk_1", 1, kind, description, tuple(paths), None, (), has_checks, 0, tuple(failures))
    return RoutingRequest("run_1", "prt_1", controls.provider, origin, role, purpose, task, controls, len(history), agent,
                          tuple(pins), tuple(user_map), tuple(history), pressure)


def codes(decision):
    return {r.code for r in decision.reasons} | {r.code for r in decision.assessment.reasons}


def test_routine_work_gets_a_routine_profile():
    d = route(req(["docs/readme.md"], "fix a typo in the readme"))
    assert (d.profile, d.effort, d.coverage, d.action) == ("routine", "low", "enforced", "run")


def test_a_small_auth_patch_is_risk_bearing_regardless_of_size():
    """AT16."""
    d = route(req(["src/auth/token.py"], "tweak the check"))
    assert d.assessment.risk in ("high", "critical") and d.profile in ("deep", "critical_review")
    review = route(req(["src/auth/login.py"], "x", purpose="review", role="reviewer"))
    assert review.profile == "deep" and review.effort == "xhigh"
    critical = route(req(["migrations/0003.sql", "billing/invoice.py"], "x"))
    assert critical.assessment.risk == "critical" and critical.profile == "critical_review" and critical.effort == "max"


def test_word_matching_is_whole_word():
    assert route(req(["src/tokenizer_utils/keyboard.py"], "improve the tokenizer")).assessment.risk == "low"


def test_unsupported_settings_are_excluded_with_evidence():
    """AT17: a setting the provider does not offer is excluded and the
    provider default is used explicitly."""
    narrow = ProviderControls("claude", "unsupported", "supported", ("low", "medium"), None)
    mapped = (Candidate("claude", "routine", "claude-x", "low", "user_map"),)
    d = route(req(["a.py"], controls=narrow, user_map=mapped))
    assert any("model is not controllable" in e.reason for e in d.excluded)
    blind = route(req(["a.py"], controls=BLIND))
    assert (blind.model, blind.effort, blind.coverage) == (None, None, "advisory")
    pinned = route(req(["a.py"], pins=[Pin("claude", effort="ultra")]))
    assert "pin_invalid" in codes(pinned) and pinned.effort is None and "downgraded" in codes(pinned)


def test_native_sessions_get_advice():
    """AT41: settings change at a supported boundary or not at all."""
    d = route(req(["src/auth/x.py"], origin="native_original"))
    assert d.coverage == "advisory" and "Advisory" in d.explanation


def test_user_pins_are_respected_and_bound_escalation():
    """AT18."""
    d = route(req(["src/auth/x.py"], pins=[Pin("claude", effort="low")]))
    assert d.effort == "low" and d.floor_met is False and "pin_below_floor" in codes(d)
    capped = route(req(["a.py"], failures=[FailureRecord("check", "c", "AssertionError")] * 3,
                       history=[PriorDecision("tsk_1", "standard", None, "medium", 0, "failed")], pins=[Pin("claude", max_profile="standard")]))
    assert capped.profile == "standard"


def test_environment_failures_are_diagnosed_not_escalated():
    """AT19 and exit criterion."""
    failures = [FailureRecord("check", "c", "E   ModuleNotFoundError: No module named 'yaml'\nFAILED tests/test_x.py")] * 3
    d = route(req(["a.py"], failures=failures, history=[PriorDecision("tsk_1", "routine", None, "low", 0, "failed")]))
    assert d.action == "diagnose_environment" and not d.escalated and d.profile == "routine"
    assert "stronger model would not help" in d.explanation


def test_repeated_hypothesis_failures_replan_with_one_bounded_escalation():
    """AT20."""
    wrong = [FailureRecord("check", "c", "AssertionError: expected 6 got 5")] * 2
    first = route(req(["a.py"], failures=wrong, history=[PriorDecision("tsk_1", "routine", None, "low", 0, "failed")]))
    assert first.action == "replan" and first.escalated and first.profile in ("standard", "deep")
    again = route(req(["a.py"], failures=wrong * 2, history=[PriorDecision("tsk_1", first.profile, None, first.effort, 1, "failed", True)]))
    assert again.action == "replan" and not again.escalated and "escalation_bounded" in codes(again)


def test_requirements_failures_ask_for_clarity():
    d = route(req(["a.py"], failures=[FailureRecord("turn", "t", "The requirement is ambiguous: which of the two APIs?")]))
    assert d.action == "clarify"


def test_budget_pressure_never_goes_below_the_floor():
    """Exit criterion."""
    base = dict(paths=["src/auth/x.py"], history=[PriorDecision("tsk_1", "critical_review", None, "max", 0, None)])
    normal = route(req(**base))
    pressed = route(req(**base, pressure="pressure"))
    critical = route(req(**base, pressure="critical"))
    floor = normal.assessment.floor
    assert critical.profile == floor and pressed.profile in (floor, "deep", "critical_review")
    assert all(("routine", "standard").count(d.profile) == 0 for d in (normal, pressed, critical))


def test_agents_raise_but_never_lower_scrutiny():
    up = route(req(["a.py"], agent=AgentAssessment(profile="deep", reason="subtle")))
    assert up.profile == "deep" and "agent_raised" in codes(up)
    down = route(req(["src/auth/x.py"], agent=AgentAssessment(risk="low", profile="routine", reason="trivial")))
    assert down.profile != "routine" and "agent_lower_ignored" in codes(down)


def test_switch_cost_and_hysteresis():
    d = route(req(["a.py"], controls=CODEX, history=[PriorDecision("tsk_x", "routine", "m1", "low", 0, "succeeded")]))
    assert d.switch and d.switch_cost == "high"  # model m1 -> default
    effort_only = route(req(["src/auth/a.py"], controls=CODEX, history=[PriorDecision("tsk_x", "routine", None, "low", 0, "succeeded")]))
    assert effort_only.switch_cost == "low"


def test_the_decision_is_always_for_the_requesting_provider_and_deterministic():
    for r in (req(["src/auth/x.py"]), req(["a.py"], controls=CODEX), req(["a.py"], origin="native_original")):
        assert route(r) == route(r)
        assert all(c.provider == r.provider for c in route(r).candidates)


@pytest.mark.parametrize("controls, expected", [
    (CLAUDE, ["low", "medium", "xhigh", "max"]),
    (CODEX, ["low", "medium", "high", "xhigh"]),
])
def test_effort_positions_rank_each_providers_own_labels(controls, expected):
    got = [next(c.effort for c in candidates_for(controls, p) if c.source == "effort_order") for p in ("routine", "standard", "deep", "critical_review")]
    assert got == expected


@pytest.mark.parametrize("text, kind, expected", [
    ("E   AssertionError: assert 5 == 6\nFAILED tests/test_a.py::test_x", None, "hypothesis"),
    ("ModuleNotFoundError: No module named 'requests'\nFAILED tests/test_a.py", None, "environment"),
    ("sh: 1: pytest: not found", None, "environment"),
    ("bash: foo: command not found", None, "environment"),
    ("PermissionError: [Errno 13] Permission denied: '/etc/x'", None, "environment"),
    ("You've hit your usage limit", None, "provider"),
    ("", "rate_limit", "provider"),
    ("the check timed out after 600 s", None, "timeout"),
    ("The requirement is ambiguous", None, "requirements"),
    ("changes requested: off by one", None, "hypothesis"),
    ("", None, "unknown"),
])
def test_failure_classification(text, kind, expected):
    assert classify(FailureRecord("check", "s", text, kind))[0] == expected
