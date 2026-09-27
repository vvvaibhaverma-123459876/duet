"""Immutable input snapshots.

A snapshot is the exact set of verification inputs in a workspace: tracked
files as they are in the working tree, plus untracked files that pass the
input policy. Each file is hashed; the tree hash identifies the snapshot, and
verification evidence and review approvals are keyed to it (R10).

Input policy (strict mode, spec 9.1/9.3):
- ignored files are never included unless explicitly requested;
- untracked files that look sensitive (.env, keys, credentials, ...) are
  excluded and reported, never silently swept in;
- symlinks are recorded as links and never followed; a link that resolves
  outside the workspace is excluded;
- transient outputs (__pycache__, .pytest_cache, ...) are not inputs, so
  checks that write caches do not invalidate their own evidence."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..runtime.artifacts import ArtifactStore
from ..runtime.contracts import ValidationError, content_hash

SENSITIVE_NAME_PATTERNS = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "id_rsa*", "id_dsa*",
    "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc", ".git-credentials", "credentials", "credentials.*",
    "*.kdbx", "secrets.*", "*.secret", "*.secrets", "service-account*.json", ".htpasswd", "*.tfstate", "*.tfvars",
    "*.tfstate.*", ".envrc", ".pgpass", ".vault-token", "kubeconfig", "*.kubeconfig", ".dockercfg", ".boto", ".s3cfg",
)
SENSITIVE_DIRS = (".ssh", ".aws", ".gnupg", ".docker", ".kube", ".azure", ".config/gcloud")
TRANSIENT_PATTERNS = (
    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".coverage", ".coverage.*",
    "htmlcov", ".duet", ".tox", ".nox", ".hypothesis", ".DS_Store",
)
DEFAULT_MAX_BLOB_BYTES = 20 * 1024 * 1024
CHUNK = 1024 * 1024


@dataclass(frozen=True)
class FileEntry:
    path: str
    mode: str  # 100644 | 100755 | 120000 | 160000
    sha256: str
    size: int

    def key(self) -> list:
        return [self.path, self.mode, self.sha256]


@dataclass(frozen=True)
class Snapshot:
    tree_hash: str
    base_sha: str | None
    files: tuple[FileEntry, ...]
    excluded: tuple[dict, ...] = ()
    changed: tuple[str, ...] = ()
    manifest_ref: str | None = None
    blobs: dict = field(default_factory=dict, compare=False)  # path -> artifact ref

    def entry(self, path: str) -> FileEntry | None:
        for item in self.files:
            if item.path == path:
                return item
        return None

    def manifest(self) -> dict:
        return {
            "tree_hash": self.tree_hash,
            "base_sha": self.base_sha,
            "files": [{"path": f.path, "mode": f.mode, "sha256": f.sha256, "size": f.size, "blob": self.blobs.get(f.path)} for f in self.files],
            "excluded": list(self.excluded),
            "changed": list(self.changed),
        }


def is_sensitive(relpath: str) -> bool:
    posix = PurePosixPath(relpath)
    name = posix.name
    if any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_NAME_PATTERNS):
        # Templates committed on purpose are not secrets.
        return not any(name.endswith(suffix) for suffix in (".example", ".sample", ".template", ".dist"))
    text = str(posix)
    return any(text == d or text.startswith(d + "/") or f"/{d}/" in f"/{text}" for d in SENSITIVE_DIRS)


def is_transient(relpath: str) -> bool:
    return any(fnmatch.fnmatch(part, pattern) for part in PurePosixPath(relpath).parts for pattern in TRANSIENT_PATTERNS)


def _git_z(args: list[str], cwd: Path) -> list[str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if proc.returncode != 0:
        raise ValidationError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return [p.decode("utf-8", errors="surrogateescape") for p in proc.stdout.split(b"\0") if p]


def _index_modes(workspace: Path) -> dict[str, tuple[str, str]]:
    """path -> (mode, object id) from the index, to recognise submodules."""
    modes = {}
    for record in _git_z(["ls-files", "-z", "--stage"], workspace):
        meta, _, path = record.partition("\t")
        parts = meta.split()
        if len(parts) >= 2:
            modes[path] = (parts[0], parts[1])
    return modes


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return "sha256:" + digest.hexdigest(), size


def capture_snapshot(
    workspace: Path | str,
    *,
    base_sha: str | None = None,
    untracked: str = "include",
    include: tuple[str, ...] | list[str] = (),
    allow_sensitive: bool = False,
    store: ArtifactStore | None = None,
    max_blob_bytes: int = DEFAULT_MAX_BLOB_BYTES,
) -> Snapshot:
    """Capture the verification inputs of `workspace`.

    untracked: "include" (untracked, non-ignored files pass the input policy),
    or "explicit" (only paths listed in `include`). `include` may also name
    ignored files; that is an explicit decision, still subject to the
    sensitive-file policy unless `allow_sensitive` (a user approval)."""
    root = Path(workspace).resolve()
    if untracked not in ("include", "explicit"):
        raise ValidationError("untracked must be 'include' or 'explicit'")
    index = _index_modes(root)
    tracked = set(index)
    deleted = set(_git_z(["ls-files", "-z", "--deleted"], root))
    candidates: dict[str, str] = {path: "tracked" for path in tracked - deleted}
    others = _git_z(["ls-files", "-z", "--others", "--exclude-standard"], root)
    requested = {str(PurePosixPath(p)) for p in include}
    for path in others:
        if untracked == "include" or path in requested:
            candidates.setdefault(path, "untracked")
    for path in requested - set(candidates):
        full = root / path
        if not _inside(root, full):
            raise ValidationError(f"requested input {path!r} is outside the workspace")
        if full.exists() or full.is_symlink():
            candidates[path] = "requested"

    files: list[FileEntry] = []
    excluded: list[dict] = []
    blobs: dict[str, str] = {}
    for path in sorted(candidates):
        origin = candidates[path]
        if is_transient(path):
            continue
        if origin != "tracked" and is_sensitive(path) and not allow_sensitive:
            excluded.append({"path": path, "reason": "sensitive file not included without approval"})
            continue
        full = root / path
        try:
            info = os.lstat(full)
        except FileNotFoundError:
            continue
        index_mode = index.get(path, ("", ""))[0]
        if index_mode == "160000":
            files.append(FileEntry(path, "160000", "git:" + index[path][1], 0))
            continue
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(full)
            resolved = (full.parent / target).resolve()
            if not _inside(root, resolved):
                excluded.append({"path": path, "reason": f"symlink escapes the workspace ({target})"})
                continue
            digest = "sha256:" + hashlib.sha256(target.encode("utf-8", errors="surrogateescape")).hexdigest()
            files.append(FileEntry(path, "120000", digest, len(target)))
            if store is not None:
                # Keep the link text so a materialised copy can recreate it.
                blobs[path] = store.put_bytes(target.encode("utf-8", errors="surrogateescape"))
            continue
        if stat.S_ISDIR(info.st_mode):
            continue  # an untracked directory entry (nested repo); its files are listed individually
        if not stat.S_ISREG(info.st_mode):
            excluded.append({"path": path, "reason": "not a regular file"})
            continue
        digest, size = _hash_file(full)
        mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
        files.append(FileEntry(path, mode, digest, size))
        if store is not None and size <= max_blob_bytes:
            blobs[path] = store.put_file(full)
    tree_hash = content_hash([entry.key() for entry in files])
    changed = _changed_paths(root, base_sha, files) if base_sha else ()
    snapshot = Snapshot(tree_hash, base_sha, tuple(files), tuple(excluded), tuple(changed), None, blobs)
    if store is not None:
        ref = store.put_text(json.dumps(snapshot.manifest(), sort_keys=True))
        snapshot = Snapshot(tree_hash, base_sha, tuple(files), tuple(excluded), tuple(changed), ref, blobs)
    return snapshot


def base_file_hashes(workspace: Path, base_sha: str) -> dict[str, tuple[str, str]]:
    """path -> (mode, sha256) of every file in `base_sha`, hashed the same way
    as snapshot entries (symlinks hash their target, like git) so the two can
    be compared. Streams all blobs through one `git cat-file --batch`."""
    root = Path(workspace)
    entries = []
    result: dict[str, tuple[str, str]] = {}
    for record in _git_z(["ls-tree", "-r", "-z", base_sha], root):
        meta, _, path = record.partition("\t")
        mode, kind, obj = meta.split()
        if kind == "commit":
            result[path] = ("160000", "git:" + obj)
        else:
            entries.append((path, mode, obj))
    if not entries:
        return result
    proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert proc.stdin is not None and proc.stdout is not None
    try:
        for path, mode, obj in entries:
            proc.stdin.write(obj.encode() + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            if len(header) < 3 or header[1] != b"blob":
                raise ValidationError(f"unexpected object for {path} in {base_sha}: {header!r}")
            size = int(header[2])
            digest = hashlib.sha256()
            remaining = size
            while remaining:
                chunk = proc.stdout.read(min(remaining, CHUNK))
                if not chunk:
                    raise ValidationError("git cat-file ended early")
                digest.update(chunk)
                remaining -= len(chunk)
            proc.stdout.read(1)  # trailing newline
            result[path] = (mode, "sha256:" + digest.hexdigest())
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    return result


def _changed_paths(root: Path, base_sha: str, files: list[FileEntry]) -> list[str]:
    base = base_file_hashes(root, base_sha)
    current = {f.path: (f.mode, f.sha256) for f in files}
    changed = {p for p in set(base) | set(current) if base.get(p) != current.get(p) and not is_transient(p)}
    return sorted(changed)


def matches_protected(path: str, pattern: str) -> bool:
    """A protected pattern is a glob (`tests/*`), a file (`check.py`) or a
    directory given with or without a trailing slash (`tests`, `tests/`),
    which covers everything below it."""
    if fnmatch.fnmatch(path, pattern):
        return True
    bare = pattern.rstrip("/")
    return bool(bare) and not any(ch in bare for ch in "*?[") and (path == bare or path.startswith(bare + "/"))


def protected_changes(snapshot: Snapshot, workspace: Path, base_sha: str | None, patterns: tuple[str, ...] | list[str]) -> list[str]:
    """Paths matching `patterns` whose content differs from `base_sha`
    (modified, added or deleted). Used to stop agents from weakening the
    acceptance inputs they are judged by (AT24)."""
    if not patterns:
        return []
    base = base_file_hashes(Path(workspace), base_sha) if base_sha else {}
    current = {f.path: (f.mode, f.sha256) for f in snapshot.files}
    hits = []
    for path in sorted(set(base) | set(current)):
        if any(matches_protected(path, pattern) for pattern in patterns) and base.get(path) != current.get(path):
            hits.append(path)
    return hits


def materialize(manifest: dict, store: ArtifactStore, dest: Path, *, recreate_links: bool = True) -> Path:
    """Write a snapshot's files into `dest` read-only, so a reviewer (or a
    check) sees an immutable copy rather than files changing underneath it.
    In-tree symlinks are recreated when their text was stored; a link that
    would point outside `dest` is refused."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=False)
    links = []
    for item in manifest["files"]:
        rel = PurePosixPath(item["path"])
        if rel.is_absolute() or ".." in rel.parts:
            raise ValidationError(f"unsafe path in manifest: {item['path']!r}")
        target = dest.joinpath(*rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if item["mode"] == "160000":
            continue
        if item["mode"] == "120000":
            if recreate_links and item.get("blob"):
                links.append((target, store.get_bytes(item["blob"]).decode("utf-8", errors="surrogateescape")))
            continue
        if not item.get("blob"):
            raise ValidationError(f"snapshot has no stored content for {item['path']}")
        target.write_bytes(store.get_bytes(item["blob"]))
        os.chmod(target, 0o555 if item["mode"] == "100755" else 0o444)
    for target, text in links:
        if os.path.isabs(text) or not _inside(dest, target.parent / text):
            raise ValidationError(f"snapshot link {target.relative_to(dest)} points outside the snapshot")
        os.symlink(text, target)
    return dest


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
