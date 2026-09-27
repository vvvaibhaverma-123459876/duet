"""D09 through the runtime service with scripted providers: a small
security-sensitive patch gets a deep review (AT16), a setting the provider
refuses is downgraded with evidence and a clamp shows as a difference
(AT17), a user pin is respected even below the floor (AT18), an environment
failure is diagnosed instead of escalated (AT19), and a native session gets
advice rather than a hidden change (AT41)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from pairkit import MUL, PY, make_repo
from test_runtime_service import CHECK, ScriptedProvider, message_ids, paths, start, wait_until  # noqa: F401  (paths is a fixture)

from duet.providers.base import SettingsRecord, TurnResult, UnsupportedSetting, UsageObservation
from duet.runtime.contracts import USER, Control
from duet.runtime.peers import ManagedPeer
from duet.runtime.service import ServiceClient

CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")


class ControllableClaude(ScriptedProvider):
    """A scripted Claude whose effort is controllable (like --effort). It can
    refuse settings at dispatch and report a different observed effort."""

    def __init__(self, paths, script, *, refuse=(), observed_effort=None):
        super().__init__(paths, script)
        self.refuse = set(refuse)
        self.observed_effort = observed_effort

    def capabilities(self):
        return SimpleNamespace(model_control=Control.UNSUPPORTED, effort_control=Control.SUPPORTED, efforts=CLAUDE_EFFORTS, models=None,
                               provider_budget_cap=False)

    def run_turn(self, request, *, on_event=None, cancel=None):
        if request.effort in self.refuse:
            self.requests.append(request)
            raise UnsupportedSetting(f"effort {request.effort!r} is not available to this organisation")
        result = super().run_turn(request, on_event=on_event, cancel=cancel)
        accepted = {"effort": request.effort} if request.effort else {}
        observed = {"effort": self.observed_effort} if self.observed_effort else {}
        return TurnResult(status=result.status, text=result.text, session_id=result.session_id, lineage=result.lineage,
                          settings=SettingsRecord({"model": request.model, "effort": request.effort}, accepted, observed),
                          usage=(UsageObservation("cost_usd", 0, "call", "emulator", quality="estimated"),))


def factory(adapters: dict):
    def make(service, run_id, provider):
        return ManagedPeer(service.coordinator, run_id=run_id, provider=provider, adapter=adapters[provider], state_root=service.paths.root).start()

    return make


def approve(tools, request):
    for mid in message_ids(request.prompt, "REVIEW_REQUEST"):
        tools.call("send", kind="REVIEW_RESULT", body="Looks right.", reply_to=mid, review={"disposition": "approve"})
    return "reviewed"


def native_codex_writes(client, repo, files: dict, *, checks=None):
    joined = client.call("join", provider="codex", objective="change the token check", repo=str(repo), checks=checks or [CHECK], peer="managed")
    me = client.with_token(joined["token"])
    claimed = me.call("claim")
    for rel, text in files.items():
        target = Path(claimed["workspace"], rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    me.call("submit", summary="small change")
    return joined, me, claimed


def decisions(service, run_id):
    return service.coordinator.routing.status(run_id)["decisions"]


def test_a_small_security_patch_gets_a_deep_review(paths, tmp_path):
    """AT16: three lines in src/auth/token.py: the review floor is deep
    whatever the size, and DUET sets Claude's own effort level for it."""
    claude = ControllableClaude(paths, approve)
    service = start(paths, peer_factory=factory({"claude": claude}))
    try:
        joined, me, claimed = native_codex_writes(ServiceClient(paths), make_repo(tmp_path / "repo"),
                                                  {"calc.py": MUL, "src/auth/token.py": "def ok(t):\n    return bool(t)\n"})
        assert wait_until(lambda: me.call("status")["lifecycle"] == "COMPLETED_VERIFIED", 30)
        review = next(d for d in decisions(service, joined["run_id"]) if d["purpose"] == "review" and d["action"] == "run")
        assert review["profile"] in ("deep", "critical_review") and review["coverage"] == "enforced"
        assert claude.requests[-1].effort == review["effort"] and review["effort"] in ("xhigh", "max")
        assert "auth" in review["explanation"]
        assert review["outcome"]["status"] == "succeeded" and review["outcome"]["differences"] == []
        assert claimed["routing"]["coverage"] == "advisory"  # the native writer got advice, not a change (AT41)
    finally:
        service.close()


def test_a_refused_setting_is_downgraded_and_a_clamp_is_reported(paths, tmp_path):
    """AT17: the organisation refuses 'max': the turn is re-routed without it
    (evidence recorded); the provider then reports a lower effort than
    accepted, which is flagged, never claimed as applied."""
    claude = ControllableClaude(paths, approve, refuse={"max"}, observed_effort="medium")
    service = start(paths, peer_factory=factory({"claude": claude}))
    try:
        service.coordinator.routing.pin(USER, "claude", min_profile="critical_review")  # asks for the top level: 'max'
        joined, me, _ = native_codex_writes(ServiceClient(paths), make_repo(tmp_path / "repo"), {"calc.py": MUL})
        assert wait_until(lambda: me.call("status")["lifecycle"] == "COMPLETED_VERIFIED", 30)
        assert [r.effort for r in claude.requests][:2] == ["max", "xhigh"]
        records = [d for d in reversed(decisions(service, joined["run_id"])) if d["purpose"] == "review" and d["action"] == "run"]
        refused, applied = records[0], records[1]
        assert refused["outcome"]["status"] == "rejected" and "not available" in refused["outcome"]["error"]
        assert applied["effort"] == "xhigh" and applied["outcome"]["differences"] == ["effort: requested xhigh, observed medium"]
    finally:
        service.close()


def test_a_user_pin_is_respected_even_below_the_floor(paths, tmp_path):
    """AT18: pinned 'low' on an auth change: DUET uses it (the user's call),
    records that the floor is not met, and changes no provider settings."""
    claude = ControllableClaude(paths, approve)
    service = start(paths, peer_factory=factory({"claude": claude}))
    try:
        service.coordinator.routing.pin(USER, "claude", effort="low")
        joined, me, _ = native_codex_writes(ServiceClient(paths), make_repo(tmp_path / "repo"),
                                            {"calc.py": MUL, "src/auth/token.py": "def ok(t):\n    return bool(t)\n"})
        assert wait_until(lambda: me.call("status")["lifecycle"] == "COMPLETED_VERIFIED", 30)
        review = next(d for d in decisions(service, joined["run_id"]) if d["purpose"] == "review" and d["action"] == "run")
        assert claude.requests[-1].effort == "low" and review["floor_met"] is False
        assert "pin" in review["explanation"]
    finally:
        service.close()


def test_an_environment_failure_is_diagnosed_not_escalated(paths, tmp_path):
    """AT19: the check fails with ModuleNotFoundError. The writer's next turn
    is told to repair the environment; its profile does not go up."""
    turns = []

    def claude(tools, request):
        turns.append(request)
        if "No messages yet" in request.prompt:
            claimed = tools.call("claim")
            Path(claimed["workspace"], "calc.py").write_text(MUL)
            tools.call("submit", summary="mul")
        return "done"

    writer = ControllableClaude(paths, claude)
    service = start(paths, peer_factory=factory({"claude": writer}))
    try:
        client = ServiceClient(paths)
        joined = client.call("join", provider="codex", objective="add mul", repo=str(make_repo(tmp_path / "repo")),
                             checks=[f'"{PY}" -c "import duet_missing_dependency_xyz"'], peer="managed", writer="peer")
        me = client.with_token(joined["token"])
        assert wait_until(lambda: len(turns) >= 2, 30)
        records = [d for d in reversed(decisions(service, joined["run_id"])) if d["action"] != "requested"]
        first, repair = records[0], records[1]
        assert repair["action"] == "diagnose_environment" and not repair["escalated"]
        assert repair["profile"] == first["profile"]
        assert "environment" in turns[1].prompt and "stronger model would not help" in turns[1].prompt
        assert me.call("status")["lifecycle"] != "COMPLETED_VERIFIED"
    finally:
        service.close()


def test_profile_requests_raise_managed_and_advise_native(paths, tmp_path):
    """A managed participant can ask for more scrutiny, from its next turn;
    a native one gets advice (its client keeps its own settings)."""
    asked = []

    def claude(tools, request):
        if not asked:
            asked.append(tools.call("request_profile", profile="critical_review", reason="subtle concurrency"))
            return "Use ints."
        return approve(tools, request)

    adapter = ControllableClaude(paths, claude)
    service = start(paths, peer_factory=factory({"claude": adapter}))
    try:
        client = ServiceClient(paths)
        joined = client.call("join", provider="codex", objective="add mul", repo=str(make_repo(tmp_path / "repo")), checks=[CHECK], peer="managed")
        me = client.with_token(joined["token"])
        me.call("send", kind="QUESTION", body="ints or floats?")
        assert wait_until(lambda: bool(asked), 20)
        assert asked[0]["decision"] == "accepted" and asked[0]["applied"]["profile"] == "critical_review"
        first_effort = adapter.requests[0].effort
        claimed = me.call("claim")
        Path(claimed["workspace"], "calc.py").write_text(MUL)
        me.call("submit", summary="mul")
        assert wait_until(lambda: me.call("status")["lifecycle"] == "COMPLETED_VERIFIED", 30)
        assert adapter.requests[-1].effort == "max" and first_effort != "max"  # applied at the next turn, not mid-turn
        native = me.call("request_profile", profile="deep", reason="hard review")
        assert native["decision"] == "advisory" and "cannot change" in native["reason"]
    finally:
        service.close()


def test_routing_cli_pins_maps_and_explains(paths, tmp_path):
    root = str(paths.root)
    run = [sys.executable, "-m", "duet", "routing"]
    pinned = subprocess.run([*run, "pin", "--provider", "claude", "--effort", "high", "--state-root", root], capture_output=True, text=True, timeout=60)
    assert pinned.returncode == 0, pinned.stderr
    mapped = subprocess.run([*run, "map", "--provider", "codex", "--profile", "deep", "--model", "my-model", "--effort", "high", "--state-root", root],
                            capture_output=True, text=True, timeout=60)
    assert mapped.returncode == 0, mapped.stderr
    shown = subprocess.run([*run, "--json", "--state-root", root], capture_output=True, text=True, timeout=60)
    report = json.loads(shown.stdout)
    assert report["schema"] == "duet.routing/1" and report["pins"][0]["effort"] == "high"
    assert report["maps"] == [{"provider": "codex", "profile": "deep", "model": "my-model", "effort": "high", "source": "user_map"}]
    subprocess.run([*run, "unpin", "--provider", "claude", "--state-root", root], capture_output=True, text=True, timeout=60, check=True)
    assert json.loads(subprocess.run([*run, "--json", "--state-root", root], capture_output=True, text=True, timeout=60).stdout)["pins"] == []
