"""Managed peers: sessions DUET launches and drives (D05).

A managed peer is a provider session DUET started itself (origin `managed`).
Its driver thread sleeps on the peer's inbox and runs one provider turn when a
message arrives, or once at kickoff when the peer is the writer. During the
turn the provider reaches DUET through the same MCP tools a native session
uses (`duet mcp serve --token-file ...`), so there is one protocol for both.

Every turn is a durable action (planned, claimed, recorded): a turn the
service loses while it runs becomes IN_DOUBT and is never replayed blindly.
Turns are capped by the run policy's `max_invocations`. Authentication,
billing and quota failures stop the peer and mark it unavailable; nothing
falls back to another account, provider or paid API (R13)."""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from importlib import resources
from pathlib import Path
from typing import Any

from ..adapters import AgentError, AuthError, BillingError, QuotaError
from ..providers.base import ProviderAdapter, TurnRequest, TurnResult
from .contracts import CONTROLLER, TERMINAL_RUN, ActionState, Liveness, MessageKind, MessageState, RunLifecycle
from .pairing import MAX_WAIT_SECONDS, PairCoordinator, other_provider

log = logging.getLogger("duet.peers")

DEFAULT_TURN_TIMEOUT = 900.0
FALLBACK_PREFIX = "[DUET: delivered from the end of the peer's turn because it did not reply with duet_send] "


def _finished(lifecycle: str) -> bool:
    """Terminal or paused: either way a managed peer stops taking turns."""
    return RunLifecycle(lifecycle) in TERMINAL_RUN or lifecycle.startswith("PAUSED_")


ACTIONABLE_FROM_PEER = frozenset(
    k.value for k in (MessageKind.QUESTION, MessageKind.ANSWER, MessageKind.REVIEW_REQUEST, MessageKind.FINDING,
                      MessageKind.BLOCKER, MessageKind.PLAN_PROPOSAL, MessageKind.TASK_PROPOSAL)
)


def _actionable(message: dict) -> bool:
    """A turn costs a model call. Start one for peer messages that ask for
    something (questions, answers to our questions, review requests,
    findings, proposals, blockers), for controller BLOCKERs and for a
    controller TASK_PROPOSAL (a task the scheduler suggests for this peer).
    Status notes and review results wait for the next real turn: a rejected
    review also produces a controller BLOCKER."""
    if message["from"] == "controller":
        return message["kind"] in (MessageKind.BLOCKER.value, MessageKind.TASK_PROPOSAL.value)
    return message["kind"] in ACTIONABLE_FROM_PEER


def participant_instructions() -> str:
    return resources.files("duet").joinpath("resources/instructions/participant.md").read_text(encoding="utf-8")


def mcp_server_config(state_root: Path, token_file: Path, provider: str) -> dict:
    """Claude-style MCP config for a managed peer's DUET tools. The token is
    passed as a 0600 file path, never on a command line or in the config."""
    server: dict[str, Any] = {
        "command": sys.executable,
        "args": ["-m", "duet", "mcp", "serve", "--state-root", str(state_root), "--token-file", str(token_file)],
        "env": {"DUET_MANAGED_PEER": "1"},
    }
    if provider == "codex":
        # Codex times MCP tool calls out (60 s by default). The key is accepted
        # by 0.157.1; whether it takes effect was not observable here.
        server["tool_timeout_sec"] = int(MAX_WAIT_SECONDS + 30)
    return {"mcpServers": {"duet": server}}


class ManagedPeer:
    def __init__(
        self,
        coordinator: PairCoordinator,
        *,
        run_id: str,
        provider: str,
        adapter: ProviderAdapter,
        state_root: Path,
        turn_timeout: float = DEFAULT_TURN_TIMEOUT,
    ) -> None:
        self.co = coordinator
        self.run_id = run_id
        self.provider = provider
        self.adapter = adapter
        self.state_root = Path(state_root)
        self.turn_timeout = turn_timeout
        registered = coordinator.register_managed(run_id, provider)
        self.participant = registered["participant"]
        self.participant_id = self.participant["participant_id"]
        self.principal = coordinator.runtime.authenticate(registered["token"])
        peers_dir = self.state_root / "peers"
        peers_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.token_file = peers_dir / f"{self.participant_id}.token"
        fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(registered["token"])
        self.session_id: str | None = None
        self.turns = 0
        self.results: list[TurnResult] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"duet-peer-{provider}-{run_id[-8:]}", daemon=True)

    # -- lifecycle -------------------------------------------------------------------------

    def start(self) -> "ManagedPeer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=30)
        close = getattr(self.adapter, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                log.exception("closing %s adapter failed", self.provider)
        try:
            self.token_file.unlink()
        except FileNotFoundError:
            pass

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    # -- loop ------------------------------------------------------------------------------

    def _is_writer(self) -> bool:
        return self.co._writer_id(self.run_id) == self.participant_id

    def _run_state(self) -> str:
        return self.co.runtime.get_run(CONTROLLER, self.run_id)["lifecycle"]

    def _loop(self) -> None:
        since = None
        kickoff = self._is_writer()
        backlog: list[dict] = []  # informational notices, shown with the next real turn
        try:
            while not self._stop.is_set():
                if _finished(self._run_state()):
                    return
                messages: list[dict] = []
                if not kickoff:
                    result = self.co.wait(self.principal, since=since, timeout=MAX_WAIT_SECONDS, cancel=self._stop)
                    if self._stop.is_set() or _finished(result["run"]["lifecycle"]):
                        return
                    since = result["last_seq"]
                    backlog.extend(result["messages"])
                    if not any(_actionable(m) for m in backlog):
                        if result["messages"] and since > self.co._cursor(self.principal):
                            self.co.runtime.ack(self.principal, up_to_seq=since)
                        continue
                    messages, backlog = backlog, []
                kickoff = False
                policy = self.co.runtime.policy_for(self.run_id)
                used = self.co.runtime.store.read().scalar(
                    "SELECT COUNT(*) FROM actions WHERE run_id = ? AND type = 'provider_turn'", (self.run_id,)
                ) or 0
                if used >= policy.max_invocations:
                    self._give_up(f"the run's invocation budget ({policy.max_invocations} provider turns) is spent")
                    return
                if not self._turn(messages):
                    return
                # The model may have acknowledged further during the turn
                # (duet_wait/duet_inbox ack_through); never move backwards.
                cursor = self.co._cursor(self.principal)
                if since is not None and since > cursor:
                    self.co.runtime.ack(self.principal, up_to_seq=since)
                since = max(since or 0, cursor)
        except Exception as exc:  # the driver must never die silently
            log.exception("managed %s peer failed", self.provider)
            self._give_up(f"driver error: {type(exc).__name__}: {exc}")

    def _turn(self, messages: list[dict]) -> bool:
        runtime = self.co.runtime
        planned = runtime.plan_action(
            CONTROLLER, run_id=self.run_id, type="provider_turn", participant_id=self.participant_id,
            input={"provider": self.provider, "turn": self.turns + 1, "messages": [m["message_id"] for m in messages], "resume": self.session_id},
        )
        action_id = planned["action"]["action_id"]
        # The action lease must outlive the turn's own timeout.
        fence = runtime.claim_action(CONTROLLER, action_id, lease_seconds=int(self.turn_timeout) + 300)["lease"]["fencing_token"]
        runtime.record_action(CONTROLLER, action_id, ActionState.RUNNING, fence=fence)
        writer = self._is_writer()
        settings = self.co.settings(self.run_id)
        request = TurnRequest(
            prompt=self._prompt(messages, writer),
            cwd=Path(settings.workspace_path),
            session_id=self.session_id,
            permission_profile="workspace_write" if writer else "read_only",
            mcp_config=mcp_server_config(self.state_root, self.token_file, self.provider),
            env={"DUET_MANAGED_PEER": "1"},
            timeout_seconds=self.turn_timeout,
        )
        self.turns += 1
        sent_before = self._last_sent_seq()
        try:
            result = self.adapter.run_turn(request, cancel=self._stop)
        except (AuthError, BillingError, QuotaError) as exc:
            runtime.record_action(CONTROLLER, action_id, ActionState.FAILED, fence=fence, result={"error": str(exc)[:2000], "kind": exc.kind})
            self._give_up(f"{exc.kind}: {exc}. DUET does not switch accounts, providers or billing to continue.")
            return False
        except AgentError as exc:
            runtime.record_action(CONTROLLER, action_id, ActionState.FAILED, fence=fence, result={"error": str(exc)[:2000], "kind": exc.kind})
            self._give_up(f"{exc.kind}: {exc}")
            return False
        except Exception as exc:
            # Record the outcome before anything else: an action left RUNNING
            # would block completion as "unobserved".
            runtime.record_action(CONTROLLER, action_id, ActionState.FAILED, fence=fence, result={"error": f"{type(exc).__name__}: {exc}"[:2000]})
            raise
        self.results.append(result)
        outcome = ActionState.SUCCEEDED if result.ok else ActionState.FAILED
        runtime.record_action(
            CONTROLLER, action_id, outcome, fence=fence, provider_invocation_id=result.provider_invocation_id,
            result={
                "status": result.status, "session_id": result.session_id, "lineage": result.lineage,
                "settings": {"requested": result.settings.requested, "accepted": result.settings.accepted, "observed": result.settings.observed},
                "usage": [u.to_dict() for u in result.usage], "warnings": list(result.warnings)[:20],
                "permission_denials": list(result.permission_denials)[:20], "text_excerpt": result.text[-2000:],
            },
        )
        if result.session_id and result.session_id != self.session_id:
            self.session_id = result.session_id
            runtime.update_participant(CONTROLLER, self.participant_id, native_session_id=result.session_id)
        if not result.ok:
            if result.status in ("cancelled", "interrupted") and self._stop.is_set():
                return False
            if result.error is not None and isinstance(result.error, (AuthError, BillingError, QuotaError)):
                self._give_up(f"{result.error.kind}: {result.error}")
                return False
        if self._last_sent_seq() == sent_before:
            self._fallback_answers(messages, result)
        # The turn's action is settled now; completion may have been waiting on it.
        self.co.try_complete(self.run_id)
        return True

    def _last_sent_seq(self) -> int:
        value = self.co.runtime.store.read().scalar(
            "SELECT MAX(seq) FROM messages WHERE run_id = ? AND sender = ?", (self.run_id, self.participant_id)
        )
        return int(value or 0)

    def _fallback_answers(self, messages: list[dict], result: TurnResult) -> None:
        """If the peer ended a turn without sending anything at all, deliver
        its final message as the answer to the questions it was given, clearly
        labelled, so the asker is not left waiting on a reply that will never
        come. A peer that replied, or asked a clarifying question instead of
        answering yet, is left alone."""
        if not result.text.strip():
            return
        tx = self.co.runtime.store.read()
        for message in messages:
            if message["kind"] != MessageKind.QUESTION.value:
                continue
            row = tx.get("messages", message["message_id"])
            if row is None or row["state"] in (MessageState.HANDLED.value, MessageState.EXPIRED.value, MessageState.CANCELLED.value):
                continue
            try:
                self.co.send(self.principal, kind="ANSWER", body=FALLBACK_PREFIX + result.text[-8000:], reply_to=message["message_id"])
            except Exception:
                log.exception("fallback answer failed")

    def _give_up(self, reason: str) -> None:
        try:
            part = self.co._participant(self.participant_id)
            if part["liveness"] != Liveness.GONE.value:
                self.co.runtime.update_participant(CONTROLLER, self.participant_id, liveness=Liveness.UNAVAILABLE)
                self.co._peer_lost(self.run_id, self.participant_id, reason)
        except Exception:
            log.exception("could not record that the %s peer stopped", self.provider)

    # -- prompt ----------------------------------------------------------------------------

    def _prompt(self, messages: list[dict], writer: bool) -> str:
        run = self.co.runtime.get_run(CONTROLLER, self.run_id)
        settings = self.co.settings(self.run_id)
        checks = [" ".join(c.get("argv") or [c.get("shell", "")]) for c in run["acceptance"].get("checks", [])]
        lines = [
            participant_instructions().strip(),
            "",
            "## This session",
            f"You are the managed {self.provider} participant in DUET run {self.run_id} (origin: managed, started by DUET).",
            f"Role: {'writer' if writer else 'reviewer'}. Your peer is {other_provider(self.provider)}.",
            f"Objective: {run['objective']}",
            f"Acceptance: DUET runs {', '.join(checks) or '(no checks)'} on the submitted snapshot, and a {other_provider(self.provider)} review is required.",
        ]
        if writer:
            lines.append(f"Your workspace is {settings.workspace_path}. Call duet_claim, edit only there, then duet_submit.")
        else:
            lines.append("You cannot edit files. Review snapshots when asked and answer questions.")
        lines += [
            "",
            "Managed sessions end their turn instead of waiting for long: when a message arrives for you, DUET starts a new turn.",
            "Before ending this turn, reply to every question and review request below with duet_send and reply_to.",
        ]
        try:
            suggestions = self.co.graph.next_for(self.principal)[:3]
        except Exception:
            suggestions = []
        if suggestions:
            lines += ["", "## DUET suggests (highest priority first)"]
            lines += [f"- {s['action']}" + (f" {s['task_id']}" if s.get("task_id") else "") + f": {s['reason']}" for s in suggestions]
        if messages:
            lines += ["", "## New DUET messages"]
            for m in messages:
                lines.append(f"[seq {m['seq']}] {m['kind']} from {m['from']} (message_id {m['message_id']}"
                             + (f", snapshot {m['snapshot_id']}" if m.get("snapshot_id") else "") + f"):\n{m['body']}")
        else:
            lines += ["", "No messages yet: start the task."]
        return "\n".join(lines)


def default_peer_factory(service: Any, run_id: str, provider: str) -> ManagedPeer:
    """Launch a real managed peer with the installed CLI and the user's
    existing login. Binary overrides exist for emulator tests only."""
    from ..providers.claude_cli import ClaudeCLIAdapter
    from ..providers.codex_appserver import CodexAppServerAdapter

    binary = os.environ.get("DUET_CLAUDE_BIN" if provider == "claude" else "DUET_CODEX_BIN", provider)
    if shutil.which(binary) is None:
        # Fail before registering: a peer that can never take a turn must not
        # appear in the run as if it were there.
        raise AgentError(f"{provider}: executable not found: {binary}", kind="not_found")
    if provider == "claude":
        adapter: ProviderAdapter = ClaudeCLIAdapter(binary, allowed_tools=("mcp__duet",), env={"DUET_MANAGED_PEER": "1"})
    else:
        adapter = CodexAppServerAdapter(binary, env={"DUET_MANAGED_PEER": "1"})
    peer = ManagedPeer(service.coordinator, run_id=run_id, provider=provider, adapter=adapter, state_root=service.paths.root)
    return peer.start()


def peer_summary(peer: ManagedPeer) -> dict:
    return {"participant_id": peer.participant_id, "provider": peer.provider, "turns": peer.turns, "session_id": peer.session_id, "alive": peer.alive}
