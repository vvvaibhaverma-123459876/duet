from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from duet.adapters import AgentResult
from duet.broker import _cap, run_session
from duet.config import ConfigError, load_config
from duet.verifiers import PytestVerifier
from duet.workspace import (
    WorkspaceError,
    acquire_lock,
    commit_after_turn,
    create_workspace,
    prepare_live_repo,
    release_lock,
    rollback_live_repo,
)


@dataclass
class MockAgent:
    name: str
    display_name: str
    reply: str

    def send(self, prompt: str, workspace: Path) -> AgentResult:
        (workspace / f"{self.name}.txt").write_text(self.reply, encoding="utf-8")
        return AgentResult(self.reply, 0, 0.01, self.reply, "")


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


# --- Bounded capture --------------------------------------------------------


def test_cap_truncates_large_output():
    big = "x" * 100000
    capped = _cap(big, limit=1000)
    assert len(capped) < len(big)
    assert "truncated by Duet" in capped


def test_cap_leaves_small_output_intact():
    assert _cap("hello", limit=1000) == "hello"


# --- Stale lock reclaim -----------------------------------------------------


def test_stale_lock_from_dead_pid_is_reclaimed(tmp_path):
    workspace = create_workspace(str(tmp_path / "w"))
    lock = workspace / ".duet" / "session.lock"
    # Simulate a crashed session: a lock file naming a pid that cannot exist.
    lock.write_text("pid=999999999\ncreated=0\n", encoding="utf-8")
    reclaimed = acquire_lock(workspace)  # should not raise
    assert reclaimed.exists()
    release_lock(workspace)


def test_live_lock_from_running_pid_blocks(tmp_path):
    workspace = create_workspace(str(tmp_path / "w"))
    with pytest.raises(WorkspaceError):
        acquire_lock(workspace)  # our own pid is alive
    release_lock(workspace)


# --- Git failure handling ---------------------------------------------------


def test_commit_failure_raises_workspace_error(tmp_path):
    # A directory that is not a git repo makes `git add -A` fail.
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("x", encoding="utf-8")
    with pytest.raises(WorkspaceError):
        commit_after_turn(plain, "claude", "Claude")


def test_broker_halts_cleanly_on_commit_failure(tmp_path, monkeypatch):
    workspace = create_workspace(str(tmp_path / "w"))
    agents = {"claude": MockAgent("claude", "Claude", "hi [[HANDOFF]]")}

    import duet.broker as broker

    def boom(*args, **kwargs):
        raise WorkspaceError("simulated git failure")

    from duet.verifiers import AlwaysUnknown

    monkeypatch.setattr(broker, "commit_after_turn", boom)
    result = run_session(
        task="t", workspace=workspace, agents=agents, start_with="claude",
        max_turns=2, wallclock_seconds=60, loop_threshold=0.9,
        verifier=AlwaysUnknown(),
        require_all_agents_for_success=False,
    )
    assert result.outcome == "halted"
    assert result.stop_condition == "WorkspaceError"
    release_lock(workspace)


# --- Config validation ------------------------------------------------------


def test_config_rejects_missing_command(tmp_path):
    cfg = tmp_path / "duet.toml"
    cfg.write_text(
        '[agents.codex]\ndisplay_name = "Codex"\nprompt_via = "stdin"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="command"):
        load_config(cfg)


def test_config_rejects_bad_output_format(tmp_path):
    cfg = tmp_path / "duet.toml"
    cfg.write_text(
        '[agents.codex]\ncommand = ["codex"]\noutput_format = "nonsense"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="output_format"):
        load_config(cfg)


def test_config_rejects_invalid_toml(tmp_path):
    cfg = tmp_path / "duet.toml"
    cfg.write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(cfg)


# --- Verifier robustness ----------------------------------------------------


def test_pytest_verifier_reports_timeout(tmp_path, monkeypatch):
    import duet.verifiers as verifiers

    monkeypatch.setattr(verifiers.shutil, "which", lambda name: "/usr/bin/pytest")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(verifiers.subprocess, "run", fake_run)
    result = PytestVerifier(timeout_seconds=1).verify(tmp_path)
    assert result.status == "failed"
    assert "timed out" in result.output


def test_pytest_verifier_handles_missing_binary(tmp_path, monkeypatch):
    import duet.verifiers as verifiers

    monkeypatch.setattr(verifiers.shutil, "which", lambda name: None)
    result = PytestVerifier().verify(tmp_path)
    assert result.status == "unknown"
    assert "not found" in result.output


# --- Live-repo mode ---------------------------------------------------------


def _make_repo(path: Path) -> Path:
    path.mkdir()
    _git(["init"], path)
    _git(["config", "user.email", "t@t.invalid"], path)
    _git(["config", "user.name", "T"], path)
    (path / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git(["add", "."], path)
    _git(["commit", "-m", "init"], path)
    return path


def test_prepare_live_repo_uses_isolated_branch(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    live = prepare_live_repo(str(repo))
    try:
        assert live.branch.startswith("duet/session-")
        current = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
        assert current == live.branch
        assert live.base_commit == _git(["rev-parse", live.original_ref], repo)
    finally:
        release_lock(live.workspace)


def test_prepare_live_repo_refuses_dirty_tree(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    (repo / "app.py").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="uncommitted"):
        prepare_live_repo(str(repo))


def test_prepare_live_repo_allows_dirty_when_opted_in(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    (repo / "app.py").write_text("dirty\n", encoding="utf-8")
    live = prepare_live_repo(str(repo), allow_dirty=True)
    release_lock(live.workspace)


def test_prepare_live_repo_rejects_non_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorkspaceError, match="not a git work tree"):
        prepare_live_repo(str(plain))


def test_duet_dir_not_committed_in_live_mode(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    live = prepare_live_repo(str(repo))
    (repo / "app.py").write_text("changed\n", encoding="utf-8")
    commit_after_turn(repo, "claude", "Claude")
    release_lock(live.workspace)
    files = _git(["show", "--stat", "--name-only", "--format=", "HEAD"], repo)
    assert ".duet" not in files


def test_allow_dirty_rollback_restores_user_work(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    (repo / "app.py").write_text("MY WORK\n", encoding="utf-8")  # dirty tracked
    (repo / "extra.txt").write_text("untracked\n", encoding="utf-8")  # dirty untracked
    live = prepare_live_repo(str(repo), allow_dirty=True)
    (repo / "app.py").write_text("agent change\n", encoding="utf-8")
    commit_after_turn(repo, "claude", "Claude")
    release_lock(live.workspace)
    rollback_live_repo(live)
    assert (repo / "app.py").read_text() == "MY WORK\n"
    assert (repo / "extra.txt").read_text() == "untracked\n"


def test_rollback_restores_original_branch(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    original = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    live = prepare_live_repo(str(repo))
    release_lock(live.workspace)
    (repo / "app.py").write_text("v2\n", encoding="utf-8")
    commit_after_turn(repo, "claude", "Claude")
    rollback_live_repo(live)
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], repo) == original
    assert (repo / "app.py").read_text() == "print('v1')\n"
    branches = _git(["branch"], repo)
    assert live.branch not in branches
