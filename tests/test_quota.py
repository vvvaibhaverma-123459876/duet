from __future__ import annotations

import time
from pathlib import Path

import pytest

from duet.adapters import AgentResult, QuotaError
from duet.broker import run_session
from duet.config import ConfigError, load_config
from duet.verifiers import AlwaysUnknown


class FakeAgent:
    def __init__(self, name: str, replies: list[object]):
        self.name = name
        self.display_name = name.title()
        self.replies = list(replies)
        self.calls = 0

    def send(self, prompt: str, workspace: Path) -> AgentResult:
        self.calls += 1
        reply = self.replies.pop(0) if self.replies else "[[DONE]] ok"
        if isinstance(reply, Exception):
            raise reply
        return AgentResult(text=reply, exit_code=0, duration_s=0.01, raw_stdout=reply, raw_stderr="")


def run(workspace, agents, on_quota="halt", quota_wait_seconds=1, wallclock=60, max_turns=6):
    return run_session(
        task="t",
        workspace=workspace,
        agents=agents,
        start_with=next(iter(agents)),
        max_turns=max_turns,
        wallclock_seconds=wallclock,
        loop_threshold=0.99,
        verifier=AlwaysUnknown(),
        on_quota=on_quota,
        quota_wait_seconds=quota_wait_seconds,
    )


def repo(tmp_path: Path) -> Path:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


class TestQuotaHalt:
    def test_halts_with_named_stop_condition(self, tmp_path):
        ws = repo(tmp_path)
        agents = {
            "claude": FakeAgent("claude", ["working on it"]),
            "codex": FakeAgent("codex", [QuotaError("codex: usage limit")]),
        }
        result = run(ws, agents, on_quota="halt")
        assert result.outcome == "halted"
        assert result.stop_condition == "QuotaExhausted(codex)"
        assert "usage limit" in result.session.transcript.error


class TestQuotaSolo:
    def test_drops_agent_and_survivor_finishes(self, tmp_path):
        ws = repo(tmp_path)
        agents = {
            "claude": FakeAgent("claude", ["turn one", "wrapping up [[DONE]]"]),
            "codex": FakeAgent("codex", [QuotaError("codex: usage limit")]),
        }
        result = run(ws, agents, on_quota="solo")
        assert result.outcome == "success"
        assert [m.agent for m in result.transcript.messages] == ["claude", "claude"]
        assert any("dropped from the rotation" in note for note in result.transcript.notes)

    def test_last_agent_quota_still_halts(self, tmp_path):
        ws = repo(tmp_path)
        agents = {"claude": FakeAgent("claude", [QuotaError("claude: usage limit")])}
        result = run(ws, agents, on_quota="solo")
        assert result.outcome == "halted"
        assert result.stop_condition == "QuotaExhausted(claude)"


class TestQuotaWait:
    def test_retries_same_agent_after_wait(self, tmp_path, monkeypatch):
        ws = repo(tmp_path)
        slept = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        codex = FakeAgent("codex", [QuotaError("codex: rate limit"), "recovered [[DONE]]"])
        agents = {"codex": codex}
        result = run(ws, agents, on_quota="wait", quota_wait_seconds=7)
        assert result.outcome == "success"
        assert slept == [7]
        assert codex.calls == 2
        assert any("waiting 7s" in note for note in result.transcript.notes)

    def test_wait_exceeding_wallclock_halts(self, tmp_path, monkeypatch):
        ws = repo(tmp_path)
        monkeypatch.setattr(time, "sleep", lambda s: pytest.fail("should not sleep past budget"))
        agents = {"codex": FakeAgent("codex", [QuotaError("codex: rate limit")])}
        result = run(ws, agents, on_quota="wait", quota_wait_seconds=120, wallclock=60)
        assert result.outcome == "halted"
        assert result.stop_condition == "QuotaExhausted(codex)"
        assert "wallclock budget" in result.session.transcript.error


class TestQuotaConfig:
    def test_invalid_on_quota_rejected(self, tmp_path):
        config = tmp_path / "duet.toml"
        config.write_text("[session]\non_quota = \"panic\"\n")
        with pytest.raises(ConfigError, match="on_quota"):
            load_config(config)

    def test_invalid_on_quota_arg_rejected(self, tmp_path):
        ws = repo(tmp_path)
        with pytest.raises(ValueError, match="on_quota"):
            run(ws, {"claude": FakeAgent("claude", [])}, on_quota="panic")


class TestQuotaErrorDetection:
    def test_cli_quota_failure_raises_quota_error(self, tmp_path):
        script = tmp_path / "fake"
        script.write_text("#!/bin/sh\ncat > /dev/null\necho 'usage limit reached' >&2\nexit 1\n")
        script.chmod(0o755)
        from duet.adapters import CLIAgent

        agent = CLIAgent(
            name="codex",
            display_name="Codex",
            command=[str(script)],
            prompt_via="stdin",
            workspace_flag="",
            output_format="text",
            timeout_seconds=30,
        )
        with pytest.raises(QuotaError):
            agent.send("hi", tmp_path)
