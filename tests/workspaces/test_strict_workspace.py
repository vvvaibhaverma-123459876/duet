"""D03: strict workspaces never touch the user's checkout, and snapshots
never sweep in secrets, ignored files or escaping symlinks."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from duet.runtime.api import Runtime
from duet.runtime.artifacts import ArtifactStore
from duet.runtime.contracts import CONTROLLER, USER, Conflict, StaleLease, ValidationError
from duet.runtime.policy import AuthorisationPolicy
from duet.runtime.store import Store
from duet.workspaces.manager import WorkspaceManager
from duet.workspaces.repo import resolve_repo
from duet.workspaces.snapshots import capture_snapshot, is_sensitive, materialize, protected_changes


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def make_user_repo(path: Path) -> Path:
    path.mkdir()
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "u@example.invalid", cwd=path)
    git("config", "user.name", "User", cwd=path)
    (path / ".gitignore").write_text(".env\nbuild/\n")
    (path / "app.py").write_text("print('v1')\n")
    (path / "tests").mkdir()
    (path / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n")
    git("add", "-A", cwd=path)
    git("commit", "-qm", "base", cwd=path)
    # The user's live, dirty state: a tracked edit, untracked notes, secrets, build output.
    (path / "app.py").write_text("print('work in progress')\n")
    (path / "notes.txt").write_text("my untracked notes\n")
    (path / ".env").write_text("API_KEY=super-secret\n")
    (path / "build").mkdir()
    (path / "build" / "out.bin").write_bytes(b"\x00" * 64)
    return path


def user_state(repo: Path) -> dict:
    return {
        "status": git("status", "--porcelain=v2", "--branch", "--untracked-files=all", "--ignored", cwd=repo),
        "head": git("rev-parse", "HEAD", cwd=repo),
        "branch": git("symbolic-ref", "--short", "HEAD", cwd=repo),
        "index": git("ls-files", "-s", cwd=repo),
        "files": {p: (repo / p).read_bytes() for p in ("app.py", "notes.txt", ".env", "build/out.bin")},
        "stash": git("stash", "list", cwd=repo),
    }


@pytest.fixture()
def env(tmp_path):
    rt = Runtime(Store(tmp_path / "state" / "rt.db"))
    manager = WorkspaceManager(rt, root=tmp_path / "state" / "worktrees")
    repo = make_user_repo(tmp_path / "user-repo")
    run = rt.create_run(USER, repo_id=resolve_repo(repo).repo_id, objective="o", policy=AuthorisationPolicy(), acceptance={})
    return rt, manager, repo, run


class TestRepoIdentity:
    def test_worktrees_share_identity_clones_do_not(self, tmp_path):
        repo = make_user_repo(tmp_path / "a")
        wt = tmp_path / "wt"
        git("worktree", "add", "-q", str(wt), "HEAD", cwd=repo)
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
        ident = resolve_repo(repo)
        assert resolve_repo(wt).repo_id == ident.repo_id
        assert resolve_repo(wt).is_linked_worktree and not ident.is_linked_worktree
        assert resolve_repo(clone).repo_id != ident.repo_id  # same origin, different repository

    def test_non_repo_rejected(self, tmp_path):
        with pytest.raises(ValidationError):
            resolve_repo(tmp_path)


class TestStrictWorkspace:
    def test_user_checkout_is_untouched(self, env):  # D03 exit: active checkout unchanged
        rt, manager, repo, run = env
        before = user_state(repo)
        ws = manager.create(run["run_id"], repo)
        (ws.path / "app.py").write_text("print('agent change')\n")
        git("add", "-A", cwd=ws.path)
        git("-c", "user.email=a@a", "-c", "user.name=a", "commit", "-qm", "agent", cwd=ws.path)
        assert user_state(repo) == before
        assert ws.branch.startswith("duet/run-")
        assert git("symbolic-ref", "--short", "HEAD", cwd=ws.path).strip() == ws.branch
        # Nothing untracked or ignored was carried over from the user's tree.
        assert not (ws.path / ".env").exists()
        assert not (ws.path / "notes.txt").exists()
        assert not (ws.path / "build").exists()
        # The worktree lives in Duet's state dir, not in the user's repository.
        assert repo not in ws.path.parents

    def test_existing_branch_is_never_reused(self, env):
        rt, manager, repo, run = env
        git("branch", "taken", cwd=repo)
        with pytest.raises(ValidationError, match="already exists"):
            manager.create(run["run_id"], repo, branch="taken")

    def test_single_writer_lease(self, env):
        rt, manager, repo, run = env
        ws = manager.create(run["run_id"], repo)
        lease = manager.acquire_writer(ws, "prt_a")
        with pytest.raises(Conflict):
            manager.acquire_writer(ws, "prt_b")
        manager.check_writer(ws, "prt_a", lease["fencing_token"])
        with pytest.raises(StaleLease):
            manager.check_writer(ws, "prt_b", lease["fencing_token"])
        manager.release_writer(ws, lease["fencing_token"])
        second = manager.acquire_writer(ws, "prt_b")
        with pytest.raises(StaleLease):
            manager.check_writer(ws, "prt_a", lease["fencing_token"])  # stale writer fenced out
        assert second["fencing_token"] == lease["fencing_token"] + 1

    def test_import_inputs_is_explicit_and_checked(self, env, tmp_path):  # AT26/AT27
        rt, manager, repo, run = env
        (repo / "link_out").symlink_to(tmp_path)  # escapes the repository
        (repo / "link_in").symlink_to("app.py")
        ws = manager.create(run["run_id"], repo)
        report = manager.import_inputs(ws, repo, ["app.py", "notes.txt", ".env", "../escape", ".git/config", "link_out", "link_in", "missing.txt"])
        assert set(report.imported) == {"app.py", "notes.txt", "link_in"}
        reasons = dict(report.refused)
        assert "secret" in reasons[".env"]
        assert "inside" in reasons["../escape"]
        assert "git internals" in reasons[".git/config"]
        assert "escapes" in reasons["link_out"]
        assert "does not exist" in reasons["missing.txt"]
        assert (ws.path / "app.py").read_text() == "print('work in progress')\n"
        assert not (ws.path / ".env").exists()
        # With explicit user approval the secret may be imported.
        assert manager.import_inputs(ws, repo, [".env"], allow_sensitive=True).imported == (".env",)


class TestSnapshots:
    def test_inputs_exclude_ignored_sensitive_and_escaping_links(self, env, tmp_path):
        rt, manager, repo, run = env
        ws = manager.create(run["run_id"], repo)
        (ws.path / "new_feature.py").write_text("def f(): return 1\n")  # agent-created: an input
        (ws.path / ".env.local").write_text("TOKEN=x\n")  # untracked secret: excluded
        (ws.path / ".env").write_text("TOKEN=y\n")  # ignored: never included
        (ws.path / "escape").symlink_to(tmp_path)
        (ws.path / "__pycache__").mkdir()
        (ws.path / "__pycache__" / "x.pyc").write_bytes(b"cache")
        snap = capture_snapshot(ws.path, base_sha=ws.base_sha)
        paths = {f.path for f in snap.files}
        assert {"app.py", "tests/test_app.py", "new_feature.py", ".gitignore"} <= paths
        assert not {".env", ".env.local", "escape", "__pycache__/x.pyc"} & paths
        excluded = {e["path"]: e["reason"] for e in snap.excluded}
        assert "sensitive" in excluded[".env.local"] and "escapes" in excluded["escape"]
        assert snap.changed == ("new_feature.py",)

    def test_explicit_ignored_secret_still_needs_approval(self, env):
        rt, manager, repo, run = env
        ws = manager.create(run["run_id"], repo)
        (ws.path / ".env").write_text("TOKEN=y\n")
        assert ".env" not in {f.path for f in capture_snapshot(ws.path, include=[".env"]).files}
        assert ".env" in {f.path for f in capture_snapshot(ws.path, include=[".env"], allow_sensitive=True).files}

    def test_tree_hash_tracks_content_mode_and_membership(self, env):
        rt, manager, repo, run = env
        ws = manager.create(run["run_id"], repo)
        a = capture_snapshot(ws.path)
        assert capture_snapshot(ws.path).tree_hash == a.tree_hash
        (ws.path / "app.py").write_text("print('changed')\n")
        b = capture_snapshot(ws.path)
        assert b.tree_hash != a.tree_hash
        os.chmod(ws.path / "app.py", 0o755)
        assert capture_snapshot(ws.path).tree_hash != b.tree_hash

    def test_materialized_snapshot_is_immutable_copy(self, env, tmp_path):
        rt, manager, repo, run = env
        ws = manager.create(run["run_id"], repo)
        store = ArtifactStore(tmp_path / "artifacts")
        snap = capture_snapshot(ws.path, store=store)
        (ws.path / "app.py").write_text("print('later edit')\n")
        view = materialize(snap.manifest(), store, tmp_path / "review")
        assert (view / "app.py").read_text() == "print('v1')\n"
        assert os.stat(view / "app.py").st_mode & 0o222 == 0  # read-only for everyone
        if hasattr(os, "geteuid") and os.geteuid() != 0:  # root ignores file modes
            with pytest.raises(PermissionError):
                (view / "app.py").write_text("tamper")

    def test_protected_paths_detect_weakened_tests(self, env):  # AT24 input
        rt, manager, repo, run = env
        ws = manager.create(run["run_id"], repo)
        (ws.path / "tests" / "test_app.py").write_text("def test_ok():\n    pass  # assertion removed\n")
        snap = capture_snapshot(ws.path)
        assert protected_changes(snap, ws.path, ws.base_sha, ["tests/*"]) == ["tests/test_app.py"]
        (ws.path / "tests" / "test_app.py").unlink()
        assert protected_changes(capture_snapshot(ws.path), ws.path, ws.base_sha, ["tests/*"]) == ["tests/test_app.py"]

    @pytest.mark.parametrize(
        "path,sensitive",
        [(".env", True), ("config/.env.production", True), (".env.example", False), ("server.pem", True), ("id_ed25519", True),
         (".ssh/config", True), ("src/keys.py", False), ("credentials.json", True), ("docs/credentials.md", True), ("app.py", False)],
    )
    def test_sensitive_classifier(self, path, sensitive):
        assert is_sensitive(path) is sensitive
