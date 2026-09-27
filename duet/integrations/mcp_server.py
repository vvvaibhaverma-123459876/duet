"""`duet mcp serve`: the DUET peer tools over MCP (D05).

A thin proxy. Every tool forwards to the runtime service over its local
socket; this process keeps no state store of its own, only:

- the session's participant token (in memory, plus a 0600 file keyed by the
  host process so an MCP server restart can reconnect the same session);
- the delivery watermark (last message sequence returned to the model), so
  a restarted proxy starts again from the durable acknowledgement cursor and
  unacknowledged messages are redelivered.

The token is never returned to the model. A native session's identity is the
host process that launched this server (the agent CLI); the service checks
that claim against the connecting process where the OS allows. A proxy
started for a managed peer (`--token-file`) can only act as that peer: it
cannot create runs, join other runs or start the service.

Uses the official MCP Python SDK (`pip install 'duet[mcp]'`, mcp>=2.2,<3)."""

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from ..runtime.contracts import DomainError, NotFound, Unauthorized, ValidationError
from ..runtime.identity import ProcessIdentity
from ..runtime.pairing import DEFAULT_WAIT_SECONDS, MAX_WAIT_SECONDS
from ..runtime.paths import ensure_private_dir
from ..runtime.service import ServiceClient, ServicePaths, ensure_service

SERVER_NAME = "duet"
FINISHED = frozenset({"COMPLETED_VERIFIED", "FAILED", "CANCELLED"})
MCP_REQUIREMENT = "mcp>=2.2,<3"


class ProxySession:
    def __init__(self, paths: ServicePaths, *, token: str | None = None, managed: bool = False, host: str | None = None) -> None:
        self.paths = paths
        self.token = token
        self.managed = managed
        self.host = host
        self.client_info: dict | None = None
        self.last_seq: int | None = None
        self.watch: str | None = None
        self._lock = threading.Lock()

    # -- persistence -------------------------------------------------------------------

    def _session_file(self) -> Path | None:
        if self.managed or not self.host:
            return None
        return ensure_private_dir(self.paths.root / "sessions") / (hashlib.sha256(self.host.encode()).hexdigest()[:32] + ".json")

    def restore(self) -> None:
        """Reconnect a native session whose MCP server restarted, unless its
        run has already ended (then the session starts fresh)."""
        path = self._session_file()
        if self.token or path is None or not path.exists():
            return
        try:
            token = json.loads(path.read_text(encoding="utf-8"))["token"]
            status = self._client(token).call("status", rpc_timeout=15.0)
            if status.get("lifecycle") in FINISHED:
                path.unlink(missing_ok=True)
                return
            self.token = token
        except (DomainError, KeyError, ValueError, OSError) as exc:
            if isinstance(exc, (Unauthorized, NotFound, KeyError, ValueError)):
                path.unlink(missing_ok=True)

    def _forget_finished_run(self) -> None:
        """A session may start a new run once its previous one has ended."""
        if not self.token or self.managed:
            return
        try:
            status = self._client(self.token).call("status", rpc_timeout=15.0)
            finished = status.get("lifecycle") in FINISHED
        except (Unauthorized, NotFound):
            finished = True
        if finished:
            self.token = None
            self.last_seq = None
            self.watch = None
            path = self._session_file()
            if path is not None:
                path.unlink(missing_ok=True)

    def _remember(self, token: str) -> None:
        path = self._session_file()
        if path is None:
            return
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"token": token, "host": self.host}, handle)
        os.replace(tmp, path)

    # -- calls -------------------------------------------------------------------------

    def _client(self, token: str | None) -> ServiceClient:
        ensure_service(self.paths, spawn=not self.managed)
        return ServiceClient(self.paths, token=token)

    def call(self, op: str, rpc_timeout: float = 60.0, **args: Any) -> Any:
        if op != "join" and not self.token:
            raise ValidationError("this session has not joined a DUET run yet; call duet_join first")
        return self._client(self.token).call(op, rpc_timeout=rpc_timeout, **{k: v for k, v in args.items() if v is not None})

    def join(self, **args: Any) -> dict:
        with self._lock:
            if self.token and (args.get("objective") or args.get("run_id")):
                self._forget_finished_run()
            if self.token:
                return self.call("join", provider=args.get("provider"), **{k: args.get(k) for k in ("objective", "run_id", "invite")})
            if self.managed:
                raise Unauthorized("a managed peer's DUET tools cannot start or join runs")
            result = self._client(None).call("join", rpc_timeout=120.0, host=self.host, client=self.client_info, **{k: v for k, v in args.items() if v is not None})
            token = result.pop("token")
            self.token = token
            self._remember(token)
            self.last_seq = None
            return result

    def wait(self, timeout: float, ack_through: int | None) -> dict:
        timeout = max(0.0, min(float(timeout), MAX_WAIT_SECONDS))
        result = self.call("wait", rpc_timeout=timeout + 30.0, since=self.last_seq, watch=self.watch, ack_through=ack_through, timeout=timeout)
        self.last_seq = result["last_seq"]
        self.watch = result["watch"]
        return result

    def close(self) -> None:
        if self.token and not self.managed:
            try:
                ServiceClient(self.paths, token=self.token).call("disconnect", rpc_timeout=5.0, reason="the session's MCP server stopped")
            except DomainError:
                pass


def _tool_error(exc: DomainError) -> Exception:
    from mcp.server.mcpserver.exceptions import ToolError

    return ToolError(json.dumps(exc.to_dict()))


def build_server(session: ProxySession):
    """Create the MCP server. Imported lazily so the core stays dependency-free."""
    import anyio
    from mcp.server.mcpserver import Context, MCPServer

    from ..runtime.peers import participant_instructions

    server = MCPServer(SERVER_NAME, instructions=participant_instructions(), version=_version())

    async def run(fn, *args, **kwargs):
        try:
            return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
        except DomainError as exc:
            raise _tool_error(exc) from None

    def capture_client(ctx: Context) -> None:
        if session.client_info is not None:
            return
        try:
            info = ctx.session.client_params.client_info
            session.client_info = {"name": info.name, "version": info.version}
        except AttributeError:
            session.client_info = {}

    @server.tool(description=(
        "Start a DUET run for this session, or join one. To start: provider, objective, repo (repository root), "
        "checks (commands that prove the task, e.g. the test command), peer ('managed' = DUET starts the other agent; "
        "'invite' = returns an invite for the user's own session), writer ('self' or 'peer'). To join: provider, run_id, invite. "
        "Joining again returns this session's existing registration."
    ))
    async def duet_join(
        ctx: Context,
        provider: str,
        objective: str | None = None,
        repo: str | None = None,
        checks: list[str] | None = None,
        protected: list[str] | None = None,
        peer: str = "managed",
        writer: str = "self",
        run_id: str | None = None,
        invite: str | None = None,
    ) -> dict[str, Any]:
        capture_client(ctx)
        joining = run_id is not None or invite is not None
        args = {"provider": provider, "run_id": run_id, "invite": invite} if joining else {
            "provider": provider, "objective": objective, "repo": repo, "checks": checks, "protected": protected, "peer": peer, "writer": writer,
        }
        return await run(session.join, **args)

    @server.tool(description=(
        "Send a structured message to your peer and return a receipt at once (it never waits for the answer). "
        "kind: QUESTION, ANSWER, FINDING, PLAN_PROPOSAL, TASK_PROPOSAL, BLOCKER, STATUS or REVIEW_RESULT. "
        "Answer with reply_to=<message_id>. REVIEW_RESULT needs review={disposition: approve|changes_requested|comment, "
        "findings: [{severity: blocking|non_blocking, summary, location}], resolves: [finding ids]}."
    ))
    async def duet_send(
        kind: str,
        body: str,
        reply_to: str | None = None,
        task_id: str | None = None,
        snapshot_id: str | None = None,
        review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await run(session.call, "send", kind=kind, body=body, reply_to=reply_to, task_id=task_id, snapshot_id=snapshot_id, review=review)

    @server.tool(description="Read messages for this session after the acknowledged cursor. ack_through=<seq> acknowledges handled messages first.")
    async def duet_inbox(ack_through: int | None = None, limit: int = 50) -> dict[str, Any]:
        return await run(session.call, "inbox", ack_through=ack_through, limit=limit)

    @server.tool(description=(
        f"Wait up to `timeout` seconds (max {int(MAX_WAIT_SECONDS)}) for anything relevant: a peer question or answer, a review "
        "request or result, a status update, or a run or peer change. Returns early. Answer the peer's questions before waiting "
        "again. ack_through=<last_seq> acknowledges what you handled."
    ))
    async def duet_wait(timeout: float = DEFAULT_WAIT_SECONDS, ack_through: int | None = None) -> dict[str, Any]:
        return await run(session.wait, timeout, ack_through)

    @server.tool(description=(
        "Propose one bounded subtask (a one-task plan). kind: code (writer only), investigate, test_design or review. "
        "Your peer must accept it (duet_decide_plan) before it exists. Tasks tied to acceptance criteria are required."
    ))
    async def duet_propose_task(description: str, kind: str = "investigate", depends_on: list[str] | None = None, acceptance_ids: list[str] | None = None) -> dict[str, Any]:
        return await run(session.call, "propose_task", description=description, kind=kind, depends_on=depends_on, acceptance_ids=acceptance_ids)

    @server.tool(description=(
        "Propose a plan: up to 12 tasks, each {key, description, kind: code|investigate|test_design|review, depends_on: [keys or task ids], "
        "acceptance_ids, parent}. It only adds tasks; the objective and acceptance contract cannot change. Your peer accepts or rejects it."
    ))
    async def duet_propose_plan(tasks: list[dict[str, Any]], rationale: str = "") -> dict[str, Any]:
        return await run(session.call, "propose_plan", tasks=tasks, rationale=rationale)

    @server.tool(description="Accept or reject your peer's plan (decision: accept|reject), or withdraw your own (withdraw).")
    async def duet_decide_plan(plan_id: str, decision: str, reason: str = "") -> dict[str, Any]:
        return await run(session.call, "decide_plan", plan_id=plan_id, decision=decision, reason=reason)

    @server.tool(description=(
        "Claim a ready task (default: the run's main task). Code tasks belong to the writer and return the workspace path; "
        "other kinds can be claimed by either participant. duet_status 'next' shows what DUET suggests for you."
    ))
    async def duet_claim(task_id: str | None = None) -> dict[str, Any]:
        return await run(session.call, "claim", task_id=task_id)

    @server.tool(description="Finish a task you own (not the main task: use duet_submit for that) with a summary and optional result text. Your peer decides whether to accept it.")
    async def duet_complete_task(task_id: str, summary: str, artifact: str | None = None) -> dict[str, Any]:
        return await run(session.call, "complete_task", task_id=task_id, summary=summary, artifact=artifact)

    @server.tool(description="Accept or reject your peer's finished task (decision: accept|reject) with a reason. A rejection sends it back for rework.")
    async def duet_decide_task(task_id: str, decision: str, reason: str = "") -> dict[str, Any]:
        return await run(session.call, "decide_task", task_id=task_id, decision=decision, reason=reason)

    @server.tool(description="Move the writer role: the writer hands it to the peer, or the reviewer takes it over when the writer is unavailable.")
    async def duet_handoff(reason: str = "") -> dict[str, Any]:
        return await run(session.call, "handoff", reason=reason)

    @server.tool(description=(
        "Writer only, main task: snapshot the workspace for review. DUET runs the acceptance checks on that exact snapshot and, "
        "unless request_review is false, asks your peer to review it. This does not certify completion."
    ))
    async def duet_submit(summary: str = "", task_id: str | None = None, request_review: bool = True, note: str = "") -> dict[str, Any]:
        return await run(session.call, "submit", rpc_timeout=120.0, summary=summary, task_id=task_id, request_review=request_review, note=note)

    @server.tool(description="Ask your peer to review a snapshot (default: the latest submitted) against the named acceptance criteria.")
    async def duet_request_review(snapshot_id: str | None = None, note: str = "", criteria: list[str] | None = None) -> dict[str, Any]:
        return await run(session.call, "request_review", snapshot_id=snapshot_id, note=note, criteria=criteria)

    @server.tool(description="Ask for a different model or effort with a reason. Peer-alpha runs fixed profiles, so this is declined with an explanation.")
    async def duet_request_profile(model: str | None = None, effort: str | None = None, reason: str = "") -> dict[str, Any]:
        return await run(session.call, "request_profile", model=model, effort=effort, reason=reason)

    @server.tool(description="Run status: participants and delivery modes, tasks, plans, contributions, open requests, checks, reviews, what completion still needs, and 'next': what DUET suggests you do.")
    async def duet_status() -> dict[str, Any]:
        return await run(session.call, "status")

    return server


def _version() -> str:
    from .. import __version__

    return __version__


def serve(state_root: str | None = None, token_file: str | None = None) -> int:
    try:
        import mcp  # noqa: F401
    except ImportError:
        import sys

        print(f"duet mcp serve needs the MCP Python SDK: pip install 'duet[mcp]' ({MCP_REQUIREMENT})", file=sys.stderr)
        return 2
    paths = ServicePaths.for_root(Path(state_root) if state_root else None)
    managed = token_file is not None or os.environ.get("DUET_MANAGED_PEER") == "1"
    token = Path(token_file).read_text(encoding="utf-8").strip() if token_file else None
    host = None if managed else str(ProcessIdentity.of(os.getppid()))
    session = ProxySession(paths, token=token, managed=managed, host=host)
    session.restore()
    server = build_server(session)
    try:
        server.run("stdio")
    finally:
        session.close()
    return 0
