"""Isolation modes against real git repositories in tmpdirs.

Nothing here mocks git: every assertion runs real `git` against real repos, because
the property under test — that a session cannot reach the source repo — is a property
of git's behavior, not of our call sites."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from duet.workspace import WorkspaceError, prepare_live_repo, release_lock, remove_worktree


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    assert proc.returncode == 0, f"git {' '.join(args)} failed in {cwd}: {proc.stderr}"
    return proc.stdout.strip()


def refs(repo: Path) -> str:
    return git("show-ref", "--head", cwd=repo)


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    """A repo with two commits on `main`, an `origin` pointing at a local bare repo,
    an untracked `.env`, and an ignored dependency directory."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    repo = tmp_path / "app"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "dev@example.invalid", cwd=repo)
    git("config", "user.name", "Dev", cwd=repo)
    (repo / ".gitignore").write_text("node_modules/\n.env\n")
    (repo / "app.py").write_text("print('one')\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "first", cwd=repo)
    (repo / "app.py").write_text("print('two')\n")
    git("commit", "-aqm", "second", cwd=repo)

    git("remote", "add", "origin", str(bare), cwd=repo)
    git("push", "-q", "-u", "origin", "main", cwd=repo)

    # Untracked/ignored files: exactly what makes a repo runnable and what a clean
    # checkout omits.
    (repo / ".env").write_text("SECRET=hunter2\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "dep.js").write_text("module.exports = 1\n")
    return repo


class TestSnapshotIsolation:
    """T2: the replica is faithful, and the source is untouchable from it."""

    def test_snapshot_replicates_history_origin_and_untracked_files(self, source):
        live = prepare_live_repo(str(source), isolate="snapshot")
        try:
            assert live.snapshot_of == source
            assert live.isolated

            assert git("log", "--format=%s", "main", cwd=live.workspace).splitlines() == ["second", "first"]
            assert git("rev-parse", "main", cwd=live.workspace) == git("rev-parse", "main", cwd=source)
            assert git("remote", "get-url", "origin", cwd=live.workspace) == git(
                "remote", "get-url", "origin", cwd=source
            )
            # Present WITHOUT --carry: this is the whole point of snapshot over worktree.
            assert (live.workspace / ".env").read_text() == "SECRET=hunter2\n"
            assert (live.workspace / "node_modules" / "dep.js").exists()
        finally:
            release_lock(live.workspace)

    def test_source_is_unchanged_by_commits_and_branches_in_the_replica(self, source):
        head_before = git("rev-parse", "HEAD", cwd=source)
        refs_before = refs(source)
        branch_before = git("symbolic-ref", "--short", "HEAD", cwd=source)
        tree_before = git("status", "--porcelain", cwd=source)

        live = prepare_live_repo(str(source), isolate="snapshot")
        try:
            (live.workspace / "app.py").write_text("print('mutated by agent')\n")
            (live.workspace / "brand_new.py").write_text("x = 1\n")
            git("add", "-A", cwd=live.workspace)
            git("-c", "user.email=a@b", "-c", "user.name=A", "commit", "-qm", "agent work", cwd=live.workspace)
            git("branch", "another-branch", cwd=live.workspace)
            git("checkout", "-qb", "third-branch", cwd=live.workspace)

            assert git("rev-parse", "HEAD", cwd=live.workspace) != head_before, "replica must have advanced"
        finally:
            release_lock(live.workspace)

        assert git("rev-parse", "HEAD", cwd=source) == head_before, "source HEAD moved"
        assert refs(source) == refs_before, "source refs changed"
        assert git("symbolic-ref", "--short", "HEAD", cwd=source) == branch_before, "source checkout switched"
        assert git("status", "--porcelain", cwd=source) == tree_before, "source working tree changed"
        assert (source / "app.py").read_text() == "print('two')\n", "source file rewritten"
        assert not (source / "brand_new.py").exists(), "replica file leaked into source"

    def test_snapshot_cuts_a_session_branch_and_leaves_main_intact(self, source):
        live = prepare_live_repo(str(source), isolate="snapshot")
        try:
            assert live.branch.startswith("duet/session-")
            assert git("symbolic-ref", "--short", "HEAD", cwd=live.workspace) == live.branch
            assert "main" in git("branch", "--format=%(refname:short)", cwd=live.workspace).split()
        finally:
            release_lock(live.workspace)

    def test_exclude_skips_named_paths(self, source):
        live = prepare_live_repo(str(source), isolate="snapshot", exclude=["node_modules"])
        try:
            assert not (live.workspace / "node_modules").exists()
            assert (live.workspace / ".env").exists(), "--exclude must not over-reach"
        finally:
            release_lock(live.workspace)

    def test_source_duet_lock_is_not_copied_into_the_replica(self, source):
        # A live lock in the source would otherwise make the replica look locked
        # by a foreign pid and abort the session.
        lock = source / ".duet" / "session.lock"
        lock.parent.mkdir()
        lock.write_text(f"pid={os.getpid()}\ncreated=1\n")
        live = prepare_live_repo(str(source), isolate="snapshot")
        try:
            assert live.workspace.exists()
        finally:
            release_lock(live.workspace)

    def test_snapshot_does_not_refuse_a_dirty_source(self, source):
        (source / "app.py").write_text("uncommitted\n")
        live = prepare_live_repo(str(source), isolate="snapshot")
        try:
            assert (live.workspace / "app.py").read_text() == "uncommitted\n"
        finally:
            release_lock(live.workspace)
        assert git("status", "--porcelain", cwd=source), "source dirt must survive untouched"

    def test_exclude_rejected_outside_snapshot(self, source):
        with pytest.raises(WorkspaceError, match="only applies to --isolate snapshot"):
            prepare_live_repo(str(source), isolate="worktree", exclude=["node_modules"])


class TestWorktreeCarry:
    """T3: worktree omits untracked files; --carry brings named ones in."""

    def test_worktree_omits_untracked_files(self, source):
        live = prepare_live_repo(str(source), isolate="worktree")
        try:
            assert not (live.workspace / ".env").exists()
            assert not (live.workspace / "node_modules").exists()
        finally:
            release_lock(live.workspace)
            remove_worktree(live, delete_branch=True)

    def test_carry_copies_named_untracked_paths_in(self, source):
        live = prepare_live_repo(str(source), isolate="worktree", carry=[".env", "node_modules"])
        try:
            assert (live.workspace / ".env").read_text() == "SECRET=hunter2\n"
            assert (live.workspace / "node_modules" / "dep.js").exists()
        finally:
            release_lock(live.workspace)
            remove_worktree(live, delete_branch=True)

    def test_carry_is_a_noop_under_snapshot(self, source):
        live = prepare_live_repo(str(source), isolate="snapshot", carry=[".env"])
        try:
            assert (live.workspace / ".env").read_text() == "SECRET=hunter2\n"
        finally:
            release_lock(live.workspace)

    def test_missing_carry_path_fails_clearly(self, source):
        with pytest.raises(WorkspaceError, match="--carry path does not exist"):
            prepare_live_repo(str(source), isolate="worktree", carry=["nope.txt"])

    def test_worktree_conflicting_with_isolate_is_rejected(self, source):
        with pytest.raises(WorkspaceError, match="--worktree conflicts with --isolate snapshot"):
            prepare_live_repo(str(source), worktree=True, isolate="snapshot")

    def test_worktree_flag_and_isolate_worktree_agree(self, source):
        live = prepare_live_repo(str(source), worktree=True)
        try:
            assert live.isolate == "worktree"
            assert live.worktree_of == source
        finally:
            release_lock(live.workspace)
            remove_worktree(live, delete_branch=True)


class TestBaseAndBranch:
    """T4: explicit --base/--branch override the duet/session-* scheme."""

    @pytest.mark.parametrize("isolate", ["none", "worktree", "snapshot"])
    def test_base_and_branch_override_session_naming(self, source, isolate):
        main_tip = git("rev-parse", "main", cwd=source)
        live = prepare_live_repo(str(source), isolate=isolate, base="main", branch="fix/x")
        try:
            assert live.branch == "fix/x"
            assert git("symbolic-ref", "--short", "HEAD", cwd=live.workspace) == "fix/x"
            assert git("merge-base", "fix/x", "main", cwd=live.workspace) == main_tip
        finally:
            release_lock(live.workspace)
            if isolate == "worktree":
                remove_worktree(live, delete_branch=True)

    def test_preexisting_branch_name_fails_cleanly_and_resets_nothing(self, source):
        git("branch", "fix/x", cwd=source)
        tip_before = git("rev-parse", "fix/x", cwd=source)
        with pytest.raises(WorkspaceError, match="branch already exists: fix/x"):
            prepare_live_repo(str(source), branch="fix/x")
        assert git("rev-parse", "fix/x", cwd=source) == tip_before, "existing branch was reset"
        assert git("symbolic-ref", "--short", "HEAD", cwd=source) == "main", "checkout switched on failure"

    def test_unresolvable_base_fails_cleanly(self, source):
        with pytest.raises(WorkspaceError, match="base ref does not resolve"):
            prepare_live_repo(str(source), isolate="snapshot", base="no-such-ref", branch="fix/y")

    def test_without_base_or_branch_the_session_scheme_is_kept(self, source):
        live = prepare_live_repo(str(source), isolate="snapshot")
        try:
            assert live.branch.startswith("duet/session-")
        finally:
            release_lock(live.workspace)


# --- T5: commit attribution, driven through the real CLI --------------------

FAKE_CLAUDE = r"""#!/bin/bash
prompt=$(cat)
[[ "$prompt" == *CLAUDE_DOCTOR_OK* ]] && { echo '{"result":"CLAUDE_DOCTOR_OK","session_id":"doc"}'; exit 0; }
n_file="$ISO_STATE/n"; n=$(cat "$n_file" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$n_file"
echo "agent line $n" >> agent_file.txt
if [[ "${AGENT_COMMITS:-0}" == "1" ]]; then
  git add -A >/dev/null 2>&1
  git commit -qm "feat: precise message $n" >/dev/null 2>&1
fi
env | grep '^GIT_AUTHOR_NAME=' >> "$ISO_STATE/env.log" || true
python3 -c "import json,sys;print(json.dumps({'result':sys.argv[1],'session_id':f'c-$n'}))" "worked [[DONE]]"
"""

FAKE_CODEX = r"""#!/bin/bash
prompt=$(cat)
[[ "$prompt" == *CODEX_DOCTOR_OK* ]] && { echo "CODEX_DOCTOR_OK"; exit 0; }
echo "reviewed, ok [[DONE]]"
"""

CONFIG = """\
[session]
start_with = "claude"
max_turns = 2
wallclock_seconds = 60
loop_threshold = 0.9

[agents.claude]
display_name = "Claude"
command = ["{fc}"]
prompt_via = "stdin"
output_format = "json"
result_json_path = "result"
session_json_path = "session_id"
resume_command = ["{fc}", "--resume", "{{session_id}}"]
timeout_seconds = 10

[agents.codex]
display_name = "Codex"
command = ["{fx}"]
prompt_via = "stdin"
output_format = "text"
timeout_seconds = 10
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    root = tmp_path_factory.mktemp("isolate")
    bindir = root / "bin"
    bindir.mkdir()
    fc, fx = bindir / "fc", bindir / "fx"
    fc.write_text(FAKE_CLAUDE)
    fx.write_text(FAKE_CODEX)
    (bindir / "claude").symlink_to(fc)
    (bindir / "codex").symlink_to(fx)
    for script in (fc, fx):
        script.chmod(0o755)
    config = root / "duet.toml"
    config.write_text(CONFIG.format(fc=fc, fx=fx))
    return {"bindir": bindir, "config": config}


@pytest.fixture()
def duet(harness, tmp_path):
    state = tmp_path / "state"
    state.mkdir()

    def run(
        *args: str, env: dict | None = None, stdin: str = "", config: Path | None = None
    ) -> subprocess.CompletedProcess:
        full_env = os.environ.copy()
        full_env["PATH"] = f"{harness['bindir']}:{full_env['PATH']}"
        full_env["ISO_STATE"] = str(state)
        full_env.pop("AGENT_COMMITS", None)
        full_env.pop("DUET_ISOLATE", None)
        full_env.pop("DUET_COMMIT_MODE", None)
        full_env.update(env or {})
        return subprocess.run(
            [sys.executable, "-m", "duet", "--config", str(config or harness["config"]), *args],
            input=stdin or None,
            text=True,
            capture_output=True,
            timeout=120,
            env=full_env,
        )

    run.state = state
    run.harness = harness
    return run


def config_with(harness, tmp_path: Path, **session_keys: str) -> Path:
    """A copy of the harness config with extra [session] keys, for testing the
    config tier of the precedence chain."""
    original = harness["config"].read_text()
    extra = "".join(f'{key} = "{value}"\n' for key, value in session_keys.items())
    patched = original.replace("loop_threshold = 0.9\n", f"loop_threshold = 0.9\n{extra}")
    path = tmp_path / "patched.toml"
    path.write_text(patched)
    return path


def authors(repo: Path) -> list[str]:
    return git("log", "--format=%an <%ae>", cwd=repo).splitlines()


def subjects(repo: Path) -> list[str]:
    return git("log", "--format=%s", cwd=repo).splitlines()


class TestCommitAttribution:
    """T5: agent-driven attributes the agent's own commits and injects none."""

    def test_agent_driven_attributes_agent_commits_and_broker_injects_none(self, duet, source):
        proc = duet(
            "run", "--repo", str(source), "--isolate", "snapshot", "--commit-mode", "agent-driven", "t",
            env={"AGENT_COMMITS": "1"},
        )
        assert "Outcome: success" in proc.stdout, proc.stdout + proc.stderr
        workspace = Path(_workspace_of(proc.stdout))

        assert "Claude <claude@duet.local>" in authors(workspace)
        assert "feat: precise message 1" in subjects(workspace), "the agent's own message must survive"
        assert not any(s.endswith(" turn") for s in subjects(workspace)), "Broker injected a commit"
        assert "Duet Broker" not in "\n".join(git("log", "--format=%cn", cwd=workspace).splitlines())
        assert "agent made 1 commit" in _transcript_of(proc.stdout)

    def test_agent_identity_reaches_the_agent_subprocess_env(self, duet, source):
        duet(
            "run", "--repo", str(source), "--isolate", "snapshot", "--commit-mode", "agent-driven", "t",
            env={"AGENT_COMMITS": "1"},
        )
        assert "GIT_AUTHOR_NAME=Claude" in (duet.state / "env.log").read_text()

    def test_default_commit_mode_still_injects_a_broker_commit(self, duet, source):
        proc = duet("run", "--repo", str(source), "--isolate", "snapshot", "t")
        assert "Outcome: success" in proc.stdout, proc.stdout + proc.stderr
        workspace = Path(_workspace_of(proc.stdout))

        assert "Claude turn" in subjects(workspace), "default mode must keep committing per turn"
        assert git("log", "--format=%cn", "-1", cwd=workspace) == "Duet Broker"
        assert "committed workspace changes" in _transcript_of(proc.stdout)

    def test_agent_driven_reports_a_dirty_tree_instead_of_hiding_it(self, duet, source):
        # AGENT_COMMITS unset: the agent edits but never commits.
        proc = duet(
            "run", "--repo", str(source), "--isolate", "snapshot", "--commit-mode", "agent-driven", "t"
        )
        assert "left uncommitted changes and made no commit" in _transcript_of(proc.stdout)

    def test_env_var_selects_commit_mode(self, duet, source):
        proc = duet(
            "run", "--repo", str(source), "--isolate", "snapshot", "t",
            env={"AGENT_COMMITS": "1", "DUET_COMMIT_MODE": "agent-driven"},
        )
        workspace = Path(_workspace_of(proc.stdout))
        assert not any(s.endswith(" turn") for s in subjects(workspace))

    def test_cli_flag_beats_env_var(self, duet, source):
        proc = duet(
            "run", "--repo", str(source), "--isolate", "snapshot", "--commit-mode", "default", "t",
            env={"DUET_COMMIT_MODE": "agent-driven"},
        )
        workspace = Path(_workspace_of(proc.stdout))
        assert "Claude turn" in subjects(workspace)


class TestIsolateThroughCli:
    """T1 contrast: bare --repo is still in-place; snapshot never touches the source."""

    def test_bare_repo_is_in_place_and_cuts_a_branch_in_the_source(self, duet, source):
        proc = duet("run", "--repo", str(source), "t")
        assert "Outcome: success" in proc.stdout
        branches = git("branch", "--list", "duet/session-*", cwd=source)
        assert branches.strip(), "bare --repo must still cut a branch in the real repo"

    def test_snapshot_leaves_the_source_repo_completely_untouched(self, duet, source):
        head_before, refs_before = git("rev-parse", "HEAD", cwd=source), refs(source)
        proc = duet("run", "--repo", str(source), "--isolate", "snapshot", "t")
        assert "Outcome: success" in proc.stdout
        assert "will not be modified" in proc.stdout

        assert git("rev-parse", "HEAD", cwd=source) == head_before
        assert refs(source) == refs_before
        assert git("branch", "--list", "duet/session-*", cwd=source).strip() == ""
        assert not (source / "agent_file.txt").exists(), "agent output leaked into the source repo"

    def test_env_var_selects_isolate_mode(self, duet, source):
        proc = duet("run", "--repo", str(source), "t", env={"DUET_ISOLATE": "snapshot"})
        assert "snapshot of" in proc.stdout
        assert git("branch", "--list", "duet/session-*", cwd=source).strip() == ""

    def test_resume_stays_in_place_even_with_isolate_env_set(self, duet, source):
        # resume reattaches real agent CLI sessions; a replica is a directory they
        # have never seen, so DUET_ISOLATE must not divert it.
        duet("run", "--repo", str(source), "t")
        proc = duet("resume", "--repo", str(source), env={"DUET_ISOLATE": "snapshot"})
        assert "Resuming duet" in proc.stdout, proc.stdout + proc.stderr
        assert "snapshot of" not in proc.stdout
        assert _workspace_of(proc.stdout) == str(source.resolve())

    def test_isolate_without_repo_is_rejected(self, duet):
        proc = duet("run", "--isolate", "snapshot", "t")
        assert proc.returncode == 1
        assert "only applies with --repo" in proc.stderr

    def test_scratch_run_unaffected_by_isolate_config_default(self, duet, tmp_path):
        # A project-wide `isolate = "snapshot"` must not make from-scratch runs
        # fail: config is the weakest tier, and a scratch workspace is already
        # isolated. Contrast with the flag/env case, which is a hard error.
        config = config_with(duet.harness, tmp_path, isolate="snapshot")
        proc = duet("run", "t", config=config)
        assert "Outcome: success" in proc.stdout, proc.stdout + proc.stderr

    def test_isolate_config_default_still_applies_with_repo(self, duet, tmp_path, source):
        config = config_with(duet.harness, tmp_path, isolate="snapshot")
        proc = duet("run", "--repo", str(source), "t", config=config)
        assert "snapshot of" in proc.stdout, proc.stdout + proc.stderr
        assert git("branch", "--list", "duet/session-*", cwd=source).strip() == ""

    def test_cli_flag_beats_isolate_config_default(self, duet, tmp_path, source):
        config = config_with(duet.harness, tmp_path, isolate="snapshot")
        proc = duet("run", "--repo", str(source), "--isolate", "none", "t", config=config)
        assert "snapshot of" not in proc.stdout
        assert git("branch", "--list", "duet/session-*", cwd=source).strip(), "in-place branch missing"

    def test_worktree_flag_is_an_alias_for_isolate_worktree(self, duet, source):
        proc = duet("run", "--repo", str(source), "--worktree", "t")
        assert "worktree of" in proc.stdout

    def test_worktree_flag_conflicting_with_isolate_is_rejected(self, duet, source):
        proc = duet("run", "--repo", str(source), "--worktree", "--isolate", "snapshot", "t")
        assert proc.returncode == 1
        assert "conflicts with" in proc.stderr

    def test_base_and_branch_through_the_cli(self, duet, source):
        proc = duet(
            "run", "--repo", str(source), "--isolate", "snapshot", "--base", "main", "--branch", "fix/cli", "t"
        )
        workspace = Path(_workspace_of(proc.stdout))
        assert git("symbolic-ref", "--short", "HEAD", cwd=workspace) == "fix/cli"


class TestReplLaunch:
    """The REPL launch accepts the same repo/isolation options as `run`."""

    def test_repl_starts_against_a_snapshot_of_the_repo(self, duet, source):
        head_before, refs_before = git("rev-parse", "HEAD", cwd=source), refs(source)
        # A non-empty argv with no subcommand lands in the interactive REPL.
        proc = duet("--repo", str(source), "--isolate", "snapshot", stdin="/quit\n")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "isolate=snapshot" in proc.stdout
        assert "will not be modified" in proc.stdout

        assert git("rev-parse", "HEAD", cwd=source) == head_before
        assert refs(source) == refs_before

    def test_repl_workspace_is_the_replica_not_the_source(self, duet, source):
        proc = duet("--repo", str(source), "--isolate", "snapshot", stdin="/workspace\n/quit\n")
        workspace = Path(_line_after(proc.stdout, "Repo session: ").split(" (", 1)[0])
        assert workspace.name == source.name
        assert workspace.resolve() != source.resolve(), "REPL ran in the source repo"
        assert "duet-snap-" in str(workspace)
        assert (workspace / ".git").exists() and (workspace / ".env").exists()

    def test_repl_rejects_isolate_without_repo(self, duet):
        proc = duet("--isolate", "snapshot", stdin="/quit\n")
        assert proc.returncode == 1
        assert "only applies with --repo" in proc.stderr

    def test_repl_carry_brings_untracked_files_into_a_worktree(self, duet, source):
        proc = duet("--repo", str(source), "--worktree", "--carry", ".env", stdin="/workspace\n/quit\n")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        workspace = Path(_line_after(proc.stdout, "Repo session: ").split(" (", 1)[0])
        assert (workspace / ".env").read_text() == "SECRET=hunter2\n"


def _workspace_of(stdout: str) -> str:
    return _line_after(stdout, "Workspace: ")


def _transcript_of(stdout: str) -> str:
    """Per-turn Duet notes live in the transcript, not on the terminal: `on_turn`
    prints the agent's own text. Read them where they actually land."""
    path = Path(_line_after(stdout, "Transcript JSON: "))
    return "\n".join(message["content"] for message in json.loads(path.read_text())["messages"])


def _line_after(stdout: str, prefix: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line.split(prefix, 1)[1].strip()
    raise AssertionError(f"no {prefix!r} line in output:\n{stdout}")
