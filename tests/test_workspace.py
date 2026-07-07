from __future__ import annotations

from pathlib import Path

import pytest

from duet.workspace import WorkspaceError, assert_safe_workspace, commit_after_turn, create_workspace


def test_denylist_refuses_home_root_and_cwd():
    for path in [Path.home(), Path("/"), Path.cwd()]:
        with pytest.raises(WorkspaceError):
            assert_safe_workspace(path)


def test_commit_sets_author(tmp_path):
    workspace = create_workspace(str(tmp_path / "w"))
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    assert commit_after_turn(workspace, "claude", "Claude")
    import subprocess

    proc = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"], cwd=workspace, text=True, capture_output=True, check=True)
    assert proc.stdout.strip() == "Claude <claude@duet.local>"


def test_back_to_back_live_sessions_get_unique_branches(tmp_path):
    import subprocess
    from duet.workspace import prepare_live_repo, release_lock

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"], cwd=tmp_path, check=True
    )
    first = prepare_live_repo(str(tmp_path))
    release_lock(first.workspace)
    second = prepare_live_repo(str(tmp_path))  # same second: must not collide
    release_lock(second.workspace)
    assert first.branch != second.branch
