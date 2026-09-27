"""D07: user-defined usage pools. Reservations are checked inside the
reserving transaction, so two runs or processes cannot take the same
remaining allowance; usage records are deduplicated; unknown stays unknown."""
from __future__ import annotations

import multiprocessing
import subprocess
import sys
from pathlib import Path

import pytest

from duet.runtime.api import Runtime
from duet.runtime.contracts import CONTROLLER, USER, Conflict, PolicyDenied, Unauthorized, ValidationError
from duet.runtime.policy import AuthorisationPolicy
from duet.runtime.pools import PoolStore
from duet.runtime.store import Store


def runtime(path: Path) -> Runtime:
    return Runtime(Store(path / "rt.db"))


def new_run(rt: Runtime) -> str:
    run = rt.create_run(USER, repo_id="repo", objective="x", policy=AuthorisationPolicy(), acceptance={"criteria": []})
    return run["run_id"]


def reserve(rt: Runtime, run_id: str, pool: str, quantity) -> dict:
    return rt.plan_action(CONTROLLER, run_id=run_id, type="provider_turn", input={"q": str(quantity)},
                          reservations=[{"provider": "codex", "pool": pool, "metric": "turns", "quantity": quantity}])


def finish(rt: Runtime, action_id: str, actuals: dict | None = None) -> None:
    fence = rt.claim_action(CONTROLLER, action_id)["lease"]["fencing_token"]
    rt.record_action(CONTROLLER, action_id, "RUNNING", fence=fence)
    rt.record_action(CONTROLLER, action_id, "SUCCEEDED", fence=fence, actuals=actuals)


def test_only_the_user_defines_allowances(tmp_path):
    pools = PoolStore(runtime(tmp_path))
    for principal in (CONTROLLER,):
        with pytest.raises(Unauthorized):
            pools.define_pool(principal, "p", provider="codex", metric="turns", unit="turns", allowance=10)
    with pytest.raises(ValidationError):
        pools.define_pool(USER, "p", provider="codex", metric="turns", unit="turns", allowance=1.5)  # no floats
    with pytest.raises(ValidationError):
        pools.define_pool(USER, "p", provider="codex", metric="turns", unit="turns", allowance=-1)
    status = pools.define_pool(USER, "p", provider="codex", metric="turns", unit="turns", allowance=10, window_seconds=3600)
    assert status["allowance"] == "10" and status["available"] == "10"


def test_reservations_draw_down_and_refuse_atomically(tmp_path):
    rt = runtime(tmp_path)
    pools = PoolStore(rt)
    pools.define_pool(USER, "codex-turns", provider="codex", metric="turns", unit="turns", allowance=2)
    run_a, run_b = new_run(rt), new_run(rt)  # two runs share the pool
    first = reserve(rt, run_a, "codex-turns", 1)
    reserve(rt, run_b, "codex-turns", 1)
    before = rt.store.read().scalar("SELECT COUNT(*) FROM actions")
    with pytest.raises(PolicyDenied, match="cannot cover"):
        reserve(rt, run_a, "codex-turns", 1)
    assert rt.store.read().scalar("SELECT COUNT(*) FROM actions") == before  # nothing half-created
    assert pools.status("codex-turns")[0]["held"] == "2"
    # A finished action's reservation is reconciled; its usage is the record, counted once.
    finish(rt, first["action"]["action_id"], {r["reservation_id"]: {"quantity": "1"} for r in rt.store.read().query("SELECT reservation_id FROM reservations WHERE action_id = ?", (first["action"]["action_id"],))})
    pools.record_usage(CONTROLLER, record_id="turn-1", pool_id="codex-turns", metric="turns", quantity=1, quality="observed", source="test", run_id=run_a)
    status = pools.status("codex-turns")[0]
    assert (status["used"], status["held"], status["available"]) == ("1", "1", "0")


def test_unconfigured_and_best_effort_pools_do_not_block(tmp_path):
    rt = runtime(tmp_path)
    pools = PoolStore(rt)
    run = new_run(rt)
    reserve(rt, run, "nobody-defined-this", 5)  # no pool: nothing was bounded
    pools.define_pool(USER, "tracked", provider="codex", metric="turns", unit="turns", allowance=1, enforcement="best_effort")
    reserve(rt, run, "tracked", 3)
    assert pools.status("tracked")[0]["available"] == "-2"  # reported honestly, not refused


def test_usage_records_are_deduplicated_and_unknown_is_not_zero(tmp_path):
    rt = runtime(tmp_path)
    pools = PoolStore(rt)
    pools.define_pool(USER, "claude-cost", provider="claude", metric="cost.estimated_usd", unit="USD", allowance="5.00")
    _, created = pools.record_usage(CONTROLLER, record_id="act1:claude-cost", pool_id="claude-cost", metric="cost.estimated_usd", quantity="0.25", quality="estimated", source="claude.turn")
    _, again = pools.record_usage(CONTROLLER, record_id="act1:claude-cost", pool_id="claude-cost", metric="cost.estimated_usd", quantity="0.25", quality="estimated", source="claude.turn")
    assert created and not again  # a replay or reconnect is a no-op
    with pytest.raises(Conflict):
        pools.record_usage(CONTROLLER, record_id="act1:claude-cost", pool_id="claude-cost", metric="cost.estimated_usd", quantity="0.30", quality="estimated", source="claude.turn")
    pools.record_usage(CONTROLLER, record_id="act2:claude-cost", pool_id="claude-cost", metric="cost.estimated_usd", quantity=None, quality="unknown", source="claude.turn")
    status = pools.status("claude-cost")[0]
    assert status["used"] == "0.25" and status["uncertain"] and status["unknown_records"] == 1
    with pytest.raises(ValidationError):
        pools.record_usage(CONTROLLER, record_id="x", pool_id="claude-cost", metric="cost.estimated_usd", quantity=None, quality="estimated", source="s")
    with pytest.raises(Unauthorized):
        pools.record_usage(USER, record_id="y", pool_id="claude-cost", metric="cost.estimated_usd", quantity="1", quality="observed", source="s")


def test_rolling_window_forgets_old_usage(tmp_path):
    rt = runtime(tmp_path)
    pools = PoolStore(rt)
    pools.define_pool(USER, "w", provider="codex", metric="turns", unit="turns", allowance=1, window_seconds=60)
    pools.record_usage(CONTROLLER, record_id="old", pool_id="w", metric="turns", quantity=1, quality="observed", source="t", observed_at="2020-01-01T00:00:00.000000Z")
    assert pools.status("w")[0]["available"] == "1"
    pools.record_usage(CONTROLLER, record_id="new", pool_id="w", metric="turns", quantity=1, quality="observed", source="t")
    assert pools.status("w")[0]["available"] == "0"


def _race(db: str, barrier, results) -> None:
    rt = Runtime(Store(db))
    run = rt.create_run(USER, repo_id="repo", objective="x", policy=AuthorisationPolicy(), acceptance={"criteria": []})["run_id"]
    barrier.wait()
    try:
        reserve(rt, run, "last-unit", 1)
        results.put("reserved")
    except PolicyDenied:
        results.put("refused")


def test_two_processes_cannot_both_take_the_last_unit(tmp_path):
    """Exit criterion: two simultaneous runs cannot locally reserve the same
    remaining allowance twice."""
    rt = runtime(tmp_path)
    PoolStore(rt).define_pool(USER, "last-unit", provider="codex", metric="turns", unit="turns", allowance=1)
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(4)
    results = ctx.Queue()
    procs = [ctx.Process(target=_race, args=(str(tmp_path / "rt.db"), barrier, results)) for _ in range(4)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
    outcomes = sorted(results.get(timeout=5) for _ in procs)
    assert outcomes == ["refused", "refused", "refused", "reserved"]


def test_usage_cli_defines_and_reports(tmp_path):
    root = tmp_path / "state"
    run = [sys.executable, "-m", "duet", "usage"]
    done = subprocess.run([*run, "pool", "set", "codex-turns", "--provider", "codex", "--metric", "turns", "--allowance", "3", "--state-root", str(root)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    shown = subprocess.run([*run, "--json", "--state-root", str(root)], capture_output=True, text=True, timeout=60)
    import json

    report = json.loads(shown.stdout)
    assert report["schema"] == "duet.usage/1" and report["pools"][0]["available"] == "3"
    bad = subprocess.run([*run, "pool", "set", "x", "--provider", "codex", "--metric", "turns", "--allowance", "1.5", "--state-root", str(root)], capture_output=True, text=True, timeout=60)
    assert bad.returncode == 1 and "whole number" in bad.stderr
