"""The router (spec 8.2-8.4). Pure and deterministic.

1. Assess the task (assessment.py).
2. Classify the newest failures: environment -> "diagnose_environment"
   (never escalate, AT19); requirements -> "clarify"; hypothesis failures
   under one approach >= repairs_per_approach -> "replan", with at most
   max_escalations_per_task one-step escalations per task (AT20). Provider
   failures belong to admission (D08).
3. Target: the floor, or the participant's previous profile for this task
   when higher (hysteresis: a non-forced change needs switch_hysteresis_turns
   at the current setting; a floor raise or escalation is forced).
   De-escalate one step after deescalate_after_successes successes, never
   below the floor.
4. Pressure from admission lowers the target one step ("pressure") or to
   the floor ("critical"), never below the floor.
5. Pins bound the profile; a pinned model/effort is used as given, even
   below the floor: floor_met False and the explanation says so. DUET never
   overrides a user pin, and never writes it to the provider.
6. Candidates for the target (profiles.py), each validated; refused ones are
   listed with their reasons (AT17); the default always validates.
7. Coverage: advisory for native sessions (AT41), enforced or partial for
   managed ones.
Always this participant and provider: routing never replaces the second
participant."""
from __future__ import annotations

from .assessment import assess
from .contracts import PROFILES, Candidate, Excluded, Reason, RoutingDecision, RoutingPolicy, RoutingRequest
from .explain import explain
from .failures import classify, consecutive
from .profiles import candidates_for, coverage, step, validate


def _rank(profile: str) -> int:
    return PROFILES.index(profile)


def route(request: RoutingRequest, policy: RoutingPolicy = RoutingPolicy()) -> RoutingDecision:
    assessment = assess(request.task, role=request.role, purpose=request.purpose, agent=request.agent, policy=policy)
    floor = assessment.floor
    reasons: list[Reason] = []
    history = list(request.history)
    task_history = [h for h in history if h.task_id == request.task.task_id]
    previous = history[-1] if history else None
    current = task_history[-1].profile if task_history else floor

    action, escalated = "run", False
    failures = [f for f in request.task.failures if classify(f)[0] != "provider"]
    newest = classify(failures[-1]) if failures else None
    if newest and newest[0] == "environment":
        action = "diagnose_environment"
        reasons.append(Reason("environment", newest[1].detail, "blocks"))
    elif newest and newest[0] == "requirements":
        action = "clarify"
        reasons.append(Reason("requirements", newest[1].detail, "blocks"))
    elif consecutive(failures, "hypothesis") >= policy.repairs_per_approach:
        action = "replan"
        escalations = sum(1 for h in task_history if h.escalated)
        if escalations < policy.max_escalations_per_task and _rank(current) < len(PROFILES) - 1:
            escalated = True
            reasons.append(Reason("escalated", f"{consecutive(failures, 'hypothesis')} failed repairs under one approach", "raises"))
        else:
            reasons.append(Reason("escalation_bounded", f"already escalated {escalations} time(s) for this task"))

    if action in ("diagnose_environment", "clarify"):
        target = max(current, floor, key=_rank)
    elif escalated:
        # One step from where the participant was; a floor the failures already raised is not stepped again.
        target = max(step(current, 1), floor, key=_rank)
    else:
        target = max(current, floor, key=_rank)
        at_current = [h for h in task_history if h.profile == current]
        successes = 0
        for h in reversed(task_history):
            if h.profile != current or h.outcome != "succeeded":
                break
            successes += 1
        if (task_history and successes >= policy.deescalate_after_successes and _rank(current) > _rank(floor)
                and len(at_current) >= policy.switch_hysteresis_turns):
            target = step(current, -1)
            reasons.append(Reason("deescalated", f"{successes} successes at {current}", "lowers"))

    if request.pressure in ("pressure", "critical") and _rank(target) > _rank(floor):
        lowered = floor if request.pressure == "critical" else max(step(target, -1), floor, key=_rank)
        if lowered != target:
            reasons.append(Reason("pressure", f"{request.pressure}: {target} -> {lowered}", "lowers"))
            target = lowered
    elif request.pressure != "none":
        reasons.append(Reason("pressure", f"{request.pressure}: already at the floor ({floor})"))

    pin = next((p for p in request.pins if p.provider == request.provider), None)
    floor_met = True
    if pin is not None:
        if pin.max_profile and _rank(target) > _rank(pin.max_profile):
            reasons.append(Reason("pin", f"max {pin.max_profile}", "lowers"))
            target = pin.max_profile
        if pin.min_profile and _rank(target) < _rank(pin.min_profile):
            reasons.append(Reason("pin", f"min {pin.min_profile}", "raises"))
            target = pin.min_profile
        if _rank(target) < _rank(floor):
            floor_met = False
            reasons.append(Reason("pin_below_floor", f"user bound {target} is below the floor {floor}", "blocks"))

    candidates: list[Candidate] = []
    excluded: list[Excluded] = []
    options = candidates_for(request.controls, target, request.user_map)
    if pin is not None and (pin.model or pin.effort):
        pinned = Candidate(request.provider, target, pin.model, pin.effort, "pin")
        problem = validate(pinned, request.controls)
        if problem:
            excluded.append(Excluded(pinned, problem))
            reasons.append(Reason("pin_invalid", problem, "blocks"))
            options = [c for c in options if c.source == "default"]
        else:
            options = [pinned]
            floor_setting = next((c for c in candidates_for(request.controls, floor, ()) if c.source == "effort_order"), None)
            if pin.effort and floor_setting and _effort_rank(request.provider, pin.effort) < _effort_rank(request.provider, floor_setting.effort):
                floor_met = False
                reasons.append(Reason("pin_below_floor", f"pinned effort {pin.effort} is below {floor_setting.effort} for {floor}", "blocks"))
    for candidate in options:
        problem = validate(candidate, request.controls)
        if problem:
            excluded.append(Excluded(candidate, problem))
        else:
            candidates.append(candidate)
    selected = candidates[0]
    wanted_specific = any(c.source != "default" for c in options) or any(e.candidate.source != "default" for e in excluded)
    if selected.source == "default" and wanted_specific:
        reasons.append(Reason("downgraded", "no requested setting is applicable; the provider default is used", "lowers"))
    if request.origin != "managed":
        reasons.append(Reason("advisory", "a native session keeps its own model and effort"))

    switch = previous is not None and (previous.model, previous.effort) != (selected.model, selected.effort)
    switch_cost = "none"
    if switch:
        switch_cost = "high" if previous.model != selected.model else "low"
        reasons.append(Reason("switch", f"{previous.model or 'default'}/{previous.effort or 'default'} -> {selected.model or 'default'}/{selected.effort or 'default'}"))

    decision = RoutingDecision(
        action=action, profile=target, model=selected.model, effort=selected.effort,
        coverage=coverage(selected, request.controls, request.origin), assessment=assessment,
        candidates=tuple(candidates), excluded=tuple(excluded), floor_met=floor_met, switch=switch,
        switch_cost=switch_cost, escalated=escalated, reasons=tuple(sorted(reasons, key=lambda r: (r.code, r.detail))), explanation="",
    )
    return RoutingDecision(**{**decision.__dict__, "explanation": explain(decision)})


def _effort_rank(provider: str, effort: str | None) -> int:
    from .profiles import EFFORT_ORDER

    order = EFFORT_ORDER.get(provider, ())
    return order.index(effort) if effort in order else -1
