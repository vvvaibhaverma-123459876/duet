from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .adapters import DEFAULT_QUOTA_MARKERS, SESSION_ID_PLACEHOLDER, CLIAgent
from .providers.process import DEFAULT_MAX_STDERR, DEFAULT_MAX_STDOUT

VALID_PROMPT_VIA = {"stdin", "stdin-sentinel", "arg"}
VALID_OUTPUT_FORMAT = {"text", "text-last-line", "json"}
VALID_ISOLATE = {"none", "worktree", "snapshot"}
VALID_COMMIT_MODE = {"default", "agent-driven"}
DEFAULT_CONFIG_RESOURCE = "resources/default_config.toml"
DEFAULT_CONFIG_LABEL = "<built-in defaults>"


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
    isolate: str = "none"
    commit_mode: str = "default"


@dataclass
class DuetConfig:
    session: SessionConfig
    agents: dict[str, CLIAgent]
    source: str = ""


def default_config_path() -> Path:
    """Filesystem path of the packaged defaults. Package data ships inside the
    wheel, so this works for wheel, editable and source-tree installs alike."""
    return Path(str(resources.files("duet").joinpath(DEFAULT_CONFIG_RESOURCE)))


def default_config_text() -> str:
    return resources.files("duet").joinpath(DEFAULT_CONFIG_RESOURCE).read_text(encoding="utf-8")


def load_config(path: str | Path | None = None) -> DuetConfig:
    config_path = discover_config_path(path)
    try:
        if config_path is None:
            label, raw = DEFAULT_CONFIG_LABEL, default_config_text()
        else:
            label, raw = str(config_path), config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
    return parse_config(raw, label)


def parse_config(raw: str, label: str = "<string>") -> DuetConfig:
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {label}: {exc}") from exc

    session_data = data.get("session", {})
    if not isinstance(session_data, dict):
        raise ConfigError(f"[session] in {label} must be a table")
    where = f"[session] in {label}"
    session = SessionConfig(
        start_with=_string(session_data, "start_with", "claude", where),
        max_turns=_int(session_data, "max_turns", 6, where, minimum=1),
        wallclock_seconds=_int(session_data, "wallclock_seconds", 900, where, minimum=1),
        loop_threshold=_number(session_data, "loop_threshold", 0.9, where, positive=True),
        on_quota=_string(session_data, "on_quota", "halt", where),
        quota_wait_seconds=_int(session_data, "quota_wait_seconds", 300, where, minimum=0),
        budget_usd=_number(session_data, "budget_usd", 0.0, where),
        isolate=_string(session_data, "isolate", "none", where),
        commit_mode=_string(session_data, "commit_mode", "default", where),
    )
    if session.on_quota not in ("halt", "solo", "wait"):
        raise ConfigError(f"invalid [session] on_quota in {label}: must be halt, solo, or wait")
    if session.isolate not in VALID_ISOLATE:
        raise ConfigError(f"invalid [session] isolate in {label}: must be one of {', '.join(sorted(VALID_ISOLATE))}")
    if session.commit_mode not in VALID_COMMIT_MODE:
        raise ConfigError(
            f"invalid [session] commit_mode in {label}: must be one of {', '.join(sorted(VALID_COMMIT_MODE))}"
        )

    agents_data = data.get("agents", {})
    if not isinstance(agents_data, dict):
        raise ConfigError(f"[agents] in {label} must be a table of agent tables")
    agents = {}
    for name, item in agents_data.items():
        if not isinstance(item, dict):
            raise ConfigError(f"agent '{name}' in {label} must be a table")
        agents[name] = _build_agent(name, item, label)
    return DuetConfig(session=session, agents=agents, source=label)


def _string(table: dict, key: str, default: str, where: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"invalid {where}: {key} must be a string, got {value!r}")
    return value


def _int(table: dict, key: str, default: int, where: str, minimum: int | None = None) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"invalid {where}: {key} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"invalid {where}: {key} must be >= {minimum}, got {value}")
    return value


def _number(table: dict, key: str, default: float, where: str, positive: bool = False) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"invalid {where}: {key} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"invalid {where}: {key} must be finite, got {value!r}")
    if positive and number <= 0:
        raise ConfigError(f"invalid {where}: {key} must be > 0, got {value}")
    if not positive and number < 0:
        raise ConfigError(f"invalid {where}: {key} must be >= 0, got {value}")
    return number


def _build_agent(name: str, item: dict, label: str) -> CLIAgent:
    where = f"agent '{name}' in {label}"
    command = item.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise ConfigError(f"agent '{name}' in {label} needs a non-empty string-list 'command'")
    prompt_via = item.get("prompt_via", "stdin")
    if prompt_via not in VALID_PROMPT_VIA:
        raise ConfigError(f"agent '{name}': prompt_via must be one of {sorted(VALID_PROMPT_VIA)}, got {prompt_via!r}")
    output_format = item.get("output_format", "text")
    if output_format not in VALID_OUTPUT_FORMAT:
        raise ConfigError(f"agent '{name}': output_format must be one of {sorted(VALID_OUTPUT_FORMAT)}, got {output_format!r}")
    result_json_path = _string(item, "result_json_path", "", where)
    if output_format == "json" and not result_json_path:
        raise ConfigError(f"agent '{name}': output_format='json' requires 'result_json_path'")
    try:
        timeout_seconds = _int(item, "timeout_seconds", 300, where, minimum=1)
    except ConfigError as exc:
        raise ConfigError(f"agent '{name}': timeout_seconds must be a positive integer: {exc}") from exc
    max_output_bytes = _int(item, "max_output_bytes", DEFAULT_MAX_STDOUT, where, minimum=1024)
    max_stderr_bytes = _int(item, "max_stderr_bytes", DEFAULT_MAX_STDERR, where, minimum=1024)
    resume_command = item.get("resume_command", [])
    if resume_command:
        if not isinstance(resume_command, list) or not all(isinstance(part, str) for part in resume_command):
            raise ConfigError(f"agent '{name}': resume_command must be a string list")
        if not any(SESSION_ID_PLACEHOLDER in part for part in resume_command):
            raise ConfigError(f"agent '{name}': resume_command must contain the {SESSION_ID_PLACEHOLDER!r} placeholder")
    cost_json_scope = _string(item, "cost_json_scope", "call", where)
    if cost_json_scope not in ("call", "session_cumulative_on_resume"):
        raise ConfigError(f"agent '{name}': cost_json_scope must be 'call' or 'session_cumulative_on_resume'")
    quota_markers = item.get("quota_markers", [])
    if not isinstance(quota_markers, list) or not all(isinstance(m, str) and m for m in quota_markers):
        raise ConfigError(f"agent '{name}': quota_markers must be a list of non-empty strings")
    return CLIAgent(
        name=name,
        display_name=_string(item, "display_name", name.title(), where),
        command=list(command),
        prompt_via=prompt_via,
        workspace_flag=_string(item, "workspace_flag", "", where),
        output_format=output_format,
        result_json_path=result_json_path,
        session_json_path=_string(item, "session_json_path", "", where),
        model=_string(item, "model", "", where),
        timeout_seconds=timeout_seconds,
        stdin_sentinel=_string(item, "stdin_sentinel", "-", where),
        resume_command=list(resume_command),
        chain_sessions=bool(item.get("chain_sessions", False)),
        cost_json_path=_string(item, "cost_json_path", "", where),
        cost_json_scope=cost_json_scope,
        quota_markers=list(quota_markers) or list(DEFAULT_QUOTA_MARKERS),
        max_output_bytes=max_output_bytes,
        max_stderr_bytes=max_stderr_bytes,
    )


def resolve_option(cli_value, env_name: str, config_value, valid: set[str] | None = None):
    """CLI flag > environment > project config > default.

    Duet had no general environment tier before isolate/commit_mode; this keeps the
    established `cli or config` shape and slots env between them. An empty string is
    treated as unset so `DUET_ISOLATE=` does not shadow the config."""
    for source, value in (("--flag", cli_value), (env_name, os.environ.get(env_name))):
        if value in (None, ""):
            continue
        if valid and value not in valid:
            raise ConfigError(f"invalid {source} value {value!r}: must be one of {', '.join(sorted(valid))}")
        return value
    return config_value


def discover_config_path(path: str | Path | None = None) -> Path | None:
    """Explicit path, then ./duet.toml, then the user config, then None for the
    packaged defaults."""
    if path:
        return Path(path).expanduser()
    local = Path.cwd() / "duet.toml"
    if local.exists():
        return local
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "duet" / "config.toml"
    if xdg.exists():
        return xdg
    return None


def write_config(path: Path, force: bool = False) -> None:
    """Write the packaged defaults to `path`. Refuses to clobber an existing
    file unless `force`: a config the user edited is not Duet's to overwrite."""
    if path.exists() and not force:
        raise ConfigError(f"{path} already exists; pass --force to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_config_text(), encoding="utf-8")
