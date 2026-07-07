from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import os
import shutil
import tomllib

from .adapters import DEFAULT_QUOTA_MARKERS, SESSION_ID_PLACEHOLDER, CLIAgent

VALID_PROMPT_VIA = {"stdin", "stdin-sentinel", "arg"}
VALID_OUTPUT_FORMAT = {"text", "text-last-line", "json"}


class ConfigError(RuntimeError):
    """Raised when a Duet config file is missing or malformed."""


@dataclass
class SessionConfig:
    start_with: str = "claude"
    max_turns: int = 6
    wallclock_seconds: int = 900
    loop_threshold: float = 0.9
    on_quota: str = "halt"
    quota_wait_seconds: int = 300
    budget_usd: float = 0.0


@dataclass
class DuetConfig:
    session: SessionConfig
    agents: dict[str, CLIAgent]


def default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "duet.toml"


def load_config(path: str | Path | None = None) -> DuetConfig:
    config_path = discover_config_path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    session_data = data.get("session", {})
    try:
        session = SessionConfig(
            start_with=session_data.get("start_with", "claude"),
            max_turns=int(session_data.get("max_turns", 6)),
            wallclock_seconds=int(session_data.get("wallclock_seconds", 900)),
            loop_threshold=float(session_data.get("loop_threshold", 0.9)),
            on_quota=str(session_data.get("on_quota", "halt")),
            quota_wait_seconds=int(session_data.get("quota_wait_seconds", 300)),
            budget_usd=float(session_data.get("budget_usd", 0.0)),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid [session] values in {config_path}: {exc}") from exc
    if session.on_quota not in ("halt", "solo", "wait"):
        raise ConfigError(f"invalid [session] on_quota in {config_path}: must be halt, solo, or wait")

    agents = {}
    for name, item in data.get("agents", {}).items():
        agents[name] = _build_agent(name, item, config_path)
    return DuetConfig(session=session, agents=agents)


def _build_agent(name: str, item: dict, config_path: Path) -> CLIAgent:
    command = item.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise ConfigError(f"agent '{name}' in {config_path} needs a non-empty string-list 'command'")
    prompt_via = item.get("prompt_via", "stdin")
    if prompt_via not in VALID_PROMPT_VIA:
        raise ConfigError(f"agent '{name}': prompt_via must be one of {sorted(VALID_PROMPT_VIA)}, got {prompt_via!r}")
    output_format = item.get("output_format", "text")
    if output_format not in VALID_OUTPUT_FORMAT:
        raise ConfigError(f"agent '{name}': output_format must be one of {sorted(VALID_OUTPUT_FORMAT)}, got {output_format!r}")
    result_json_path = item.get("result_json_path", "")
    if output_format == "json" and not result_json_path:
        raise ConfigError(f"agent '{name}': output_format='json' requires 'result_json_path'")
    try:
        timeout_seconds = int(item.get("timeout_seconds", 300))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"agent '{name}': timeout_seconds must be an integer: {exc}") from exc
    if timeout_seconds <= 0:
        raise ConfigError(f"agent '{name}': timeout_seconds must be positive")
    resume_command = item.get("resume_command", [])
    if resume_command:
        if not isinstance(resume_command, list) or not all(isinstance(part, str) for part in resume_command):
            raise ConfigError(f"agent '{name}': resume_command must be a string list")
        if not any(SESSION_ID_PLACEHOLDER in part for part in resume_command):
            raise ConfigError(f"agent '{name}': resume_command must contain the {SESSION_ID_PLACEHOLDER!r} placeholder")
    return CLIAgent(
        name=name,
        display_name=item.get("display_name", name.title()),
        command=list(command),
        prompt_via=prompt_via,
        workspace_flag=item.get("workspace_flag", ""),
        output_format=output_format,
        result_json_path=result_json_path,
        session_json_path=item.get("session_json_path", ""),
        model=item.get("model", ""),
        timeout_seconds=timeout_seconds,
        stdin_sentinel=item.get("stdin_sentinel", "-"),
        resume_command=list(resume_command),
        chain_sessions=bool(item.get("chain_sessions", False)),
        cost_json_path=item.get("cost_json_path", ""),
        quota_markers=list(item.get("quota_markers", [])) or list(DEFAULT_QUOTA_MARKERS),
    )


def discover_config_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path).expanduser()
    local = Path.cwd() / "duet.toml"
    if local.exists():
        return local
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "duet" / "config.toml"
    if xdg.exists():
        return xdg
    return default_config_path()


def write_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(default_config_path(), path)
