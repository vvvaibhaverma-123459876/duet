from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .adapters import Agent, AgentError, QuotaError
from .logging_setup import get_logger
from .prompting import build_prompt
from .stopconditions import StopPolicy, strip_control_tokens
from .transcript import Message, Transcript
from .verifiers import Verifier
from .workspace import WorkspaceError, commit_after_turn, workspace_state

log = get_logger()

MAX_CAPTURE_CHARS = 20000


def _cap(text: str, limit: int = MAX_CAPTURE_CHARS) -> str:
    """Bound stored agent output so a chatty CLI or huge repo cannot exhaust
    memory or bloat the transcript. Keeps head and tail with a marker."""
    if len(text) <= limit:
        return text
    head = text[: limit // 2].rstrip()
    tail = text[-limit // 2 :].lstrip()
    return f"{head}\n...[{len(text) - limit} chars truncated by Duet]...\n{tail}"


@dataclass
class Session:
    task: str
    workspace: Path
    transcript: Transcript
    outcome: str = "unknown"
    stop_condition: str = ""


@dataclass
class SessionResult:
    session: Session
    transcript_path: Path | None
    markdown_path: Path | None
    outcome: str
    stop_condition: str

    @property
    def transcript(self) -> Transcript:
        return self.session.transcript

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "stop_condition": self.stop_condition,
            "workspace": str(self.session.workspace),
            "transcript_path": str(self.transcript_path) if self.transcript_path else None,
            "markdown_path": str(self.markdown_path) if self.markdown_path else None,
            "transcript": self.session.transcript.to_dict(),
        }


def run_session(
    task: str,
    workspace: Path,
    agents: dict[str, Agent],
    start_with: str,
    max_turns: int,
    wallclock_seconds: int,
    loop_threshold: float,
    verifier: Verifier,
    roles: dict[str, str] | None = None,
    on_turn: Callable[[str], None] | None = None,
    require_all_agents_for_success: bool = True,
    on_quota: str = "halt",
    quota_wait_seconds: int = 300,
) -> SessionResult:
    if start_with not in agents:
        raise ValueError(f"unknown start agent: {start_with}")
    if on_quota not in ("halt", "solo", "wait"):
        raise ValueError(f"on_quota must be halt, solo, or wait, got {on_quota!r}")
    order = list(agents.keys())
    if not order:
        raise ValueError("duet requires at least one available agent")
    pointer = order.index(start_with)
    transcript = Transcript(task=task, workspace=str(workspace))
    policy = StopPolicy(max_turns, wallclock_seconds, loop_threshold, verifier)
    started_at = time.monotonic()
    partner_last = ""

    turn = 0
    while turn < max_turns:
        agent_name = order[pointer % len(order)]
        agent = agents[agent_name]
        role = (roles or {}).get(agent_name, f"You are {agent.display_name}. Collaborate constructively and move the task forward.")
        prompt = build_prompt(task, transcript, partner_last, workspace_state(workspace), role)
        if on_turn:
            on_turn(f"\n--- Turn {turn + 1}: {agent.display_name} ---")
        try:
            result = agent.send(prompt, workspace)
        except QuotaError as exc:
            log.warning("turn %s (%s) quota exhausted: %s", turn + 1, agent_name, exc)
            if on_quota == "solo" and len(order) > 1:
                order.remove(agent_name)
                pointer = pointer % len(order)
                note = (
                    f"{agent.display_name} hit its usage limit and was dropped from the rotation; "
                    f"continuing solo. Its review/verification of later turns is pending."
                )
                transcript.note(note)
                if on_turn:
                    on_turn(f"[Duet: {note}]")
                continue
            if on_quota == "wait":
                elapsed = time.monotonic() - started_at
                if elapsed + quota_wait_seconds >= wallclock_seconds:
                    transcript.outcome = "halted"
                    transcript.stop_condition = f"QuotaExhausted({agent_name})"
                    transcript.error = f"{exc} (waiting {quota_wait_seconds}s would exceed the wallclock budget)"
                    session = Session(task, workspace, transcript, "halted", transcript.stop_condition)
                    return _result(session, save_artifacts(workspace, transcript))
                note = f"{agent.display_name} hit its usage limit; waiting {quota_wait_seconds}s before retrying."
                transcript.note(note)
                if on_turn:
                    on_turn(f"[Duet: {note}]")
                time.sleep(quota_wait_seconds)
                continue
            transcript.outcome = "halted"
            transcript.stop_condition = f"QuotaExhausted({agent_name})"
            transcript.error = str(exc)
            session = Session(task, workspace, transcript, "halted", transcript.stop_condition)
            return _result(session, save_artifacts(workspace, transcript))
        except AgentError as exc:
            log.warning("turn %s (%s) halted: %s", turn + 1, agent_name, exc)
            transcript.outcome = "halted"
            transcript.stop_condition = "AgentError"
            transcript.error = str(exc)
            session = Session(task, workspace, transcript, "halted", "AgentError")
            return _result(session, save_artifacts(workspace, transcript))
        cleaned, token = strip_control_tokens(result.text)
        try:
            committed = commit_after_turn(workspace, agent_name, agent.display_name)
        except WorkspaceError as exc:
            log.error("turn %s (%s) commit failed: %s", turn + 1, agent_name, exc)
            transcript.outcome = "halted"
            transcript.stop_condition = "WorkspaceError"
            transcript.error = str(exc)
            session = Session(task, workspace, transcript, "halted", "WorkspaceError")
            return _result(session, save_artifacts(workspace, transcript))
        content = cleaned + (f"\n\n[Duet: committed workspace changes]" if committed else "\n\n[Duet: no workspace changes]")
        message = Message(
            turn_index=turn + 1,
            agent=agent_name,
            content=content,
            exit_code=result.exit_code,
            duration_s=result.duration_s,
            raw_stdout=_cap(result.raw_stdout),
            raw_stderr=_cap(result.raw_stderr),
        )
        transcript.add(message)
        partner_last = cleaned
        if on_turn:
            on_turn(f"{agent.display_name} ({result.duration_s:.2f}s):\n{cleaned}")
        turn += 1
        pointer += 1
        decision = policy.check(
            transcript=transcript,
            current=message,
            control_token=token,
            started_at=started_at,
            workspace=workspace,
        )
        # Success requires every agent still in the rotation to have spoken;
        # an agent dropped for quota no longer blocks it.
        if decision.should_stop and decision.outcome == "success" and require_all_agents_for_success and not _all_agents_spoke(transcript, order):
            if on_turn:
                on_turn(f"Stop candidate deferred until both agents have contributed: {decision.condition}")
            continue
        if decision.should_stop:
            transcript.outcome = decision.outcome
            transcript.stop_condition = decision.condition
            session = Session(task, workspace, transcript, decision.outcome, decision.condition)
            return _result(session, save_artifacts(workspace, transcript))

    transcript.outcome = "halted"
    transcript.stop_condition = f"MaxTurns({max_turns})"
    session = Session(task, workspace, transcript, "halted", transcript.stop_condition)
    return _result(session, save_artifacts(workspace, transcript))


def _all_agents_spoke(transcript: Transcript, agent_names: list[str]) -> bool:
    spoken = {message.agent for message in transcript.messages}
    return set(agent_names).issubset(spoken)


def save_artifacts(workspace: Path, transcript: Transcript, path: Path | None = None) -> tuple[Path, Path]:
    out_dir = path or workspace / ".duet"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"transcript-{stamp}.json"
    md_path = out_dir / f"transcript-{stamp}.md"
    transcript.save_json(json_path)
    md_path.write_text(transcript.render_markdown(), encoding="utf-8")
    return json_path, md_path


def _result(session: Session, paths: tuple[Path, Path]) -> SessionResult:
    return SessionResult(session, paths[0], paths[1], session.outcome, session.stop_condition)
