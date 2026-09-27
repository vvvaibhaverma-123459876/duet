"""Human-readable routing explanations (the D09 "routing explanation
output"): one paragraph per decision, and a compact listing of persisted
decisions that flags requested/accepted/observed differences (AT17)."""
from __future__ import annotations

from .contracts import RoutingDecision

ACTIONS = {
    "diagnose_environment": "The last failure is environmental: repair the environment first; a stronger model would not help.",
    "clarify": "The requirement looks unclear: state an assumption or ask one precise question.",
    "replan": "Repairs under this approach keep failing: propose a different approach.",
}


def explain(decision: RoutingDecision) -> str:
    a = decision.assessment
    setting = f"model {decision.model or 'default'}, effort {decision.effort or 'default'}"
    parts = [f"Profile {decision.profile} ({setting}) for risk {a.risk}, uncertainty {a.uncertainty}, floor {a.floor}."]
    areas = [r.detail for r in a.reasons if r.code.startswith("area:")]
    if areas:
        parts.append("Risk-bearing: " + ", ".join(f"{r.code[5:]} ({r.detail})" for r in a.reasons if r.code.startswith("area:")) + ".")
    for reason in decision.reasons:
        if reason.code in ("pressure", "pin", "pin_below_floor", "pin_invalid", "escalated", "escalation_bounded", "deescalated", "downgraded"):
            parts.append(f"{reason.code.replace('_', ' ')}: {reason.detail}.")
    if not decision.floor_met:
        parts.append("The user's pin is respected below the floor; the change needs the stronger review.")
    if decision.excluded:
        parts.append("Excluded: " + "; ".join(e.reason for e in decision.excluded) + ".")
    parts.append({"enforced": "Applied at the next turn.", "partial": "Partly applied: not every setting is controllable.",
                  "advisory": "Advisory: a native session keeps its own settings; this is a suggestion."}[decision.coverage])
    if decision.action in ACTIONS:
        parts.append(ACTIONS[decision.action])
    text = " ".join(parts)
    return text if len(text) <= 900 else text[:897] + "..."


def format_records(records: list[dict]) -> str:
    lines = []
    for r in records:
        setting = f"{r.get('model') or 'default'}/{r.get('effort') or 'default'}"
        line = (f"{r.get('created_at', '')[:19]} {r.get('provider')} {r.get('role')} {r.get('purpose')} {r.get('task_id') or ''}: "
                f"{r.get('profile')} {setting} [{r.get('coverage')}] {r.get('action')}")
        if r.get("floor_met") is False:
            line += " (below floor: user pin)"
        outcome = r.get("outcome") or {}
        if outcome:
            line += f" -> {outcome.get('status')}"
            for difference in outcome.get("differences") or []:
                line += f"\n    ! {difference}"
        lines.append(line)
    return "\n".join(lines)
