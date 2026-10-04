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
import tempfile
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
_GIT_NONPRINTABLE = bytes((*range(0, 8), 11, *range(14, 27), *range(28, 32), 127))


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


def _git_settings(root: Path) -> dict[str, str]:
    proc = subprocess.run(["git", "config", "--null", "--get-regexp", r"^core\.(autocrlf|eol|filemode)$"], cwd=root, capture_output=True)
    if proc.returncode not in (0, 1):
        raise ValidationError(f"could not read Git checkout settings: {proc.stderr.decode(errors='replace').strip()}")
    return dict(record.decode().partition("\n")[::2] for record in proc.stdout.split(b"\0") if record)


def _true(value: str) -> bool:
    return value.lower() in ("", "true", "yes", "on", "1")


def _checkout_crlf(root: Path, base_sha: str, paths: list[str]) -> dict[str, str]:
    """Paths whose base blobs Git may check out as CRLF, and text/auto policy.

    Only the built-in newline conversion is recognised. Attribute lookup
    cannot run clean/smudge filters, and uses an isolated base-commit index:
    editing or staging tracked .gitattributes must not hide protected-file
    edits. Existing user/system/info attributes and Git checkout settings
    remain trusted repository configuration, as elsewhere in Duet.
    No worktree file or real index is changed by these commands.
    """
    settings = _git_settings(root)
    with tempfile.TemporaryDirectory(prefix="duet-base-index-") as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
        env.pop("GIT_ATTR_SOURCE", None)
        for args, data in (
            (["-c", "core.splitIndex=false", "read-tree", base_sha], None),
            (["check-attr", "--cached", "-z", "--stdin", "text", "crlf", "eol"],
             b"".join(path.encode("utf-8", errors="surrogateescape") + b"\0" for path in paths)),
        ):
            proc = subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=root, env=env, input=data, capture_output=True)
            if proc.returncode != 0:
                raise ValidationError(f"could not read base checkout attributes: {proc.stderr.decode(errors='replace').strip()}")
    fields = proc.stdout.split(b"\0")[:-1]
    attrs: dict[str, dict[str, str]] = {}
    for pos in range(0, len(fields), 3):
        path, name, value = (item.decode("utf-8", errors="surrogateescape") for item in fields[pos:pos + 3])
        attrs.setdefault(path, {})[name] = value
    autocrlf = settings.get("core.autocrlf", "false").lower()
    native_crlf = settings.get("core.eol", "native").lower() == "crlf" or (
        settings.get("core.eol", "native").lower() == "native" and os.name == "nt"
    )
    default_crlf = autocrlf != "input" and (_true(autocrlf) or native_crlf)
    policies = {}
    for path, values in attrs.items():
        text = values["text"]
        if text not in ("set", "unset", "auto", "input"):
            text = values["crlf"]
        eol = values["eol"]
        if text == "unset" or eol == "lf":
            continue
        if eol == "crlf":
            policies[path] = "auto" if text == "auto" else "text"
        elif text in ("set", "auto") and default_crlf:
            policies[path] = "auto" if text == "auto" else "text"
        elif text not in ("set", "auto", "input") and _true(autocrlf):
            policies[path] = "auto"
    return policies


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
    trust_filemode = os.name != "nt" or _true(_git_settings(root).get("core.filemode", "true"))
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
        if not trust_filemode and index_mode in ("100644", "100755"):
            mode = index_mode  # Windows cannot represent Git executable bits
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
    return _base_file_versions(Path(workspace), base_sha)[0]


def _base_file_versions(root: Path, base_sha: str, *, checkout: bool = False) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """Raw base hashes plus permitted CRLF checkout hashes, streamed once.

    Snapshot identities always hash exact bytes. Comparison accepts the raw
    base or its Git CRLF checkout form, never a normalised current file. This
    also works for immutable snapshots after the live workspace has changed.
    """
    entries = []
    result: dict[str, tuple[str, str]] = {}
    checkout_hashes: dict[str, str] = {}
    for record in _git_z(["ls-tree", "-r", "-z", base_sha], root):
        meta, _, path = record.partition("\t")
        mode, kind, obj = meta.split()
        if kind == "commit":
            result[path] = ("160000", "git:" + obj)
        else:
            entries.append((path, mode, obj))
    if not entries:
        return result, checkout_hashes
    policies = _checkout_crlf(root, base_sha, [path for path, mode, _ in entries if mode in ("100644", "100755")]) if checkout else {}
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
            policy = policies.get(path)
            crlf_digest = hashlib.sha256()
            pending_cr = b""
            carriage = nul = nonprintable = printable = 0
            last = b""
            remaining = size
            while remaining:
                chunk = proc.stdout.read(min(remaining, CHUNK))
                if not chunk:
                    raise ValidationError("git cat-file ended early")
                digest.update(chunk)
                if policy:
                    if policy == "auto":
                        # Git's automatic conversion leaves CR-containing and
                        # binary blobs untouched (including its control-byte
                        # heuristic, not only files containing a NUL).
                        carriage += chunk.count(b"\r")
                        nul += chunk.count(b"\0")
                        bad = len(chunk) - len(chunk.translate(None, _GIT_NONPRINTABLE))
                        nonprintable += bad
                        printable += len(chunk) - bad - chunk.count(b"\r") - chunk.count(b"\n")
                    data = pending_cr + chunk
                    pending_cr = b"\r" if data.endswith(b"\r") else b""
                    if pending_cr:
                        data = data[:-1]
                    crlf_digest.update(data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
                    last = chunk[-1:]
                remaining -= len(chunk)
            proc.stdout.read(1)  # trailing newline
            result[path] = (mode, "sha256:" + digest.hexdigest())
            if policy:
                crlf_digest.update(pending_cr)
                nonprintable -= last == b"\x1a"  # Git permits a trailing DOS EOF byte
                if policy == "text" or (not carriage and not nul and nonprintable <= printable // 128):
                    checkout_hashes[path] = "sha256:" + crlf_digest.hexdigest()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    return result, checkout_hashes


def _modified_paths(root: Path, base_sha: str | None, files: tuple[FileEntry, ...] | list[FileEntry]) -> set[str]:
    base, checkout = _base_file_versions(root, base_sha, checkout=True) if base_sha else ({}, {})
    current = {f.path: (f.mode, f.sha256) for f in files}
    changed = set()
    for path in set(base) | set(current):
        before, after = base.get(path), current.get(path)
        if before == after:
            continue
        if before and after and before[0] == after[0] and checkout.get(path) == after[1]:
            continue
        changed.add(path)
    return changed


def _changed_paths(root: Path, base_sha: str, files: list[FileEntry]) -> list[str]:
    return sorted(path for path in _modified_paths(root, base_sha, files) if not is_transient(path))


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
    return sorted(path for path in _modified_paths(Path(workspace), base_sha, snapshot.files)
                  if any(matches_protected(path, pattern) for pattern in patterns))


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
