"""v2 commands: `duet pair`, `duet mcp serve`, `duet service ...`, and
`duet status --run` / `duet stop --run` (D05).

These talk to the local runtime service; the legacy commands are untouched.
Exit codes follow the D01 table: 0 verified success, 2 halted/failed,
130 interrupted or cancelled by the user, 1 usage or setup errors."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from .runtime.contracts import TERMINAL_RUN, DomainError, RunLifecycle

V2_COMMANDS = {"pair", "mcp", "service", "usage"}


def add_parsers(sub) -> None:
    pair = sub.add_parser("pair", help="start a managed Claude-Codex pair on a task (DUET launches both sessions)")
    pair.add_argument("task", help="the objective")
    pair.add_argument("--repo", default=".", help="repository to work on (default: current directory)")
    pair.add_argument("--check", action="append", required=True, metavar="CMD", help="command that verifies the task (repeatable; run without a shell)")
    pair.add_argument("--protect", action="append", default=None, metavar="GLOB", help="paths the change must not modify (e.g. tests/*)")
    pair.add_argument("--writer", choices=["claude", "codex"], default="claude", help="which session edits the code (default: claude)")
    pair.add_argument("--no-wait", action="store_true", help="start the pair and return the run id")
    pair.add_argument("--json", action="store_true", help="print the final status as JSON")
    pair.add_argument("--state-root", default=None, help=argparse.SUPPRESS)

    mcp = sub.add_parser("mcp", help="MCP integration for native Claude/Codex sessions")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    serve = mcp_sub.add_parser("serve", help="run the DUET MCP server on stdio (configure this in Claude Code or Codex)")
    serve.add_argument("--state-root", default=None, help=argparse.SUPPRESS)
    serve.add_argument("--token-file", default=None, help=argparse.SUPPRESS)

    service = sub.add_parser("service", help="the local DUET runtime service")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    run = service_sub.add_parser("run", help="run the service in the foreground")
    run.add_argument("--state-root", default=None, help=argparse.SUPPRESS)
    run.add_argument("--idle-exit", type=float, default=900.0, metavar="SECONDS", help="exit after this long idle with no live runs (0: never)")
    for name, text in (("status", "show whether the service runs and its recent runs"), ("stop", "stop the service and its managed peers")):
        cmd = service_sub.add_parser(name, help=text)
        cmd.add_argument("--state-root", default=None, help=argparse.SUPPRESS)


def add_usage_parser(sub) -> None:
    usage = sub.add_parser("usage", help="local usage pools and recorded usage (v2)")
    usage.add_argument("--run", default=None, metavar="RUN_ID", help="also show the usage recorded for this run")
    usage.add_argument("--json", action="store_true", help="versioned JSON output")
    usage.add_argument("--state-root", default=None, help=argparse.SUPPRESS)
    usage_sub = usage.add_subparsers(dest="usage_command")
    pool = usage_sub.add_parser("pool", help="define usage pools")
    pool_sub = pool.add_subparsers(dest="pool_command", required=True)
    set_ = pool_sub.add_parser("set", help="define or change a pool (you authorise the allowance; agents cannot)")
    set_.add_argument("pool_id")
    set_.add_argument("--provider", required=True, choices=["claude", "codex"])
    set_.add_argument("--metric", required=True, choices=["turns", "cost.estimated_usd"], help="what the pool counts")
    set_.add_argument("--allowance", default=None, help="the allowance (an integer, or a decimal for cost); omit for tracking only")
    set_.add_argument("--window-seconds", type=int, default=None, help="rolling window; omit for the lifetime of the pool")
    set_.add_argument("--enforcement", choices=["local_bound", "best_effort"], default="local_bound")
    set_.add_argument("--state-root", default=None, help=argparse.SUPPRESS)


def extend_legacy(status_parser, stop_parser) -> None:
    status_parser.add_argument("--run", default=None, metavar="RUN_ID", help="show a v2 pair run instead of detecting sessions")
    status_parser.add_argument("--json", action="store_true", help="with --run: versioned JSON output")
    status_parser.add_argument("--state-root", default=None, help=argparse.SUPPRESS)
    stop_parser.add_argument("--run", default=None, metavar="RUN_ID", help="cancel a v2 pair run and stop its managed peers")
    stop_parser.add_argument("--state-root", default=None, help=argparse.SUPPRESS)


def handles(args) -> bool:
    return args.command in V2_COMMANDS or (args.command in ("status", "stop") and getattr(args, "run", None))


def dispatch(args) -> int:
    try:
        if args.command == "pair":
            return _pair(args)
        if args.command == "mcp":
            from .integrations.mcp_server import serve

            return serve(args.state_root, args.token_file)
        if args.command == "service":
            return _service(args)
        if args.command == "usage":
            return _usage(args)
        if args.command == "status":
            return _run_status(args)
        if args.command == "stop":
            return _run_stop(args)
    except DomainError as exc:
        print(f"duet: {exc.message}", file=sys.stderr)
        return 1
    raise SystemExit(f"unhandled command {args.command}")


# --- helpers ------------------------------------------------------------------------------


def _paths(args):
    from .runtime.service import ServicePaths

    return ServicePaths.for_root(Path(args.state_root) if getattr(args, "state_root", None) else None)


def _local_coordinator(paths):
    """Read or cancel a run without a running service (no peers attached)."""
    from .runtime.api import Runtime
    from .runtime.artifacts import ArtifactStore
    from .runtime.pairing import PairCoordinator
    from .runtime.store import Store

    return PairCoordinator(Runtime(Store(paths.db)), ArtifactStore(paths.artifacts), state_root=paths.pairs)


def _controller(paths):
    from .runtime.service import ServiceClient

    return ServiceClient.as_controller(paths)


def format_status(status: dict) -> str:
    lines = [
        f"Run {status['run_id']}: {status['lifecycle']} ({status['collaboration']})",
        f"  objective: {status['objective']}",
        f"  repo: {status['repo']}",
        f"  workspace: {status['workspace']} (branch {status['branch']})",
        "  participants:",
    ]
    for part in status["participants"]:
        caps = part["capabilities"]
        lines.append(
            f"    {part['provider']:<6} {part['role']:<8} origin={part['origin']} liveness={part['liveness']} "
            f"receive={caps['receive']} identity={caps['native_identity']} containment={caps['containment']}"
        )
    for task in status["tasks"]:
        owner = next((p["provider"] for p in status["participants"] if p["participant_id"] == task["owner"]), None)
        lines.append(
            f"  task {task['task_id']} [{task.get('kind', 'code')}]: {task['state']}{' (required)' if task['required'] else ''}"
            + (f" owner={owner}" if owner else "") + f" - {task['description'][:70]}"
        )
    for plan in status.get("plans", []):
        lines.append(f"  plan {plan['plan_id']}: {plan['state']} ({plan['tasks']} task(s))")
    if status.get("contributions"):
        by_provider: dict[str, list[str]] = {}
        for c in status["contributions"]:
            by_provider.setdefault(c["provider"], []).append(c["kind"])
        lines.append("  contributions (from evidence): " + "; ".join(f"{p}: {', '.join(sorted(set(k)))}" for p, k in sorted(by_provider.items())))
    for item in status.get("interventions", []):
        lines.append(f"  intervention {item['status']}: {item['reason'][:120]}")
    ver = status["verification"]
    if ver.get("snapshot_id"):
        lines.append(f"  latest snapshot {ver['snapshot_id']}: changed {', '.join(ver['changed']) or '(none)'}")
        for check_id, record in ver["checks"].items():
            lines.append(f"    check {check_id}: {record['status']}")
        if ver.get("checks_running"):
            lines.append("    checks: running")
        for review in ver["reviews"]:
            lines.append(f"    review by {review['reviewer_provider']}: {review['disposition']}")
        completion = ver["completion"]
        lines.append(f"  completion: {completion['outcome']}")
        for item in completion["items"]:
            if not item["ok"]:
                lines.append(f"    missing {item['name']}: {item['detail']}")
    else:
        lines.append("  no snapshot submitted yet")
    if status["open_requests"]:
        lines.append(f"  open requests: {len(status['open_requests'])}")
    lines.append(f"  profile: {status['profile']}")
    return "\n".join(lines)


def _exit_for(lifecycle: str) -> int:
    if lifecycle == RunLifecycle.COMPLETED_VERIFIED.value:
        return 0
    if lifecycle == RunLifecycle.CANCELLED.value:
        return 130
    return 2


# --- commands -----------------------------------------------------------------------------


def _pair(args) -> int:
    from .runtime.service import ensure_service

    if os.environ.get("DUET_MANAGED_PEER") == "1":
        print("duet pair is refused inside a DUET-managed session: a peer cannot start another pair.", file=sys.stderr)
        return 1
    paths = _paths(args)
    ensure_service(paths)
    client = _controller(paths)
    status = client.call(
        "pair", rpc_timeout=300.0, objective=args.task, repo=str(Path(args.repo).resolve()), checks=args.check,
        protected=args.protect, writer=args.writer,
    )
    run_id = status["run_id"]
    print(f"Started pair run {run_id}: both sessions are DUET-managed (this does not test native-origin pairing).", file=sys.stderr)
    if args.no_wait:
        print(json.dumps(status, indent=2) if args.json else run_id)
        return 0
    last = None
    try:
        while True:
            status = client.call("run_status", run_id=run_id)
            summary = (status["lifecycle"], status["collaboration"], json.dumps(status["verification"].get("checks", {}), sort_keys=True))
            if summary != last:
                print(f"[{time.strftime('%H:%M:%S')}] {status['lifecycle']} / {status['collaboration']}", file=sys.stderr)
                last = summary
            if RunLifecycle(status["lifecycle"]) in TERMINAL_RUN:
                break
            time.sleep(2.0)
    except KeyboardInterrupt:
        client.call("cancel", run_id=run_id, reason="interrupted from the terminal")
        status = client.call("run_status", run_id=run_id)
    print(json.dumps(status, indent=2) if args.json else format_status(status))
    return _exit_for(status["lifecycle"])


def _service(args) -> int:
    from .runtime.peers import default_peer_factory
    from .runtime.service import RuntimeService, ServiceClient, ServiceRunning, service_running

    paths = _paths(args)
    if args.service_command == "run":
        if os.environ.get("DUET_MANAGED_PEER") == "1":
            print("refusing to start a DUET service inside a DUET-managed session", file=sys.stderr)
            return 1
        try:
            service = RuntimeService(paths, peer_factory=default_peer_factory, idle_exit_seconds=args.idle_exit or None)
        except ServiceRunning as exc:
            print(exc.message, file=sys.stderr)
            return 1
        signal.signal(signal.SIGTERM, lambda *_: service.request_stop())
        try:
            service.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    if args.service_command == "status":
        if not service_running(paths):
            print(f"not running (state: {paths.root})")
            return 3
        info = ServiceClient(paths).ping()
        print(f"running: pid {info['pid']}, version {info['version']}, state {info['root']}")
        for run in _controller(paths).call("runs"):
            print(f"  {run['run_id']}  {run['lifecycle']:<18} {run['collaboration']:<16} {run['objective'][:60]}")
        return 0
    if args.service_command == "stop":
        if not service_running(paths):
            print("not running")
            return 0
        _controller(paths).call("shutdown")
        print("stopping")
        return 0
    raise SystemExit(f"unknown service command {args.service_command}")


def _run_status(args) -> int:
    from .runtime.service import service_running

    paths = _paths(args)
    if service_running(paths):
        status = _controller(paths).call("run_status", run_id=args.run)
    else:
        status = _local_coordinator(paths).run_status(args.run)
    print(json.dumps(status, indent=2) if args.json else format_status(status))
    return 0


def _run_stop(args) -> int:
    from .runtime.service import service_running

    paths = _paths(args)
    if service_running(paths):
        _controller(paths).call("cancel", run_id=args.run, reason="stopped with duet stop")
    else:
        _local_coordinator(paths).cancel(args.run, reason="stopped with duet stop (service not running)")
    print(f"cancelled {args.run}")
    return 0


def _usage(args) -> int:
    from .runtime.api import Runtime
    from .runtime.contracts import USER
    from .runtime.pools import PoolStore
    from .runtime.store import Store

    paths = _paths(args)
    store = PoolStore(Runtime(Store(paths.db)))
    if getattr(args, "usage_command", None) == "pool":
        unit = "turns" if args.metric == "turns" else "USD"
        status = store.define_pool(
            USER, args.pool_id, provider=args.provider, metric=args.metric, unit=unit,
            allowance=args.allowance, window_seconds=args.window_seconds, enforcement=args.enforcement,
        )
        print(json.dumps(status, indent=2) if args.json else _format_pool(status))
        return 0
    report = {"schema": "duet.usage/1", "pools": store.status(), "run": None}
    if args.run:
        report["run"] = {"run_id": args.run, "records": store.run_usage(args.run)}
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    if not report["pools"]:
        print("no usage pools defined (DUET tracks provider turns and cost only against pools you define)")
    for pool in report["pools"]:
        print(_format_pool(pool))
    if report["run"] is not None:
        records = report["run"]["records"]
        print(f"run {args.run}: {len(records)} usage record(s)")
        for record in records:
            quantity = record["quantity"] if record["quantity"] is not None else "unknown"
            print(f"  {record['observed_at']}  {record['pool_id']:<24} {record['metric']:<20} {quantity} ({record['quality']})")
    return 0


def _format_pool(pool: dict) -> str:
    window = f" per {pool['window_seconds']}s" if pool["window_seconds"] else ""
    allowance = pool["allowance"] if pool["allowance"] is not None else "untracked limit"
    line = (
        f"{pool['pool_id']}: {pool['provider']} {pool['metric']} used {pool['used']}, reserved {pool['held']}, "
        f"allowance {allowance}{window} [{pool['enforcement']}]"
    )
    if pool["available"] is not None:
        line += f", available {pool['available']}"
    if pool["uncertain"]:
        line += f" (uncertain: {pool['unknown_records']} record(s) of unknown size)"
    return line
