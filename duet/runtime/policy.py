"""Run-scoped authorisation policy.

The policy is supplied by the user (CLI flags, user config, or an explicit
approval) and pinned to a run by its content hash. Repository-controlled
configuration may only *narrow* it: `restrict()` never widens a permission.
Peer messages, repository instructions and tool output carry no authority at
all; they are content, not policy (spec §9.2)."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .contracts import PolicyDenied, ValidationError, canonical_json, check_int, content_hash

COMMAND_CATEGORIES = frozenset({"read", "edit", "test", "build", "format", "git_commit", "install_deps", "network_fetch"})
NETWORK_SCOPES = ("none", "package_registries", "any")  # ordered: narrowest first


@dataclass(frozen=True)
class AuthorisationPolicy:
    version: int = 1
    repo_roots: tuple[str, ...] = ()
    command_categories: frozenset[str] = frozenset({"read", "edit", "test", "build", "format", "git_commit"})
    network: str = "none"
    allow_commit: bool = True  # to the Duet-owned branch only
    allow_push: bool = False
    allow_merge: bool = False
    paid_fallback: bool = False  # never enabled implicitly (R13)
    max_invocations: int = 40
    max_discussion_messages: int = 200
    max_repair_attempts: int = 2
    deadline_seconds: int = 3600
    solo_allowed: bool = False  # pair mode by default (R01)
    extras: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        unknown = set(self.command_categories) - COMMAND_CATEGORIES
        if unknown:
            raise ValidationError(f"unknown command categories: {sorted(unknown)}")
        if self.network not in NETWORK_SCOPES:
            raise ValidationError(f"network must be one of {NETWORK_SCOPES}")
        for name in ("max_invocations", "max_discussion_messages", "deadline_seconds"):
            check_int(getattr(self, name), name, minimum=1)
        check_int(self.max_repair_attempts, "max_repair_attempts", minimum=0)
        for flag in ("allow_commit", "allow_push", "allow_merge", "paid_fallback", "solo_allowed"):
            if not isinstance(getattr(self, flag), bool):
                raise ValidationError(f"{flag} must be a boolean")

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "repo_roots": list(self.repo_roots),
            "command_categories": sorted(self.command_categories),
            "network": self.network,
            "allow_commit": self.allow_commit,
            "allow_push": self.allow_push,
            "allow_merge": self.allow_merge,
            "paid_fallback": self.paid_fallback,
            "max_invocations": self.max_invocations,
            "max_discussion_messages": self.max_discussion_messages,
            "max_repair_attempts": self.max_repair_attempts,
            "deadline_seconds": self.deadline_seconds,
            "solo_allowed": self.solo_allowed,
            "extras": self.extras,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuthorisationPolicy":
        if not isinstance(data, dict):
            raise ValidationError("policy must be an object")
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            raise ValidationError(f"unknown policy fields: {sorted(unknown)}")
        values = dict(data)
        if "repo_roots" in values:
            values["repo_roots"] = tuple(values["repo_roots"])
        if "command_categories" in values:
            values["command_categories"] = frozenset(values["command_categories"])
        return cls(**values)

    def hash(self) -> str:
        return content_hash(self.to_dict())

    def canonical(self) -> str:
        return canonical_json(self.to_dict())

    def restrict(self, project: dict | None) -> "AuthorisationPolicy":
        """Apply repository-controlled settings, which may only narrow. Any
        attempt to widen is ignored and reported, never honoured."""
        if not project:
            return self
        narrowed = {}
        widening = []
        if "command_categories" in project:
            requested = frozenset(project["command_categories"])
            narrowed["command_categories"] = self.command_categories & requested
            widening += sorted(requested - self.command_categories)
        if "network" in project:
            requested = project["network"]
            if requested not in NETWORK_SCOPES:
                raise ValidationError(f"project network must be one of {NETWORK_SCOPES}")
            if NETWORK_SCOPES.index(requested) <= NETWORK_SCOPES.index(self.network):
                narrowed["network"] = requested
            else:
                widening.append(f"network={requested}")
        for flag in ("allow_commit", "allow_push", "allow_merge", "paid_fallback", "solo_allowed"):
            if flag in project:
                if project[flag] is False:
                    narrowed[flag] = False
                elif project[flag] is True and not getattr(self, flag):
                    widening.append(f"{flag}=true")
        for limit in ("max_invocations", "max_discussion_messages", "max_repair_attempts", "deadline_seconds"):
            if limit in project:
                value = check_int(project[limit], limit, minimum=0)
                if value <= getattr(self, limit):
                    narrowed[limit] = value
                else:
                    widening.append(f"{limit}={value}")
        result = replace(self, **narrowed)
        if widening:
            result = replace(result, extras={**result.extras, "ignored_widening": widening})
        return result


def require(policy: AuthorisationPolicy, capability: str) -> None:
    """Raise PolicyDenied unless the pinned policy grants `capability`."""
    if capability in COMMAND_CATEGORIES:
        if capability not in policy.command_categories:
            raise PolicyDenied(f"command category {capability!r} is not authorised for this run")
        return
    flags = {"push": policy.allow_push, "merge": policy.allow_merge, "commit": policy.allow_commit, "paid_fallback": policy.paid_fallback}
    if capability in flags:
        if not flags[capability]:
            raise PolicyDenied(f"{capability} is not authorised for this run")
        return
    raise PolicyDenied(f"unknown capability {capability!r}")
