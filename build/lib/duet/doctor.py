from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import DuetConfig


@dataclass
class Check:
    name: str
    ok: bool
    message: str
    hard: bool = True
    agent: str | None = None


def run_doctor(config: DuetConfig, live: bool = True) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("not running as root", os.geteuid() != 0 if hasattr(os, "geteuid") else True, "Claude bypass mode refuses root/sudo."))
    checks.append(Check("python >= 3.11", sys.version_info >= (3, 11), f"running {platform.python_version()}; install Python 3.11+"))
    checks.append(Check("platform", True, "Windows users should prefer WSL for consistent Claude/Codex sandbox behavior.", hard=False))
    checks.append(_path_check("git"))
    checks.append(_path_check("claude", agent="claude"))
    checks.append(_path_check("codex", agent="codex"))
    checks.append(_scratch_check())
    if live:
        if shutil.which("claude"):
            checks.append(_round_trip("claude", config, "Reply with exactly: CLAUDE_DOCTOR_OK", "CLAUDE_DOCTOR_OK"))
        if shutil.which("codex"):
            checks.append(_round_trip("codex", config, "Reply with exactly: CODEX_DOCTOR_OK", "CODEX_DOCTOR_OK"))
    checks.append(Check("cost warning", True, "Each turn calls a frontier model and draws from your plan usage window; keep max_turns low.", hard=False))
    return checks


def hard_failures(checks: list[Check]) -> list[Check]:
    failures = [check for check in checks if check.hard and not check.ok and check.agent is None]
    agent_checks = [check for check in checks if check.agent and "authenticated" in check.name]
    path_agent_checks = [check for check in checks if check.agent and "on PATH" in check.name]
    if agent_checks:
        if not any(check.ok for check in agent_checks):
            failures.extend([check for check in agent_checks if not check.ok])
    elif path_agent_checks and not any(check.ok for check in path_agent_checks):
        failures.extend([check for check in path_agent_checks if not check.ok])
    return failures


def available_agent_names(checks: list[Check]) -> set[str]:
    available = set()
    for check in checks:
        if check.agent and "authenticated" in check.name and check.ok:
            available.add(check.agent)
    if not available:
        for check in checks:
            if check.agent and "on PATH" in check.name and check.ok:
                available.add(check.agent)
    return available


def format_checks(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        status = "PASS" if check.ok else "FAIL"
        lines.append(f"[{status}] {check.name}: {check.message}")
    return "\n".join(lines)


def _path_check(binary: str, agent: str | None = None) -> Check:
    path = shutil.which(binary)
    hint = path or f"{binary} was not found on PATH; install it and ensure cron/non-login shells inherit PATH"
    return Check(f"{binary} on PATH", bool(path), hint, agent=agent)


def _scratch_check() -> Check:
    try:
        with tempfile.TemporaryDirectory(prefix="duet-doctor-") as tmp:
            Path(tmp, "probe.txt").write_text("ok", encoding="utf-8")
        return Check("scratch workspace writable", True, "temporary workspace is writable")
    except OSError as exc:
        return Check("scratch workspace writable", False, str(exc))


def _round_trip(agent_name: str, config: DuetConfig, prompt: str, expected: str) -> Check:
    try:
        with tempfile.TemporaryDirectory(prefix=f"duet-{agent_name}-doctor-") as tmp:
            workspace = Path(tmp)
            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            result = config.agents[agent_name].send(prompt, workspace)
        ok = expected in result.text
        hint = result.text if ok else f"unexpected response: {result.text[:300]}; run `{agent_name} login` or the CLI's auth command"
        return Check(f"{agent_name} authenticated round-trip", ok, hint, agent=agent_name)
    except Exception as exc:
        return Check(f"{agent_name} authenticated round-trip", False, f"{exc}; run `{agent_name} login` or the CLI's auth command", agent=agent_name)
