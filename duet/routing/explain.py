"""Human-readable routing explanations (the D09 "routing explanation
output").

explain(decision: RoutingDecision) -> str: one paragraph: what was chosen
  (profile, model/effort or "provider default"), why (risk, floor, failures,
  pressure, pins), what was excluded and why, the coverage ("enforced",
  "partial", "advisory: a native session keeps its own settings; this is a
  suggestion"), and the action when it is not "run".

format_records(records: list[dict]) -> str: a compact multi-line listing of
  persisted decision records (dicts as stored by the runtime: created_at,
  provider, role, purpose, task_id, profile, model, effort, coverage, action,
  floor_met, and outcome {accepted, observed, status} when known), newest
  first, flagging requested != accepted != observed settings (AT17)."""
from __future__ import annotations

from .contracts import RoutingDecision


def explain(decision: RoutingDecision) -> str:
    raise NotImplementedError


def format_records(records: list[dict]) -> str:
    raise NotImplementedError
