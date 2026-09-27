from __future__ import annotations

import math
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .adapters import (
    Agent,
    AgentError,
    AgentTimeoutError,
    AuthError,
    BillingError,
    ModelUnavailableError,
    OutputLimitError,
    QuotaError,
)
from .logging_setup import get_logger
from .prompting import build_prompt, format_verification
from .stopconditions import StopPolicy, strip_control_tokens
from .transcript import Message, Transcript
from .verifiers import VerificationResult, Verifier
from .workspace import WorkspaceError, commit_after_turn, workspace_state

log = get_logger()

MAX_CAPTURE_CHARS = 20000
MAX_EVIDENCE_OUTPUT_CHARS = 8000

# Outcome vocabulary (DECISIONS.md D-002). Only `success` is a verified
# completion; everything else says precisely why it is not.
OUTCOME_EXIT_CODES = {
    "success": 0,
    "halted": 2,
    "unverified": 3,
    "review_pending": 4,
    "interrupted": 130,
}


def exit_code_for(outcome: str) -> int:
    return OUTCOME_EXIT_CODES.get(outcome, 2)


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

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.outcome)

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "stop_condition": self.stop_condition,
            "exit_code": self.exit_code,
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
    budget_usd: float = 0.0,
    commit_mode: str = "default",
) -> SessionResult:
    if start_with not in agents:
        raise ValueError(f"unknown start agent: {start_with}")
    if on_quota not in ("halt", "solo", "wait"):
        raise ValueError(f"on_quota must be halt, solo, or wait, got {on_quota!r}")
    if commit_mode not in ("default", "agent-driven"):
        raise ValueError(f"commit_mode must be default or agent-driven, got {commit_mode!r}")
    if not isinstance(max_turns, int) or max_turns < 1:
        raise ValueError(f"max_turns must be a positive integer, got {max_turns!r}")
    if not _finite(wallclock_seconds) or wallclock_seconds <= 0:
        raise ValueError(f"wallclock_seconds must be positive, got {wallclock_seconds!r}")
    if not _finite(budget_usd) or budget_usd < 0:
        raise ValueError(f"budget_usd must be a finite non-negative number, got {budget_usd!r}")
    if not _finite(quota_wait_seconds) or quota_wait_seconds < 0:
        raise ValueError(f"quota_wait_seconds must be non-negative, got {quota_wait_seconds!r}")
    order = list(agents.keys())
    if not order:
        raise ValueError("duet requires at least one available agent")

    run = _Run(task, workspace, verifier, on_turn)
    transcript = run.transcript
    started_at = time.monotonic()
    deadline = started_at + wallclock_seconds

    baseline = verifier.verify(workspace)
    transcript.baseline_verification = _evidence(verifier, baseline)
    policy = StopPolicy(max_turns, wallclock_seconds, loop_threshold, verifier, baseline_status=baseline.status)
    verification_note = format_verification(baseline) if baseline.status != "unknown" else ""

    if budget_usd > 0:
        unreported = [name for name, agent in agents.items() if not getattr(agent, "cost_json_path", "")]
        if unreported:
            run.note(
                f"budget ${budget_usd:.2f} covers reported costs only; {', '.join(unreported)} "
                f"{'reports' if len(unreported) == 1 else 'report'} no cost, so that usage is not counted against it"
            )

    pointer = order.index(start_with)
    partner_last = ""
    dropped: list[str] = []  # agents removed for quota; they still owe review
    last_turn_of: dict[str, int] = {}
    last_change_turn = 0
    in_flight: str | None = None
    turn = 0
    try:
        while turn < max_turns:
            # Admission: never dispatch a turn the session cannot afford.
            if time.monotonic() >= deadline:
                return run.finish("halted", f"WallClockBudget({wallclock_seconds})", policy)
            if budget_usd > 0 and transcript.total_cost_usd >= budget_usd:
                run.note(
                    f"budget exhausted: ${transcript.total_cost_usd:.4f} spent of ${budget_usd:.2f} cap (reported costs only)"
                )
                return run.finish("halted", f"BudgetExceeded(${budget_usd:.2f})", policy)

            agent_name = order[pointer % len(order)]
            agent = agents[agent_name]
            role = (roles or {}).get(agent_name, f"You are {agent.display_name}. Collaborate constructively and move the task forward.")
            prompt = build_prompt(task, transcript, partner_last, workspace_state(workspace), role, verification_note)
            if on_turn:
                on_turn(f"\n--- Turn {turn + 1}: {agent.display_name} ---")
            head_before = _head(workspace) if commit_mode == "agent-driven" else ""
            if hasattr(agent, "deadline"):
                agent.deadline = deadline  # clamp the turn to the session budget
            in_flight = agent_name
            try:
                result = agent.send(prompt, workspace)
            except QuotaError as exc:
                in_flight = None
                log.warning("turn %s (%s) quota/rate limit: %s", turn + 1, agent_name, exc)
                if on_quota == "solo" and len(order) > 1:
                    order.remove(agent_name)
                    dropped.append(agent_name)
                    pointer = pointer % len(order)
                    run.note(
                        f"{agent.display_name} hit its usage limit and was dropped from the rotation; "
                        f"continuing solo. Its review of the final state is still required, so the "
                        f"best possible outcome is review_pending until it reviews."
                    )
                    continue
                if on_quota == "wait":
                    if time.monotonic() + quota_wait_seconds >= deadline:
                        return run.finish(
                            "halted",
                            f"QuotaExhausted({agent_name})",
                            policy,
                            error=f"{exc} (waiting {quota_wait_seconds}s would exceed the wallclock budget)",
                            rerun_verification=True,
                        )
                    run.note(f"{agent.display_name} hit its usage limit; waiting {quota_wait_seconds}s before retrying.")
                    time.sleep(quota_wait_seconds)
                    continue
                return run.finish("halted", f"QuotaExhausted({agent_name})", policy, error=str(exc), rerun_verification=True)
            except AgentError as exc:
                in_flight = None
                log.warning("turn %s (%s) halted: %s", turn + 1, agent_name, exc)
                return run.finish("halted", _error_condition(agent_name, exc), policy, error=str(exc), rerun_verification=True)
            in_flight = None
            cleaned, token = strip_control_tokens(result.text)
            if commit_mode == "agent-driven":
                # The agent owns its commit sequence and messages; injecting a Broker
                # commit here would bury or mis-message them. Report what it actually did.
                note, changed = _agent_commit_note(workspace, head_before)
            else:
                try:
                    changed = commit_after_turn(workspace, agent_name, agent.display_name)
                except WorkspaceError as exc:
                    log.error("turn %s (%s) commit failed: %s", turn + 1, agent_name, exc)
                    return run.finish("halted", "WorkspaceError", policy, error=str(exc), rerun_verification=True)
                note = "[Duet: committed workspace changes]" if changed else "[Duet: no workspace changes]"
            message = Message(
                turn_index=turn + 1,
                agent=agent_name,
                content=cleaned + "\n\n" + note,
                exit_code=result.exit_code,
                duration_s=result.duration_s,
                raw_stdout=_cap(result.raw_stdout),
                raw_stderr=_cap(result.raw_stderr),
                cost_usd=result.cost_usd,
                changed=changed,
            )
            transcript.add(message)
            turn += 1
            pointer += 1
            last_turn_of[agent_name] = turn
            if changed:
                last_change_turn = turn
            partner_last = cleaned
            if on_turn:
                on_turn(f"{agent.display_name} ({result.duration_s:.2f}s):\n{cleaned}")

            # Completion is decided before any budget consequence: a turn that
            # finished the task is recognised even if it also spent the budget.
            decision = policy.check(
                transcript=transcript,
                current=message,
                control_token=token,
                started_at=started_at,
                workspace=workspace,
            )
            verification_note = format_verification(policy.last_result, policy.done_rejected)
            if policy.done_rejected and on_turn:
                on_turn(f"[Duet: {agent.display_name}'s completion claim was not accepted: verification failed]")
            completing = decision.should_stop and decision.outcome in ("success", "unverified")
            if completing and require_all_agents_for_success and not _all_agents_spoke(transcript, order):
                if on_turn:
                    on_turn(f"Stop candidate deferred until both agents have contributed: {decision.condition}")
                continue
            if decision.should_stop:
                outcome, condition = decision.outcome, decision.condition
                pending = [name for name in dropped if last_turn_of.get(name, 0) < last_change_turn]
                if pending and outcome == "success":
                    outcome = "review_pending"
                    condition = f"{condition}+ReviewPending({','.join(pending)})"
                if pending:
                    run.note(
                        f"review pending: {', '.join(pending)} left the rotation before reviewing the final changes; "
                        f"resume when its limit resets to obtain that review"
                    )
                return run.finish(outcome, condition, policy)

        return run.finish("halted", f"MaxTurns({max_turns})", policy)
    except KeyboardInterrupt:
        log.warning("session interrupted")
        if in_flight:
            run.note(
                f"interrupted during {in_flight}'s turn; any partial changes from that turn were left "
                f"uncommitted and their effects are unverified"
            )
        return run.finish("interrupted", "Interrupted", policy, verify=False)


class _Run:
    """Bookkeeping for one session: notes, final evidence, artifacts."""

    def __init__(self, task: str, workspace: Path, verifier: Verifier, on_turn: Callable[[str], None] | None) -> None:
        self.task = task
        self.workspace = workspace
        self.verifier = verifier
        self.on_turn = on_turn
        self.transcript = Transcript(task=task, workspace=str(workspace))

    def note(self, text: str) -> None:
        self.transcript.note(text)
        if self.on_turn:
            self.on_turn(f"[Duet: {text}]")

    def finish(
        self,
        outcome: str,
        condition: str,
        policy: StopPolicy,
        error: str = "",
        rerun_verification: bool = False,
        verify: bool = True,
    ) -> SessionResult:
        """Record the final verification evidence and save artifacts.

        When a halt may have left the workspace in a state the verifier has
        not seen (an agent failed mid-turn), the checks run once more: running
        out of inference budget must not prevent local finalisation. A
        cancellation never triggers new verification work."""
        transcript = self.transcript
        final: VerificationResult | None = policy.last_result
        if verify and (rerun_verification or final is None):
            try:
                final = self.verifier.verify(self.workspace)
            except Exception as exc:  # verification must never mask the halt reason
                log.error("final verification failed to run: %s", exc)
                final = VerificationResult("unknown", False, f"verification could not run: {exc}")
        if final is not None:
            transcript.final_verification = _evidence(self.verifier, final)
        transcript.outcome = outcome
        transcript.stop_condition = condition
        if error:
            transcript.error = error
        session = Session(self.task, self.workspace, transcript, outcome, condition)
        return _result(session, save_artifacts(self.workspace, transcript))


def _error_condition(agent_name: str, exc: AgentError) -> str:
    if isinstance(exc, AuthError):
        return f"AuthFailed({agent_name})"
    if isinstance(exc, BillingError):
        return f"BillingBlocked({agent_name})"
    if isinstance(exc, ModelUnavailableError):
        return f"ModelUnavailable({agent_name})"
    if isinstance(exc, AgentTimeoutError):
        return f"AgentTimeout({agent_name})"
    if isinstance(exc, OutputLimitError):
        return f"OutputLimit({agent_name})"
    return "AgentError"


def _evidence(verifier: Verifier, result: VerificationResult) -> dict:
    output = result.output or ""
    if len(output) > MAX_EVIDENCE_OUTPUT_CHARS:
        output = "...[truncated]...\n" + output[-MAX_EVIDENCE_OUTPUT_CHARS:]
    return {
        "verifier": getattr(verifier, "name", "unknown"),
        "status": result.status,
        "output": output,
        "at": datetime.now(UTC).isoformat(),
    }


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _head(workspace: Path) -> str:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, text=True, capture_output=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _agent_commit_note(workspace: Path, head_before: str) -> tuple[str, bool]:
    """Under agent-driven mode Duet commits nothing, so the turn note reports what
    the agent committed on its own — and flags a dirty tree rather than hiding it
    behind an auto-commit. Returns (note, workspace_changed)."""
    head_after = _head(workspace)
    if head_after and head_after != head_before:
        span = f"{head_before}..{head_after}" if head_before else head_after
        subjects = subprocess.run(
            ["git", "log", "--format=%h %s", span], cwd=workspace, text=True, capture_output=True
        ).stdout.strip()
        count = len(subjects.splitlines())
        plural = "commit" if count == 1 else "commits"
        return f"[Duet: agent made {count} {plural}]\n{subjects}", True
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=workspace, text=True, capture_output=True).stdout.strip()
    if dirty:
        return "[Duet: agent left uncommitted changes and made no commit]", True
    return "[Duet: agent made no commit and left no changes]", False


def _all_agents_spoke(transcript: Transcript, agent_names: list[str]) -> bool:
    spoken = {message.agent for message in transcript.messages}
    return set(agent_names).issubset(spoken)


def save_artifacts(workspace: Path, transcript: Transcript, path: Path | None = None) -> tuple[Path, Path]:
    out_dir = path or workspace / ".duet"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    json_path = out_dir / f"transcript-{stamp}.json"
    md_path = out_dir / f"transcript-{stamp}.md"
    transcript.save_json(json_path)
    md_path.write_text(transcript.render_markdown(), encoding="utf-8")
    return json_path, md_path


def _result(session: Session, paths: tuple[Path, Path]) -> SessionResult:
    return SessionResult(session, paths[0], paths[1], session.outcome, session.stop_condition)
