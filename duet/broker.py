from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .adapters import Agent, AgentError
from .prompting import build_prompt
from .stopconditions import StopPolicy, strip_control_tokens
from .transcript import Message, Transcript
from .verifiers import Verifier
from .workspace import commit_after_turn, workspace_state


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
) -> SessionResult:
    if start_with not in agents:
        raise ValueError(f"unknown start agent: {start_with}")
    order = list(agents.keys())
    if not order:
        raise ValueError("duet requires at least one available agent")
    first_index = order.index(start_with)
    transcript = Transcript(task=task, workspace=str(workspace))
    policy = StopPolicy(max_turns, wallclock_seconds, loop_threshold, verifier)
    started_at = time.monotonic()
    partner_last = ""

    for turn in range(max_turns):
        agent_name = order[(first_index + turn) % len(order)]
        agent = agents[agent_name]
        role = (roles or {}).get(agent_name, f"You are {agent.display_name}. Collaborate constructively and move the task forward.")
        prompt = build_prompt(task, transcript, partner_last, workspace_state(workspace), role)
        if on_turn:
            on_turn(f"\n--- Turn {turn + 1}: {agent.display_name} ---")
        try:
            result = agent.send(prompt, workspace)
        except AgentError as exc:
            transcript.outcome = "halted"
            transcript.stop_condition = "AgentError"
            transcript.error = str(exc)
            session = Session(task, workspace, transcript, "halted", "AgentError")
            return _result(session, save_artifacts(workspace, transcript))
        cleaned, token = strip_control_tokens(result.text)
        committed = commit_after_turn(workspace, agent_name, agent.display_name)
        content = cleaned + (f"\n\n[Duet: committed workspace changes]" if committed else "\n\n[Duet: no workspace changes]")
        message = Message(
            turn_index=turn + 1,
            agent=agent_name,
            content=content,
            exit_code=result.exit_code,
            duration_s=result.duration_s,
            raw_stdout=result.raw_stdout,
            raw_stderr=result.raw_stderr,
        )
        transcript.add(message)
        partner_last = cleaned
        if on_turn:
            on_turn(f"{agent.display_name} ({result.duration_s:.2f}s):\n{cleaned}")
        decision = policy.check(
            transcript=transcript,
            current=message,
            control_token=token,
            started_at=started_at,
            workspace=workspace,
        )
        if decision.should_stop and decision.outcome == "success" and require_all_agents_for_success and not _all_agents_spoke(transcript, agents):
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


def _all_agents_spoke(transcript: Transcript, agents: dict[str, Agent]) -> bool:
    spoken = {message.agent for message in transcript.messages}
    return set(agents).issubset(spoken)


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
