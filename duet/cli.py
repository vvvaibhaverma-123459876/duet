from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .adapters import AgentError
from .broker import run_session
from .config import ConfigError, load_config, write_config
from .doctor import available_agent_names, format_checks, hard_failures, run_doctor
from .logging_setup import configure_logging, get_logger
from .control import running_targets, stop_target
from .detect import detect_activity, format_activity
from .repl import Repl
from .resumestate import ResumeError, ResumeState, load_resume_state, save_resume_state
from .sessions import (
    format_codex_sessions,
    format_peek,
    format_sessions,
    list_claude_sessions,
    list_codex_sessions,
    peek_session,
)
from .transcript import Transcript
from .verifiers import AlwaysUnknown, PytestVerifier
from .workspace import (
    LiveRepo,
    WorkspaceError,
    create_workspace,
    git_log_summary,
    prepare_live_repo,
    release_lock,
    rollback_live_repo,
    seed_demo,
)

log = get_logger()


DEMO_TASK = """Implement and verify roman_to_int in the seeded workspace.

Claude and Codex should collaborate sequentially. The Implementer edits roman.py to satisfy the spec. The Verifier adds useful edge-case tests to test_roman.py and reviews the implementation for bugs, reporting issues back. They iterate. The Broker runs PytestVerifier after each turn; the session succeeds only when pytest actually passes."""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv and not sys.stdin.isatty():
        argv = ["run", sys.stdin.read()]

    parser = argparse.ArgumentParser(prog="duet")
    parser.add_argument("--config", default=None)
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--seed-demo", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR (or set DUET_LOG)")
    parser.add_argument("--log-file", default=None, help="also write logs to this file")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor")

    for name in ("run", "exec"):
        run = sub.add_parser(name)
        run.add_argument("task", nargs="?")
        run.add_argument("--workspace")
        run.add_argument("--repo", help="operate on an existing git repo (live mode) on an isolated duet/ branch")
        run.add_argument("--branch", help="branch name to create for the session (live mode)")
        run.add_argument("--allow-dirty", action="store_true", help="permit uncommitted changes in the target repo")
        run.add_argument("--rollback-on-failure", action="store_true", help="discard the duet branch if the session does not succeed")
        run.add_argument("--start", choices=["claude", "codex"])
        run.add_argument("--max-turns", type=int)
        run.add_argument("--verify", choices=["pytest", "none"], default="none")
        run.add_argument("--seed-demo", action="store_true")
        run.add_argument("--interactive-handoff", action="store_true")
        run.add_argument("--output-format", choices=["text", "json"], default="text")
        run.add_argument(
            "--attach",
            action="append",
            metavar="AGENT=SESSION_ID",
            help="resume an existing agent session (e.g. claude=<uuid> from `duet sessions`); repeatable",
        )
        run.add_argument(
            "--chain-sessions",
            action="store_true",
            help="carry each agent's CLI session forward between turns instead of stateless spawns",
        )
        run.add_argument(
            "--on-quota",
            choices=["halt", "solo", "wait"],
            default=None,
            help="when an agent hits its usage limit: halt cleanly (default), continue solo with the other agent, or wait and retry",
        )
        run.add_argument("--quota-wait-seconds", type=int, default=None, help="wait interval for --on-quota wait (default 300)")

    sessions = sub.add_parser("sessions", help="list agent CLI sessions available to attach or peek")
    sessions.add_argument("agent", nargs="?", choices=["claude", "codex"], default="claude")
    sessions.add_argument("--repo", default=".", help="repo the sessions belong to (claude only; default: current directory)")
    sessions.add_argument("--limit", type=int, default=10)

    status = sub.add_parser("status", help="detect agent sessions and processes relevant to a repo")
    status.add_argument("--repo", default=".", help="repo to inspect (default: current directory)")

    connect = sub.add_parser(
        "connect",
        help="resume existing independent claude/codex sessions as one duet; cold-starts agents with no session",
    )
    connect.add_argument("task", nargs="?")
    connect.add_argument("--repo", default=".", help="repo the sessions belong to and to work on (live mode)")
    connect.add_argument("--claude", metavar="SESSION_ID", help="claude session to resume (default: newest idle for repo)")
    connect.add_argument("--codex", metavar="SESSION_ID", help="codex session to resume (default: newest idle for repo)")
    connect.add_argument("--fork-live", action="store_true", help="allow resuming a session that looks live (forks its state)")
    connect.add_argument("--branch")
    connect.add_argument("--allow-dirty", action="store_true")
    connect.add_argument("--rollback-on-failure", action="store_true")
    connect.add_argument("--start", choices=["claude", "codex"])
    connect.add_argument("--max-turns", type=int)
    connect.add_argument("--verify", choices=["pytest", "none"], default="none")
    connect.add_argument("--output-format", choices=["text", "json"], default="text")
    connect.add_argument("--on-quota", choices=["halt", "solo", "wait"], default=None)
    connect.add_argument("--quota-wait-seconds", type=int, default=None)

    resume = sub.add_parser("resume", help="continue the last duet run in a workspace/repo, re-attaching both agents")
    resume.add_argument("task", nargs="?", help="override the continuation prompt (default: continue the saved task)")
    resume.add_argument("--repo", default=".", help="workspace or repo of the halted run (default: current directory)")
    resume.add_argument(
        "--wait-ready",
        nargs="?",
        type=int,
        const=600,
        default=None,
        metavar="SECONDS",
        help="poll doctor every SECONDS (default 600) until all saved agents pass, e.g. after a quota halt",
    )
    resume.add_argument("--max-turns", type=int)
    resume.add_argument("--verify", choices=["pytest", "none"], default="none")
    resume.add_argument("--start", choices=["claude", "codex"])
    resume.add_argument("--allow-dirty", action="store_true")
    resume.add_argument("--output-format", choices=["text", "json"], default="text")
    resume.add_argument("--on-quota", choices=["halt", "solo", "wait"], default=None)
    resume.add_argument("--quota-wait-seconds", type=int, default=None)

    stop = sub.add_parser("stop", help="stop a running duet, claude, or codex session (asks which if ambiguous)")
    stop.add_argument("kind", nargs="?", choices=["duet", "claude", "codex"], help="what to stop (default: ask)")
    stop.add_argument("--repo", default=".", help="repo whose duet lock to check (default: current directory)")
    stop.add_argument("--force", action="store_true", help="SIGTERM instead of SIGINT")
    stop.add_argument("--yes", action="store_true", help="skip the confirmation prompt (required when not a TTY)")

    talk = sub.add_parser("talk", help="one turn with a single agent, resuming its newest session for the repo")
    talk.add_argument("agent", choices=["claude", "codex"])
    talk.add_argument("message", nargs="?", help="message to send (default: read stdin)")
    talk.add_argument("--repo", default=".", help="repo context (default: current directory)")
    talk.add_argument("--session", metavar="SESSION_ID", help="session to resume (default: newest for repo)")
    talk.add_argument("--new", action="store_true", help="start a fresh session instead of resuming")
    talk.add_argument("--fork-live", action="store_true", help="allow resuming a session that looks live")

    peek = sub.add_parser("peek", help="read-only tail of an agent session, safe while it is still running")
    peek.add_argument("agent", choices=["claude", "codex"])
    peek.add_argument("session_id", nargs="?", help="session to peek (default: most recently active)")
    peek.add_argument("--repo", default=".", help="repo the session belongs to (claude only)")
    peek.add_argument("--lines", type=int, default=30, help="number of recent events to show")

    replay = sub.add_parser("replay")
    replay.add_argument("transcript_json")

    init = sub.add_parser("init")
    scope = init.add_mutually_exclusive_group()
    scope.add_argument("--user", action="store_true")
    scope.add_argument("--project", action="store_true")

    args = parser.parse_args(argv)

    if args.version:
        print(f"duet {__version__}")
        return 0

    configure_logging(args.log_level, args.log_file)
    _install_signal_handlers()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    if args.command == "doctor":
        checks = run_doctor(config)
        print(format_checks(checks))
        return 1 if hard_failures(checks) else 0

    if args.command == "sessions":
        if args.agent == "codex":
            print(format_codex_sessions(list_codex_sessions(limit=args.limit)))
        else:
            repo = Path(args.repo)
            print(format_sessions(repo, list_claude_sessions(repo, limit=args.limit)))
        return 0

    if args.command == "status":
        print(format_activity(detect_activity(Path(args.repo))))
        return 0

    if args.command == "connect":
        return _connect(args, config)

    if args.command == "resume":
        return _resume(args, config)

    if args.command == "stop":
        return _stop(args)

    if args.command == "talk":
        return _talk(args, config)

    if args.command == "peek":
        return _peek(args)

    if args.command == "replay":
        print(Transcript.load_json(Path(args.transcript_json)).render_markdown(), end="")
        return 0

    if args.command == "init":
        target = _init_target(args)
        write_config(target)
        print(f"Wrote {target}")
        return 0

    if args.command in {"run", "exec"}:
        return _run_headless(args, config)

    return _run_interactive(args, config)


def _run_headless(args, config) -> int:
    checks = run_doctor(config)
    if args.output_format == "text":
        print(format_checks(checks))
    failures = hard_failures(checks)
    if failures:
        message = "\n".join(f"- {failure.name}: {failure.message}" for failure in failures)
        if args.output_format == "json":
            print(json.dumps({"outcome": "halted", "error": message}, indent=2))
        else:
            print(f"\nAborting: doctor found hard failures:\n{message}", file=sys.stderr)
        return 1

    available = available_agent_names(checks)
    agents = {name: agent for name, agent in config.agents.items() if name in available}

    try:
        _apply_attach(agents, getattr(args, "attach", None) or [])
    except ValueError as exc:
        if args.output_format == "json":
            print(json.dumps({"outcome": "halted", "error": str(exc)}, indent=2))
        else:
            print(f"Cannot attach: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "chain_sessions", False):
        for agent in agents.values():
            agent.chain_sessions = True

    live: LiveRepo | None = None
    try:
        if args.repo:
            live = prepare_live_repo(args.repo, branch=args.branch, allow_dirty=args.allow_dirty)
            workspace = live.workspace
        else:
            workspace = create_workspace(args.workspace)
    except WorkspaceError as exc:
        message = f"Cannot prepare workspace: {exc}"
        if args.output_format == "json":
            print(json.dumps({"outcome": "halted", "error": str(exc)}, indent=2))
        else:
            print(message, file=sys.stderr)
        return 1

    if live and args.output_format == "text":
        print(f"Live repo: {live.workspace} (branch {live.branch}, base {live.base_commit or 'no commits yet'})")

    success = False
    try:
        task = args.task or DEMO_TASK
        if args.seed_demo:
            seed_demo(workspace)
            task = DEMO_TASK if not args.task else args.task + "\n\n" + DEMO_TASK
        start = args.start or config.session.start_with
        if start not in agents:
            start = next(iter(agents))
        max_turns = args.max_turns or config.session.max_turns
        verifier = PytestVerifier() if args.verify == "pytest" or args.seed_demo else AlwaysUnknown()
        result = run_session(
            task=task,
            workspace=workspace,
            agents=agents,
            start_with=start,
            max_turns=max_turns,
            wallclock_seconds=config.session.wallclock_seconds,
            loop_threshold=config.session.loop_threshold,
            verifier=verifier,
            roles=_roles(start),
            on_turn=print if args.output_format == "text" else None,
            require_all_agents_for_success=len(agents) > 1,
            on_quota=getattr(args, "on_quota", None) or config.session.on_quota,
            quota_wait_seconds=getattr(args, "quota_wait_seconds", None) or config.session.quota_wait_seconds,
        )
        try:
            save_resume_state(
                workspace,
                ResumeState(
                    task=task,
                    outcome=result.outcome,
                    stop_condition=result.stop_condition,
                    mode="live" if live else "scratch",
                    workspace=str(workspace),
                    sessions={
                        name: (getattr(agent, "last_session_id", "") or getattr(agent, "session_id", ""))
                        for name, agent in agents.items()
                    },
                    branch=live.branch if live else "",
                ),
            )
        except OSError as exc:
            log.warning("could not save resume manifest: %s", exc)
        if args.output_format == "json":
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print("\n=== Duet summary ===")
            print(f"Outcome: {result.outcome}")
            print(f"Stop condition: {result.stop_condition}")
            for note in result.session.transcript.notes:
                print(f"Note: {note}")
            print(f"Workspace: {workspace}")
            if live:
                print(f"Branch: {live.branch} (review with `git -C {live.workspace} log {live.branch}` and merge deliberately)")
            print(f"Transcript JSON: {result.transcript_path}")
            print(f"Markdown log: {result.markdown_path}")
            for name, agent in agents.items():
                if getattr(agent, "session_id", ""):
                    print(f"Session ({name}): {agent.session_id} (re-attach with --attach {name}={agent.session_id})")
            if result.session.transcript.error:
                print(f"Error: {result.session.transcript.error}")
            print("\nGit log:")
            print(git_log_summary(workspace))
            if isinstance(verifier, PytestVerifier):
                final = verifier.verify(workspace)
                print("\nFinal pytest:")
                print(final.output)
        success = result.outcome == "success"
        return 0 if success else 2
    except KeyboardInterrupt:
        log.warning("interrupted by signal; shutting down")
        print("\nInterrupted; shutting down cleanly.", file=sys.stderr)
        return 130
    finally:
        if live and not success:
            if args.rollback_on_failure:
                rollback_live_repo(live)
                print(f"Rolled back: discarded branch {live.branch}.", file=sys.stderr)
            else:
                print(f"Duet branch left at {live.branch} for inspection.", file=sys.stderr)
        release_lock(workspace)


def _connect(args, config) -> int:
    """Resolve which existing sessions to resume, then delegate to the normal
    headless run with --attach specs synthesized from detection."""
    repo = Path(args.repo)
    activity = detect_activity(repo)
    attach: list[str] = []
    notes: list[str] = []
    for agent, explicit in (("claude", args.claude), ("codex", args.codex)):
        session_id, live = _resolve_connect_session(activity, agent, explicit)
        if session_id is None:
            notes.append(f"{agent}: no session found for this repo — cold start")
            continue
        if live and not args.fork_live:
            print(
                f"{agent} session {session_id} looks LIVE (transcript written in the last 3 minutes).\n"
                f"Resuming would fork its conversation state while it is mid-task.\n"
                f"Observe it instead with `duet peek {agent} {session_id}`, or pass --fork-live to connect anyway.",
                file=sys.stderr,
            )
            return 1
        state = "live, forked deliberately" if live else "idle"
        notes.append(f"{agent}: resuming session {session_id} ({state})")
        attach.append(f"{agent}={session_id}")
    if args.output_format == "text":
        for note in notes:
            print(f"Connect: {note}")
    run_args = argparse.Namespace(
        task=args.task,
        workspace=None,
        repo=args.repo,
        branch=args.branch,
        allow_dirty=args.allow_dirty,
        rollback_on_failure=args.rollback_on_failure,
        start=args.start,
        max_turns=args.max_turns,
        verify=args.verify,
        seed_demo=False,
        interactive_handoff=False,
        output_format=args.output_format,
        attach=attach,
        chain_sessions=True,
        on_quota=args.on_quota,
        quota_wait_seconds=args.quota_wait_seconds,
    )
    return _run_headless(run_args, config)


def _resolve_connect_session(activity, agent: str, explicit: str | None) -> tuple[str | None, bool]:
    if explicit:
        for detected in activity.candidates(agent):
            if detected.info.session_id == explicit:
                return explicit, detected.live
        # Trust an explicit id even if detection did not surface it.
        return explicit, False
    best = activity.best(agent)
    if best is None:
        return None, False
    return best.info.session_id, best.live


def _resume(args, config) -> int:
    workspace = Path(args.repo)
    try:
        state = load_resume_state(workspace)
    except ResumeError as exc:
        print(f"Cannot resume: {exc}", file=sys.stderr)
        return 1
    specs = state.attach_specs()
    print(f"Resuming duet in {workspace.resolve()} (previous outcome: {state.outcome}, {state.stop_condition or 'no stop condition'})")
    for spec in specs:
        print(f"  re-attaching {spec}")
    missing = [name for name, session_id in state.sessions.items() if not session_id]
    for name in missing:
        print(f"  {name}: no saved session id — cold start")

    if args.wait_ready is not None:
        interval = max(args.wait_ready, 30)
        wanted = set(state.sessions)
        while True:
            checks = run_doctor(config)
            ready = wanted & available_agent_names(checks)
            if ready == wanted:
                print("All agents ready; resuming now.", flush=True)
                break
            waiting_for = ", ".join(sorted(wanted - ready))
            # Flush so progress is visible even when output is redirected to a log.
            print(f"Not ready yet ({waiting_for}); retrying in {interval}s. Ctrl-C to abort.", flush=True)
            time.sleep(interval)

    task = args.task or (
        f"{state.task}\n\n[Duet: this is a resumed session. The previous run ended with "
        f"outcome={state.outcome} ({state.stop_condition or 'n/a'}). Review the workspace and your own "
        f"memory of prior turns, then continue from where the work stopped instead of starting over.]"
    )
    run_args = argparse.Namespace(
        task=task,
        workspace=None if state.mode == "live" else str(workspace),
        repo=str(workspace) if state.mode == "live" else None,
        branch=None,
        allow_dirty=args.allow_dirty,
        rollback_on_failure=False,
        start=args.start,
        max_turns=args.max_turns,
        verify=args.verify,
        seed_demo=False,
        interactive_handoff=False,
        output_format=args.output_format,
        attach=specs,
        chain_sessions=True,
        on_quota=args.on_quota,
        quota_wait_seconds=args.quota_wait_seconds,
    )
    return _run_headless(run_args, config)


def _stop(args) -> int:
    targets = running_targets(Path(args.repo))
    if args.kind:
        targets = [target for target in targets if target.kind == args.kind]
    if not targets:
        what = args.kind or "duet/claude/codex"
        print(f"Nothing to stop: no running {what} sessions found.", file=sys.stderr)
        return 1
    interactive = sys.stdin.isatty()
    if not interactive and not args.yes:
        print("Refusing to stop without confirmation: pass --yes (and a kind) when not on a TTY.", file=sys.stderr)
        return 1
    chosen: list = []
    if args.yes:
        chosen = targets
    else:
        print("Running sessions:")
        for index, target in enumerate(targets, start=1):
            note = "  <- may be the Claude Code session you are typing in" if target.kind == "claude" else ""
            print(f"  {index}. {target.describe()}{note}")
        answer = input("Stop which? (number, 'all', or q to abort): ").strip().lower()
        if answer in ("q", "quit", ""):
            print("Aborted; nothing stopped.")
            return 0
        if answer == "all":
            chosen = targets
        else:
            try:
                chosen = [targets[int(answer) - 1]]
            except (ValueError, IndexError):
                print(f"No such option: {answer!r}; nothing stopped.", file=sys.stderr)
                return 1
    for target in chosen:
        print(stop_target(target, force=args.force))
    return 0


def _talk(args, config) -> int:
    if args.agent not in config.agents:
        print(f"Agent {args.agent!r} is not configured.", file=sys.stderr)
        return 1
    agent = config.agents[args.agent]
    repo = Path(args.repo)
    if not args.new:
        session_id, live = _resolve_connect_session(detect_activity(repo), args.agent, args.session)
        if session_id is None:
            print(f"No {args.agent} session found for {repo.resolve()}; starting fresh (use --new to silence this).")
        elif live and not args.fork_live:
            print(
                f"{args.agent} session {session_id} looks LIVE. Peek instead (`duet peek {args.agent} {session_id}`) "
                f"or pass --fork-live to resume anyway.",
                file=sys.stderr,
            )
            return 1
        else:
            agent.session_id = session_id
    agent.chain_sessions = True
    message = args.message or sys.stdin.read()
    if not message.strip():
        print("Nothing to send.", file=sys.stderr)
        return 1
    try:
        result = agent.send(message, repo)
    except AgentError as exc:
        print(f"{args.agent}: {exc}", file=sys.stderr)
        return 1
    print(result.text)
    if agent.session_id:
        print(f"\n[session {agent.session_id} — continue with `duet talk {args.agent} --repo {args.repo}`]")
    return 0


def _peek(args) -> int:
    if args.agent == "codex":
        candidates = list_codex_sessions(limit=50)
    else:
        candidates = list_claude_sessions(Path(args.repo), limit=50)
    if args.session_id:
        candidates = [info for info in candidates if info.session_id == args.session_id]
    if not candidates:
        where = "" if args.agent == "codex" else f" for repo {Path(args.repo).resolve()}"
        target = f"session {args.session_id!r}" if args.session_id else "sessions"
        print(f"No {args.agent} {target} found{where}.", file=sys.stderr)
        return 1
    info = candidates[0]
    events = peek_session(info.path, args.agent, limit=args.lines)
    print(format_peek(info, args.agent, events))
    return 0


def _apply_attach(agents: dict, specs: list[str]) -> None:
    for spec in specs:
        name, separator, session_id = spec.partition("=")
        name, session_id = name.strip(), session_id.strip()
        if not separator or not name or not session_id:
            raise ValueError(f"--attach expects AGENT=SESSION_ID, got {spec!r}")
        if name not in agents:
            raise ValueError(f"unknown or unavailable agent {name!r} (available: {', '.join(agents) or 'none'})")
        agent = agents[name]
        if not getattr(agent, "resume_command", None):
            raise ValueError(f"agent {name!r} has no resume_command configured in duet.toml")
        agent.session_id = session_id
        agent.chain_sessions = True


def _install_signal_handlers() -> None:
    """Translate SIGTERM into KeyboardInterrupt so the same cleanup path that
    handles Ctrl-C (releasing locks, saving artifacts) also runs when a
    supervisor or `kill` stops the process."""

    def _raise_interrupt(signum, frame):
        raise KeyboardInterrupt()

    for sig in (signal.SIGTERM,):
        try:
            signal.signal(sig, _raise_interrupt)
        except (ValueError, OSError):
            pass  # not on the main thread, or unsupported platform


def _run_interactive(args, config) -> int:
    checks = run_doctor(config)
    print(format_checks(checks))
    failures = hard_failures(checks)
    if failures:
        print("\nAborting: doctor found hard failures.", file=sys.stderr)
        return 1
    available = available_agent_names(checks)
    repl = Repl(config, available, no_color=args.no_color)
    if args.seed_demo:
        seed_demo(repl.workspace)
    return repl.run()


def _init_target(args) -> Path:
    if args.user:
        import os

        return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "duet" / "config.toml"
    return Path.cwd() / "duet.toml"


def _roles(start: str) -> dict[str, str]:
    other = "codex" if start == "claude" else "claude"
    return {
        start: "You are the Implementer. Edit source files to implement the requested behavior, run tests when useful, then hand off.",
        other: "You are the Verifier. Add edge-case tests to test_roman.py and review the implementation for bugs, reporting issues back.",
    }


if __name__ == "__main__":
    raise SystemExit(main())
