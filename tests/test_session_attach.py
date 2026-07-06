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


from duet.sessions import format_codex_sessions, format_peek, list_codex_sessions, peek_session


def seed_codex_rollout(codex_home: Path, session_id: str, cwd: str) -> Path:
    day = codex_home / "sessions" / "2026" / "07" / "06"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-07-06T10-00-00-{session_id}.jsonl"
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": session_id, "cwd": cwd}}),
        json.dumps({"payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the auth gate"}]}}),
        json.dumps({"payload": {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"npm test"}'}}),
        json.dumps({"payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "tests pass, committing"}]}}),
        json.dumps({"payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>noise</environment_context>"}]}}),
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


class TestCodexSessions:
    def test_lists_with_cwd_and_preview(self, tmp_path):
        seed_codex_rollout(tmp_path, "019f3333-e461-7073-8c09-548fcdc7167d", "/work/repo")
        sessions = list_codex_sessions(codex_home=tmp_path)
        assert len(sessions) == 1
        info = sessions[0]
        assert info.session_id == "019f3333-e461-7073-8c09-548fcdc7167d"
        assert info.cwd == "/work/repo"
        assert info.preview == "fix the auth gate"
        assert "cwd=/work/repo" in format_codex_sessions(sessions)

    def test_missing_home_is_empty(self, tmp_path):
        assert list_codex_sessions(codex_home=tmp_path / "nope") == []


class TestPeek:
    def test_codex_peek_shows_messages_and_tools_skips_noise(self, tmp_path):
        path = seed_codex_rollout(tmp_path, "abc", "/work/repo")
        events = peek_session(path, "codex")
        assert events == [
            "[user] fix the auth gate",
            '[tool exec_command] {"cmd":"npm test"}',
            "[assistant] tests pass, committing",
        ]

    def test_claude_peek_parses_project_jsonl(self, tmp_path):
        path = tmp_path / "session.jsonl"
        lines = [
            json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "on it"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ]}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": "<system-reminder>skip me</system-reminder>"}}),
        ]
        path.write_text("\n".join(lines) + "\n")
        events = peek_session(path, "claude")
        assert events[0] == "[user] hello"
        assert events[1].startswith("[assistant] on it [tool Bash]")
        assert len(events) == 2

    def test_peek_limit_keeps_newest(self, tmp_path):
        path = seed_codex_rollout(tmp_path, "abc", "/work/repo")
        events = peek_session(path, "codex", limit=1)
        assert events == ["[assistant] tests pass, committing"]

    def test_format_peek_header(self, tmp_path):
        path = seed_codex_rollout(tmp_path, "abc", "/work/repo")
        info = list_codex_sessions(codex_home=tmp_path)[0]
        rendered = format_peek(info, "codex", peek_session(path, "codex"))
        assert "read-only peek" in rendered and "cwd=/work/repo" in rendered


class TestWorkspacePlaceholder:
    def test_inline_workspace_suppresses_appended_flag(self, tmp_path):
        agent = make_agent(
            session_id="abc",
            workspace_flag="-C",
            resume_command=["codex", "exec", "-C", "{workspace}", "resume", "{session_id}"],
        )
        cmd, _ = agent.build_command("hi", tmp_path)
        assert cmd[: 6] == ["codex", "exec", "-C", str(tmp_path), "resume", "abc"]
        assert cmd.count("-C") == 1

    def test_no_placeholder_still_appends_flag(self, tmp_path):
        agent = make_agent(workspace_flag="--add-dir")
        cmd, _ = agent.build_command("hi", tmp_path)
        assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path)
