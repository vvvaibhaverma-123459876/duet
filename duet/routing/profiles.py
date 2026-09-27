"""Logical profiles mapped to provider settings (spec 8.2). Pure.

EFFORT_ORDER: each provider's OWN effort labels in increasing intensity, as
that provider documents them (Codex app-server ReasoningEffort; Claude Code
--effort). Used only to rank a provider's labels against each other, never
to compare providers.

candidates_for(controls, profile, user_map): best first:
1. the user's maps for (provider, profile), in order (source "user_map");
2. when effort is controllable and discovered: with L the discovered labels
   known to EFFORT_ORDER (never "none"/"minimal"), ranked, n = len(L):
   routine L[0], standard L[round((n-1)/3)], deep L[round(2(n-1)/3)],
   critical_review L[n-1] (round half up), model left alone (source
   "effort_order"). Claude low/medium/high/xhigh/max -> low, medium, xhigh,
   max; Codex low/medium/high/xhigh -> low, medium, high, xhigh;
3. always: the provider's (or user's) default, untouched (source "default").
No model name is ever produced except from the user's maps."""
from __future__ import annotations

import math

from .contracts import PROFILES, Candidate, ProviderControls

EFFORT_ORDER: dict[str, tuple[str, ...]] = {
    "codex": ("none", "minimal", "low", "medium", "high", "xhigh"),
    "claude": ("low", "medium", "high", "xhigh", "max"),
}
_NEVER_AUTO = {"none", "minimal"}


def _round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def ranked_efforts(controls: ProviderControls) -> tuple[str, ...]:
    order = EFFORT_ORDER.get(controls.provider)
    if not order or controls.effort_control != "supported" or not controls.efforts:
        return ()
    return tuple(e for e in order if e in controls.efforts and e not in _NEVER_AUTO)


def candidates_for(controls: ProviderControls, profile: str, user_map: tuple[Candidate, ...] = ()) -> list[Candidate]:
    out = [c for c in user_map if c.provider == controls.provider and c.profile == profile]
    ranked = ranked_efforts(controls)
    if ranked:
        n = len(ranked)
        index = {"routine": 0, "standard": _round_half_up((n - 1) / 3), "deep": _round_half_up(2 * (n - 1) / 3), "critical_review": n - 1}[profile]
        out.append(Candidate(controls.provider, profile, None, ranked[index], "effort_order"))
    out.append(Candidate(controls.provider, profile, None, None, "default"))
    return out


def validate(candidate: Candidate, controls: ProviderControls) -> str | None:
    if candidate.model is not None:
        if controls.model_control != "supported":
            return f"model is not controllable on {controls.provider}"
        if controls.models is not None and candidate.model not in controls.models:
            return f"model {candidate.model} is not in the discovered model list"
    if candidate.effort is not None:
        if controls.effort_control != "supported":
            return f"effort is not controllable on {controls.provider}"
        if controls.efforts is not None and candidate.effort not in controls.efforts:
            return f"effort {candidate.effort} is not supported by {controls.provider} (supported: {', '.join(controls.efforts) or 'none'})"
    return None


def coverage(candidate: Candidate, controls: ProviderControls, origin: str) -> str:
    if origin != "managed" or (candidate.model is None and candidate.effort is None):
        return "advisory"
    fields = [(candidate.model, controls.model_control), (candidate.effort, controls.effort_control)]
    return "enforced" if all(control == "supported" for value, control in fields if value is not None) else "partial"


def step(profile: str, delta: int) -> str:
    return PROFILES[max(0, min(len(PROFILES) - 1, PROFILES.index(profile) + delta))]
