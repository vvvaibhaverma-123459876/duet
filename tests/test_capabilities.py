from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from duet.adapters import AgentResult, CLIAgent, QuotaError
from duet.broker import run_session
from duet.registry import RunEntry, finish_run, format_runs, list_runs, register_run
from duet.verifiers import AlwaysUnknown, CommandVerifier, CompositeVerifier, PytestVerifier, build_verifier
from duet.workspace import prepare_live_repo, release_lock, remove_worktree


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "f").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


class TestVerifiers:
    def test_command_verifier_pass_and_fail(self, tmp_path):
        assert CommandVerifier("true").verify(tmp_path).status == "passed"
        result = CommandVerifier("echo nope >&2; exit 3").verify(tmp_path)
        assert result.status == "failed" and "nope" in result.output

    def test_command_verifier_timeout_fails(self, tmp_path):
        result = CommandVerifier("sleep 5", timeout_seconds=1).verify(tmp_path)
        assert result.status == "failed" and "timed out" in result.output

    def test_composite_all_pass(self, tmp_path):
        composite = CompositeVerifier([CommandVerifier("true"), CommandVerifier("true")])
        assert composite.verify(tmp_path).status == "passed"

    def test_composite_one_failure_fails(self, tmp_path):
        composite = CompositeVerifier([CommandVerifier("true"), CommandVerifier("false")])
        assert composite.verify(tmp_path).status == "failed"

    def test_composite_unknown_without_failure(self, tmp_path):
        composite = CompositeVerifier([CommandVerifier("true"), AlwaysUnknown()])
        assert composite.verify(tmp_path).status == "unknown"

    def test_build_verifier_specs(self):
        assert isinstance(build_verifier([]), AlwaysUnknown)
        assert isinstance(build_verifier(["none"]), AlwaysUnknown)
        assert isinstance(build_verifier(["pytest"]), PytestVerifier)
        assert isinstance(build_verifier(["cmd:make check"]), CommandVerifier)
        assert isinstance(build_verifier(["pytest", "cmd:npm test"]), CompositeVerifier)
        with pytest.raises(ValueError, match="unknown verifier"):
            build_verifier(["jest"])
        with pytest.raises(ValueError, match="empty command"):
            build_verifier(["cmd:"])


class TestWorktreeIsolation:
    def test_worktree_leaves_checkout_untouched(self, tmp_path):
        repo = make_repo(tmp_path)
        before = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo, capture_output=True, text=True).stdout
        live = prepare_live_repo(str(repo), worktree=True)
        try:
            assert live.worktree_of == repo.resolve()
            assert live.workspace != repo
            after = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo, capture_output=True, text=True).stdout
            assert after == before  # user's checkout never switched
            wt_branch = subprocess.run(
                ["git", "symbolic-ref", "--short", "HEAD"], cwd=live.workspace, capture_output=True, text=True
            ).stdout.strip()
            assert wt_branch.startswith("duet/session-")
        finally:
            release_lock(live.workspace)
            remove_worktree(live)

    def test_worktree_commits_visible_in_main_repo(self, tmp_path):
        repo = make_repo(tmp_path)
        live = prepare_live_repo(str(repo), worktree=True)
        (live.workspace / "new.txt").write_text("agent work\n")
        subprocess.run(["git", "add", "-A"], cwd=live.workspace, check=True)
        subprocess.run(
            ["git", "-c", "user.email=a@a", "-c", "user.name=a", "commit", "-qm", "agent turn"],
            cwd=live.workspace,
            check=True,
        )
        release_lock(live.workspace)
        remove_worktree(live)
        log = subprocess.run(["git", "log", "--oneline", live.branch], cwd=repo, capture_output=True, text=True).stdout
        assert "agent turn" in log  # branch survives worktree removal

    def test_worktree_rollback_deletes_branch(self, tmp_path):
        repo = make_repo(tmp_path)
        live = prepare_live_repo(str(repo), worktree=True)
        release_lock(live.workspace)
        remove_worktree(live, delete_branch=True)
        branches = subprocess.run(["git", "branch", "--list", "duet/*"], cwd=repo, capture_output=True, text=True).stdout
        assert branches.strip() == ""

    def test_dirty_main_checkout_does_not_block_worktree(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / "f").write_text("uncommitted edit\n")
        live = prepare_live_repo(str(repo), worktree=True)
        try:
            assert (live.workspace / "f").read_text() == "base\n"  # from HEAD, not the dirty tree
        finally:
            release_lock(live.workspace)
            remove_worktree(live)


class FakeCostAgent:
    def __init__(self, name: str, cost: float):
        self.name = name
        self.display_name = name.title()
        self.cost = cost

    def send(self, prompt: str, workspace: Path) -> AgentResult:
        return AgentResult(
            text="work continues", exit_code=0, duration_s=0.01, raw_stdout="", raw_stderr="", cost_usd=self.cost
        )


class TestBudget:
    def test_budget_halts_session(self, tmp_path):
        repo = make_repo(tmp_path)
        from duet.verifiers import AlwaysUnknown

        result = run_session(
            task="t",
            workspace=repo,
            agents={"claude": FakeCostAgent("claude", 0.30)},
            start_with="claude",
            max_turns=10,
            wallclock_seconds=60,
            loop_threshold=2.0,  # disable loop detector for identical replies
            verifier=AlwaysUnknown(),
            budget_usd=0.50,
        )
        assert result.outcome == "halted"
        assert result.stop_condition == "BudgetExceeded($0.50)"
        assert len(result.transcript.messages) == 2  # 0.30 + 0.30 crosses 0.50
        assert result.transcript.total_cost_usd == pytest.approx(0.60)

    def test_zero_budget_means_unlimited(self, tmp_path):
        repo = make_repo(tmp_path)
        from duet.verifiers import AlwaysUnknown

        result = run_session(
            task="t",
            workspace=repo,
            agents={"claude": FakeCostAgent("claude", 5.0)},
            start_with="claude",
            max_turns=2,
            wallclock_seconds=60,
            loop_threshold=2.0,
            verifier=AlwaysUnknown(),
            budget_usd=0.0,
        )
        assert result.stop_condition == "MaxTurns(2)"


class TestCostParsing:
    def test_cost_parsed_from_json_output(self, tmp_path):
        script = tmp_path / "fake"
        payload = json.dumps({"result": "ok", "session_id": "s", "total_cost_usd": 0.0421})
        script.write_text(f"#!/bin/sh\ncat > /dev/null\necho '{payload}'\n")
        script.chmod(0o755)
        agent = CLIAgent(
            name="claude",
            display_name="Claude",
            command=[str(script)],
            prompt_via="stdin",
            workspace_flag="",
            output_format="json",
            timeout_seconds=30,
            result_json_path="result",
            cost_json_path="total_cost_usd",
        )
        assert agent.send("hi", tmp_path).cost_usd == pytest.approx(0.0421)

    def test_missing_cost_path_is_zero_not_error(self, tmp_path):
        script = tmp_path / "fake"
        script.write_text('#!/bin/sh\ncat > /dev/null\necho \'{"result":"ok"}\'\n')
        script.chmod(0o755)
        agent = CLIAgent(
            name="claude",
            display_name="Claude",
            command=[str(script)],
            prompt_via="stdin",
            workspace_flag="",
            output_format="json",
            timeout_seconds=30,
            result_json_path="result",
            cost_json_path="total_cost_usd",
        )
        assert agent.send("hi", tmp_path).cost_usd == 0.0


class TestQuotaMarkers:
    def _agent(self, tmp_path, markers):
        script = tmp_path / "fake"
        script.write_text("#!/bin/sh\ncat > /dev/null\necho 'Fehler: Kontingent aufgebraucht' >&2\nexit 1\n")
        script.chmod(0o755)
        return CLIAgent(
            name="codex",
            display_name="Codex",
            command=[str(script)],
            prompt_via="stdin",
            workspace_flag="",
            output_format="text",
            timeout_seconds=30,
            quota_markers=markers,
        )

    def test_custom_marker_detected(self, tmp_path):
        agent = self._agent(tmp_path, ["kontingent"])
        with pytest.raises(QuotaError):
            agent.send("hi", tmp_path)

    def test_default_markers_miss_custom_message(self, tmp_path):
        from duet.adapters import DEFAULT_QUOTA_MARKERS, AgentError

        agent = self._agent(tmp_path, list(DEFAULT_QUOTA_MARKERS))
        with pytest.raises(AgentError) as exc:
            agent.send("hi", tmp_path)
        assert not isinstance(exc.value, QuotaError)


class TestRegistry:
    def test_register_finish_list_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        run_id = register_run(Path("/w"), "duet/x", "fix the things")
        entries = list_runs()
        assert entries[0].run_id == run_id
        assert entries[0].status() == "running"  # our own pid is alive
        finish_run(run_id, "success", cost_usd=0.12)
        entries = list_runs()
        assert entries[0].status() == "success"
        assert entries[0].cost_usd == pytest.approx(0.12)
        rendered = format_runs(entries)
        assert "success" in rendered and "fix the things" in rendered

    def test_dead_pid_shows_died(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        entry = RunEntry(run_id="x", pid=2 ** 30, workspace="/w", branch="", task_head="t", started=1.0)
        assert entry.status() == "died"

    def test_empty_registry_formats(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert "No duet runs" in format_runs(list_runs())
