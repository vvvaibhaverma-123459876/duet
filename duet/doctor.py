from __future__ import annotations

import dataclasses
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import DuetConfig

# Flags that put an agent CLI into a permission-bypass mode. Claude Code
# refuses its bypass mode when run as root, so such an agent cannot work there.
BYPASS_FLAGS = ("--dangerously-skip-permissions",)
BYPASS_PERMISSION_MODES = ("bypassPermissions",)


@dataclass
class Check:
    name: str
    ok: bool
    message: str
    hard: bool = True
    agent: str | None = None


def run_doctor(config: DuetConfig, live: bool = True) -> list[Check]:
    checks: list[Check] = []
    root = _is_root()
    checks.append(
        Check(
            "running as root",
            True,
            "yes: agents configured with a permission-bypass flag are disabled unless the CLI allows it"
            if root
            else "no",
            hard=False,
        )
    )
    checks.append(Check("python >= 3.11", sys.version_info >= (3, 11), f"running {platform.python_version()}; install Python 3.11+"))
    checks.append(Check("platform", True, "Windows users should prefer WSL for consistent Claude/Codex sandbox behavior.", hard=False))
    checks.append(_path_check("git"))
    checks.append(_scratch_check())
    for name, agent in config.agents.items():
        binary = agent.command[0] if agent.command else name
        path_check = _path_check(binary, agent=name, label=f"{name} on PATH")
        checks.append(path_check)
        blocked = _root_block(name, agent) if root else None
        if blocked:
            checks.append(blocked)
        if live and path_check.ok and not blocked:
            checks.append(_round_trip(name, config, f"Reply with exactly: {name.upper()}_DOCTOR_OK", f"{name.upper()}_DOCTOR_OK"))
    checks.append(Check("cost warning", True, "Each turn calls a frontier model and draws from your plan usage window; keep max_turns low.", hard=False))
    return checks


def available_agent_names(checks: list[Check]) -> set[str]:
    """An agent is available when none of its own hard checks failed."""
    by_agent: dict[str, list[Check]] = {}
    for check in checks:
        if check.agent:
            by_agent.setdefault(check.agent, []).append(check)
    return {agent for agent, own in by_agent.items() if not any(c.hard and not c.ok for c in own)}


def hard_failures(checks: list[Check]) -> list[Check]:
    """Global hard failures always block. Agent failures block only when no
    agent at all is usable; one working agent means solo mode."""
    failures = [check for check in checks if check.hard and not check.ok and check.agent is None]
    if not available_agent_names(checks):
        failures.extend(check for check in checks if check.agent and check.hard and not check.ok)
    return failures


def format_checks(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        status = "PASS" if check.ok else ("FAIL" if check.hard else "WARN")
        lines.append(f"[{status}] {check.name}: {check.message}")
    return "\n".join(lines)


def uses_permission_bypass(command: list[str]) -> bool:
    for index, part in enumerate(command):
        if part in BYPASS_FLAGS:
            return True
        if part == "--permission-mode" and index + 1 < len(command) and command[index + 1] in BYPASS_PERMISSION_MODES:
            return True
        if part.startswith("--permission-mode=") and part.split("=", 1)[1] in BYPASS_PERMISSION_MODES:
            return True
    return False


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _root_block(name: str, agent) -> Check | None:
    commands = [agent.command, getattr(agent, "resume_command", []) or []]
    if not any(uses_permission_bypass(cmd) for cmd in commands):
        return None
    if os.environ.get("IS_SANDBOX") == "1":
        return Check(
            f"{name} bypass mode as root",
            True,
            "IS_SANDBOX=1 is set; the CLI's own root check decides whether bypass mode runs",
            hard=False,
            agent=name,
        )
    return Check(
        f"{name} bypass mode as root",
        False,
        "this agent's command uses a permission-bypass flag, which the CLI refuses as root; "
        "run Duet as a non-root user or configure a non-bypass permission mode",
        agent=name,
    )


def _path_check(binary: str, agent: str | None = None, label: str | None = None) -> Check:
    path = shutil.which(binary)
    hint = path or f"{binary} was not found on PATH; install it and ensure cron/non-login shells inherit PATH"
    return Check(label or f"{binary} on PATH", bool(path), hint, agent=agent)


def _scratch_check() -> Check:
    try:
        with tempfile.TemporaryDirectory(prefix="duet-doctor-") as tmp:
            Path(tmp, "probe.txt").write_text("ok", encoding="utf-8")
        return Check("scratch workspace writable", True, "temporary workspace is writable")
    except OSError as exc:
        return Check("scratch workspace writable", False, str(exc))


def _round_trip(agent_name: str, config: DuetConfig, prompt: str, expected: str) -> Check:
    # Probe with a copy so the probe's throwaway session id, chaining state and
    # deadline never leak into the agent the session will actually use.
    probe = dataclasses.replace(
        config.agents[agent_name], session_id="", last_session_id="", chain_sessions=False, deadline=None, _session_cost_seen={}
    )
    try:
        with tempfile.TemporaryDirectory(prefix=f"duet-{agent_name}-doctor-") as tmp:
            workspace = Path(tmp)
            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            result = probe.send(prompt, workspace)
        ok = expected in result.text
        hint = result.text if ok else f"unexpected response: {result.text[:300]}; run `{agent_name} login` or the CLI's auth command"
        return Check(f"{agent_name} authenticated round-trip", ok, hint, agent=agent_name)
    except Exception as exc:
        return Check(f"{agent_name} authenticated round-trip", False, f"{exc}; run `{agent_name} login` or the CLI's auth command", agent=agent_name)
