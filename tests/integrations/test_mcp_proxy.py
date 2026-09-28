"""D05 over real MCP: two `duet mcp serve` stdio processes play the native
Claude and Codex sessions (the test drives their tool calls in place of a
model). The service is started on demand by the first proxy. Requires the
optional MCP SDK (`pip install 'duet[mcp]'`)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")
import anyio  # noqa: E402  (installed with mcp)
from mcp import Client, StdioServerParameters  # noqa: E402
from pairkit import MUL, PY, make_repo  # noqa: E402

from duet.runtime.service import ServiceClient, ServicePaths, service_running  # noqa: E402

TOOLS = {
    "duet_join", "duet_send", "duet_inbox", "duet_wait", "duet_propose_task", "duet_propose_plan", "duet_decide_plan",
    "duet_claim", "duet_complete_task", "duet_decide_task", "duet_handoff", "duet_submit", "duet_request_review",
    "duet_request_profile", "duet_status",
}


class ToolFailure(Exception):
    def __init__(self, payload: dict):
        super().__init__(payload.get("message"))
        self.payload = payload


def proxy(state: Path, *, wrapped: bool = False) -> StdioServerParameters:
    """wrapped: launch through a shell so the proxy's host process (its
    parent) differs from the test process, like a second agent CLI."""
    env = {"DUET_STATE_DIR": str(state), "PYTHONPATH": os.pathsep.join(sys.path)}
    if wrapped and os.name == "nt":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        return StdioServerParameters(command=comspec, args=["/d", "/c", PY, "-m", "duet", "mcp", "serve"], env=env)
    if wrapped:
        return StdioServerParameters(command="/bin/sh", args=["-c", f'"{PY}" -m duet mcp serve; exit $?'], env=env)
    return StdioServerParameters(command=PY, args=["-m", "duet", "mcp", "serve"], env=env)


async def call(client: Client, tool: str, **args):
    result = await client.call_tool(tool, args)
    if result.is_error:
        text = result.content[0].text
        raise ToolFailure(json.loads(text[text.index("{"):]) if "{" in text else {"message": text})
    return result.structured_content


def paths_for(state: Path) -> ServicePaths:
    return ServicePaths.for_root(state / "v2")


@pytest.fixture()
def state(tmp_path):
    root = tmp_path / "state"
    yield root
    paths = paths_for(root)
    if service_running(paths):
        ServiceClient.as_controller(paths).call("shutdown")


def test_native_claude_and_native_codex_pair_over_mcp(tmp_path, state):
    repo = make_repo(tmp_path / "repo")

    async def scenario():
        async with Client(proxy(state), mode="legacy") as claude, Client(proxy(state, wrapped=True), mode="legacy") as codex:
            tools = {t.name for t in (await claude.list_tools()).tools}
            assert tools == TOOLS
            assert "Answer your peer's questions and review requests first" in (claude.instructions or "")

            started = await call(claude, "duet_join", provider="claude", objective="add mul(a, b) to calc.py", repo=str(repo),
                                 checks=[f'"{PY}" check_feature.py'], peer="invite", writer="self")
            assert "duet_pt_" not in json.dumps(started)  # the bearer token never reaches the model
            assert started["role"] == "writer" and started["origin"] == "native_original"
            joined = await call(codex, "duet_join", provider="codex", run_id=started["run_id"], invite=started["invite"]["code"])
            assert joined["role"] == "reviewer" and joined["peer"]["provider"] == "claude"

            await call(claude, "duet_wait", timeout=0)
            await call(codex, "duet_wait", timeout=0)

            # A asks B while B is already waiting: B's wait wakes (no polling by the model).
            async with anyio.create_task_group() as group:
                box = {}

                async def codex_waits():
                    box["wait"] = await call(codex, "duet_wait", timeout=20)

                group.start_soon(codex_waits)
                await anyio.sleep(0.5)
                q1 = await call(claude, "duet_send", kind="QUESTION", body="Should mul accept floats?")
            assert [m["message_id"] for m in box["wait"]["messages"]] == [q1["message_id"]]

            q2 = await call(codex, "duet_send", kind="QUESTION", body="Are there float callers today?")  # B asks A first
            got = await call(claude, "duet_wait", timeout=10)
            assert [m["kind"] for m in got["messages"]] == ["QUESTION"] and got["awaiting_replies"][0]["message_id"] == q1["message_id"]
            await call(claude, "duet_send", kind="ANSWER", body="No, only ints.", reply_to=q2["message_id"], )
            got = await call(codex, "duet_wait", timeout=10, ack_through=box["wait"]["last_seq"])
            assert got["messages"][0]["reply_to"] == q2["message_id"]
            await call(codex, "duet_send", kind="ANSWER", body="Then ints are enough.", reply_to=q1["message_id"])
            got = await call(claude, "duet_wait", timeout=10)
            assert got["messages"][0]["reply_to"] == q1["message_id"] and got["awaiting_replies"] == []

            with pytest.raises(ToolFailure) as denied:
                await call(codex, "duet_claim")
            assert denied.value.payload["code"] == "policy_denied"

            claimed = await call(claude, "duet_claim")
            Path(claimed["workspace"], "calc.py").write_text(MUL)
            submitted = await call(claude, "duet_submit", summary="added mul")
            request = None
            for _ in range(10):
                got = await call(codex, "duet_wait", timeout=10)
                request = next((m for m in got["messages"] if m["kind"] == "REVIEW_REQUEST"), request)
                if request:
                    break
            assert request and request["snapshot_id"] == submitted["snapshot_id"]
            snapshot_dir = next(line.split(": ", 1)[1] for line in request["body"].splitlines() if line.startswith("Read-only copy"))
            assert Path(snapshot_dir, "calc.py").read_text() == MUL
            await call(codex, "duet_send", kind="REVIEW_RESULT", body="Correct.", reply_to=request["message_id"], review={"disposition": "approve"})

            lifecycle = None
            for _ in range(20):
                got = await call(claude, "duet_wait", timeout=10)
                lifecycle = got["run"]["lifecycle"]
                if lifecycle == "COMPLETED_VERIFIED":
                    break
            assert lifecycle == "COMPLETED_VERIFIED", await call(claude, "duet_status")
            status = await call(codex, "duet_status")
            assert all(item["ok"] for item in status["verification"]["completion"]["items"])
            assert {p["origin"] for p in status["participants"]} == {"native_original"}
            profile = await call(codex, "duet_request_profile", model="gpt-6", effort="high", reason="test")
            assert profile["decision"] == "declined"

    anyio.run(scenario)
    ping = ServiceClient(paths_for(state)).ping()
    assert ping["pid"] != os.getpid()  # started on demand as its own process, exactly one


def test_proxy_restart_reconnects_the_same_session(tmp_path, state):
    repo = make_repo(tmp_path / "repo")

    async def scenario():
        async with Client(proxy(state, wrapped=True), mode="legacy") as codex:
            async with Client(proxy(state), mode="legacy") as claude:
                started = await call(claude, "duet_join", provider="claude", objective="x", repo=str(repo), checks=[f'"{PY}" check_feature.py'], peer="invite")
                await call(codex, "duet_join", provider="codex", run_id=started["run_id"], invite=started["invite"]["code"])
                first = await call(codex, "duet_wait", timeout=0)
            # Claude's MCP server stopped: the peer sees it without polling the model.
            changed = await call(codex, "duet_wait", timeout=10, ack_through=first["last_seq"])
            assert changed["run"]["collaboration"] == "PEER_UNAVAILABLE"
            question = await call(codex, "duet_send", kind="QUESTION", body="still there?")
            async with Client(proxy(state), mode="legacy") as claude_again:
                again = await call(claude_again, "duet_join", provider="claude")  # same host process: same participant
                assert again["rejoined"] and again["participant_id"] == started["participant_id"]
                got = await call(claude_again, "duet_wait", timeout=5)
                assert question["message_id"] in [m["message_id"] for m in got["messages"]]
                status = await call(claude_again, "duet_status")
                assert len(status["participants"]) == 2 and status["collaboration"] == "PAIR_ACTIVE"
                with pytest.raises(ToolFailure) as refused:
                    await call(claude_again, "duet_join", provider="claude", objective="a second run", repo=str(repo), checks=["true"])
                assert refused.value.payload["code"] == "policy_denied"

    anyio.run(scenario)


def test_tools_before_join_explain_themselves(tmp_path, state):
    async def scenario():
        async with Client(proxy(state), mode="legacy") as claude:
            with pytest.raises(ToolFailure) as failure:
                await call(claude, "duet_wait", timeout=0)
            assert failure.value.payload["code"] == "validation" and "duet_join" in failure.value.payload["message"]

    anyio.run(scenario)
