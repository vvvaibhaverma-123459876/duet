"""Acceptance contracts.

The contract says what "done" means for a run: criteria, the checks that
evidence them, and protected paths agents must not modify (the tests and
configuration they are judged by). It is supplied by the user, stored in the
runtime, versioned, and only the user can change it (runtime D-011); agents
never edit it and cannot relax it.

Checks are argv lists by default. A shell string is allowed only as an
explicit `shell` mode in a user-authored contract, preserving the legacy
`cmd:` verifier without ever parsing a model-supplied command line."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .. import oscompat
from ..runtime.contracts import ValidationError, check_int, check_text, content_hash

SCHEMA = "duet.acceptance/1"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_CHECK_TIMEOUT = 3600
NO_TESTS_POLICIES = ("unknown", "fail", "pass")


@dataclass(frozen=True)
class CheckSpec:
    id: str
    argv: tuple[str, ...] = ()
    shell: str | None = None
    cwd: str = "."
    timeout_seconds: int = 600
    env_allow: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    no_tests: str = "unknown"
    required: bool = True

    def __post_init__(self) -> None:
        if not ID_RE.match(self.id or ""):
            raise ValidationError(f"invalid check id {self.id!r}")
        if bool(self.argv) == bool(self.shell):
            raise ValidationError(f"check {self.id}: give exactly one of argv or shell")
        for part in self.argv:
            check_text(part, f"check {self.id} argv", limit=4096, allow_empty=True)
        if self.shell is not None:
            check_text(self.shell, f"check {self.id} shell", limit=4096)
        rel = PurePosixPath(self.cwd)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValidationError(f"check {self.id}: cwd must be relative to the workspace")
        check_int(self.timeout_seconds, f"check {self.id} timeout_seconds", minimum=1, maximum=MAX_CHECK_TIMEOUT)
        for name in self.env_allow:
            if not ENV_NAME_RE.match(name):
                raise ValidationError(f"check {self.id}: invalid env name {name!r}")
        for name, value in self.env:
            if not ENV_NAME_RE.match(name):
                raise ValidationError(f"check {self.id}: invalid env name {name!r}")
            check_text(value, f"check {self.id} env {name}", limit=4096, allow_empty=True)
        if self.no_tests not in NO_TESTS_POLICIES:
            raise ValidationError(f"check {self.id}: no_tests must be one of {NO_TESTS_POLICIES}")

    @property
    def command(self) -> list[str]:
        return list(self.argv) if self.argv else oscompat.shell_argv(self.shell or "")

    def to_dict(self) -> dict:
        data = {
            "id": self.id,
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "env_allow": list(self.env_allow),
            "env": dict(self.env),
            "no_tests": self.no_tests,
            "required": self.required,
        }
        if self.argv:
            data["argv"] = list(self.argv)
        else:
            data["shell"] = self.shell
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "CheckSpec":
        if not isinstance(data, dict):
            raise ValidationError("check must be an object")
        unknown = set(data) - {"id", "argv", "shell", "cwd", "timeout_seconds", "env_allow", "env", "no_tests", "required"}
        if unknown:
            raise ValidationError(f"unknown check fields {sorted(unknown)}")
        argv = data.get("argv") or ()
        if not isinstance(argv, (list, tuple)):
            raise ValidationError("argv must be a list")
        env = data.get("env") or {}
        if not isinstance(env, dict):
            raise ValidationError("env must be an object")
        return cls(
            id=data.get("id", ""),
            argv=tuple(argv),
            shell=data.get("shell"),
            cwd=data.get("cwd", "."),
            timeout_seconds=data.get("timeout_seconds", 600),
            env_allow=tuple(data.get("env_allow") or ()),
            env=tuple(sorted(env.items())),
            no_tests=data.get("no_tests", "unknown"),
            required=bool(data.get("required", True)),
        )


@dataclass(frozen=True)
class Criterion:
    id: str
    description: str
    checks: tuple[str, ...] = ()
    required: bool = True
    review: bool = True  # needs a non-author review
    # "change": new behaviour, shown by a check that fails without it (or an
    # explicit review); "preserve": existing behaviour that must keep
    # passing (a check green before and after is the right evidence). D10.
    kind: str = "change"

    def __post_init__(self) -> None:
        if not ID_RE.match(self.id or ""):
            raise ValidationError(f"invalid criterion id {self.id!r}")
        check_text(self.description, f"criterion {self.id} description", limit=4096)
        if self.kind not in ("change", "preserve"):
            raise ValidationError(f"criterion {self.id} kind must be change or preserve")

    def to_dict(self) -> dict:
        data = {"id": self.id, "description": self.description, "checks": list(self.checks), "required": self.required, "review": self.review}
        if self.kind != "change":  # omitted by default, so earlier contract hashes are unchanged
            data["kind"] = self.kind
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Criterion":
        if not isinstance(data, dict):
            raise ValidationError("criterion must be an object")
        unknown = set(data) - {"id", "description", "checks", "required", "review", "kind"}
        if unknown:
            raise ValidationError(f"unknown criterion fields {sorted(unknown)}")
        return cls(
            id=data.get("id", ""),
            description=data.get("description", ""),
            checks=tuple(data.get("checks") or ()),
            required=bool(data.get("required", True)),
            review=bool(data.get("review", True)),
            kind=data.get("kind", "change"),
        )


@dataclass(frozen=True)
class AcceptanceContract:
    criteria: tuple[Criterion, ...]
    checks: tuple[CheckSpec, ...] = ()
    protected_paths: tuple[str, ...] = ()
    notes: str = ""
    extra: dict = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        ids = [c.id for c in self.checks]
        if len(ids) != len(set(ids)):
            raise ValidationError("check ids must be unique")
        crit_ids = [c.id for c in self.criteria]
        if len(crit_ids) != len(set(crit_ids)):
            raise ValidationError("criterion ids must be unique")
        known = set(ids)
        for criterion in self.criteria:
            missing = [c for c in criterion.checks if c not in known]
            if missing:
                raise ValidationError(f"criterion {criterion.id} references unknown checks {missing}")
        for pattern in self.protected_paths:
            check_text(pattern, "protected path", limit=512)
            if PurePosixPath(pattern).is_absolute() or ".." in PurePosixPath(pattern).parts:
                raise ValidationError(f"protected path must be relative: {pattern!r}")

    def check(self, check_id: str) -> CheckSpec:
        for spec in self.checks:
            if spec.id == check_id:
                return spec
        raise ValidationError(f"no check {check_id!r} in the acceptance contract")

    def required_checks(self) -> tuple[str, ...]:
        """Checks referenced by a required criterion, plus checks flagged
        required on their own. Every one must pass on the final snapshot."""
        referenced = {c for criterion in self.criteria if criterion.required for c in criterion.checks}
        flagged = {spec.id for spec in self.checks if spec.required}
        return tuple(spec.id for spec in self.checks if spec.id in referenced | flagged)

    def review_required(self) -> bool:
        return any(c.required and c.review for c in self.criteria)

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "criteria": [c.to_dict() for c in self.criteria],
            "checks": [c.to_dict() for c in self.checks],
            "protected_paths": list(self.protected_paths),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AcceptanceContract":
        if not isinstance(data, dict):
            raise ValidationError("acceptance contract must be an object")
        schema = data.get("schema", SCHEMA)
        if schema != SCHEMA:
            raise ValidationError(f"unsupported acceptance schema {schema!r}")
        unknown = set(data) - {"schema", "criteria", "checks", "protected_paths", "notes"}
        if unknown:
            raise ValidationError(f"unknown acceptance fields {sorted(unknown)}")
        return cls(
            criteria=tuple(Criterion.from_dict(c) for c in data.get("criteria") or ()),
            checks=tuple(CheckSpec.from_dict(c) for c in data.get("checks") or ()),
            protected_paths=tuple(data.get("protected_paths") or ()),
            notes=data.get("notes", ""),
        )

    def hash(self) -> str:
        return content_hash(self.to_dict())
