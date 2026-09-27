"""D08 in the store: finishing reserves, draws, provider scope, quota holds,
overshoot, and the D07 review findings on double counting, exhausted cost
pools and in-doubt turns. Everything here is also replayed from the event log."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from duet.runtime.api import Runtime
from duet.runtime.budgeting import Budgeting
from duet.runtime.contracts import CONTROLLER, USER, PolicyDenied, RunLifecycle
from duet.runtime.policy import AuthorisationPolicy
from duet.runtime.pools import PoolStore
from duet.runtime.store import Store
from duet.usage.admission import AdmissionPolicy
from duet.usage.reservations import ReservationBook, to_ms


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        return self.now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def rt(tmp_path: Path, clock):
    return Runtime(Store(tmp_path / "rt.db"), clock=clock)


def new_run(rt: Runtime) -> str:
    return rt.create_run(USER, repo_id="repo", objective="x", policy=AuthorisationPolicy(), acceptance={"criteria": []})["run_id"]


def finish(rt: Runtime, action_id: str, pools: PoolStore, run_id: str, quantity: str = "1", metric_pool: tuple = ()) -> None:
    fence = rt.claim_action(CONTROLLER, action_id)["lease"]["fencing_token"]
    rt.record_action(CONTROLLER, action_id, "RUNNING", fence=fence)
    actuals = {}
    for res in rt.store.read().query("SELECT reservation_id, pool, metric FROM reservations WHERE action_id = ?", (action_id,)):
        amount = "1" if res["metric"] == "turns" else quantity
        pools.record_usage(CONTROLLER, record_id=f"{action_id}:{res['pool']}", pool_id=res["pool"], metric=res["metric"], quantity=amount,
                           quality="observed", source="test", run_id=run_id, action_id=action_id)
        actuals[res["reservation_id"]] = {"quantity": amount}
    rt.record_action(CONTROLLER, action_id, "SUCCEEDED", fence=fence, actuals=actuals)


def admit(book: ReservationBook, run_id: str, action_class: str, purpose: str, provider: str = "codex", **kw):
    return book.admit(run_id=run_id, provider=provider, action_class=action_class, purpose=purpose, input={"p": purpose}, **kw)


def pool_status(pools: PoolStore, pool_id: str) -> tuple:
    s = pools.status(pool_id)[0]
    return s["used"], s["held"], s["available"]


def test_review_capacity_is_reserved_and_drawn_once(rt):
    """AT13 and AT14 against the store: the reserve is held when the pair
    forms; optional work cannot touch it; the review draws it exactly once."""
    pools, book = PoolStore(rt), ReservationBook(rt)
    pools.define_pool(USER, "codex-turns", provider="codex", metric="turns", unit="turns", allowance=4)
    run = new_run(rt)
    reserves = book.ensure_finishing(run, writer=None, reviewer="codex")
    assert [(r["purpose"], r["units_left"], r["quantity_json"]) for r in reserves] == [("review", 2, '"2"')]
    assert book.ensure_finishing(run, writer=None, reviewer="codex") == reserves  # idempotent
    assert pool_status(pools, "codex-turns") == ("0", "2", "2")
    for _ in range(2):
        finish(rt, admit(book, run, "optional", "investigate")["action"]["action_id"], pools, run)
    deferred = admit(book, run, "optional", "investigate")
    assert deferred["decision"].verdict == "defer" and deferred["action"] is None
    review = admit(book, run, "finishing", "review")
    assert review["decision"].verdict == "admit"
    assert pool_status(pools, "codex-turns") == ("2", "2", "0")  # 1 still earmarked + 1 for the running review: not 3
    finish(rt, review["action"]["action_id"], pools, run)
    assert pool_status(pools, "codex-turns") == ("3", "1", "0")
    reserve = book.status(run)["finishing"][0]
    assert (reserve["units_left"], reserve["held"]) == (1, "1")
    verdicts = [(a["action_class"], a["verdict"]) for a in reversed(book.status(run)["admissions"])]
    assert verdicts == [("optional", "admit"), ("optional", "admit"), ("optional", "defer"), ("finishing", "admit")]
    rt.transition_run(USER, run, RunLifecycle.CANCELLED, reason="done")
    assert pool_status(pools, "codex-turns") == ("3", "0", "1")  # the unused reserve is given back
    assert rt.store.verify_replay() == []


def test_another_runs_optional_work_cannot_take_this_runs_reserve(rt):
    pools, book = PoolStore(rt), ReservationBook(rt)
    pools.define_pool(USER, "codex-turns", provider="codex", metric="turns", unit="turns", allowance=3)
    run_a, run_b = new_run(rt), new_run(rt)
    book.ensure_finishing(run_a, writer=None, reviewer="codex")
    assert admit(book, run_b, "optional", "investigate")["decision"].verdict == "admit"
    assert admit(book, run_b, "optional", "investigate")["decision"].verdict == "defer"
    assert admit(book, run_a, "finishing", "review")["decision"].verdict == "admit"


def test_a_claude_review_is_never_funded_from_codex_capacity(rt):
    """Exit criterion (either direction): pools and reserves are provider scoped."""
    pools, book = PoolStore(rt), ReservationBook(rt)
    pools.define_pool(USER, "codex-turns", provider="codex", metric="turns", unit="turns", allowance=100)
    pools.define_pool(USER, "claude-turns", provider="claude", metric="turns", unit="turns", allowance=0)
    run = new_run(rt)
    book.ensure_finishing(run, writer="codex", reviewer="claude")
    finishing = {(r["purpose"], r["pool"]): r for r in book.status(run)["finishing"]}
    assert finishing[("review", "claude-turns")]["shortfall"] == "2" and finishing[("repair", "codex-turns")]["held"] == "2"
    review = admit(book, run, "finishing", "review", provider="claude")
    assert review["decision"].verdict == "pause" and review["decision"].pause_kind == "budget"
    assert pool_status(pools, "codex-turns") == ("0", "2", "98")  # untouched
    with pytest.raises(PolicyDenied, match="cannot fund claude work"):
        rt.plan_action(CONTROLLER, run_id=run, type="provider_turn", input={},
                       reservations=[{"provider": "claude", "pool": "codex-turns", "metric": "turns", "quantity": 1}])
    with pytest.raises(PolicyDenied, match="counts turns"):
        rt.plan_action(CONTROLLER, run_id=run, type="provider_turn", input={},
                       reservations=[{"provider": "codex", "pool": "codex-turns", "metric": "cost.estimated_usd", "quantity": 1}])


def test_cost_reserves_follow_the_estimate_and_record_shortfalls(rt):
    pools, book = PoolStore(rt), ReservationBook(rt)
    pools.define_pool(USER, "codex-usd", provider="codex", metric="cost.estimated_usd", unit="USD", allowance="1.20")
    run = new_run(rt)
    book.ensure_finishing(run, writer="codex", reviewer=None)
    assert book.status(run)["finishing"][0]["per_unit"] is None  # no measured turn yet: nothing invented
    first = admit(book, run, "required", "implement")
    assert first["decision"].lines[0].unknown_size
    finish(rt, first["action"]["action_id"], pools, run, quantity="0.20")
    book.resize_finishing(run, "codex")
    reserve = book.status(run)["finishing"][0]
    assert (reserve["per_unit"], reserve["held"], reserve["shortfall"]) == ("0.30", "0.60", None)  # 2 repairs x (0.20 x 1.5)
    finish(rt, admit(book, run, "required", "implement")["action"]["action_id"], pools, run, quantity="0.20")
    # 0.40 used, 0.60 held for repairs, 0.20 free: an implementation turn (0.30) no longer fits.
    blocked = admit(book, run, "required", "implement")
    assert blocked["decision"].verdict == "pause" and "finishing reserves" in blocked["decision"].reason
    assert admit(book, run, "finishing", "repair")["decision"].verdict == "admit"
    assert rt.store.verify_replay() == []


def test_overshoot_is_recorded_not_hidden(rt):
    """AT46: a turn can exceed its estimate; the pool reports it."""
    pools, book = PoolStore(rt), ReservationBook(rt)
    pools.define_pool(USER, "codex-usd", provider="codex", metric="cost.estimated_usd", unit="USD", allowance="1.00")
    run = new_run(rt)
    finish(rt, admit(book, run, "required", "implement")["action"]["action_id"], pools, run, quantity="0.10")
    second = admit(book, run, "required", "implement")  # reserves 0.15
    finish(rt, second["action"]["action_id"], pools, run, quantity="0.90")
    status = pools.status("codex-usd")[0]
    assert (status["used"], status["available"], status["overshoot"]) == ("1.00", "0.00", "0.75")
    assert admit(book, run, "required", "implement")["decision"].verdict == "pause"


def test_recorded_usage_supersedes_the_reservation(rt):
    """Review finding: between recording a turn's usage and settling its
    action, the turn was counted twice and another run was refused."""
    pools = PoolStore(rt)
    pools.define_pool(USER, "p", provider="codex", metric="turns", unit="turns", allowance=2)
    run_a, run_b = new_run(rt), new_run(rt)
    planned = rt.plan_action(CONTROLLER, run_id=run_a, type="provider_turn", input={}, reservations=[{"provider": "codex", "pool": "p", "metric": "turns", "quantity": 1}])
    action_id = planned["action"]["action_id"]
    pools.record_usage(CONTROLLER, record_id=f"{action_id}:p", pool_id="p", metric="turns", quantity=1, quality="observed", source="t", run_id=run_a, action_id=action_id)
    assert pool_status(pools, "p") == ("1", "0", "1")
    rt.plan_action(CONTROLLER, run_id=run_b, type="provider_turn", input={}, reservations=[{"provider": "codex", "pool": "p", "metric": "turns", "quantity": 1}])


def test_an_exhausted_cost_pool_refuses_zero_reservations(rt):
    """Review finding: `0 > 0` let turns through an exhausted (or $0) pool."""
    pools = PoolStore(rt)
    pools.define_pool(USER, "zero", provider="claude", metric="cost.estimated_usd", unit="USD", allowance="0")
    run = new_run(rt)
    with pytest.raises(PolicyDenied):
        rt.plan_action(CONTROLLER, run_id=run, type="provider_turn", input={}, reservations=[{"provider": "claude", "pool": "zero", "metric": "cost.estimated_usd", "quantity": "0"}])


def test_quota_holds_probe_once_and_release(rt, clock):
    """AT40 at the store: after the resume time only one admission probes;
    the others wait for its outcome; success releases the hold."""
    book = ReservationBook(rt)
    run_a, run_b = new_run(rt), new_run(rt)
    hold = book.place_hold("codex", "rate_limit: usage limit reached")
    assert hold.resume_at_ms == to_ms(clock()) + 5 * 60_000 and not book.hold_passed("codex")
    assert admit(book, run_a, "required", "implement")["decision"].verdict == "pause"
    clock.advance(minutes=6)
    probe = admit(book, run_a, "required", "implement")
    assert probe["decision"].hold == "probe"
    waiting = admit(book, run_b, "required", "implement")
    assert waiting["decision"].verdict == "defer" and waiting["decision"].transient
    book.settle_probe("codex", probe["action"]["action_id"], quota_failed=False)
    assert book.hold("codex") is None
    assert admit(book, run_b, "required", "implement")["decision"].hold is None
    again = book.place_hold("codex", "rate_limit again")
    assert again.attempts == 0  # a new pause after a release starts the backoff over
    assert rt.store.verify_replay() == []


def test_external_usage_shows_as_a_gauge_change_and_replans(rt):
    """AT12: usage outside DUET appears only as a higher reading; optional
    work is deferred under the new pressure, never a promised balance."""
    book = ReservationBook(rt)
    run = new_run(rt)
    now = to_ms(rt.clock())
    book.observe_quota("codex", [{"window": "codex:primary:300m", "used_percent": D(40), "resets_at_ms": now + 3_600_000, "observed_at_ms": now, "source": "t"}])
    assert admit(book, run, "optional", "investigate")["decision"].verdict == "admit"
    book.observe_quota("codex", [{"window": "codex:primary:300m", "used_percent": D(93), "resets_at_ms": now + 3_600_000, "observed_at_ms": now + 1000, "source": "t"}])
    assert admit(book, run, "optional", "investigate")["decision"].verdict == "defer"
    assert admit(book, run, "required", "implement")["decision"].verdict == "admit"
    gauge = book.status()["gauges"][0]
    assert (gauge["used_percent"], gauge["previous_percent"]) == ("93", "40") and "not a guaranteed balance" in gauge["note"]
    # A late, older reading never replaces the newer one.
    book.observe_quota("codex", [{"window": "codex:primary:300m", "used_percent": D(10), "resets_at_ms": None, "observed_at_ms": now - 5000, "source": "t"}])
    assert book.status()["gauges"][0]["used_percent"] == "93"


def test_in_doubt_turns_count_as_dispatched_with_unknown_cost(rt, clock):
    """Review finding: an in-doubt turn's usage became zero. A turn lost
    with the service counts as one turn; its cost is unknown, not zero."""
    pools, book = PoolStore(rt), ReservationBook(rt)
    pools.define_pool(USER, "codex-turns", provider="codex", metric="turns", unit="turns", allowance=5)
    pools.define_pool(USER, "codex-usd", provider="codex", metric="cost.estimated_usd", unit="USD", allowance="5")
    run = new_run(rt)
    planned = admit(book, run, "required", "implement")
    action_id = planned["action"]["action_id"]
    fence = rt.claim_action(CONTROLLER, action_id, lease_seconds=60)["lease"]["fencing_token"]
    rt.record_action(CONTROLLER, action_id, "RUNNING", fence=fence)
    clock.advance(minutes=5)
    report = rt.reconcile(CONTROLLER)
    assert report["in_doubt"] == [action_id]
    budget = Budgeting.__new__(Budgeting)
    budget.co, budget.book = SimpleNamespace(runtime=rt), book
    assert budget.settle_in_doubt_turns(report["in_doubt"]) == [action_id]
    assert pool_status(pools, "codex-turns")[:2] == ("1", "0")
    usd = pools.status("codex-usd")[0]
    assert (usd["used"], usd["held"], usd["uncertain"]) == ("0", "0", True)
    assert rt.store.read().require("actions", action_id)["state"] == "FAILED"
    assert rt.store.verify_replay() == []


def test_admission_policy_is_configurable_per_service(rt):
    book = ReservationBook(rt, AdmissionPolicy(quota_defer_percent=D(50)))
    run = new_run(rt)
    now = to_ms(rt.clock())
    book.observe_quota("claude", [{"window": "five_hour", "used_percent": D(60), "resets_at_ms": None, "observed_at_ms": now, "source": "statusline"}])
    assert admit(book, run, "optional", "investigate", provider="claude")["decision"].verdict == "defer"
