"""Hermetic end-to-end battery: drives the real `duet` CLI as a subprocess
against deterministic fake agent binaries. No network, no real model calls,
so it always runs (unlike the DUET_E2E-gated real-CLI test)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from exeshim import make_exe

FAKE_CLAUDE = r"""
import json, os, sys, time
prompt = sys.stdin.read()
if "CLAUDE_DOCTOR_OK" in prompt:
    print(json.dumps({"result": "CLAUDE_DOCTOR_OK", "session_id": "doc"})); sys.exit(0)
state = os.environ["BATTERY_STATE"]
with open(os.path.join(state, "fc-args.log"), "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
n_file = os.path.join(state, "fc-n")
try:
    n = int(open(n_file).read().strip() or 0)
except OSError:
    n = 0
n += 1
open(n_file, "w").write(f"{n}\n")
mode = os.environ.get("FC_MODE") or "done"
if mode == "done":
    r = "finishing [[DONE]]" if n >= 2 else f"turn {n} work [[HANDOFF]]"
elif mode == "work":
    r = f"turn {n} work [[HANDOFF]]"
elif mode == "mention":
    r = "I will not claim completion yet; [[DONE]] comes later\nstill working [[HANDOFF]]"
elif mode == "loop":
    r = "identical repeated answer every time"
elif mode == "hang":
    time.sleep(30); r = "too late"
elif mode == "edit":
    with open("file.txt", "a") as f:
        f.write(f"line-{n}\n")
    r = "edited [[DONE]]" if n >= 2 else "edited [[HANDOFF]]"
elif mode == "fail":
    sys.stderr.write("boom\n"); sys.exit(3)
print(json.dumps({"result": r, "session_id": f"fc-{n}", "total_cost_usd": 0.25}))
"""

FAKE_CODEX = r"""
import os, sys
prompt = sys.stdin.read()
mode = os.environ.get("FX_MODE") or "ok"
if "CODEX_DOCTOR_OK" in prompt:
    if mode == "deadstart":
        sys.stderr.write("usage limit hit\n"); sys.exit(1)
    print("CODEX_DOCTOR_OK"); sys.exit(0)
if mode == "ok":
    print("reviewed, ok [[DONE]]")
elif mode == "loop":
    print("identical codex reply each round")
elif mode == "quota":
    sys.stderr.write("You've hit your usage limit.\n"); sys.exit(1)
elif mode == "quota1":
    f = os.path.join(os.environ["BATTERY_STATE"], "fx-failed")
    if os.path.exists(f):
        print("recovered [[DONE]]")
    else:
        open(f, "w").close(); sys.stderr.write("rate limit\n"); sys.exit(1)
elif mode == "deadstart":
    sys.stderr.write("usage limit\n"); sys.exit(1)
"""

CONFIG = """\
[session]
start_with = "claude"
max_turns = 4
wallclock_seconds = 60
loop_threshold = 0.9

[agents.claude]
display_name = "Claude"
command = ['{fc}']
prompt_via = "stdin"
workspace_flag = ""
output_format = "json"
result_json_path = "result"
session_json_path = "session_id"
cost_json_path = "total_cost_usd"
resume_command = ['{fc}', "--resume", "{{session_id}}"]
timeout_seconds = 5

[agents.codex]
display_name = "Codex"
command = ['{fx}']
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
    fc = make_exe(bindir, "fc", source=FAKE_CLAUDE)
    fx = make_exe(bindir, "fx", source=FAKE_CODEX)
    # Doctor gates round-trips on shutil.which("claude"/"codex"); provide stubs.
    make_exe(bindir, "claude", source=FAKE_CLAUDE)
    make_exe(bindir, "codex", source=FAKE_CODEX)
    config = root / "duet.toml"
    config.write_text(CONFIG.format(fc=fc, fx=fx))
    return {"root": root, "bindir": bindir, "config": config}


@pytest.fixture()
def duet(harness, tmp_path):
    state = tmp_path / "state"
    state.mkdir()

    def run(*args: str, env: dict | None = None, stdin: str = "", cwd: Path | None = None) -> subprocess.CompletedProcess:
        full_env = os.environ.copy()
        full_env["PATH"] = f"{harness['bindir']}{os.pathsep}{full_env['PATH']}"
        full_env["BATTERY_STATE"] = str(state)
        full_env.pop("FC_MODE", None)
        full_env.pop("FX_MODE", None)
        full_env.update(env or {})
        return subprocess.run(
            [sys.executable, "-m", "duet", "--config", str(harness["config"]), *args],
            # never the test runner's own stdin (a console on Windows CI): no TTY unless given input
            **({"input": stdin} if stdin else {"stdin": subprocess.DEVNULL}),
            text=True,
            capture_output=True,
            timeout=120,
            env=full_env,
            cwd=cwd,
        )

    run.state = state
    run.harness_config = harness["config"]
    run.harness_bindir = harness["bindir"]
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
    def test_scratch_run_without_verifier_is_unverified(self, duet):
        # D-002: agreement to stop is a claim; with no verifier it is reported
        # as unverified (exit 3), never as success.
        proc = duet("run", "t")
        assert "Outcome: unverified" in proc.stdout
        assert "Turn 2: Codex" in proc.stdout
        assert proc.returncode == 3

    def test_scratch_run_with_passing_verifier_is_success(self, duet):
        proc = duet("run", "--verify", "cmd:true", "t")
        assert "Outcome: success" in proc.stdout
        assert "Final verification: passed" in proc.stdout
        assert proc.returncode == 0

    def test_loop_detector_halts_repetition(self, duet):
        proc = duet("run", "--max-turns", "6", "t", env={"FC_MODE": "loop", "FX_MODE": "loop"})
        assert "LoopDetector" in proc.stdout

    def test_hung_agent_killed_at_timeout_no_zombies(self, duet):
        proc = duet("run", "t", env={"FC_MODE": "hang"})
        assert "timed out after 5s" in proc.stdout + proc.stderr
        if os.name == "nt":
            import psutil

            left = [p for p in psutil.process_iter(["cmdline"]) if any("fc.py" in part for part in (p.info["cmdline"] or []))]
            assert not left, f"fake agent left running after timeout kill: {left}"
        else:
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
        assert "Outcome: unverified" in proc.stdout
        assert "dropped from the rotation" in proc.stdout

    def test_solo_completion_keeps_partner_review_pending(self, duet, tmp_path):
        # R14: quota loss must not remove the partner's review obligation. The
        # survivor's changes pass the checks, but codex never reviewed them.
        repo = live_repo(tmp_path)
        proc = duet(
            "run", "--repo", str(repo), "--on-quota", "solo", "--verify", "cmd:true", "t",
            env={"FX_MODE": "quota", "FC_MODE": "edit"},
        )
        assert "Outcome: review_pending" in proc.stdout
        assert "ReviewPending(codex)" in proc.stdout
        assert proc.returncode == 4

    def test_wait_retries_same_agent(self, duet):
        proc = duet("run", "--on-quota", "wait", "--quota-wait-seconds", "1", "t", env={"FX_MODE": "quota1"})
        assert "Outcome: unverified" in proc.stdout
        assert "waiting 1s" in proc.stdout

    def test_preflight_excludes_dead_agent(self, duet):
        proc = duet("run", "t", env={"FX_MODE": "deadstart"})
        assert "Outcome: unverified" in proc.stdout  # claude proceeds alone


class TestResume:
    def test_manifest_saved_and_resume_succeeds(self, duet, tmp_path):
        repo = live_repo(tmp_path)
        duet("run", "--repo", str(repo), "t", env={"FX_MODE": "quota"})
        manifest = json.loads((repo / ".duet" / "resume.json").read_text())
        assert manifest["stop_condition"] == "QuotaExhausted(codex)"
        (duet.state / "fc-n").unlink(missing_ok=True)
        proc = duet("resume", "--repo", str(repo))
        assert "Resuming duet" in proc.stdout
        assert "Outcome: unverified" in proc.stdout

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
        # fake claude reports $0.25/turn and never claims done; codex reports
        # nothing. After claude's second turn ($0.50 >= $0.30) no new turn is admitted.
        proc = duet("run", "--budget-usd", "0.30", "--max-turns", "6", "t", env={"FC_MODE": "work", "FX_MODE": "loop"})
        assert "BudgetExceeded($0.30)" in proc.stdout
        assert "Model cost: $0.5000 reported; 1 turn(s) with unknown cost" in proc.stdout
        assert "codex reports no cost" in proc.stdout
        assert "Turn 4" not in proc.stdout

    def test_completion_on_budget_spending_turn_is_recognised(self, duet):
        # Budget-before-verification regression: the turn that exhausts the
        # budget also finishes the task; the checks decide, not the budget.
        proc = duet("run", "--budget-usd", "0.30", "--verify", "cmd:true", "t", env={"FX_MODE": "loop"})
        assert "Outcome: success" in proc.stdout
        assert "BudgetExceeded" not in proc.stdout

    def test_cost_reported_on_normal_run(self, duet):
        proc = duet("run", "t")
        assert "Model cost: $0.25" in proc.stdout  # one claude turn reported


class TestPs:
    def test_ps_lists_run_with_outcome(self, duet, tmp_path):
        env = {"XDG_STATE_HOME": str(tmp_path / "state")}
        duet("run", "t", env=env)
        proc = duet("ps", env=env)
        assert "unverified" in proc.stdout
        assert "$0.25+?" in proc.stdout  # codex turn cost unknown, shown as such
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


class TestD01Cli:
    def test_task_is_required(self, duet):
        proc = duet("run")
        assert proc.returncode == 1
        assert "a task is required" in proc.stderr

    def test_empty_piped_task_is_rejected(self, tmp_path, monkeypatch, capsys):
        # Bare `duet` with piped stdin routes to `run`; an empty pipe is not a task.
        import io

        from duet.cli import main

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.setattr(sys, "stdin", io.StringIO("   \n"))
        assert main([]) == 1
        assert "a task is required" in capsys.readouterr().err

    def test_invalid_numeric_flags_rejected(self, duet):
        for flag, value in (("--max-turns", "0"), ("--budget-usd", "-1"), ("--budget-usd", "nan"), ("--quota-wait-seconds", "-3")):
            proc = duet("run", flag, value, "t")
            assert proc.returncode == 2, (flag, value)  # argparse usage error

    def test_resume_from_inside_scratch_workspace(self, duet, tmp_path):
        # B7: `cd <workspace> && duet resume` used to be refused as "unsafe cwd".
        workspace = tmp_path / "scratch"
        first = duet("run", "--workspace", str(workspace), "t", env={"FX_MODE": "quota"})
        assert "QuotaExhausted(codex)" in first.stdout
        (duet.state / "fc-n").unlink(missing_ok=True)
        proc = duet("resume", cwd=workspace)
        assert "Resuming duet" in proc.stdout, proc.stderr
        assert "Outcome: unverified" in proc.stdout

    def test_rollback_spares_unverified_work(self, duet, tmp_path):
        repo = live_repo(tmp_path)
        proc = duet("run", "--repo", str(repo), "--rollback-on-failure", "t", env={"FC_MODE": "edit"})
        assert "Outcome: unverified" in proc.stdout
        assert "Not rolled back" in proc.stderr
        branches = subprocess.run(["git", "branch", "--list", "duet/session-*"], cwd=repo, capture_output=True, text=True).stdout
        assert branches.strip()

    def test_interrupt_leaves_a_resumable_manifest(self, duet, tmp_path):
        repo = live_repo(tmp_path)
        proc = subprocess.Popen(
            [sys.executable, "-m", "duet", "--config", str(duet.harness_config), "run", "--repo", str(repo), "t"],
            env={**os.environ, "PATH": f"{duet.harness_bindir}{os.pathsep}{os.environ['PATH']}", "BATTERY_STATE": str(duet.state), "FC_MODE": "hang"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            # Windows: its own console process group, so it can receive Ctrl+Break alone.
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        deadline = time.monotonic() + 20
        while not (duet.state / "fc-n").exists() and time.monotonic() < deadline:
            time.sleep(0.05)  # wait until the fake agent's turn is in flight
        time.sleep(0.3)
        proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 130, out + err
        assert "Outcome: interrupted" in out
        manifest = json.loads((repo / ".duet" / "resume.json").read_text())
        assert manifest["outcome"] == "interrupted"

    def test_init_refuses_to_clobber(self, duet, tmp_path):
        (tmp_path / "duet.toml").write_text("# mine\n")
        proc = duet("init", "--project", cwd=tmp_path)
        assert proc.returncode == 1 and "already exists" in proc.stderr
        assert (tmp_path / "duet.toml").read_text() == "# mine\n"
        assert duet("init", "--project", "--force", cwd=tmp_path).returncode == 0


def test_registry_survives_concurrent_writers(tmp_path):
    code = (
        "import sys; from pathlib import Path; from duet.registry import register_run, finish_run;"
        "rid = register_run(Path('/w'), '', 'task ' + sys.argv[1]); finish_run(rid, 'halted')"
    )
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path)}
    procs = [subprocess.Popen([sys.executable, "-c", code, str(i)], env=env) for i in range(12)]
    for proc in procs:
        assert proc.wait(timeout=60) == 0
    entries = json.loads((tmp_path / "duet" / "runs.json").read_text())
    assert sorted(e["task_head"] for e in entries) == sorted(f"task {i}" for i in range(12))
    assert all(e["outcome"] == "halted" for e in entries)
