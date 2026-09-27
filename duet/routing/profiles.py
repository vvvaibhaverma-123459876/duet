"""Logical profiles mapped to provider settings (spec 8.2). Pure.

EFFORT_ORDER: each provider's OWN effort labels in increasing intensity, as
  documented by that provider (Codex app-server ReasoningEffort enum:
  none < minimal < low < medium < high < xhigh; Claude Code --effort:
  low < medium < high < xhigh < max). Used only to rank a provider's own
  labels against each other; never to compare providers.

candidates_for(controls, profile, user_map=()) -> list[Candidate]
  Candidates for `profile` on `controls.provider`, best first:
  1. user_map entries for (provider, profile) (source "user_map"), in order;
  2. if effort is controllable and the discovered efforts can be ranked with
     EFFORT_ORDER: the effort at the profile's position among the provider's
     ranked, discovered labels (source "effort_order"), with model None.
     Positions: with n ranked labels (excluding "none"/"minimal", which are
     never chosen automatically), routine -> index 0, standard -> index
     n//2 - (1 if n >= 4 else 0) clipped to [0, n-1]... keep it simple and
     documented: routine = lowest, critical_review = highest, standard and
     deep spread evenly in between (round half up), all distinct when n >= 4;
  3. always last: Candidate(model=None, effort=None, source="default") — the
     provider's (or user's) own default, which is always allowed.
  No model names are ever produced here except from user_map.

validate(candidate, controls) -> str | None
  None if DUET can apply it; else the exclusion reason:
  - model set but model_control != "supported" -> "model is not controllable on <provider>"
  - model set, controls.models known and model not in it -> "model <m> is not in the discovered model list"
  - effort set but effort_control != "supported" -> "effort is not controllable on <provider>"
  - effort set, controls.efforts known and effort not in it -> "effort <e> is not supported by <provider> (supported: ...)"

coverage(candidate, controls, origin) -> str
  "advisory" for a native origin (DUET cannot set a native session's model)
  or when nothing in the candidate is set; "enforced" when every set field
  is controllable; "partial" otherwise."""
from __future__ import annotations

from .contracts import Candidate, ProviderControls

EFFORT_ORDER: dict[str, tuple[str, ...]] = {
    "codex": ("none", "minimal", "low", "medium", "high", "xhigh"),
    "claude": ("low", "medium", "high", "xhigh", "max"),
}


def candidates_for(controls: ProviderControls, profile: str, user_map: tuple[Candidate, ...] = ()) -> list[Candidate]:
    raise NotImplementedError


def validate(candidate: Candidate, controls: ProviderControls) -> str | None:
    raise NotImplementedError


def coverage(candidate: Candidate, controls: ProviderControls, origin: str) -> str:
    raise NotImplementedError
