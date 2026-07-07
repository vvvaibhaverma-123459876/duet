"""Hermetic end-to-end battery: drives the real `duet` CLI as a subprocess
against deterministic fake agent binaries. No network, no real model calls,
so it always runs (unlike the DUET_E2E-gated real-CLI test)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FAKE_CLAUDE = r"""#!/bin/bash
prompt=$(cat)
[[ "$prompt" == *CLAUDE_DOCTOR_OK* ]] && { echo '{"result":"CLAUDE_DOCTOR_OK","session_id":"doc"}'; exit 0; }
echo "$@" >> "$BATTERY_STATE/fc-args.log"
n_file="$BATTERY_STATE/fc-n"; n=$(cat "$n_file" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$n_file"
case "${FC_MODE:-done}" in
  done)    [[ $n -ge 2 ]] && r="finishing [[DONE]]" || r="turn $n work [[HANDOFF]]" ;;
  loop)    r="identical repeated answer every time" ;;
  hang)    sleep 30; r="too late" ;;
  edit)    echo "line-$n" >> file.txt; [[ $n -ge 2 ]] && r="edited [[DONE]]" || r="edited [[HANDOFF]]" ;;
  fail)    echo "boom" >&2; exit 3 ;;
esac
python3 -c "import json,sys;print(json.dumps({'result':sys.argv[1],'session_id':f'fc-$n','total_cost_usd':0.25}))" "$r"
"""

FAKE_CODEX = r"""#!/bin/bash
prompt=$(cat)
if [[ "$prompt" == *CODEX_DOCTOR_OK* ]]; then
  [[ "${FX_MODE:-ok}" == "deadstart" ]] && { echo "usage limit hit" >&2; exit 1; }
  echo "CODEX_DOCTOR_OK"; exit 0
fi
case "${FX_MODE:-ok}" in
  ok)      echo "reviewed, ok [[DONE]]" ;;
  loop)    echo "identical codex reply each round" ;;
  quota)   echo "You've hit your usage limit." >&2; exit 1 ;;
  quota1)  f="$BATTERY_STATE/fx-failed"; [[ -f $f ]] && echo "recovered [[DONE]]" || { touch $f; echo "rate limit" >&2; exit 1; } ;;
  deadstart) echo "usage limit" >&2; exit 1 ;;
esac
"""

CONFIG = """\
[session]
start_with = "claude"
max_turns = 4
wallclock_seconds = 60
loop_threshold = 0.9

[agents.claude]
display_name = "Claude"
command = ["{fc}"]
prompt_via = "stdin"
workspace_flag = ""
output_format = "json"
result_json_path = "result"
session_json_path = "session_id"
cost_json_path = "total_cost_usd"
resume_command = ["{fc}", "--resume", "{{session_id}}"]
timeout_seconds = 5

[agents.codex]
display_name = "Codex"
command = ["{fx}"]
prompt_via = "stdin"
workspace_flag = ""
output_format = "text"
timeout_seconds = 5
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    root = tmp_path_factory.mktemp("battery")
    bindir = root / "bin"
    bindir.mkdir()
    fc, fx = bindir / "fc", bindir / "fx"
    fc.write_text(FAKE_CLAUDE)
    fx.write_text(FAKE_CODEX)
    # Doctor gates round-trips on shutil.which("claude"/"codex"); provide stubs.
    (bindir / "claude").symlink_to(fc)
    (bindir / "codex").symlink_to(fx)
    for script in (fc, fx):
        script.chmod(0o755)
    config = root / "duet.toml"
    config.write_text(CONFIG.format(fc=fc, fx=fx))
    return {"root": root, "bindir": bindir, "config": config}


@pytest.fixture()
def duet(harness, tmp_path):
    state = tmp_path / "state"
    state.mkdir()

    def run(*args: str, env: dict | None = None, stdin: str = "") -> subprocess.CompletedProcess:
        full_env = os.environ.copy()
        full_env["PATH"] = f"{harness['bindir']}:{full_env['PATH']}"
        full_env["BATTERY_STATE"] = str(state)
        full_env.pop("FC_MODE", None)
        full_env.pop("FX_MODE", None)
        full_env.update(env or {})
        return subprocess.run(
            [sys.executable, "-m", "duet", "--config", str(harness["config"]), *args],
            input=stdin or None,
            text=True,
            capture_output=True,
            timeout=120,
            env=full_env,
        )

    run.state = state
    return run


def live_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "live"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "f").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"], cwd=repo, check=True
    )
    return repo


class TestCoreLoop:
    def test_scratch_run_success_both_agents(self, duet):
        proc = duet("run", "t")
        assert "Outcome: success" in proc.stdout
        assert "Turn 2: Codex" in proc.stdout

    def test_loop_detector_halts_repetition(self, duet):
        proc = duet("run", "--max-turns", "6", "t", env={"FC_MODE": "loop", "FX_MODE": "loop"})
        assert "LoopDetector" in proc.stdout

    def test_hung_agent_killed_at_timeout_no_zombies(self, duet):
        proc = duet("run", "t", env={"FC_MODE": "hang"})
        assert "timed out after 5s" in proc.stdout + proc.stderr
        ps = subprocess.run(["pgrep", "-f", "bin/fc"], capture_output=True)
        assert ps.returncode != 0, "fake agent left running after timeout kill"

    def test_agent_failure_halts_as_agent_error(self, duet):
        proc = duet("run", "t", env={"FC_MODE": "fail"})
        assert "Stop condition: AgentError" in proc.stdout


class TestLiveRepoSafety:
    def test_branch_isolation_and_agent_commits(self, duet, tmp_path):
        repo = live_repo(tmp_path)
        duet("run", "--repo", str(repo), "t", env={"FC_MODE": "edit"})
        branches = subprocess.run(
            ["git", "branch", "--list", "duet/session-*"], cwd=repo, capture_output=True, text=True
        ).stdout.split()
        assert branches, "duet session branch missing"
        base = subprocess.run(["git", "log", "--format=%s", branches[-1]], cwd=repo, capture_output=True, text=True)
        assert "base" in base.stdout  # original commit still the root

    def test_dirty_repo_refused(self, duet, tmp_path):
        repo = live_repo(tmp_path)
        (repo / "f").write_text("dirty\n")
        proc = duet("run", "--repo", str(repo), "t")
        assert proc.returncode != 0

    def test_rollback_on_failure_discards_branch(self, duet, tmp_path):
        repo = live_repo(tmp_path)
        duet("run", "--repo", str(repo), "--rollback-on-failure", "t", env={"FC_MODE": "fail"})
        branches = subprocess.run(
            ["git", "branch", "--list", "duet/session-*"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert branches == ""

    def test_lock_contention_rejected(self, duet, tmp_path):
        repo = live_repo(tmp_path)
        duet("run", "--repo", str(repo), "t")  # first run installs .duet exclusion
        lock = repo / ".duet" / "session.lock"
        lock.parent.mkdir(exist_ok=True)
        lock.write_text(f"pid={os.getpid()}\ncreated=1\n")
        proc = duet("run", "--repo", str(repo), "t")
        assert "already locked" in proc.stdout + proc.stderr


class TestAttach:
    def test_malformed_attach_rejected(self, duet):
        proc = duet("run", "--attach", "garbage", "t")
        assert "AGENT=SESSION_ID" in proc.stdout + proc.stderr

    def test_unknown_agent_rejected(self, duet):
        proc = duet("run", "--attach", "gpt5=abc", "t")
        assert "unknown or unavailable" in proc.stdout + proc.stderr

    def test_attach_resumes_and_chains(self, duet):
        duet("run", "--attach", "claude=seed-123", "t", env={"FX_MODE": "deadstart"})
        lines = (duet.state / "fc-args.log").read_text().splitlines()
        assert "--resume seed-123" in lines[0]
        assert "--resume fc-1" in lines[-1]


class TestQuotaPolicies:
    def test_halt_names_exhausted_agent(self, duet):
        proc = duet("run", "t", env={"FX_MODE": "quota"})
        assert "QuotaExhausted(codex)" in proc.stdout

    def test_solo_survivor_finishes_with_note(self, duet):
        proc = duet("run", "--on-quota", "solo", "t", env={"FX_MODE": "quota"})
        assert "Outcome: success" in proc.stdout
        assert "dropped from the rotation" in proc.stdout

    def test_wait_retries_same_agent(self, duet):
        proc = duet("run", "--on-quota", "wait", "--quota-wait-seconds", "1", "t", env={"FX_MODE": "quota1"})
        assert "Outcome: success" in proc.stdout
        assert "waiting 1s" in proc.stdout

    def test_preflight_excludes_dead_agent(self, duet):
        proc = duet("run", "t", env={"FX_MODE": "deadstart"})
        assert "Outcome: success" in proc.stdout  # claude proceeds alone


class TestResume:
    def test_manifest_saved_and_resume_succeeds(self, duet, tmp_path):
        repo = live_repo(tmp_path)
        duet("run", "--repo", str(repo), "t", env={"FX_MODE": "quota"})
        manifest = json.loads((repo / ".duet" / "resume.json").read_text())
        assert manifest["stop_condition"] == "QuotaExhausted(codex)"
        (duet.state / "fc-n").unlink(missing_ok=True)
        proc = duet("resume", "--repo", str(repo))
        assert "Resuming duet" in proc.stdout
        assert "Outcome: success" in proc.stdout

    def test_missing_manifest_clean_error(self, duet, tmp_path):
        proc = duet("resume", "--repo", str(tmp_path))
        assert proc.returncode == 1
        assert "no resume manifest" in proc.stderr

    def test_corrupt_manifest_clean_error(self, duet, tmp_path):
        (tmp_path / ".duet").mkdir()
        (tmp_path / ".duet" / "resume.json").write_text("{broken")
        proc = duet("resume", "--repo", str(tmp_path))
        assert proc.returncode == 1
        assert "cannot read" in proc.stderr


class TestWorktree:
    def test_worktree_run_never_switches_checkout(self, duet, tmp_path):
        repo = live_repo(tmp_path)
        before = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo, capture_output=True, text=True).stdout
        proc = duet("run", "--repo", str(repo), "--worktree", "t", env={"FC_MODE": "edit"})
        after = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo, capture_output=True, text=True).stdout
        assert before == after, "worktree mode must not switch the user's checkout"
        assert "worktree of" in proc.stdout
        assert "Worktree kept at" in proc.stderr
        branches = subprocess.run(
            ["git", "branch", "--list", "duet/session-*"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert branches.strip(), "duet branch must exist in the main repo"

    def test_worktree_rollback_cleans_everything(self, duet, tmp_path):
        repo = live_repo(tmp_path)
        duet("run", "--repo", str(repo), "--worktree", "--rollback-on-failure", "t", env={"FC_MODE": "fail"})
        branches = subprocess.run(["git", "branch", "--list", "duet/*"], cwd=repo, capture_output=True, text=True).stdout
        assert branches.strip() == ""
        worktrees = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout
        assert "duet-wt-" not in worktrees


class TestVerifyAndBudget:
    def test_command_verifier_gates_done(self, duet, tmp_path):
        repo = live_repo(tmp_path)
        # Verifier fails => [[DONE]] is not honored => session runs out of turns.
        proc = duet("run", "--repo", str(repo), "--max-turns", "2", "--verify", "cmd:exit 1", "t")
        assert "Outcome: success" not in proc.stdout
        assert "MaxTurns" in proc.stdout

    def test_command_verifier_pass_allows_done(self, duet, tmp_path):
        repo = live_repo(tmp_path)
        proc = duet("run", "--repo", str(repo), "--verify", "cmd:true", "t")
        assert "Outcome: success" in proc.stdout

    def test_composite_verify_all_must_pass(self, duet, tmp_path):
        repo = live_repo(tmp_path)
        proc = duet("run", "--repo", str(repo), "--max-turns", "2", "--verify", "cmd:true", "--verify", "cmd:false", "t")
        assert "Outcome: success" not in proc.stdout

    def test_invalid_verify_spec_rejected(self, duet):
        proc = duet("run", "--verify", "jest", "t")
        assert proc.returncode == 1
        assert "Invalid --verify" in proc.stderr

    def test_budget_halts_and_reports_cost(self, duet):
        # fake claude reports $0.25/turn; budget $0.30 halts on turn 2's spend
        proc = duet("run", "--budget-usd", "0.30", "--max-turns", "6", "t", env={"FX_MODE": "loop"})
        assert "BudgetExceeded($0.30)" in proc.stdout
        assert "Model cost: $" in proc.stdout

    def test_cost_reported_on_normal_run(self, duet):
        proc = duet("run", "t")
        assert "Model cost: $0.25" in proc.stdout  # one claude turn reported


class TestPs:
    def test_ps_lists_run_with_outcome(self, duet, tmp_path):
        env = {"XDG_STATE_HOME": str(tmp_path / "state")}
        duet("run", "t", env=env)
        proc = duet("ps", env=env)
        assert "success" in proc.stdout
        assert "Recent duet runs" in proc.stdout


class TestLifecycle:
    def test_stop_refuses_without_tty_or_yes(self, duet, tmp_path):
        # Plant a stoppable target (a live duet lock) so the guard is reached
        # even on machines with no claude/codex processes running.
        lock = tmp_path / ".duet" / "session.lock"
        lock.parent.mkdir()
        lock.write_text(f"pid={os.getpid()}\ncreated=1\n")
        proc = duet("stop", "--repo", str(tmp_path))
        assert proc.returncode == 1
        assert "Refusing to stop" in proc.stderr

    def test_stop_no_targets_reports_cleanly(self, duet, tmp_path):
        proc = duet("stop", "duet", "--repo", str(tmp_path), "--yes")
        assert "Nothing to stop" in proc.stderr

    def test_talk_solo_turn_via_stdin(self, duet, tmp_path):
        repo = live_repo(tmp_path)
        proc = duet("talk", "claude", "--new", "--repo", str(repo), stdin="ping")
        assert "turn 1 work" in proc.stdout
