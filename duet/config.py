from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import os
import shutil
import tomllib

from .adapters import CLIAgent


@dataclass
class SessionConfig:
    start_with: str = "claude"
    max_turns: int = 6
    wallclock_seconds: int = 900
    loop_threshold: float = 0.9


@dataclass
class DuetConfig:
    session: SessionConfig
    agents: dict[str, CLIAgent]


def default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "duet.toml"


def load_config(path: str | Path | None = None) -> DuetConfig:
    config_path = discover_config_path(path)
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    session_data = data.get("session", {})
    session = SessionConfig(
        start_with=session_data.get("start_with", "claude"),
        max_turns=int(session_data.get("max_turns", 6)),
        wallclock_seconds=int(session_data.get("wallclock_seconds", 900)),
        loop_threshold=float(session_data.get("loop_threshold", 0.9)),
    )
    agents = {}
    for name, item in data.get("agents", {}).items():
        agents[name] = CLIAgent(
            name=name,
            display_name=item.get("display_name", name.title()),
            command=list(item["command"]),
            prompt_via=item.get("prompt_via", "stdin"),
            workspace_flag=item.get("workspace_flag", ""),
            output_format=item.get("output_format", "text"),
            result_json_path=item.get("result_json_path", ""),
            session_json_path=item.get("session_json_path", ""),
            model=item.get("model", ""),
            timeout_seconds=int(item.get("timeout_seconds", 300)),
            stdin_sentinel=item.get("stdin_sentinel", "-"),
        )
    return DuetConfig(session=session, agents=agents)


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
