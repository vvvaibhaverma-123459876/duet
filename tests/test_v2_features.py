from __future__ import annotations

import io
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from duet.adapters import AgentError, AgentResult, CLIAgent
from duet.config import load_config
from duet.doctor import Check, available_agent_names, hard_failures
from duet.repl import Repl
from duet.workspace import WorkspaceError, create_workspace, release_lock


@dataclass
class MockAgent:
    name: str
    display_name: str
    reply: str

    def send(self, prompt: str, workspace: Path) -> AgentResult:
        (workspace / f"{self.name}.txt").write_text(self.reply, encoding="utf-8")
        return AgentResult(self.reply, 0, 0.01, self.reply, "")


def test_workspace_lock_blocks_second_session(tmp_path):
    workspace = create_workspace(str(tmp_path / "locked"))
    with pytest.raises(WorkspaceError):
        create_workspace(str(workspace))
    release_lock(workspace)
    workspace2 = create_workspace(str(workspace))
    release_lock(workspace2)


def test_repo_local_git_identity_set(tmp_path):
    workspace = create_workspace(str(tmp_path / "identity"))
    proc = subprocess.run(["git", "config", "--local", "user.email"], cwd=workspace, text=True, capture_output=True, check=True)
    assert proc.stdout.strip() == "duet@example.invalid"


def test_config_precedence_prefers_local_file(tmp_path, monkeypatch):
    (tmp_path / "duet.toml").write_text(
        """
[session]
start_with = "codex"
max_turns = 2
wallclock_seconds = 3
loop_threshold = 0.8

[agents.codex]
display_name = "Codex"
command = ["codex"]
prompt_via = "stdin"
workspace_flag = "-C"
output_format = "text"
timeout_seconds = 1
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert load_config().session.start_with == "codex"


def test_solo_mode_availability_and_no_hard_failure():
    checks = [
        Check("git", True, "ok"),
        Check("claude authenticated round-trip", True, "ok", agent="claude"),
        Check("codex authenticated round-trip", False, "missing", agent="codex"),
    ]
    assert available_agent_names(checks) == {"claude"}
    assert hard_failures(checks) == []


def test_repl_scripted_commands_and_task(tmp_path):
    config = load_config(Path(__file__).resolve().parents[1] / "duet.toml")
    config.agents = {"claude": MockAgent("claude", "Claude", "done [[DONE]]")}
    config.session.max_turns = 2
    stdin = io.StringIO("/status\n/handoff claude\nhello\n/log\n/diff\n/stop\n/quit\n")
    stdout = io.StringIO()
    repl = Repl(config, {"claude"}, workspace=create_workspace(str(tmp_path / "repl")), stdin=stdin, stdout=stdout)
    assert repl.run() == 0
    out = stdout.getvalue()
    assert "Next speaker forced" in out
    assert "Outcome: success" in out
    assert "Unknown command" not in out
    assert "Claude <claude@duet.local>" in out


def test_repl_unknown_command_hint(tmp_path):
    config = load_config(Path(__file__).resolve().parents[1] / "duet.toml")
    config.agents = {"claude": MockAgent("claude", "Claude", "done [[DONE]]")}
    stdout = io.StringIO()
    repl = Repl(config, {"claude"}, workspace=create_workspace(str(tmp_path / "repl2")), stdin=io.StringIO("/wat\n/quit\n"), stdout=stdout)
    repl.run()
    assert "Type /help" in stdout.getvalue()


def test_adapter_timeout_raises_agent_error(tmp_path):
    agent = CLIAgent(
        name="slow",
        display_name="Slow",
        command=[sys.executable, "-c", "import time; time.sleep(5)"],
        prompt_via="stdin",
        workspace_flag="",
        output_format="text",
        timeout_seconds=1,
    )
    with pytest.raises(AgentError, match="timed out"):
        agent.send("x", tmp_path)


def test_adapter_malformed_json_falls_back(tmp_path):
    agent = CLIAgent(
        name="jsonish",
        display_name="Jsonish",
        command=[sys.executable, "-c", "print('plain output')"],
        prompt_via="stdin",
        workspace_flag="",
        output_format="json",
        result_json_path="result",
        timeout_seconds=5,
    )
    result = agent.send("x", tmp_path)
    assert "plain output" in result.text
    assert "fell back to raw stdout" in result.text
