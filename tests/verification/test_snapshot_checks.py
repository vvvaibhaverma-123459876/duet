"""Review fixes: checks run on a fresh copy of exactly the snapshot, so files
the snapshot excluded (escaping links, ignored files) and edits made while a
check runs cannot influence evidence; the no-tests policy never hides a
failing exit; protected directories and more secret files are recognised."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from duet.runtime.artifacts import ArtifactStore
from duet.verification.acceptance import CheckSpec
from duet.verification.runner import run_check, run_check_on_snapshot
from duet.workspaces.snapshots import capture_snapshot, is_sensitive, matches_protected

PY = sys.executable
MUL = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
CHECK = CheckSpec("feature", argv=(PY, "-c", "import calc; assert calc.mul(3, 4) == 12; print('ok')"))


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "u@example.invalid"], ["config", "user.name", "U"]):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    return root


def snapshot_check(tmp_path, root, spec=CHECK):
    store = ArtifactStore(tmp_path / "artifacts")
    snap = capture_snapshot(root, store=store)
    import json

    manifest = json.loads(store.get_bytes(snap.manifest_ref).decode())
    return snap, run_check_on_snapshot(spec, manifest, store, tmp_path / "scratch", expected_tree_hash=snap.tree_hash)


def test_escaping_symlink_cannot_make_a_check_pass(tmp_path):
    root = repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "calc.py").write_text(MUL)
    (root / "calc.py").unlink()
    os.symlink(outside / "calc.py", root / "calc.py")
    assert run_check(CHECK, root).status == "passed"  # the live tree is fooled
    snap, outcome = snapshot_check(tmp_path, root)
    assert any(e["path"] == "calc.py" for e in snap.excluded)
    assert outcome.status == "failed"  # the snapshot does not contain mul()


def test_ignored_package_cannot_shadow_the_snapshot(tmp_path):
    root = repo(tmp_path)
    (root / ".gitignore").write_text("calc/\n")
    (root / "calc").mkdir()
    (root / "calc" / "__init__.py").write_text(MUL)
    assert run_check(CHECK, root).status == "passed"
    snap, outcome = snapshot_check(tmp_path, root)
    assert snap.entry("calc/__init__.py") is None
    assert outcome.status == "failed"


def test_edits_while_the_check_runs_cannot_reach_it(tmp_path):
    """Modify-then-restore during a check: the live tree looks unchanged
    before and after, but the check read the temporary content."""
    root = repo(tmp_path)
    spec = CheckSpec("slow", argv=(PY, "-c", "import time; time.sleep(0.8); import calc; assert calc.mul(3, 4) == 12"))
    store = ArtifactStore(tmp_path / "artifacts")
    snap = capture_snapshot(root, store=store)
    edited = threading.Event()
    checked = threading.Event()

    def flip():
        (root / "calc.py").write_text(MUL)
        edited.set()
        checked.wait(30)
        (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")

    thread = threading.Thread(target=flip)
    thread.start()
    try:
        # The edit must follow capture and overlap verification. A timer
        # started before capture races with Git subprocess startup on Windows.
        assert edited.wait(10)
        outcome = run_check_on_snapshot(spec, snap.manifest(), store, tmp_path / "scratch", expected_tree_hash=snap.tree_hash)
    finally:
        checked.set()
        thread.join()
    assert outcome.status == "failed" and outcome.snapshot_before == snap.tree_hash


def test_in_tree_links_are_reproduced_and_the_copy_is_removed(tmp_path):
    root = repo(tmp_path)
    (root / "calc.py").write_text(MUL)
    os.symlink("calc.py", root / "alias.py")
    spec = CheckSpec("link", argv=(PY, "-c", "import os, alias; assert alias.mul(2, 5) == 10; print(os.getcwd())"))
    snap, outcome = snapshot_check(tmp_path, root, spec)
    assert outcome.status == "passed", outcome.output
    ran_in = Path(outcome.output.strip().splitlines()[-1])
    assert str(ran_in).startswith(str(tmp_path / "scratch")) and not ran_in.exists()


def test_a_copy_that_does_not_reproduce_the_snapshot_is_not_run(tmp_path):
    root = repo(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    snap = capture_snapshot(root, store=store)
    import json

    manifest = json.loads(store.get_bytes(snap.manifest_ref).decode())
    outcome = run_check_on_snapshot(CHECK, manifest, store, tmp_path / "scratch", expected_tree_hash="sha256:" + "0" * 64)
    assert outcome.status == "unknown" and "does not reproduce" in outcome.detail


def test_no_tests_policy_never_hides_a_failing_exit(tmp_path):
    root = repo(tmp_path)
    go_like = "print('?   \\tpkg/a\\t[no test files]'); print('--- FAIL: TestMul'); raise SystemExit(1)"
    outcome = run_check(CheckSpec("go", argv=(PY, "-c", go_like), no_tests="pass"), root)
    assert outcome.status == "failed"
    empty = run_check(CheckSpec("empty", argv=(PY, "-c", "print('no tests ran')"), no_tests="unknown"), root)
    assert empty.status == "unknown"


def test_protected_directories_with_or_without_slash():
    assert matches_protected("tests/test_calc.py", "tests")
    assert matches_protected("tests/unit/test_x.py", "tests/")
    assert matches_protected("tests/test_calc.py", "tests/*")
    assert not matches_protected("testsuite/x.py", "tests")
    assert matches_protected("check_feature.py", "check_feature.py")


def test_more_secret_files_are_recognised():
    for name in (".envrc", ".pgpass", ".vault-token", "kubeconfig", "prod.tfstate.backup", "cluster.kubeconfig"):
        assert is_sensitive(name), name
    assert not is_sensitive("calc.py")
