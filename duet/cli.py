from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .broker import run_session
from .config import load_config, write_config
from .doctor import available_agent_names, format_checks, hard_failures, run_doctor
from .repl import Repl
from .transcript import Transcript
from .verifiers import AlwaysUnknown, PytestVerifier
from .workspace import create_workspace, git_log_summary, release_lock, seed_demo


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
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor")

    for name in ("run", "exec"):
        run = sub.add_parser(name)
        run.add_argument("task", nargs="?")
        run.add_argument("--workspace")
        run.add_argument("--start", choices=["claude", "codex"])
        run.add_argument("--max-turns", type=int)
        run.add_argument("--verify", choices=["pytest", "none"], default="none")
        run.add_argument("--seed-demo", action="store_true")
        run.add_argument("--interactive-handoff", action="store_true")
        run.add_argument("--output-format", choices=["text", "json"], default="text")

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

    config = load_config(args.config)

    if args.command == "doctor":
        checks = run_doctor(config)
        print(format_checks(checks))
        return 1 if hard_failures(checks) else 0

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
    workspace = create_workspace(args.workspace)
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
        )
        if args.output_format == "json":
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print("\n=== Duet summary ===")
            print(f"Outcome: {result.outcome}")
            print(f"Stop condition: {result.stop_condition}")
            print(f"Workspace: {workspace}")
            print(f"Transcript JSON: {result.transcript_path}")
            print(f"Markdown log: {result.markdown_path}")
            if result.session.transcript.error:
                print(f"Error: {result.session.transcript.error}")
            print("\nGit log:")
            print(git_log_summary(workspace))
            if isinstance(verifier, PytestVerifier):
                final = verifier.verify(workspace)
                print("\nFinal pytest:")
                print(final.output)
        return 0 if result.outcome == "success" else 2
    finally:
        release_lock(workspace)


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
