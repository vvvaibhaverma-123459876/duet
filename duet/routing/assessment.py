"""Task risk and uncertainty assessment (spec 8.1). Pure and deterministic.

Risk comes from what a change touches, not from its size (AT16):
authentication, secrets and crypto, payments, migrations, concurrency,
recovery, permissions and data deletion are risk-bearing areas. They are
detected from changed path segments and file names, and from whole words
in the task description (so "tokenizer" is not "token", "keyboard" is not
"key"). One area makes a change "high"; two areas, or an area together with
a migration or deletion, make it "critical". Otherwise size and blast
radius decide (> 400 lines or > 15 files: "medium"; touching protected
paths: "high").

Uncertainty rises with investigative work, hedged descriptions, missing
checks (untestable work is also at least "medium" risk) and failed
hypotheses. Environment failures never raise either: they are not about
reasoning. An agent can raise risk, uncertainty or the floor, never lower
them (the attempt is recorded)."""
from __future__ import annotations

import re
from fnmatch import fnmatch

from .contracts import PROFILES, RISKS, UNCERTAINTIES, AgentAssessment, Assessment, Reason, RoutingPolicy, TaskFacts
from .failures import classify

# area -> (path segment / file name stems, description words)
AREAS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "auth": (("auth", "login", "oauth", "session", "sessions", "password", "passwd", "sso", "jwt"),
             ("auth", "authentication", "authorisation", "authorization", "login", "oauth", "password", "session", "jwt", "sso")),
    "secrets": (("secret", "secrets", "token", "tokens", "credential", "credentials", "crypt", "crypto", "key", "keys", "api_key", "apikey", "cert", "certs"),
                ("secret", "secrets", "token", "tokens", "credential", "credentials", "encryption", "decrypt", "encrypt", "crypto", "api key")),
    "payment": (("payment", "payments", "billing", "invoice", "invoices", "checkout", "stripe"),
                ("payment", "payments", "billing", "invoice", "refund", "checkout")),
    "migration": (("migration", "migrations", "alembic", "schema"), ("migration", "migrations", "schema change")),
    "concurrency": (("lock", "locks", "mutex", "thread", "threads", "async", "concurrency", "concurrent", "queue"),
                    ("race", "deadlock", "concurrency", "concurrent", "thread", "threads", "lock", "mutex", "async")),
    "recovery": (("recovery", "backup", "backups", "restore"), ("recovery", "recover", "backup", "restore", "crash")),
    "permissions": (("permission", "permissions", "sandbox", "policy", "policies", "acl", "rbac"),
                    ("permission", "permissions", "sandbox", "privilege", "rbac", "acl")),
    "deletion": ((), ("delete data", "drop table", "purge", "wipe", "truncate")),
}
_HEDGES = ("unclear", "maybe", "investigate", "why", "flaky", "intermittent", "sometimes", "unknown")


def _up(scale: tuple[str, ...], value: str, steps: int = 1) -> str:
    return scale[min(len(scale) - 1, scale.index(value) + steps)]


def _path_parts(path: str) -> set[str]:
    parts = set()
    for segment in re.split(r"[/\\]", path.lower()):
        stem = segment.rsplit(".", 1)[0] if "." in segment else segment
        parts.add(stem)
        parts.update(p for p in re.split(r"[_\-.]", stem) if p)
    return parts


def _areas(task: TaskFacts) -> list[Reason]:
    found: dict[str, str] = {}
    for path in task.changed_paths:
        parts = _path_parts(path)
        if path.lower().endswith(".sql"):
            found.setdefault("migration", path)
        for area, (names, _) in AREAS.items():
            if parts & set(names) or "api_key" in path.lower() and area == "secrets":
                found.setdefault(area, path)
    text = task.description.lower()
    for area, (_, words) in AREAS.items():
        for word in words:
            if re.search(rf"(?<![a-z0-9_]){re.escape(word)}(?![a-z0-9_])", text):
                found.setdefault(area, word)
                break
    return [Reason(f"area:{area}", detail, "raises") for area, detail in sorted(found.items())]


def assess(task: TaskFacts, *, role: str, purpose: str, agent: AgentAssessment | None = None, policy: RoutingPolicy = RoutingPolicy()) -> Assessment:
    reasons = _areas(task)
    areas = {r.code.split(":", 1)[1] for r in reasons}
    if len(areas) >= 2 or (areas and areas & {"migration", "deletion"} and len(areas) >= 2):
        risk = "critical"
    elif areas:
        risk = "high"
    else:
        risk = "low"
        big = (task.changed_lines or 0) > 400 or len(task.changed_paths) > 15
        if big:
            risk = "medium"
            reasons.append(Reason("size", f"{len(task.changed_paths)} files, {task.changed_lines or 'unknown'} lines", "raises"))
    touched = [p for p in task.changed_paths if any(fnmatch(p, pat) or p.startswith(pat.rstrip("/*") + "/") for pat in task.protected_paths)]
    if touched:
        reasons.append(Reason("protected", touched[0], "raises"))
        if RISKS.index(risk) < RISKS.index("high"):
            risk = "high"
    uncertainty = "medium" if task.kind in ("investigate", "test_design") else "low"
    text = task.description.lower()
    hedge = next((w for w in _HEDGES if re.search(rf"\b{w}\b", text)), None)
    if hedge:
        uncertainty = _up(UNCERTAINTIES, uncertainty)
        reasons.append(Reason("hedged", hedge, "raises"))
    if not task.has_checks:
        uncertainty = _up(UNCERTAINTIES, uncertainty)
        if RISKS.index(risk) < RISKS.index("medium"):
            risk = "medium"
        reasons.append(Reason("untestable", "no required check", "raises"))
    hypotheses = sum(1 for f in task.failures if classify(f)[0] == "hypothesis")
    if hypotheses:
        uncertainty = _up(UNCERTAINTIES, uncertainty, hypotheses)
        reasons.append(Reason("failures", f"{hypotheses} failed attempt(s)", "raises"))
        if hypotheses >= 2:
            risk = _up(RISKS, risk)
    floor = dict(policy.floor_for_risk)[risk]
    if purpose == "review" and task.kind == "code":
        wanted = policy.risky_review_floor if risk in ("high", "critical") else policy.review_floor
        if PROFILES.index(wanted) > PROFILES.index(floor):
            floor = wanted
            reasons.append(Reason("review_floor", wanted))
    if uncertainty == "high" and PROFILES.index(floor) < PROFILES.index("deep"):
        floor = "deep"
    if agent is not None:
        raised = False
        if agent.risk in RISKS and RISKS.index(agent.risk) > RISKS.index(risk):
            risk, raised = agent.risk, True
        if agent.uncertainty in UNCERTAINTIES and UNCERTAINTIES.index(agent.uncertainty) > UNCERTAINTIES.index(uncertainty):
            uncertainty, raised = agent.uncertainty, True
        if agent.profile in PROFILES and PROFILES.index(agent.profile) > PROFILES.index(floor):
            floor, raised = agent.profile, True
        lower = (agent.profile in PROFILES and PROFILES.index(agent.profile) < PROFILES.index(floor)) or (
            agent.risk in RISKS and RISKS.index(agent.risk) < RISKS.index(risk))
        if raised:
            reasons.append(Reason("agent_raised", (agent.reason or "participant request")[:200], "raises"))
        elif lower:
            reasons.append(Reason("agent_lower_ignored", (agent.reason or "participant request")[:200]))
    return Assessment(risk, uncertainty, floor, tuple(sorted(reasons, key=lambda r: (r.code, r.detail))))
