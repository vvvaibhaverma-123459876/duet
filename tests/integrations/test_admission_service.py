"""D08 through the runtime service with scripted providers: quota loss keeps
the review pending (AT07), a managed pair pauses and resumes at the reset,
a cap the provider cannot enforce stops paid work before it starts (AT15),
a provider that can enforce one gets it per call, and a passive quota read
ends a pause without spending a turn (AT40). Nothing switches provider,
account or billing."""
from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from pairkit import MUL, make_repo
from test_runtime_service import CHECK, ScriptedProvider, message_ids, paths, start, wait_until  # noqa: F401  (paths is a fixture)

from duet.adapters import QuotaError
from duet.providers.base import UsageObservation
from duet.runtime.contracts import USER
from duet.runtime.peers import ManagedPeer
from duet.runtime.pools import PoolStore
from duet.runtime.service import ServiceClient
from duet.usage.admission import AdmissionPolicy

FAST = AdmissionPolicy(backoff_base_ms=1500, backoff_max_ms=3000, reset_grace_ms=0)
API_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY")


def factory(adapters: dict, launched: list):
    def make(service, run_id, provider):
        adapter = adapters[provider]
        launched.append(provider)
        return ManagedPeer(service.coordinator, run_id=run_id, provider=provider, adapter=adapter, state_root=service.paths.root).start()

    return make


def reviewer_script(fail_first: int):
    """Approves review requests, after raising a quota error `fail_first` times."""
    state = {"failures": 0}

    def codex(tools, request):
        mids = message_ids(request.prompt, "REVIEW_REQUEST")
        if mids and state["failures"] < fail_first:
            state["failures"] += 1
            raise QuotaError("codex: You've hit your usage limit.", kind="quota")
        for mid in mids:
            tools.call("send", kind="REVIEW_RESULT", body="Looks right.", reply_to=mid, review={"disposition": "approve"})
        return "reviewed"

    return codex


def notes(client) -> list[str]:
    return [m["body"] for m in client.call("inbox")["messages"] if m["kind"] == "STATUS"]


def test_peer_quota_loss_keeps_the_review_pending(paths, tmp_path):
    """AT07 and exit criterion: the writer may go on, checks pass, but pair
    completion waits for the review; after the reset one turn resumes it."""
    codex = ScriptedProvider(paths, reviewer_script(fail_first=1))
    launched: list = []
    service = start(paths, peer_factory=factory({"codex": codex}, launched))
    service.coordinator.budget.book.policy = FAST
    try:
        client = ServiceClient(paths)
        joined = client.call("join", provider="claude", objective="add mul", repo=str(make_repo(tmp_path / "repo")), checks=[CHECK], peer="managed")
        me = client.with_token(joined["token"])
        claimed = me.call("claim")
        Path(claimed["workspace"], "calc.py").write_text(MUL)
        me.call("submit", summary="mul")
        assert wait_until(lambda: any("paused for quota" in n for n in notes(me)), 20)
        note = next(n for n in notes(me) if "paused for quota" in n)
        assert "completion waits for it" in note and "does not switch accounts" in note
        status = me.call("status")
        assert status["lifecycle"] == "REVIEWING"  # a native participant is present: the run stays active
        assert status["admission"]["holds"][0]["provider"] == "codex"
        assert wait_until(lambda: me.call("status")["verification"]["checks"].get("check1", {}).get("status") == "passed", 20)
        completion = me.call("status")["verification"]["completion"]
        assert completion["outcome"] != "COMPLETED_VERIFIED" and not next(i for i in completion["items"] if i["name"] == "non_author_review")["ok"]
        assert len(codex.requests) == 1  # no retry before the resume time
        assert wait_until(lambda: me.call("status")["lifecycle"] == "COMPLETED_VERIFIED", 30)
        assert len(codex.requests) == 2 and launched == ["codex"]  # one probe turn, same provider and adapter
        assert all(not (set(API_KEYS) & set(r.env or {})) for r in codex.requests)
        assert service.coordinator.budget.book.hold("codex") is None
    finally:
        service.close()


def test_managed_pair_pauses_for_quota_and_resumes_after_the_reset(paths, tmp_path):
    def claude(tools, request):
        if "No messages yet" in request.prompt:
            claimed = tools.call("claim")
            Path(claimed["workspace"], "calc.py").write_text(MUL)
            tools.call("submit", summary="mul")
        return "submitted"

    adapters = {"claude": ScriptedProvider(paths, claude), "codex": ScriptedProvider(paths, reviewer_script(fail_first=1))}
    launched: list = []
    service = start(paths, peer_factory=factory(adapters, launched))
    service.coordinator.budget.book.policy = FAST
    try:
        controller = ServiceClient.as_controller(paths)
        run_id = controller.call("pair", objective="add mul", repo=str(make_repo(tmp_path / "repo")), checks=[CHECK], writer="claude")["run_id"]
        seen: set = set()

        def lifecycle():
            state = controller.call("run_status", run_id=run_id)["lifecycle"]
            seen.add(state)
            return state

        assert wait_until(lambda: lifecycle() == "COMPLETED_VERIFIED", 60)
        assert "PAUSED_QUOTA" in seen or any(
            e.payload["to"] == "PAUSED_QUOTA" for e in service.store.events(run_id=run_id) if e.type == "run.lifecycle"
        )
        assert len(adapters["codex"].requests) == 2 and sorted(launched) == ["claude", "codex"]
        reasons = [e.payload["reason"] for e in service.store.events(run_id=run_id) if e.type == "run.lifecycle" and e.payload["to"] == "RECONCILING"]
        assert reasons and "resume time" in reasons[0]
    finally:
        service.close()


def test_an_unenforceable_cap_stops_paid_work_before_it_starts(paths, tmp_path):
    """AT15: disclosed before paid work; the user changes the pool and resumes."""
    codex = ScriptedProvider(paths, lambda tools, request: "noted")
    service = start(paths, peer_factory=factory({"codex": codex}, []))
    try:
        pools = PoolStore(service.runtime)
        pools.define_pool(USER, "codex-usd", provider="codex", metric="cost.estimated_usd", unit="USD", allowance="5", enforcement="provider_cap")
        client = ServiceClient(paths)
        joined = client.call("join", provider="claude", objective="x", repo=str(make_repo(tmp_path / "repo")), checks=[CHECK], peer="managed")
        me = client.with_token(joined["token"])
        me.call("send", kind="QUESTION", body="ints?")
        assert wait_until(lambda: me.call("status")["lifecycle"] == "PAUSED_APPROVAL", 20)
        assert any("cannot enforce" in n for n in notes(me)) and codex.requests == []
        pools.define_pool(USER, "codex-usd", provider="codex", metric="cost.estimated_usd", unit="USD", allowance="5", enforcement="local_bound")
        resumed = ServiceClient.as_controller(paths).call("resume", run_id=joined["run_id"], reason="pool changed to local_bound")
        assert resumed["lifecycle"] == "EXECUTING"
        assert wait_until(lambda: len(codex.requests) == 1, 20)
        assert codex.requests[0].max_budget_usd is None  # local_bound: nothing is claimed of the provider
    finally:
        service.close()


class CappedClaude(ScriptedProvider):
    def capabilities(self):
        return SimpleNamespace(provider_budget_cap=True)


def test_a_provider_enforced_cap_is_passed_per_call(paths, tmp_path):
    claude = CappedClaude(paths, lambda tools, request: "noted")
    service = start(paths, peer_factory=factory({"claude": claude}, []))
    try:
        PoolStore(service.runtime).define_pool(USER, "claude-usd", provider="claude", metric="cost.estimated_usd", unit="USD", allowance="2.50", enforcement="provider_cap")
        client = ServiceClient(paths)
        joined = client.call("join", provider="codex", objective="x", repo=str(make_repo(tmp_path / "repo")), checks=[CHECK], peer="managed")
        me = client.with_token(joined["token"])
        me.call("send", kind="QUESTION", body="ints?")
        assert wait_until(lambda: len(claude.requests) == 1, 20)
        assert claude.requests[0].max_budget_usd == Decimal("2.50")
        decision = service.coordinator.budget.book.status(joined["run_id"])["admissions"][0]["detail"]
        assert decision["enforcement"] == {"claude-usd": "provider_cap"}
    finally:
        service.close()


class PassiveCodex(ScriptedProvider):
    """Offers a quota read that costs no model turn (like account/rateLimits/read)."""

    reads = 0

    def read_rate_limits(self):
        PassiveCodex.reads += 1
        resets = int(time.time()) + 3600
        return (UsageObservation("quota.used_percent", Decimal(5), "window", "codex.account.rateLimits", unit="percent", key=f"codex:primary:300m:resets={resets}"),), None


def test_a_passive_quota_read_ends_the_pause_without_a_probe(paths, tmp_path):
    codex = PassiveCodex(paths, reviewer_script(fail_first=1))
    service = start(paths, peer_factory=factory({"codex": codex}, []))
    service.coordinator.budget.book.policy = FAST
    try:
        client = ServiceClient(paths)
        joined = client.call("join", provider="claude", objective="add mul", repo=str(make_repo(tmp_path / "repo")), checks=[CHECK], peer="managed")
        me = client.with_token(joined["token"])
        claimed = me.call("claim")
        Path(claimed["workspace"], "calc.py").write_text(MUL)
        me.call("submit", summary="mul")
        assert wait_until(lambda: me.call("status")["lifecycle"] == "COMPLETED_VERIFIED", 30)
        assert PassiveCodex.reads >= 1 and len(codex.requests) == 2
        admissions = service.coordinator.budget.book.status(joined["run_id"])["admissions"]
        last_review = next(a for a in admissions if a["purpose"] == "review" and a["verdict"] == "admit")
        assert last_review["detail"]["quota"] == "observed" and last_review["detail"]["hold"] is None  # released by the read, not probed
        gauges = service.coordinator.budget.book.status()["gauges"]
        assert gauges[0]["used_percent"] == "5" and gauges[0]["source"] == "codex.account.rateLimits"
    finally:
        service.close()


# -- the managed turn path without a service (review findings on D07 usage) --------------------


class Results:
    name = "claude"

    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def run_turn(self, request, *, on_event=None, cancel=None):
        self.requests.append(request)
        return self.results.pop(0)(request)


def claude_result(session: str, cumulative: str):
    from duet.providers.base import SettingsRecord, TurnResult

    def make(request):
        lineage = "new" if request.session_id is None else ("resumed_same" if session == request.session_id else "resumed_new_id")
        scope = "session_cumulative" if request.session_id else "call"
        usage = (UsageObservation("cost_usd", Decimal(cumulative), scope, "claude.result.total_cost_usd", quality="estimated", unit="USD"),)
        return TurnResult(status="completed", text="ok", session_id=session, lineage=lineage,
                          settings=SettingsRecord({"resume": request.session_id, "fork": False}, {}, {"session_id": session}), usage=usage, duration_s=1.0)

    return make


def managed_claude(tmp_path, results):
    from pairkit import coordinator, live_host

    co = coordinator(tmp_path / "state")
    started = co.join(None, provider="codex", objective="add mul", repo=str(make_repo(tmp_path / "repo")), checks=[CHECK], peer="invite", host=live_host())
    peer = ManagedPeer(co, run_id=started["run_id"], provider="claude", adapter=Results(results), state_root=tmp_path / "state")
    return co, peer, started["run_id"]


QUESTION = [{"kind": "QUESTION", "message_id": "msg_q", "seq": 1, "from": "codex", "body": "ints?"}]


def test_turn_cost_survives_a_resume_that_returns_a_new_session(tmp_path):
    """Review finding: per-session totals undercounted after `resumed_new_id`
    and the negative delta was dropped. The run's total is used instead."""
    co, peer, run_id = managed_claude(tmp_path, [claude_result("s1", "0.05"), claude_result("s2", "0.07"), claude_result("s3", "0.10")])
    pools = PoolStore(co.runtime)
    pools.define_pool(USER, "claude-cost", provider="claude", metric="cost.estimated_usd", unit="USD", allowance="10")
    for _ in range(3):
        assert peer._turn(QUESTION) == "done"
    assert [(r["quantity"], r["quality"]) for r in pools.run_usage(run_id)] == [("0.05", "estimated"), ("0.02", "estimated"), ("0.03", "estimated")]
    status = pools.status("claude-cost")[0]
    assert (status["used"], status["uncertain"]) == ("0.10", False)


def test_an_exhausted_cost_pool_stops_the_turn_before_dispatch(tmp_path):
    """Review finding: a $0 (or spent) cost pool let turns through."""
    co, peer, run_id = managed_claude(tmp_path, [claude_result("s1", "0.40")])
    PoolStore(co.runtime).define_pool(USER, "claude-cost", provider="claude", metric="cost.estimated_usd", unit="USD", allowance="0")
    peer._stop.set()  # return from the pause instead of waiting for the user
    assert peer._turn(QUESTION) == "retry"
    assert peer.adapter.requests == []
    assert co.runtime.get_run(USER, run_id)["lifecycle"] == "PAUSED_BUDGET"
