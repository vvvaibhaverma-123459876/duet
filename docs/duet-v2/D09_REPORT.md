# D09 Report: adaptive model and effort routing

Each managed turn now gets a model and effort chosen from the task's
assessed risk and its history. The choice is recorded with its reasons,
and after the turn DUET records what the provider actually accepted and
observed. Native sessions get the same decision as advice. Everything here
is exercised with scripted providers; no real provider was involved.

The routing library was first delegated to a background agent, which hit a
session rate limit before writing any code. It was then written in this
session.

## What exists now

| Piece | Module | What it does |
|---|---|---|
| Contracts | `duet/routing/contracts.py` | Profiles (routine < standard < deep < critical_review), task facts, assessments, candidates, pins, prior decisions, decisions, and the policy settings. |
| Assessment | `duet/routing/assessment.py` | Risk from what a change touches (auth, secrets, payment, migrations, concurrency, recovery, permissions, deletion), found in path segments and in whole words of the description, not from its size. Uncertainty from investigative work, hedged wording, missing checks and failed hypotheses. An agent can raise scrutiny, never lower it. |
| Failures | `duet/routing/failures.py` | Each failure is classified as environment, requirements, hypothesis, provider, timeout or unknown. A ModuleNotFoundError inside pytest output counts as environment. |
| Profiles | `duet/routing/profiles.py` | Candidates per provider: the user's maps, then that provider's own effort labels ranked on its documented scale, then the provider default. No model names are built in. Also validation and coverage. |
| Policy | `duet/routing/policy.py` | `route()`: failure handling (diagnose the environment, clarify, re-plan with at most one escalation per task), hysteresis and de-escalation, pressure bounded by the floor, user pins, candidate validation with recorded exclusions, coverage, switch cost. |
| Explanations | `duet/routing/explain.py` | One paragraph per decision, and a listing that flags requested, accepted and observed differences. |
| Runtime | `duet/runtime/routing_control.py`, migration `0007_routing.sql` | Builds the request from the store, persists decisions (`routing.decided`) and outcomes (`routing.outcome`). User-only pins and profile maps. `duet_request_profile`. Advice for native sessions. |
| Managed turns | `duet/runtime/peers.py` | Routes each turn and passes the model and effort to the adapter. A setting refused before dispatch is excluded and the turn re-routed. Non-run actions add guidance to the prompt. |
| CLI | `duet routing [--run RUN] [--json]`, `duet routing pin/unpin/map` | Decisions with their explanations, pins and maps (`duet.routing/1`). `duet status --run` shows the latest decision for each participant. |

## Exit criteria

| Criterion | Evidence | Status |
|---|---|---|
| Routine and risk-bearing cases choose appropriate eligible profiles under the configured policy | `test_routine_work_gets_a_routine_profile`, `test_a_small_auth_patch_is_risk_bearing_regardless_of_size`, `test_word_matching_is_whole_word`, `test_effort_positions_rank_each_providers_own_labels`; service: `test_a_small_security_patch_gets_a_deep_review` | TESTED_SIM |
| Unsupported model/effort settings are rejected or explicitly downgraded with evidence | `test_unsupported_settings_are_excluded_with_evidence`; service: `test_a_refused_setting_is_downgraded_and_a_clamp_is_reported` (the organisation refuses `max`, the turn is re-routed to `xhigh`, and the observed `medium` is flagged) | TESTED_SIM |
| An environment failure does not trigger pointless stronger-model retries | `test_environment_failures_are_diagnosed_not_escalated`; service: `test_an_environment_failure_is_diagnosed_not_escalated` | TESTED_SIM |
| Budget pressure cannot route below the required quality floor | `test_budget_pressure_never_goes_below_the_floor` | TESTED_SIM |

Acceptance tests:

| ID | Evidence |
|---|---|
| AT16 | small auth patch → deep review regardless of line count (pure and service tests) |
| AT17 | refused setting downgraded with evidence; a clamped effort is flagged as a difference |
| AT18 | `test_a_user_pin_is_respected_even_below_the_floor`: the pin is used, `floor_met` is false, and no provider settings are written |
| AT19 | environment failure → `diagnose_environment`, no escalation |
| AT20 | two hypothesis failures → re-plan with one escalation; afterwards, re-plan without another escalation (with D06 loop control) |
| AT41 | native sessions get `advisory` decisions; managed settings change only between turns (`test_profile_requests_raise_managed_and_advise_native`) |

## Tests

- New: `tests/routing/test_routing_policy.py` (26) and
  `tests/integrations/test_routing_service.py` (6).
- Suites: core as root, 849 passed and 6 skipped; with `mcp==2.2.0` as
  root, 854 passed and 4 skipped; whole suite as non-root with MCP, 854
  passed and 4 skipped.

## Limits

- Risk detection is keyword and path based: explainable and conservative,
  not semantic. A risky change in innocuously named files is assessed by
  size alone, unless an agent raises it.
- Effort rankings come from each provider's documented label order. With
  no user map, models are left at their defaults. Codex can change models,
  but DUET picks a model only when the user has mapped one.
- A native session's model cannot be changed by DUET. Its decisions are
  recorded advice.
- Switch cost is qualitative (none, low, high). Token costs of context
  reloads are not estimated.
- Routing never blocks a turn. When the routing library fails, the
  provider defaults apply and the failure is logged.
