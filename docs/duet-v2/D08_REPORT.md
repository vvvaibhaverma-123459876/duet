# D08 Report: completion-aware admission and finishing reserves

DUET now asks before it dispatches a provider turn: may this action run now,
given the allowances the user set, the provider's quota readings, and the
capacity held back to finish the run? The answer is admit, defer or pause.
It is never a silent overspend and never a paid fallback. Everything here
is exercised with scripted or emulated providers. No real provider traffic
was involved.

## What exists now

| Piece | Module | What it does |
|---|---|---|
| Estimates | `duet/usage/estimation.py` | Explainable ranges. A turn uses exactly 1 of a turns pool. For cost, the high end is the largest recent observed turn plus 50% headroom (25% after five samples). With no observed turn the estimate is unknown, never an invented number. |
| Decisions | `duet/usage/admission.py` | Pure `decide()`. It applies the 7.3 inequality per pool; finishing actions draw on their earmarked reserve; pools are provider scoped; there is a bounded policy for unknown sizes and unknown quota; quota thresholds (defer optional work at 90%, pause the provider at 100% until the window's reset); a single probe after a pause; and per-pool enforcement labels. `plan_finishing()` sizes the finishing reserve. |
| Reservations | `duet/usage/reservations.py`, migration `0006_admission.sql` | `ReservationBook` gathers pools, reserves, gauges and holds inside one write transaction. It decides, then plans the admitted action with its reservations; a line drawn from a reserve moves capacity instead of holding it twice. Every decision is recorded. Quota gauges and holds are persisted as events. |
| Run control | `duet/runtime/budgeting.py` | Reserves finishing capacity when the pair forms. Applies pause verdicts: a participant pause with a notice to the peer, `PAUSED_QUOTA` when nobody else can move the run, `PAUSED_BUDGET` or `PAUSED_APPROVAL` for the user. Resumes paused runs (from the user, or from the service monitor once a quota hold reaches its resume time). Settles turns a restart left in doubt. |
| Managed turns | `duet/runtime/peers.py` | Each turn is classified: a review is finishing work; a repair while the task has changes requested is finishing; implementing the open task, answering questions and blockers is required; the rest is optional. The turn is then admitted. A quota failure pauses the provider (not the peer) until the reset or a bounded backoff; auth and billing failures still stop it. The Claude per-call `--max-budget-usd` is passed only for a provider_cap pool. Before retrying, Codex's quota windows are read passively (`account/rateLimits/read`, no model call). |
| Store | `duet/runtime/{api,pools,reducer}.py` | `plan_action` accepts reservations drawn from a finishing reserve. Reservations must match the pool's provider and metric. A run that ends releases its reserves. Pool status reports `finishing_held` and `overshoot`. |
| CLI | `duet usage` (`duet.usage/2`), `duet usage pool set --enforcement provider_cap`, `duet resume --run RUN` | Gauges (labelled "observed, includes use outside DUET; not a balance"), holds, finishing reserves and recent admissions. |

## Exit criteria

| Criterion | Evidence | Status |
|---|---|---|
| Optional exploration cannot consume reserved mandatory finishing capacity | `test_optional_work_cannot_spend_the_finishing_reserve` (pure); `test_review_capacity_is_reserved_and_drawn_once` and `test_another_runs_optional_work_cannot_take_this_runs_reserve` (store); `test_local_turn_allowance_protects_the_review` (service: two review turns are reserved at pair formation, the second optional FINDING turn is deferred and the peer is told, then the review runs from its reserve and the run completes) | TESTED_SIM |
| A required Claude review cannot be funded solely from Codex capacity, or vice versa | `test_capacity_of_one_provider_never_funds_another` (pure); `test_a_claude_review_is_never_funded_from_codex_capacity` (store: 100 Codex turns free and 0 Claude turns means the Claude review pauses for the user, the Codex pool is untouched, and a cross-provider or cross-metric reservation is refused by `plan_action`); `test_finishing_reserves_go_to_the_provider_that_must_do_the_work` | TESTED_SIM |
| No paid fallback, purchase, automatic redemption or account rotation occurs | `test_decisions_never_name_another_provider`; the service tests assert one adapter per provider across pause and resume, no API-key variables in any request, and notices saying DUET does not switch accounts, providers or billing. Nothing in D08 can select a provider, account or billing mode. | TESTED_SIM |
| Quota loss preserves review pending; passing local tests alone cannot bypass it | `test_peer_quota_loss_keeps_the_review_pending` (AT07: the Codex reviewer hits its limit; the native Claude writer is told and may continue; checks pass, but `non_author_review` stays unmet and the run does not complete; after the resume time one probe turn reviews and the run completes); `test_managed_pair_pauses_for_quota_and_resumes_after_the_reset` (both managed: `PAUSED_QUOTA`, then the monitor resumes with RECONCILING → REVIEWING) | TESTED_SIM |

## Acceptance tests

| ID | Result | Evidence |
|---|---|---|
| AT07 | Implementation may continue, pair success stays unavailable | `test_peer_quota_loss_keeps_the_review_pending` |
| AT12 | Refresh triggers re-plan with uncertainty | `test_external_usage_shows_as_a_gauge_change_and_replans`: a reading of 93% after 40% defers optional work but not required work. The gauge keeps the previous reading and says it is not a balance. A late, older reading never replaces a newer one. |
| AT13 | Optional work deferred, reserve preserved | see exit criteria |
| AT14 | Earmarked allocation drawn once | `test_a_finishing_action_draws_its_reserve_once`; `test_review_capacity_is_reserved_and_drawn_once` (pool held stays 2 during the review, not 3); `test_what_the_reserve_does_not_cover_needs_free_capacity` |
| AT15 | Limitation explicit before paid work | `test_a_provider_cap_is_used_only_where_the_provider_enforces_it`; `test_an_unenforceable_cap_stops_paid_work_before_it_starts` (a Codex `provider_cap` cost pool: `PAUSED_APPROVAL` with zero provider requests; the user switches the pool to `local_bound` and runs `resume`); `test_a_provider_enforced_cap_is_passed_per_call` (Claude gets `--max-budget-usd` = what is left) |
| AT40 | Refresh/reconcile before admission; no probe storm | `test_after_a_quota_pause_one_probe_goes_first`, `test_quota_holds_probe_once_and_release` (two runs: one probe, the other waits for its outcome), `test_a_passive_quota_read_ends_the_pause_without_a_probe` |
| AT46 | Actuals and uncertainty recorded; no fictional cap | `test_overshoot_is_recorded_not_hidden`: a turn reserving 0.15 that used 0.90 shows `overshoot` 0.75 and the next turn pauses. `test_unknown_turn_size_is_bounded_not_imagined`: turns of unknown size run one at a time and are labelled as able to overshoot. |

## Review findings folded in (internal Claude review of D06/D07)

An internal review agent audited D06 and D07 at `5814b11` and reproduced
each finding. This is not the independent Codex review. The usage findings
are fixed here. The task-graph findings (1–4, 9–11) are being fixed in a
separate change; see `HANDOFF.md`.

| Finding | Fix | Test |
|---|---|---|
| 5: a turn counted twice between recording its usage and settling its reservation, refusing another run (or for ever after a crash in that gap) | A HELD reservation whose action already has a usage record in the pool no longer counts | `test_recorded_usage_supersedes_the_reservation` |
| 6: a cost pool never refused at exactly zero remaining, including a $0 allowance | A zero reservation needs `available > 0`; unknown-size turns need room | `test_an_exhausted_cost_pool_refuses_zero_reservations`, `test_an_exhausted_cost_pool_stops_the_turn_before_dispatch` |
| 7: per-turn cost went wrong (negative, then dropped) when a resume returned a new session id | The delta comes from the run's total across the peer's sessions; a negative delta is recorded as unknown | `test_turn_cost_survives_a_resume_that_returns_a_new_session` |
| 8: an in-doubt turn's usage became zero, and nothing settled in-doubt turns | At service start an in-doubt provider turn is recorded as one dispatched turn with unknown cost and settled FAILED. It is never re-dispatched. A lost probe re-places its hold. | `test_in_doubt_turns_count_as_dispatched_with_unknown_cost` |
| 12: "only the user can define a pool" overstated the protection | The wording is now "cooperative". `duet usage pool set` writes as the local user, and any process running as that user can do the same (see D-026). | docs |

## Tests

- New: `tests/usage/test_admission.py` (19),
  `tests/runtime/test_finishing_reserves.py` (11),
  `tests/integrations/test_admission_service.py` (7). One service test was
  rewritten for D08 semantics: `test_local_turn_allowance_is_reserved_and_recorded`
  became `test_local_turn_allowance_protects_the_review`. With the review now
  reserved, an optional turn can no longer spend the last of an allowance.
- Full suite: 802 passed, 6 skipped (core). The MCP venv and non-root
  results are in `HANDOFF.md`.

## Limits

- Pools, reserves and holds bound only the turns DUET schedules (managed
  peers). A native session's own turns are neither admitted nor reserved.
  When the reviewer is native, no review reserve is made.
- Quota holds are keyed by provider, assuming one login per provider CLI on
  this machine. Usage outside DUET shows up only as a changed gauge, and
  Claude quota is observed only from its status line (installer: D12) or
  from its errors.
- A finishing reserve is sized when the pair forms, from the pools that
  exist then. A pool defined later gets a reserve only when the next
  admission for that run creates it. Re-estimation happens after each
  managed turn (cost pools only).
- `PAUSED_BUDGET` and `PAUSED_APPROVAL` resume only through `duet resume`.
  If the service has exited meanwhile, managed peers are not relaunched.
  `PAUSED_QUOTA` keeps the service alive and resumes by itself.
- A deferred optional turn is acknowledged, and the peer is told. A deferred
  plan or task proposal stays pending until its proposer withdraws or
  repeats it: nothing re-offers it when pressure eases.
- Codex reports no cost. Codex cost pools therefore stay uncertain and admit
  one unknown-size turn at a time. Codex cannot take a `provider_cap` cost
  pool at all.
- The thresholds (90% and 100%), the headroom (x1.5, then x1.25), the reserve
  sizes (two review turns, two repair turns) and the backoff (5 to 60
  minutes) are conservative defaults, not calibrated. `AdmissionPolicy`
  holds them.
