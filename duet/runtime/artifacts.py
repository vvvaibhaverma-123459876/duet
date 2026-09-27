"""Content-addressed artifact store.

Artifacts (verifier logs, snapshot manifests and file blobs, checkpoint
reports) are addressed by `sha256:<hex>` references, never by caller-supplied
paths, so a reference cannot be used to read arbitrary files. Blobs are
written atomically and made read-only: stored evidence is immutable."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from .contracts import NotFound, ValidationError

REF_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
CHUNK = 1024 * 1024


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        (self.root / "sha256").mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.root, 0o700)

    def _path(self, ref: str) -> Path:
        match = REF_RE.match(ref or "")
        if not match:
            raise ValidationError(f"invalid artifact reference {ref!r}")
        digest = match.group(1)
        return self.root / "sha256" / digest[:2] / digest

    def put_bytes(self, data: bytes) -> str:
        ref = "sha256:" + hashlib.sha256(data).hexdigest()
        path = self._path(ref)
        if path.exists():
            return ref
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o400)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return ref

    def put_text(self, text: str) -> str:
        return self.put_bytes(text.encode("utf-8"))

    def put_file(self, source: Path) -> str:
        """Stream a file in; returns its reference. Symlinks are not followed."""
        if source.is_symlink():
            raise ValidationError(f"refusing to store symlink {source} as a blob")
        digest = hashlib.sha256()
        with open(source, "rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK), b""):
                digest.update(chunk)
        ref = "sha256:" + digest.hexdigest()
        path = self._path(ref)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as out, open(source, "rb") as handle:
                    for chunk in iter(lambda: handle.read(CHUNK), b""):
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                check = hashlib.sha256(Path(tmp).read_bytes()).hexdigest()
                if "sha256:" + check != ref:
                    raise ValidationError(f"{source} changed while it was being stored")
                os.chmod(tmp, 0o400)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        return ref

    def has(self, ref: str) -> bool:
        return self._path(ref).exists()

    def get_bytes(self, ref: str) -> bytes:
        path = self._path(ref)
        if not path.exists():
            raise NotFound(f"artifact {ref} not found")
        data = path.read_bytes()
        if "sha256:" + hashlib.sha256(data).hexdigest() != ref:
            raise ValidationError(f"artifact {ref} is corrupt")
        return data

    def get_text(self, ref: str) -> str:
        return self.get_bytes(ref).decode("utf-8")
