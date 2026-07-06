from __future__ import annotations

import json
from pathlib import Path

import pytest

from duet.adapters import CLIAgent
from duet.cli import _apply_attach
from duet.config import ConfigError, load_config
from duet.sessions import claude_project_dir, format_sessions, list_claude_sessions


def make_agent(**overrides) -> CLIAgent:
    defaults = dict(
        name="claude",
        display_name="Claude",
        command=["claude", "-p", "--output-format", "json"],
        prompt_via="stdin",
        workspace_flag="--add-dir",
        output_format="json",
        timeout_seconds=30,
        result_json_path="result",
        session_json_path="session_id",
        resume_command=["claude", "-p", "--output-format", "json", "--resume", "{session_id}"],
    )
    defaults.update(overrides)
    return CLIAgent(**defaults)


class TestBuildCommand:
    def test_no_session_uses_base_command(self, tmp_path):
        agent = make_agent()
        cmd, _ = agent.build_command("hi", tmp_path)
        assert cmd[: 4] == ["claude", "-p", "--output-format", "json"]
        assert "--resume" not in cmd

    def test_session_id_substituted_into_resume_command(self, tmp_path):
        agent = make_agent(session_id="abc-123")
        cmd, _ = agent.build_command("hi", tmp_path)
        assert cmd[cmd.index("--resume") + 1] == "abc-123"

    def test_session_id_without_resume_command_falls_back(self, tmp_path):
        agent = make_agent(session_id="abc-123", resume_command=[])
        cmd, _ = agent.build_command("hi", tmp_path)
        assert "--resume" not in cmd


class TestSessionChaining:
    def _fake_claude(self, tmp_path: Path, session_id: str) -> Path:
        script = tmp_path / "fake_claude"
        payload = json.dumps({"result": "done", "session_id": session_id})
        script.write_text(f"#!/bin/sh\ncat > /dev/null\necho '{payload}'\n")
        script.chmod(0o755)
        return script

    def test_chain_adopts_returned_session_id(self, tmp_path):
        script = self._fake_claude(tmp_path, "next-session")
        agent = make_agent(command=[str(script)], resume_command=[str(script), "{session_id}"], chain_sessions=True, workspace_flag="")
        result = agent.send("hello", tmp_path)
        assert result.session_id == "next-session"
        assert agent.session_id == "next-session"

    def test_without_chain_session_id_not_adopted(self, tmp_path):
        script = self._fake_claude(tmp_path, "next-session")
        agent = make_agent(command=[str(script)], chain_sessions=False, workspace_flag="")
        agent.send("hello", tmp_path)
        assert agent.session_id == ""


class TestApplyAttach:
    def test_sets_session_and_enables_chaining(self):
        agent = make_agent()
        _apply_attach({"claude": agent}, ["claude=abc-123"])
        assert agent.session_id == "abc-123"
        assert agent.chain_sessions is True

    def test_rejects_bad_spec(self):
        with pytest.raises(ValueError, match="AGENT=SESSION_ID"):
            _apply_attach({"claude": make_agent()}, ["abc-123"])

    def test_rejects_unknown_agent(self):
        with pytest.raises(ValueError, match="unknown or unavailable"):
            _apply_attach({"claude": make_agent()}, ["codex=abc"])

    def test_rejects_agent_without_resume_command(self):
        agent = make_agent(resume_command=[])
        with pytest.raises(ValueError, match="no resume_command"):
            _apply_attach({"claude": agent}, ["claude=abc"])


class TestConfig:
    def test_default_config_has_resume_commands(self):
        config = load_config()
        assert "{session_id}" in " ".join(config.agents["claude"].resume_command)
        assert "{session_id}" in " ".join(config.agents["codex"].resume_command)

    def test_resume_command_requires_placeholder(self, tmp_path):
        config = tmp_path / "duet.toml"
        config.write_text(
            "[agents.claude]\n"
            'command = ["claude"]\n'
            'resume_command = ["claude", "--resume"]\n'
        )
        with pytest.raises(ConfigError, match="placeholder"):
            load_config(config)


class TestSessionListing:
    def _seed_session(self, home: Path, repo: Path, session_id: str, user_text: str) -> None:
        project = claude_project_dir(repo, claude_home=home)
        project.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"type": "summary", "summary": "x"}),
            json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": user_text}]}}),
        ]
        (project / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n")

    def test_lists_sessions_with_preview(self, tmp_path):
        home, repo = tmp_path / "claude-home", tmp_path / "repo"
        repo.mkdir()
        self._seed_session(home, repo, "aaa-111", "fix the login bug")
        sessions = list_claude_sessions(repo, claude_home=home)
        assert [s.session_id for s in sessions] == ["aaa-111"]
        assert sessions[0].preview == "fix the login bug"
        rendered = format_sessions(repo, sessions)
        assert "aaa-111" in rendered and "fix the login bug" in rendered

    def test_missing_project_dir_is_empty(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        assert list_claude_sessions(repo, claude_home=tmp_path / "claude-home") == []
