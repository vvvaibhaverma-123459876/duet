# Capability and Traceability Matrix

This matrix maps each requirement (R) and acceptance test (AT) in the v2
specification to its milestone, the tests that cover it, and its evidence
status. It is updated at each milestone.

**Status legend** (highest level reached):
`NOT_STARTED` · `LEGACY_PARTIAL` (exists in v1 with gaps) · `IMPLEMENTED` (code
merged, not yet tested) · `TESTED_SIM` (automated tests against
fixtures/emulators) · `REVIEWED` (independent non-author review recorded) ·
`LIVE_PROVEN` (real authenticated provider run recorded) · `NOT_RUN` (a
required real test that has not been executed).

A skipped real-provider test is `NOT_RUN`. It is never evidence of
compatibility.

## Client capability evidence (observed, not assumed)

| Client | Version | How observed | Relevant capabilities | Not established |
|---|---|---|---|---|
| Claude Code | 2.1.283 | `claude --help` (fixture) | `-p`; `--output-format json\|stream-json`; `--input-format stream-json`; `--model`; `--effort low..max`; `--resume`; `--session-id`; `--fork-session`; `--mcp-config`; `--strict-mcp-config`; `--permission-mode`; `--allowed-tools`; `--max-budget-usd` (provider-enforced, `--print` only); `--no-session-persistence` | Authenticated output schema on this machine; whether effort/model are applied (observed settings); hook/status-line payloads |
| Codex CLI | 0.157.1 | installed in the session scratchpad only; `--help`, generated app-server JSON schema, live unauthenticated handshake (fixtures) | app-server v2: initialize, model/list with per-model efforts, thread start/resume/fork, turn start/interrupt, token-usage and rate-limit notifications; exec `--json` events | Authenticated turns (no login, and the environment blocks api.openai.com): success-path events come from the schema, not live observation |
| MCP Python SDK | 2.2.0 (PyPI) | package metadata | stdio server/client | Compatibility beyond the tested version |

## Requirements

| ID | Requirement | Milestones | Status |
|---|---|---|---|
| R01 | Partnership with evidenced contributions | D05, D06, D10 | TESTED_SIM (both providers registered; contributions recorded only from evidence: authored changes, reviews, accepted task results and plans; messages never count; peer loss holds completion); joint-authorship reviews: D10 |
| R02 | Original-session continuity | D05, D12 | TESTED_SIM (native session stays the participant: rejoin is idempotent, restart reconnects via host identity, a dead host becomes `gone` and its token stops working); LIVE: NOT_RUN |
| R03 | Two-way initiative | D05 | TESTED_SIM (either side asks at any time; non-blocking send; waits wake on questions) — real models: NOT_RUN |
| R04 | One objective/contract | D02, D06 | TESTED_SIM (one run, versioned acceptance contract, user-only changes; shared plans only add tasks and cannot touch the objective or contract) |
| R05 | Bounded autonomy | D02, D03 | TESTED_SIM (runtime: principal-bound authority, narrowing-only project policy); workspace enforcement: D03 |
| R06 | Honest resources | D01, D07 | TESTED_SIM (legacy nullable cost; v2 observations with quality/scope/epoch, unknown never zero, context and quota as gauges, deduplicated pool records, cumulative totals as deltas) |
| R07 | Completion reserve | D08 | TESTED_SIM (review and repair turns reserved per provider when the pair forms; finishing turns draw once; optional work cannot touch reserves; quota loss keeps the review pending) |
| R08 | Supported controls only | D04, D09 | TESTED_SIM (discovery; requested/accepted/observed recorded per routed turn, differences flagged; refused settings excluded with evidence; native sessions advisory) |
| R09 | Evidence-based completion | D01, D03, D10 | TESTED_SIM (predicate with criteria demonstrated by fail-to-pass or explicit review; per-file non-author coverage; deterministic final report) |
| R10 | Revision integrity | D03, D10 | TESTED_SIM (evidence and reviews keyed to snapshot + contract hash; delta reviews need a recorded basis; reuse by fingerprint) |
| R11 | Safe writes | D03, D11 | TESTED_SIM (strict worktree; one fenced writer; isolated task worktrees with their own leases; fenced integration by the writer; conflicts write nothing) |
| R12 | Recoverability | D02, D11 | TESTED_SIM (atomic transactions; IN_DOUBT never re-dispatched; in-doubt turns and integrations settled from evidence at start); chaos harness: not built |
| R13 | No silent paid fallback | D04, D08 | TESTED_SIM (no `--bare`, no API-key paths, billing errors never retried as quota; admission only admits, defers or pauses the same provider; quota pauses wait for the reset; an unenforceable cap pauses before paid work) |
| R14 | No weakened standards | D01, D08 | LEGACY: TESTED_SIM (solo mode yields `review_pending`); v2: TESTED_SIM (quota or budget pressure defers optional work and pauses; the review and checks stay required) |
| R15 | Compatibility | D01, D13 | LEGACY_PARTIAL (documented outcome/exit-code change D-002; v1 transcript migration) |

## Acceptance tests

| ID | Scenario | Milestone | Test(s) | Status |
|---|---|---|---|---|
| AT01 | Launch from existing Claude | D05 | simulated: `test_native_claude_and_managed_codex_talk_both_ways_and_ship_a_reviewed_patch` (service), `test_native_claude_and_native_codex_pair_over_mcp` (real MCP proxies); procedure: `PEER_ALPHA_TEST.md` | TESTED_SIM; LIVE: NOT_RUN |
| AT02 | Launch from existing Codex | D05 | simulated: `test_native_codex_initiates_and_answers_a_claude_follow_up` (native Codex starts the run, managed Claude asks back, the original Codex answers), `test_invite_is_single_use_and_provider_bound`; procedure: `PEER_ALPHA_TEST.md` | TESTED_SIM; LIVE: NOT_RUN |
| AT03 | DUET launches both managed | D05 | `test_duet_pair_with_emulated_managed_sessions` (CLI → service → real adapters → emulated provider processes → `duet mcp serve`), `test_duet_originated_pair_is_labelled_managed`; gated real: `tests/e2e_peer/test_real_pair.py` | TESTED_SIM; LIVE: NOT_RUN |
| AT04 | Both ask while waiting | D05 | `test_a_asks_b_b_asks_a_a_answers_b_continues`, `test_simultaneous_questions_do_not_deadlock`, `test_waits_are_served_concurrently`, MCP pair test (wait already blocked when the question arrives) | TESTED_SIM |
| AT05 | Redelivery/reconnect | D02/D05 | `test_redelivery_until_acknowledged`, `test_unacknowledged_messages_are_redelivered_after_reconnect`, `test_proxy_restart_reconnects_the_same_session` (MCP), `test_restart_settles_interrupted_checks` | TESTED_SIM |
| AT06 | Live delivery unsupported | D12 | `duet capabilities` (live delivery unavailable), `test_stop_hook_asks_the_session_to_answer_its_peer` | TESTED_SIM |
| AT07 | Peer quota before mandatory review | D08 | `test_peer_quota_loss_keeps_the_review_pending` | TESTED_SIM |
| AT08 | Unknown usage | D07 | `test_unknown_usage_is_never_zero`, `test_absent_final_usage_after_cancellation_stays_unknown`, `test_usage_records_are_deduplicated_and_unknown_is_not_zero` (pool marked uncertain) | TESTED_SIM |
| AT09 | Two runs share pool | D07 | `test_two_models_and_two_runs_share_one_pool_without_double_counting`, `test_reservations_draw_down_and_refuse_atomically`, `test_two_processes_cannot_both_take_the_last_unit` | TESTED_SIM |
| AT10 | Duplicate cumulative telemetry | D07 | `test_statusline_and_cli_result_count_the_same_turn_once`, `test_replayed_codex_updates_count_once`, `test_cumulative_resume_totals_become_deltas`, emulated pair cost pool | TESTED_SIM |
| AT11 | Context counter changes | D07 | `test_context_occupancy_never_enters_consumption`, `test_a_decrease_is_a_reset_not_a_negative_or_absolute_delta` | TESTED_SIM |
| AT12 | External usage consumes capacity | D08 | `test_external_usage_shows_as_a_gauge_change_and_replans` | TESTED_SIM |
| AT13 | Optional work vs finishing reserve | D08 | `test_local_turn_allowance_protects_the_review`, `test_optional_work_cannot_spend_the_finishing_reserve` | TESTED_SIM |
| AT14 | Mandatory action draws reserve once | D08 | `test_review_capacity_is_reserved_and_drawn_once`, `test_a_finishing_action_draws_its_reserve_once` | TESTED_SIM |
| AT15 | Hard billing cap unavailable | D08 | `test_an_unenforceable_cap_stops_paid_work_before_it_starts`, `test_a_provider_enforced_cap_is_passed_per_call` | TESTED_SIM |
| AT16 | Small security-sensitive patch | D09 | `test_a_small_security_patch_gets_a_deep_review` | TESTED_SIM |
| AT17 | Unsupported effort / org clamp | D09 | `test_a_refused_setting_is_downgraded_and_a_clamp_is_reported` | TESTED_SIM |
| AT18 | User pins model/effort | D09/D12 | `test_a_user_pin_is_respected_even_below_the_floor` | TESTED_SIM (D09 part) |
| AT19 | Missing dependency failure | D09 | `test_an_environment_failure_is_diagnosed_not_escalated` | TESTED_SIM |
| AT20 | Repeated no-progress repairs | D06/D09 | `test_repeated_failures_replan_then_pause` (D06), `test_repeated_hypothesis_failures_replan_with_one_bounded_escalation` | TESTED_SIM (D09 part) |
| AT21 | Old green suite, feature absent | D01/D10 | `test_a_suite_green_before_the_change_does_not_demonstrate_the_criterion` | TESTED_SIM |
| AT22 | DONE with missing verifier | D01 | `test_d01_semantics.py::TestCompletion::test_done_with_missing_verifier_is_unverified`, battery `test_scratch_run_without_verifier_is_unverified` | TESTED_SIM |
| AT23 | Approval of old revision | D10 | `test_an_approval_is_stale_after_a_change_unless_a_delta_review_names_its_basis` | TESTED_SIM |
| AT24 | Agent edits acceptance policy | D03/D10 | `test_a_new_contract_version_voids_earlier_evidence` | TESTED_SIM |
| AT25 | Joint authorship reviews | D10 | `test_joint_work_needs_per_file_review_and_both_acknowledgements` | TESTED_SIM |
| AT26 | Dirty active checkout | D03 | `test_user_checkout_is_untouched`, `test_import_inputs_is_explicit_and_checked` | TESTED_SIM |
| AT27 | Secret-bearing ignored file / escaping symlink | D03/D13 | `test_inputs_exclude_ignored_sensitive_and_escaping_links`, `test_import_inputs_is_explicit_and_checked`, `test_sensitive_classifier` | TESTED_SIM (D03); threat-model pass: D13 |
| AT28 | Concurrent writers | D11 | `test_two_agents_write_at_once_in_separate_checkouts` | TESTED_SIM |
| AT29 | Crash around dispatch | D02/D11 | `test_an_integration_done_before_a_crash_is_not_repeated`, `test_in_doubt_is_never_redispatched` | TESTED_SIM |
| AT30 | Crash during commit/integration | D11 | `test_an_integration_lost_before_it_wrote_is_redone_from_evidence`, `test_in_doubt_turns_count_as_dispatched_with_unknown_cost` | TESTED_SIM (no chaos harness) |
| AT31 | Cancel while waiting/working/verifying | D04/D11 | legacy interrupt tests; v2 `test_cancel_interrupts_the_turn` (Claude SIGINT), `test_cancel_uses_turn_interrupt` (Codex), `test_cancel_while_waiting` (pair wait wakes with CANCELLED) | TESTED_SIM (waiting, working); verifying across a crash: D11 |
| AT32 | PID reuse / unrelated sessions | D11 | `test_stopping_duet_leaves_unrelated_sessions_alone` | TESTED_SIM |
| AT33 | Peer text claims user approval | D02/D13 | `TestAuthority::test_peer_text_cannot_grant_approval`, `test_message_text_carries_no_authority`, `test_authentication_rules` (identity never a parameter) | TESTED_SIM (runtime + service); installer threat model: D13 |
| AT34 | Malformed/flooded provider output | D04 | legacy `test_process.py`; v2 `test_flood_is_bounded`, `test_stream_process_truncates_long_lines`, malformed-line and server-death tests | TESTED_SIM |
| AT35 | Unknown/failed/skipped required check | D03/D10 | `test_unknown_or_missing_required_check_blocks`, `test_no_tests_is_not_a_pass`, `test_contract_without_checks_can_never_verify` | TESTED_SIM |
| AT36 | Inputs mutate during verification | D03 | `test_mutation_during_check_invalidates`, `test_transient_caches_do_not_invalidate` | TESTED_SIM |
| AT37 | Legacy transcript/config migration | D13 | | NOT_STARTED |
| AT38 | Wheel without source tree | D13 | `test_packaging.py` (defaults, migrations 0001–0003, participant instructions, Claude skill) | TESTED_SIM |
| AT39 | Integration install/uninstall | D12/D13 | `test_setup_is_planned_approved_and_reversible`, `test_uninstall_leaves_what_the_user_changed_and_what_it_did_not_own` | TESTED_SIM (fake CLIs) |
| AT40 | Quota reset with stale telemetry | D08/D11 | `test_quota_holds_probe_once_and_release`, `test_a_passive_quota_read_ends_the_pause_without_a_probe` | TESTED_SIM (D08 part); crash matrix: D11 |
| AT41 | Mid-turn setting change unsupported | D04/D09 | `test_native_sessions_get_advice`, `test_profile_requests_raise_managed_and_advise_native` | TESTED_SIM |
| AT42 | Resume/fork semantics differ | D04/D12 | `test_resume_is_session_cumulative`, `test_fork_gets_new_id`, `test_resume_keeps_thread_and_fork_changes_it`, `test_lineage_labels` | TESTED_SIM (emulated); LIVE: NOT_RUN |
| AT43 | No sandbox for borrowed session | D13 | | NOT_STARTED |
| AT44 | Same snapshot checked twice | D10 | `test_an_identical_snapshot_reuses_its_evidence` | TESTED_SIM |
| AT45 | Native subagents spawn | D11 | | NOT_STARTED |
| AT46 | Estimate overshoot before final usage | D08/D11 | `test_overshoot_is_recorded_not_hidden`, `test_in_doubt_turns_count_as_dispatched_with_unknown_cost` | TESTED_SIM (D08 part); crash matrix: D11 |
| AT47 | Integration changes tested tree | D10/D11 | | NOT_STARTED |
| AT48 | Release handoff | D14 | | NOT_STARTED |
