"""The router (spec 8.2-8.4). Pure and deterministic.

route(request, policy=RoutingPolicy()) -> RoutingDecision

Order of reasoning (each step adds Reasons):
1. assessment = assess(request.task, role, purpose, agent, policy).
2. Failure handling (8.4), from request.task.failures via failures.py:
   - newest failure "environment" -> action "diagnose_environment": keep the
     participant's current profile (or the floor), never escalate; the
     explanation says the environment needs repair (AT19).
   - newest "requirements" -> action "clarify": keep the profile; the
     explanation asks for an explicit assumption or a question.
   - "provider" failures are ignored here (admission handles them).
   - hypothesis failures under the current approach >= policy.repairs_per_approach
     -> action "replan" (a peer-assisted re-plan, D06), and at most
     policy.max_escalations_per_task escalations per task: escalate one
     profile step (escalated=True) only if more reasoning is plausible (the
     failures are hypothesis failures) and the task has not already been
     escalated (history); otherwise stay and replan.
   - otherwise action "run".
3. Target = max(floor, the participant's previous profile for this task if
   it is higher and within hysteresis). De-escalate one step only after
   policy.deescalate_after_successes consecutive successes at the current
   profile and never below the floor. A non-forced change requires the
   previous setting to have been used for >= policy.switch_hysteresis_turns
   turns (escalation and floor raises are forced).
4. Pressure (from admission): "pressure" lowers the target one step, and
   "critical" to the floor, but NEVER below the floor (exit criterion).
   Reason code "pressure".
5. Pins: a pin's min/max bound the target (max wins over pressure, min over
   floor lowering); a pinned model/effort is used as the setting (source
   "pin") even if it maps below the floor; then floor_met=False with a
   Reason(code="pin_below_floor", weight="blocks") and the explanation says
   the user's pin is respected and the change needs the stronger review.
   DUET never overrides a user pin.
6. Candidates for the target profile (profiles.candidates_for), each
   validated (profiles.validate); invalid ones go to `excluded` with their
   reason (AT17: rejected or explicitly downgraded, with evidence). The
   first valid candidate is selected; the "default" candidate always
   validates. If the selected candidate is the default while a specific
   setting was wanted, add Reason(code="downgraded", ...).
7. Coverage from profiles.coverage (native -> "advisory", AT41: a native
   session's setting cannot change mid-turn; the decision is advice).
8. switch = the (model, effort) differs from the participant's newest
   history entry; switch_cost: "none" if no switch, "high" when the model
   changes on a resumed session (the new model re-reads the context),
   "low" when only the effort changes.
9. Never another provider or participant: the decision is always for
   request.provider / request.participant_id (a routing decision can never
   replace the second participant to save cost).
10. explanation: one paragraph built from the reasons (explain.explain).
All reason lists sorted deterministically; same request -> same decision."""
from __future__ import annotations

from .contracts import RoutingDecision, RoutingPolicy, RoutingRequest


def route(request: RoutingRequest, policy: RoutingPolicy = RoutingPolicy()) -> RoutingDecision:
    raise NotImplementedError
