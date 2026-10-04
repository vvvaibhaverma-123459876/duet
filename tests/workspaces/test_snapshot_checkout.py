"""Checkout conversions must not look like edits to protected inputs.

Use real Git checkouts and attributes, with no provider calls. Snapshot
identity remains the exact content seen by verification and review.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from duet.runtime.artifacts import ArtifactStore
from duet.verification.acceptance import CheckSpec
from duet.verification.runner import run_check_on_snapshot
from duet.workspaces.snapshots import FileEntry, Snapshot, capture_snapshot, materialize, protected_changes


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def checkout(repo: Path) -> None:
    # checkout-index may retain stat-clean files despite --force, so remove
    # these fixture files to actually exercise Git's checkout conversion.
    for name in git(repo, "ls-files").splitlines():
        (repo / name).unlink()
    git(repo, "checkout-index", "--force", "--all")


@pytest.fixture
def repository(tmp_path):
    def create(files: dict[str, bytes], *, attributes: str = ""):
        root = tmp_path / "repo"
        root.mkdir()
        git(root, "init", "-q")
        for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("core.autocrlf", "false"), ("core.eol", "lf")):
            git(root, "config", key, value)
        if attributes:
            files = {".gitattributes": attributes.encode(), **files}
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        git(root, "add", "-A")
        git(root, "commit", "-qm", "base")
        return root, git(root, "rev-parse", "HEAD")
    return create


@pytest.mark.parametrize("autocrlf,attributes,eol,crlf", [
    ("true", "", "lf", True),
    ("input", "", "crlf", False),
    ("false", "", "crlf", False),
    ("false", "*.py text eol=crlf\n", "lf", True),
    ("true", "*.py -text\n", "crlf", False),
    ("true", "*.py text eol=lf\n", "crlf", False),
    ("false", "*.py text\n", "crlf", True),
    ("false", "*.py text=auto\n", "crlf", True),
    ("true", "*.py crlf=input\n", "crlf", False),
])
def test_clean_git_checkout_is_not_a_protected_edit(repository, autocrlf, attributes, eol, crlf):
    original = b"assert 2 + 2 == 4\n"
    root, base = repository({"check.py": original}, attributes=attributes)
    git(root, "config", "core.autocrlf", autocrlf)
    git(root, "config", "core.eol", eol)
    checkout(root)
    raw = (root / "check.py").read_bytes()
    assert raw == (original.replace(b"\n", b"\r\n") if crlf else original)
    snapshot = capture_snapshot(root, base_sha=base)
    assert snapshot.changed == ()
    assert protected_changes(snapshot, root, base, ["check.py"]) == []
    assert snapshot.entry("check.py").sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert snapshot.entry("check.py").size == len(raw)


def test_raw_snapshot_identity_and_stored_bytes_survive_checkout_comparison(repository, tmp_path):
    root, base = repository({"check.py": b"assert True\n"})
    git(root, "config", "core.autocrlf", "true")
    original = capture_snapshot(root, base_sha=base)
    checkout(root)
    store = ArtifactStore(tmp_path / "artifacts")
    snapshot = capture_snapshot(root, base_sha=base, store=store)
    assert original.tree_hash != snapshot.tree_hash
    assert original.changed == snapshot.changed == ()
    assert store.get_bytes(snapshot.blobs["check.py"]) == b"assert True\r\n"
    view = materialize(snapshot.manifest(), store, tmp_path / "view")
    assert (view / "check.py").read_bytes() == b"assert True\r\n"
    # Checking the old snapshot must not read new bytes from the live tree.
    (root / "check.py").write_bytes(b"pass\r\n")
    assert protected_changes(snapshot, root, base, ["check.py"]) == []
    changed = capture_snapshot(root, base_sha=base)
    assert changed.changed == ("check.py",)
    assert protected_changes(changed, root, base, ["check.py"]) == ["check.py"]
    (root / "check.py").write_bytes(b"assert True\r\n")
    assert protected_changes(changed, root, base, ["check.py"]) == ["check.py"]


@pytest.mark.parametrize("staged", [False, True])
def test_edited_attributes_cannot_hide_a_protected_binary_change(repository, staged):
    root, base = repository({"check.dat": b"critical bytes\n"}, attributes="*.dat -text\n")
    (root / ".gitattributes").write_bytes(b"*.dat text eol=crlf\n")
    if staged:
        git(root, "add", ".gitattributes")
    (root / "check.dat").write_bytes(b"critical bytes\r\n")
    index_before = git(root, "ls-files", "--stage")
    snapshot = capture_snapshot(root, base_sha=base)
    assert protected_changes(snapshot, root, base, ["check.dat"]) == ["check.dat"]
    assert git(root, "ls-files", "--stage") == index_before


def test_snapshot_comparison_never_executes_external_filters(repository, tmp_path):
    root, base = repository({"check.py": b"assert True\n"}, attributes="*.py filter=spy text eol=crlf\n")
    marker = tmp_path / "filter-ran"
    command = f'echo invoked > "{marker.as_posix()}"; cat'
    git(root, "config", "filter.spy.clean", command)
    git(root, "config", "filter.spy.smudge", command)
    (root / "check.py").write_bytes(b"assert True\r\n")
    snapshot = capture_snapshot(root, base_sha=base)
    assert protected_changes(snapshot, root, base, ["check.py"]) == []
    assert not marker.exists()


@pytest.mark.parametrize("content", [
    b"binary\0line\n", b"\x01\x02\n", b"existing\r\nplus lf\n", b"bare\rthen lf\n",
])
def test_auto_conversion_keeps_binary_and_existing_cr_bytes_exact(repository, content):
    root, base = repository({"check.dat": content})
    git(root, "config", "core.autocrlf", "true")
    checkout(root)
    assert (root / "check.dat").read_bytes() == content
    assert capture_snapshot(root, base_sha=base).changed == ()
    (root / "check.dat").write_bytes(content.replace(b"\n", b"\r\n"))
    snapshot = capture_snapshot(root, base_sha=base)
    assert protected_changes(snapshot, root, base, ["check.dat"]) == ["check.dat"]


def test_crlf_conversion_is_correct_across_stream_boundaries(repository, monkeypatch):
    # Small chunks put a CR at a chunk boundary and DOS EOF in the final chunk.
    root, base = repository({"check.py": b"abcd\r\nefgh\nijk\n\x1a"})
    # Commit an explicit text policy after the mixed blob, preserving its bytes.
    (root / ".gitattributes").write_bytes(b"*.py text eol=crlf\n")
    git(root, "add", ".gitattributes")
    git(root, "commit", "-qm", "checkout policy")
    base = git(root, "rev-parse", "HEAD")
    checkout(root)
    monkeypatch.setattr("duet.workspaces.snapshots.CHUNK", 5)
    assert capture_snapshot(root, base_sha=base).changed == ()


def test_deleted_and_added_protected_paths_still_count(repository):
    root, base = repository({"checks/old.py": b"assert True\n"})
    git(root, "config", "core.autocrlf", "true")
    (root / "checks/old.py").unlink()
    (root / "checks/new.py").write_bytes(b"assert True\r\n")
    snapshot = capture_snapshot(root, base_sha=base)
    assert protected_changes(snapshot, root, base, ["checks"]) == ["checks/new.py", "checks/old.py"]


def test_symlink_blobs_never_receive_text_conversion(repository):
    # Create the Git link entry directly so this also runs on Windows hosts
    # without permission to create OS symlinks. A link target is exact bytes.
    root, _ = repository({"link": b"target\n"}, attributes="* text eol=crlf\n")
    oid = git(root, "hash-object", "-w", "--no-filters", "link")
    git(root, "update-index", "--cacheinfo", f"120000,{oid},link")
    git(root, "commit", "-qm", "link")
    base = git(root, "rev-parse", "HEAD")
    for content, expected in ((b"target\n", []), (b"target\r\n", ["link"])):
        entry = FileEntry("link", "120000", "sha256:" + hashlib.sha256(content).hexdigest(), len(content))
        snapshot = Snapshot("unused", base, (entry,))
        assert protected_changes(snapshot, root, base, ["link"]) == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows uses index modes because it has no executable bits")
def test_filemode_false_preserves_index_mode_and_verification_copy(repository, tmp_path):
    root, _ = repository({"run.py": b"print('ok')\n"})
    git(root, "config", "core.filemode", "false")
    git(root, "update-index", "--chmod=+x", "run.py")
    git(root, "commit", "-qm", "executable")
    base = git(root, "rev-parse", "HEAD")
    store = ArtifactStore(tmp_path / "artifacts")
    snapshot = capture_snapshot(root, base_sha=base, store=store)
    assert snapshot.entry("run.py").mode == "100755"
    assert snapshot.changed == ()
    outcome = run_check_on_snapshot(CheckSpec("mode", argv=(sys.executable, "run.py")), snapshot.manifest(), store,
                                    tmp_path / "checks", expected_tree_hash=snapshot.tree_hash)
    assert outcome.status == "passed", outcome.detail
    git(root, "update-index", "--chmod=-x", "run.py")
    changed = capture_snapshot(root, base_sha=base)
    assert protected_changes(changed, root, base, ["run.py"]) == ["run.py"]


@pytest.mark.skipif(os.name == "nt", reason="requires native executable bits")
@pytest.mark.parametrize("filemode", ["true", "false"])
def test_posix_snapshot_keeps_actual_filesystem_mode_changes(repository, filemode):
    root, base = repository({"run.py": b"print('ok')\n"})
    git(root, "config", "core.filemode", filemode)
    os.chmod(root / "run.py", 0o755)
    snapshot = capture_snapshot(root, base_sha=base)
    assert snapshot.entry("run.py").mode == "100755"
    assert protected_changes(snapshot, root, base, ["run.py"]) == ["run.py"]
