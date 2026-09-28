"""D05: the runtime service endpoint and managed peer driver, over the real
Unix socket. Managed providers here are scripted adapters that act through
the same service API a real provider reaches via `duet mcp serve`."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

from duet.oscompat import IS_WINDOWS
from pairkit import MUL, PY, dead_host, live_host, make_repo

from duet.adapters import AgentError, AuthError
from duet.providers.base import SettingsRecord, TurnResult, UsageObservation
from duet.runtime.contracts import CONTROLLER, PolicyDenied, Unauthorized, ValidationError
from duet.runtime.peers import FALLBACK_PREFIX, ManagedPeer
from duet.runtime.service import RuntimeService, ServiceClient, ServicePaths, ServiceRunning

CHECK = [PY, "check_feature.py"]


@pytest.fixture()
def paths(tmp_path):
    return ServicePaths.for_root(tmp_path / "state")


def start(paths, **kw):
    kw.setdefault("idle_exit_seconds", None)
    return RuntimeService(paths, **kw).start()


def wait_until(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# --- endpoint ---------------------------------------------------------------------------


class TestEndpoint:
    def test_socket_is_private_and_single_instance(self, paths):
        service = start(paths)
        try:
            if not IS_WINDOWS:  # Windows: the profile directory's ACL, see the access-key test
                assert stat.S_IMODE(os.stat(paths.socket).st_mode) == 0o600
                assert stat.S_IMODE(os.stat(paths.root).st_mode) == 0o700
                assert stat.S_IMODE(os.stat(paths.secret).st_mode) == 0o600
            assert ServiceClient(paths).ping()["pid"] == os.getpid()
            with pytest.raises(ServiceRunning):
                RuntimeService(paths)
        finally:
            service.close()
        assert not paths.socket.exists() and not paths.secret.exists()

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows uses a loopback endpoint and an access key")
    def test_windows_requests_need_the_access_key(self, paths):
        """Loopback TCP is reachable by other local users: without the per-start
        key from the private state directory, nothing is served, not even ping."""
        from duet.runtime.service import _connect

        service = start(paths)
        try:
            endpoint = json.loads(paths.socket.read_text())
            assert endpoint["host"] == "127.0.0.1" and paths.access.exists()
            for auth in ({}, {"access": "wrong"}):
                with _connect(paths, 5) as sock:
                    sock.sendall((json.dumps({"op": "ping", "args": {}, "auth": auth}) + "\n").encode())
                    reply = json.loads(sock.makefile().readline())
                assert reply["ok"] is False and reply["error"]["code"] == "unauthorized"
            assert ServiceClient(paths).ping()["pid"] == os.getpid()  # the client sends the key
        finally:
            service.close()
        assert not paths.access.exists() and not paths.socket.exists()

    @pytest.mark.skipif(IS_WINDOWS, reason="Unix socket path length (sun_path); Windows uses a loopback endpoint")
    def test_long_state_paths_get_a_short_socket(self, tmp_path):
        deep = tmp_path.joinpath(*["nested-directory-name"] * 6) / "state"
        long_paths = ServicePaths.for_root(deep)
        assert len(os.fsencode(str(long_paths.socket))) <= 100
        service = start(long_paths)
        try:
            assert ServiceClient(long_paths).ping()["root"] == str(long_paths.root)
        finally:
            service.close()

    @pytest.mark.skipif(IS_WINDOWS, reason="Unix socket placement; Windows uses a loopback endpoint")
    def test_the_short_socket_does_not_depend_on_tmpdir(self, tmp_path, monkeypatch):
        """macOS CI: proxies started by an MCP client have no TMPDIR, so a
        socket placed under tempfile.gettempdir() was looked for in two places."""
        import tempfile

        deep = tmp_path.joinpath(*["nested-directory-name"] * 6) / "state"
        before = ServicePaths.for_root(deep).socket
        other = tmp_path / "elsewhere"
        other.mkdir(mode=0o700)
        monkeypatch.setenv("TMPDIR", str(other))
        monkeypatch.setattr(tempfile, "tempdir", None)  # gettempdir() re-reads TMPDIR
        assert ServicePaths.for_root(deep).socket == before

    def test_authentication_rules(self, paths, tmp_path):
        service = start(paths)
        try:
            anonymous = ServiceClient(paths)
            with pytest.raises(Unauthorized, match="needs a participant token"):
                anonymous.call("status")
            with pytest.raises(Unauthorized, match="bad service secret"):
                ServiceClient(paths, secret="nope").call("runs")
            with pytest.raises(Unauthorized, match="unknown participant token"):
                ServiceClient(paths, token="duet_pt_" + "x" * 40).call("status")
            repo = make_repo(tmp_path / "repo")
            joined = anonymous.call("join", provider="claude", objective="add mul", repo=str(repo), checks=[CHECK], peer="invite")
            participant = anonymous.with_token(joined["token"])
            with pytest.raises(Unauthorized, match="cannot call 'runs'"):
                participant.call("runs")
            with pytest.raises(ValidationError, match="unknown arguments"):
                participant.call("send", kind="QUESTION", body="x", sender="user")  # identity is never a parameter
            assert ServiceClient.as_controller(paths).call("runs")[0]["run_id"] == joined["run_id"]
        finally:
            service.close()

    def test_malformed_and_oversized_requests(self, paths):
        from duet.runtime.service import _connect

        service = start(paths)
        try:
            with _connect(paths, 5) as sock:
                sock.sendall(b"not json\n")
                reply = json.loads(sock.makefile().readline())
            assert reply["ok"] is False and reply["error"]["code"] == "validation"
            with _connect(paths, 5) as sock:
                try:
                    sock.sendall(b"x" * (2 * 1024 * 1024))
                except OSError:
                    pass
                reply = sock.makefile().readline()
            assert not reply or json.loads(reply)["error"]["code"] == "validation"
            assert ServiceClient(paths).ping()  # still serving
        finally:
            service.close()

    @pytest.mark.skipif(not Path("/proc").is_dir(), reason="host ancestry is checked through /proc")
    def test_claimed_host_must_be_an_ancestor(self, paths, tmp_path):
        service = start(paths)
        repo = make_repo(tmp_path / "repo")
        sleeper = subprocess.Popen(["sleep", "30"])
        try:
            from duet.runtime.identity import ProcessIdentity

            client = ServiceClient(paths)
            args = dict(provider="claude", objective="x", repo=str(repo), checks=[CHECK], peer="invite")
            with pytest.raises(Unauthorized, match="not an ancestor"):
                client.call("join", host=str(ProcessIdentity.of(sleeper.pid)), **args)
            with pytest.raises(Unauthorized, match="not running"):
                client.call("join", host=dead_host(), **args)
            assert client.call("join", host=live_host(), **args)["origin"] == "native_original"
        finally:
            sleeper.kill()
            sleeper.wait()
            service.close()

    def test_waits_are_served_concurrently(self, paths, tmp_path):
        service = start(paths)
        try:
            repo = make_repo(tmp_path / "repo")
            client = ServiceClient(paths)
            a = client.with_token(client.call("join", provider="claude", objective="x", repo=str(repo), checks=[CHECK], peer="invite")["token"])
            info = a.call("status")
            started = client.call("join", provider="codex", objective="y", repo=str(repo), checks=[CHECK], peer="invite")
            invite = started["invite"]["code"]
            b = client.with_token(client.call("join", provider="claude", run_id=started["run_id"], invite=invite)["token"])
            codex = client.with_token(started["token"])
            since = codex.call("wait", timeout=0)["last_seq"]
            box = {}
            waiter = threading.Thread(target=lambda: box.setdefault("r", codex.call("wait", rpc_timeout=60, since=since, timeout=20)))
            waiter.start()
            time.sleep(0.3)
            assert a.call("status")["run_id"] == info["run_id"]  # other requests are not blocked by the wait
            q = b.call("send", kind="QUESTION", body="hello?")
            waiter.join(10)
            assert [m["message_id"] for m in box["r"]["messages"]] == [q["message_id"]]
        finally:
            service.close()

    def test_restart_settles_interrupted_checks(self, paths, tmp_path):
        service = start(paths)
        repo = make_repo(tmp_path / "repo")
        client = ServiceClient(paths)
        joined = client.call("join", provider="claude", objective="x", repo=str(repo), checks=[CHECK], peer="invite")
        run_id = joined["run_id"]
        from duet.runtime.contracts import CONTROLLER

        action = service.runtime.plan_action(CONTROLLER, run_id=run_id, type="check", input={"check_id": "check1"})["action"]["action_id"]
        service.runtime.claim_action(CONTROLLER, action)
        service.close()  # crashed mid-check: the lease owner is this process, so fake a dead owner
        from duet.runtime.store import Store

        store = Store(paths.db)
        store.connection().execute("UPDATE leases SET owner = ? WHERE resource = ?", (dead_host(), f"action:{action}"))
        store.close()
        service = start(paths)
        try:
            row = service.store.read().get("actions", action)
            assert row["state"] == "FAILED" and "restarted" in row["reconciliation"]
            assert client.with_token(joined["token"]).call("status")["run_id"] == run_id  # tokens survive restarts
        finally:
            service.close()

    def test_idle_service_exits_without_live_runs(self, paths):
        service = RuntimeService(paths, idle_exit_seconds=0.5).start()
        thread = threading.Thread(target=lambda: service._stop.wait(10))
        thread.start()
        thread.join(10)
        assert service._stop.is_set()
        service.close()


# --- managed peers ------------------------------------------------------------------------


class ScriptedProvider:
    """Stands in for a provider CLI: during a turn it uses the DUET tools
    with the managed peer's own token, read from the MCP config DUET passed."""

    def __init__(self, paths: ServicePaths, script):
        self.paths = paths
        self.script = script
        self.requests = []
        self.closed = False

    def run_turn(self, request, *, on_event=None, cancel=None):
        self.requests.append(request)
        args = request.mcp_config["mcpServers"]["duet"]["args"]
        token = Path(args[args.index("--token-file") + 1]).read_text().strip()
        tools = ServiceClient(self.paths, token=token)
        text = self.script(tools, request) or ""
        session = request.session_id or f"sess-{len(self.requests)}"
        return TurnResult(
            status="completed", text=text, session_id=session,
            lineage="resumed_same" if request.session_id else "new",
            settings=SettingsRecord(requested={}, accepted={"mcp_servers": ["duet"]}, observed={"session_id": session}),
            usage=(UsageObservation("cost_usd", Decimal("0.01"), "call", "emulator"),),
        )

    def close(self):
        self.closed = True


def message_ids(prompt: str, kind: str) -> list[str]:
    import re

    return re.findall(rf"\] {kind} from \w+ \(message_id (msg_\w+)", prompt)


def factory_for(script_by_provider, providers_out):
    def factory(service, run_id, provider):
        adapter = ScriptedProvider(service.paths, script_by_provider[provider])
        peer = ManagedPeer(service.coordinator, run_id=run_id, provider=provider, adapter=adapter, state_root=service.paths.root)
        providers_out[provider] = (peer, adapter)
        return peer.start()

    return factory


class TestManagedPeer:
    def test_native_claude_and_managed_codex_talk_both_ways_and_ship_a_reviewed_patch(self, paths, tmp_path):
        """Claude-originated (AT01 shape, simulated provider): the native
        session asks, the managed Codex asks back before answering, the native
        answers, Codex continues; then one reviewed patch completes."""
        log = []

        def codex(tools, request):
            prompt = request.prompt
            for mid in message_ids(prompt, "QUESTION"):
                if not any(entry[0] == "asked" for entry in log):
                    q = tools.call("send", kind="QUESTION", body="Before I answer: must mul handle floats?")
                    log.append(("asked", q["message_id"], mid))
                    return "asked a clarifying question"
            for mid in message_ids(prompt, "ANSWER"):
                original = next(entry[2] for entry in log if entry[0] == "asked")
                tools.call("send", kind="ANSWER", body="Then integer multiplication is enough.", reply_to=original)
                log.append(("answered", original))
            for mid in message_ids(prompt, "REVIEW_REQUEST"):
                assert "+def mul(a, b):" in prompt
                tools.call("send", kind="REVIEW_RESULT", body="Looks right.", reply_to=mid, review={"disposition": "approve"})
                log.append(("reviewed", mid))
            return "done"

        peers = {}
        service = start(paths, peer_factory=factory_for({"codex": codex}, peers))
        try:
            repo = make_repo(tmp_path / "repo")
            client = ServiceClient(paths)
            joined = client.call("join", provider="claude", objective="add mul", repo=str(repo), checks=[CHECK], peer="managed", host=live_host())
            me = client.with_token(joined["token"])
            assert joined["peer"]["origin"] == "managed" and joined["role"] == "writer"
            peer, adapter = peers["codex"]
            assert adapter.requests == []  # the reviewer is not started until it has something to do

            since = me.call("wait", timeout=0)["last_seq"]
            question = me.call("send", kind="QUESTION", body="Should I name it mul or multiply?")
            got = me.call("wait", rpc_timeout=60, since=since, timeout=20)
            back = [m for m in got["messages"] if m["kind"] == "QUESTION"]
            assert back and back[0]["from"] == "codex"  # B asked A before answering
            me.call("send", kind="ANSWER", body="Ints only.", reply_to=back[0]["message_id"])
            answer = None
            last = got["last_seq"]
            for _ in range(10):
                got = me.call("wait", rpc_timeout=60, since=last, timeout=20)
                last = got["last_seq"]
                answer = next((m for m in got["messages"] if m["kind"] == "ANSWER" and m["reply_to"] == question["message_id"]), None)
                if answer:
                    break
            assert answer and not answer["body"].startswith(FALLBACK_PREFIX)
            # the fake records "answered" after its send returns, which can be after we see the message
            assert wait_until(lambda: [e[0] for e in log[:2]] == ["asked", "answered"], 10), log
            assert len(adapter.requests) == 2 and adapter.requests[1].session_id == "sess-1"  # same managed session resumed

            claimed = me.call("claim")
            Path(claimed["workspace"], "calc.py").write_text(MUL)
            me.call("submit", summary="mul added")
            assert wait_until(lambda: client.with_token(joined["token"]).call("status")["lifecycle"] == "COMPLETED_VERIFIED", 60), me.call("status")
            status = me.call("status")
            assert status["verification"]["reviews"][0]["reviewer_provider"] == "codex"
            actions = service.store.read().query("SELECT type, state, participant_id FROM actions WHERE run_id = ? ORDER BY created_at", (joined["run_id"],))
            turns = [a for a in actions if a["type"] == "provider_turn"]
            assert len(turns) == 3 and all(a["state"] == "SUCCEEDED" for a in turns)
            # peers stop with the run: stop() ends the thread, then closes the adapter
            assert wait_until(lambda: not peer.alive and adapter.closed, 10)
            assert not peer.token_file.exists()
        finally:
            service.close()

    def test_native_codex_initiates_and_answers_a_claude_follow_up(self, paths, tmp_path):
        """Codex-originated (AT02 shape, simulated provider): the original
        Codex session starts the run with a managed Claude, and Claude's
        follow-up question is answered by that same Codex session."""
        def claude(tools, request):
            for mid in message_ids(request.prompt, "QUESTION"):
                back = tools.call("send", kind="QUESTION", body="Which module should hold it?")
                return f"asked back {back['message_id']} before answering {mid}"
            if message_ids(request.prompt, "ANSWER"):  # got what it asked for: now answer Codex
                tools.call("send", kind="ANSWER", body="Use calc.py.", reply_to=first_question[0])
            return "done"

        first_question: list[str] = []
        peers = {}
        service = start(paths, peer_factory=factory_for({"claude": claude}, peers))
        try:
            repo = make_repo(tmp_path / "repo")
            client = ServiceClient(paths)
            joined = client.call("join", provider="codex", objective="add mul", repo=str(repo), checks=[CHECK], peer="managed", host=live_host())
            codex = client.with_token(joined["token"])
            assert joined["origin"] == "native_original" and joined["peer"]["provider"] == "claude" and joined["peer"]["origin"] == "managed"
            since = codex.call("wait", timeout=0)["last_seq"]
            first_question.append(codex.call("send", kind="QUESTION", body="Where should mul live?")["message_id"])
            got = codex.call("wait", rpc_timeout=60, since=since, timeout=20)
            follow_up = next(m for m in got["messages"] if m["kind"] == "QUESTION")
            assert follow_up["from"] == "claude"
            codex.call("send", kind="ANSWER", body="calc.py, ints only.", reply_to=follow_up["message_id"])  # the original Codex answers
            last, answer = got["last_seq"], None
            for _ in range(10):
                got = codex.call("wait", rpc_timeout=60, since=last, timeout=20)
                last = got["last_seq"]
                answer = next((m for m in got["messages"] if m["kind"] == "ANSWER" and m["reply_to"] == first_question[0]), None)
                if answer:
                    break
            assert answer and answer["from"] == "claude"
            parts = service.coordinator.runtime.participants(CONTROLLER, joined["run_id"])
            assert {(p["provider"], p["origin"]) for p in parts} == {("codex", "native_original"), ("claude", "managed")}
        finally:
            service.close()

    def test_duet_originated_pair_is_labelled_managed(self, paths, tmp_path):
        def claude(tools, request):
            if "No messages yet" in request.prompt:
                claimed = tools.call("claim")
                Path(claimed["workspace"], "calc.py").write_text(MUL)
                tools.call("submit", summary="mul")
            return "submitted"

        def codex(tools, request):
            for mid in message_ids(request.prompt, "REVIEW_REQUEST"):
                tools.call("send", kind="REVIEW_RESULT", body="ok", reply_to=mid, review={"disposition": "approve"})
            return "reviewed"

        peers = {}
        service = start(paths, peer_factory=factory_for({"claude": claude, "codex": codex}, peers))
        try:
            repo = make_repo(tmp_path / "repo")
            controller = ServiceClient.as_controller(paths)
            status = controller.call("pair", objective="add mul", repo=str(repo), checks=[CHECK], writer="claude")
            assert {p["origin"] for p in status["participants"]} == {"managed"}
            run_id = status["run_id"]
            assert wait_until(lambda: controller.call("run_status", run_id=run_id)["lifecycle"] == "COMPLETED_VERIFIED", 60)
            final = controller.call("run_status", run_id=run_id)
            roles = {p["provider"]: p["role"] for p in final["participants"]}
            assert roles == {"claude": "writer", "codex": "reviewer"}
            assert peers["claude"][1].requests[0].permission_profile == "workspace_write"
            assert peers["codex"][1].requests[0].permission_profile == "read_only"
        finally:
            service.close()

    def test_unanswered_question_gets_a_labelled_fallback(self, paths, tmp_path):
        peers = {}
        service = start(paths, peer_factory=factory_for({"codex": lambda tools, request: "Use integers."}, peers))
        try:
            repo = make_repo(tmp_path / "repo")
            client = ServiceClient(paths)
            joined = client.call("join", provider="claude", objective="x", repo=str(repo), checks=[CHECK], peer="managed")
            me = client.with_token(joined["token"])
            since = me.call("wait", timeout=0)["last_seq"]
            q = me.call("send", kind="QUESTION", body="ints or floats?")
            got = me.call("wait", rpc_timeout=60, since=since, timeout=20)
            answer = next(m for m in got["messages"] if m["kind"] == "ANSWER")
            assert answer["reply_to"] == q["message_id"] and answer["body"].startswith(FALLBACK_PREFIX)
        finally:
            service.close()

    def test_auth_failure_stops_the_peer_without_fallback(self, paths, tmp_path):
        def failing(tools, request):
            raise AuthError("codex: not logged in", kind="auth")

        peers = {}
        service = start(paths, peer_factory=factory_for({"codex": failing}, peers))
        try:
            repo = make_repo(tmp_path / "repo")
            client = ServiceClient(paths)
            joined = client.call("join", provider="claude", objective="x", repo=str(repo), checks=[CHECK], peer="managed")
            me = client.with_token(joined["token"])
            me.call("send", kind="QUESTION", body="there?")
            assert wait_until(lambda: me.call("status")["collaboration"] == "PEER_UNAVAILABLE", 20)
            peer, adapter = peers["codex"]
            assert wait_until(lambda: not peer.alive, 10) and len(adapter.requests) == 1  # no retry, no other account
            notes = [m["body"] for m in me.call("inbox")["messages"] if m["kind"] == "STATUS"]
            assert any("does not switch accounts" in n for n in notes)
        finally:
            service.close()

    def test_repeated_turn_failures_stop_the_peer_instead_of_hanging(self, paths, tmp_path):
        """Windows CI: the writer's first turn failed (a launcher problem) and
        the pair waited forever, since no message would ever come. A failing
        turn is retried once, then the peer stops and says why."""

        class Crashing(ScriptedProvider):
            def run_turn(self, request, *, on_event=None, cancel=None):
                self.requests.append(request)
                return TurnResult(
                    status="failed", text="", session_id=None, lineage="new",
                    settings=SettingsRecord(requested={}, accepted={}, observed={}),
                    error=AgentError("claude: exited with status 1 before any output", kind="crash"),
                )

        peers = {}

        def factory(service, run_id, provider):
            adapter = Crashing(service.paths, None)
            peer = ManagedPeer(service.coordinator, run_id=run_id, provider=provider, adapter=adapter, state_root=service.paths.root)
            peers[provider] = (peer, adapter)
            return peer.start()

        service = start(paths, peer_factory=factory)
        try:
            repo = make_repo(tmp_path / "repo")
            client = ServiceClient(paths)
            joined = client.call("join", provider="codex", objective="x", repo=str(repo), checks=[CHECK], peer="managed", writer="peer")
            me = client.with_token(joined["token"])
            assert wait_until(lambda: me.call("status")["collaboration"] == "PEER_UNAVAILABLE", 30)
            peer, adapter = peers["claude"]
            assert wait_until(lambda: not peer.alive, 10) and len(adapter.requests) == 2  # one retry, then stop
            notes = [m["body"] for m in me.call("inbox")["messages"] if m["kind"] == "STATUS"]
            assert any("2 turns in a row failed" in n and "exited with status 1" in n for n in notes)
        finally:
            service.close()

    def test_status_notes_do_not_start_turns(self, paths, tmp_path):
        peers = {}
        service = start(paths, peer_factory=factory_for({"codex": lambda tools, request: "ok"}, peers))
        try:
            repo = make_repo(tmp_path / "repo")
            client = ServiceClient(paths)
            joined = client.call("join", provider="claude", objective="x", repo=str(repo), checks=[CHECK], peer="managed")
            me = client.with_token(joined["token"])
            me.call("send", kind="STATUS", body="fyi: reading the code")
            time.sleep(1.0)
            assert peers["codex"][1].requests == []
            me.call("send", kind="QUESTION", body="now a question")
            assert wait_until(lambda: len(peers["codex"][1].requests) == 1, 20)
            assert "fyi: reading the code" in peers["codex"][1].requests[0].prompt  # the note rides along
        finally:
            service.close()

    def test_missing_provider_binary_fails_the_join_up_front(self, paths, tmp_path, monkeypatch):
        from duet.runtime.peers import default_peer_factory

        monkeypatch.setenv("DUET_CODEX_BIN", str(tmp_path / "no-such-codex"))
        service = start(paths, peer_factory=default_peer_factory)
        try:
            repo = make_repo(tmp_path / "repo")
            with pytest.raises(PolicyDenied, match="executable not found"):
                ServiceClient(paths).call("join", provider="claude", objective="x", repo=str(repo), checks=[CHECK], peer="managed")
            parts = service.store.read().query("SELECT provider FROM participants")
            assert [p["provider"] for p in parts] == ["claude"]  # no phantom codex participant
        finally:
            service.close()

    def test_local_turn_allowance_protects_the_review(self, paths, tmp_path):
        """D08 (AT13, AT14): the review turns are reserved when the pair
        forms; optional turns may use only what is left, and the review turn
        draws on its reserve instead of holding the capacity twice."""
        from duet.runtime.contracts import USER
        from duet.runtime.pools import PoolStore

        peers = {}

        def codex(tools, request):
            for mid in message_ids(request.prompt, "REVIEW_REQUEST"):
                tools.call("send", kind="REVIEW_RESULT", body="Looks right.", reply_to=mid, review={"disposition": "approve"})
            return "noted"

        service = start(paths, peer_factory=factory_for({"codex": codex}, peers))
        try:
            pools = PoolStore(service.runtime)
            pools.define_pool(USER, "codex-turns", provider="codex", metric="turns", unit="turns", allowance=3)
            repo = make_repo(tmp_path / "repo")
            client = ServiceClient(paths)
            joined = client.call("join", provider="claude", objective="x", repo=str(repo), checks=[CHECK], peer="managed")
            me = client.with_token(joined["token"])
            finishing = me.call("status")["admission"]["finishing"]
            assert [(f["purpose"], f["provider"], f["held"], f["units_left"]) for f in finishing] == [("review", "codex", "2", 2)]
            me.call("send", kind="FINDING", body="one")
            assert wait_until(lambda: len(peers["codex"][1].requests) == 1, 20)
            assert wait_until(lambda: pools.status("codex-turns")[0]["used"] == "1", 20)
            me.call("send", kind="FINDING", body="two")
            assert wait_until(lambda: any("deferred optional work" in m["body"] for m in me.call("inbox")["messages"] if m["kind"] == "STATUS"), 20)
            assert len(peers["codex"][1].requests) == 1  # the optional turn would have spent the review's capacity
            claimed = me.call("claim")
            Path(claimed["workspace"], "calc.py").write_text(MUL)
            me.call("submit", summary="mul added")
            assert wait_until(lambda: me.call("status")["lifecycle"] == "COMPLETED_VERIFIED", 30)
            assert len(peers["codex"][1].requests) == 2  # the review ran, funded by its reserve
            status = pools.status("codex-turns")[0]
            assert (status["used"], status["held"], status["available"]) == ("2", "0", "1")  # held once, then released with the run
            admissions = service.coordinator.budget.book.status(joined["run_id"])["admissions"]
            verdicts = [(a["action_class"], a["purpose"], a["verdict"]) for a in reversed(admissions)]
            assert verdicts == [("optional", "investigate", "admit"), ("optional", "investigate", "defer"), ("finishing", "review", "admit")]
            review = next(a for a in admissions if a["purpose"] == "review")
            assert review["detail"]["lines"][0]["draw_from"] == finishing[0]["reservation_id"]
            records = pools.run_usage(joined["run_id"])
            assert [(r["pool_id"], r["quantity"], r["quality"]) for r in records] == [("codex-turns", "1", "observed")] * 2
        finally:
            service.close()

    def test_turn_budget_is_enforced(self, paths, tmp_path):
        from duet.runtime.policy import AuthorisationPolicy

        peers = {}
        service = RuntimeService(paths, peer_factory=factory_for({"codex": lambda tools, request: "noted"}, peers), idle_exit_seconds=None)
        service.coordinator.policy = AuthorisationPolicy(max_invocations=1)
        service.start()
        try:
            repo = make_repo(tmp_path / "repo")
            client = ServiceClient(paths)
            joined = client.call("join", provider="claude", objective="x", repo=str(repo), checks=[CHECK], peer="managed")
            me = client.with_token(joined["token"])
            me.call("send", kind="FINDING", body="one")
            assert wait_until(lambda: len(peers["codex"][1].requests) == 1, 20)
            me.call("send", kind="FINDING", body="two")
            assert wait_until(lambda: me.call("status")["collaboration"] == "PEER_UNAVAILABLE", 20)
            assert len(peers["codex"][1].requests) == 1
        finally:
            service.close()


class TestReviewFixes:
    def test_driver_survives_the_model_acknowledging_further(self, paths, tmp_path):
        def codex(tools, request):
            for mid in message_ids(request.prompt, "QUESTION"):
                got = tools.call("wait", timeout=0)
                tools.call("inbox", ack_through=got["last_seq"])  # as the instructions say
                tools.call("send", kind="ANSWER", body="yes", reply_to=mid)
            return "answered"

        peers = {}
        service = start(paths, peer_factory=factory_for({"codex": codex}, peers))
        try:
            repo = make_repo(tmp_path / "repo")
            client = ServiceClient(paths)
            me = client.with_token(client.call("join", provider="claude", objective="x", repo=str(repo), checks=[CHECK], peer="managed")["token"])
            for n in range(2):
                q = me.call("send", kind="QUESTION", body=f"question {n}")
                assert wait_until(lambda: any(m["reply_to"] == q["message_id"] for m in me.call("inbox")["messages"]), 20), n
            peer, adapter = peers["codex"]
            assert peer.alive and len(adapter.requests) == 2
            assert me.call("status")["collaboration"] == "PAIR_ACTIVE"
        finally:
            service.close()

    def test_restart_marks_orphaned_managed_peers_unavailable(self, paths, tmp_path):
        peers = {}
        service = start(paths, peer_factory=factory_for({"codex": lambda tools, request: ""}, peers))
        repo = make_repo(tmp_path / "repo")
        client = ServiceClient(paths)
        joined = client.call("join", provider="claude", objective="x", repo=str(repo), checks=[CHECK], peer="managed")
        service.close()
        service = start(paths)  # a restart: nobody drives the managed codex any more
        try:
            me = client.with_token(joined["token"])
            status = me.call("status")
            codex = next(p for p in status["participants"] if p["provider"] == "codex")
            assert codex["liveness"] == "unavailable" and status["collaboration"] == "PEER_UNAVAILABLE"
            notes = [m["body"] for m in me.call("inbox")["messages"]]
            assert any("unavailable" in n and ("service stopped" in n or "service restarted" in n) for n in notes), notes
        finally:
            service.close()

    def test_interrupted_check_reopens_the_task(self, paths, tmp_path):
        from duet.runtime.store import Store

        service = start(paths)
        repo = make_repo(tmp_path / "repo")
        client = ServiceClient(paths)
        started = client.call("join", provider="claude", objective="x", repo=str(repo), checks=[CHECK], peer="invite")
        client.call("join", provider="codex", run_id=started["run_id"], invite=started["invite"]["code"])
        me = client.with_token(started["token"])
        claimed = me.call("claim")
        Path(claimed["workspace"], "calc.py").write_text(MUL)
        submitted = me.call("submit", request_review=False)
        assert service.coordinator.wait_for_checks(submitted["snapshot_id"], 60)
        action = service.runtime.plan_action(CONTROLLER, run_id=started["run_id"], type="check", input={"check_id": "check1"})["action"]["action_id"]
        service.runtime.claim_action(CONTROLLER, action)
        service.close()
        store = Store(paths.db)
        store.connection().execute("UPDATE leases SET owner = ? WHERE resource = ?", (dead_host(), f"action:{action}"))
        store.close()
        service = start(paths)
        try:
            main = me.call("status")["tasks"][0]
            assert main["state"] == "CHANGES_REQUESTED"
            assert any("interrupted by a service restart" in m["body"] for m in me.call("inbox")["messages"] if m["kind"] == "BLOCKER")
            me.call("claim")  # the writer can act on "submit again"
        finally:
            service.close()

    def test_a_native_session_can_start_a_new_run_after_its_run_ends(self, paths, tmp_path):
        from duet.integrations.mcp_server import ProxySession

        service = start(paths)
        try:
            repo = make_repo(tmp_path / "repo")
            session = ProxySession(paths, host=live_host())
            first = session.join(provider="claude", objective="first", repo=str(repo), checks=[" ".join(CHECK)], peer="invite")
            ServiceClient.as_controller(paths).call("cancel", run_id=first["run_id"], reason="done with it")
            second = session.join(provider="claude", objective="second", repo=str(repo), checks=[" ".join(CHECK)], peer="invite")
            assert second["run_id"] != first["run_id"] and second["objective"] == "second"
            restarted = ProxySession(paths, host=live_host())
            restarted.restore()
            assert restarted.token == session.token  # the live run is reconnected, not the finished one
        finally:
            service.close()


# --- no recursion -------------------------------------------------------------------------


class TestNoRecursivePairs:
    def test_managed_credentials_cannot_start_or_join_runs(self, paths, tmp_path):
        peers = {}
        service = start(paths, peer_factory=factory_for({"codex": lambda tools, request: ""}, peers))
        try:
            repo = make_repo(tmp_path / "repo")
            client = ServiceClient(paths)
            client.call("join", provider="claude", objective="x", repo=str(repo), checks=[CHECK], peer="managed")
            token = peers["codex"][0].token_file.read_text()
            managed = client.with_token(token)
            with pytest.raises(PolicyDenied, match="already belongs to a run"):
                managed.call("join", provider="codex", objective="spawn another pair", repo=str(repo), checks=[CHECK])
            assert managed.call("join", provider="codex")["origin"] == "managed"
        finally:
            service.close()

    def test_managed_proxy_cannot_start_runs(self, paths):
        from duet.integrations.mcp_server import ProxySession

        session = ProxySession(paths, managed=True)
        with pytest.raises(Unauthorized, match="cannot start or join"):
            session.join(provider="codex", objective="spawn", repo=".", checks=["true"])
        assert not paths.socket.exists()  # it did not start a service either

    @pytest.mark.parametrize("argv", [["pair", "task", "--check", "true"], ["service", "run"]])
    def test_cli_refuses_inside_a_managed_session(self, tmp_path, argv):
        env = dict(os.environ, DUET_MANAGED_PEER="1", DUET_STATE_DIR=str(tmp_path / "state"))
        proc = subprocess.run([sys.executable, "-m", "duet", *argv], env=env, capture_output=True, text=True, cwd=tmp_path, timeout=60)
        assert proc.returncode == 1 and "managed" in proc.stderr


# --- CLI ----------------------------------------------------------------------------------


class TestCli:
    def test_status_and_stop_by_run_id_without_a_service(self, paths, tmp_path):
        service = start(paths)
        repo = make_repo(tmp_path / "repo")
        joined = ServiceClient(paths).call("join", provider="claude", objective="add mul", repo=str(repo), checks=[CHECK], peer="invite")
        service.close()
        run = [sys.executable, "-m", "duet"]
        common = ["--state-root", str(paths.root)]
        shown = subprocess.run([*run, "status", "--run", joined["run_id"], "--json", *common], capture_output=True, text=True, timeout=60)
        assert shown.returncode == 0, shown.stderr
        status = json.loads(shown.stdout)
        assert status["schema"] == "duet.pair-status/1" and status["lifecycle"] == "PLANNING"
        assert status["participants"][0]["capabilities"]["receive"] == "checkpoint"
        text = subprocess.run([*run, "status", "--run", joined["run_id"], *common], capture_output=True, text=True, timeout=60)
        assert "origin=native_original" in text.stdout and "no snapshot submitted yet" in text.stdout
        stopped = subprocess.run([*run, "stop", "--run", joined["run_id"], *common], capture_output=True, text=True, timeout=60)
        assert stopped.returncode == 0, stopped.stderr
        final = json.loads(subprocess.run([*run, "status", "--run", joined["run_id"], "--json", *common], capture_output=True, text=True, timeout=60).stdout)
        assert final["lifecycle"] == "CANCELLED"
