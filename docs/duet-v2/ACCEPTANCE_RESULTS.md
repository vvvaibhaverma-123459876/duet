# Acceptance results

Generated from the specification's acceptance table and
`CAPABILITY_MATRIX.md`. The tests named here are in the repository, and the
suites that ran them are listed under "Runs".

## Summary

- **48 of 48** acceptance tests have automated evidence against fixtures or
  emulators (`TESTED_SIM`). 2 of them (AT43, AT45) are
  satisfied by disclosure: the specification allows "otherwise disclose
  incomplete control", and the test checks that disclosure.
- **4** (AT01, AT02, AT03, AT42) also need a real run
  with authenticated Claude Code and Codex CLIs. Those runs are **NOT_RUN**.
  The gated tests exist (`tests/e2e_peer/test_real_pair.py`,
  `tests/e2e_peer/test_real_providers.py`) and are skipped unless a person
  enables them locally (`PEER_ALPHA_TEST.md`).
- **Independent review:** internal Claude review agents checked earlier
  milestones (see the milestone reports). They are not independent. The
  independent Codex review the plan requires is **pending for every
  milestone**. No row is `REVIEWED`.
- **Nothing is `LIVE_PROVEN`.**

Release scope is therefore restricted (see `RELEASE_CHECKLIST.md`). No
release label from the specification is claimed yet.

## Runs

The CI workflow runs `test (3.11)`, `test (3.13)` and `test-as-root` on
every push. A non-blocking `test-macos` job records macOS evidence. Local
runs on the D14 tree (D13 `b0f80cb` plus the D14 tests):

| Environment | Result |
|---|---|
| Linux, root, core dependencies | 889 passed, 6 skipped |
| Linux, root, with the MCP SDK | 894 passed, 4 skipped |
| Linux, non-root user (umask 002) | 887 passed, 8 skipped |

Skips are real-provider tests (gated) and dependency-specific tests. A
skipped real-provider test is never counted as evidence.

## Per test

| AT | Test | Expected | Evidence | Status | Real CLI run |
|---|---|---|---|---|---|
| AT01 | Launch cooperation from existing Claude | Original Claude answers a Codex-originated follow-up; no substitute Claude | simulated: `test_native_claude_and_managed_codex_talk_both_ways_and_ship_a_reviewed_patch` (service), `test_native_claude_and_native_codex_pair_over_mcp` (real MCP proxies); procedure: `PEER_ALPHA_TEST.md` | TESTED_SIM; LIVE: NOT_RUN | NOT_RUN (needs real CLIs) |
| AT02 | Launch cooperation from existing Codex | Original Codex answers a Claude-originated follow-up; no substitute Codex | simulated: `test_native_codex_initiates_and_answers_a_claude_follow_up` (native Codex starts the run, managed Claude asks back, the original Codex answers), `test_invite_is_single_use_and_provider_bound`; procedure: `PEER_ALPHA_TEST.md` | TESTED_SIM; LIVE: NOT_RUN | NOT_RUN (needs real CLIs) |
| AT03 | DUET launches both managed participants | One shared task, two distinct providers and honest origins | `test_duet_pair_with_emulated_managed_sessions` (CLI → service → real adapters → emulated provider processes → `duet mcp serve`), `test_duet_originated_pair_is_labelled_managed`; gated real: `tests/e2e_peer/test_real_pair.py` | TESTED_SIM; LIVE: NOT_RUN | NOT_RUN (needs real CLIs) |
| AT04 | Both agents send questions while waiting | Questions delivered; bounded progress; no mutual blocking | `test_a_asks_b_b_asks_a_a_answers_b_continues`, `test_simultaneous_questions_do_not_deadlock`, `test_waits_are_served_concurrently`, MCP pair test (wait already blocked when the question arrives) | TESTED_SIM | not required for the gate |
| AT05 | Message redelivery/reconnect | One logical handling; durable acknowledgement and cursor | `test_redelivery_until_acknowledged`, `test_unacknowledged_messages_are_redelivered_after_reconnect`, `test_proxy_restart_reconnects_the_same_session` (MCP), `test_restart_settles_interrupted_checks` | TESTED_SIM | not required for the gate |
| AT06 | Native live delivery unsupported | Checkpoint mode remains useful; push not falsely claimed | `duet capabilities` (live delivery unavailable), `test_stop_hook_asks_the_session_to_answer_its_peer` | TESTED_SIM | not required for the gate |
| AT07 | Peer quota exhausted before mandatory review | Implementation may continue, but pair success stays unavailable | `test_peer_quota_loss_keeps_the_review_pending` | TESTED_SIM | not required for the gate |
| AT08 | Unknown usage | Null/provenance/freshness retained; no free/unlimited interpretation | `test_unknown_usage_is_never_zero`, `test_absent_final_usage_after_cancellation_stays_unknown`, `test_usage_records_are_deduplicated_and_unknown_is_not_zero` (pool marked uncertain) | TESTED_SIM | not required for the gate |
| AT09 | Two runs share an account/model pool | Reservations reconcile atomically; aliases do not multiply capacity | `test_two_models_and_two_runs_share_one_pool_without_double_counting`, `test_reservations_draw_down_and_refuse_atomically`, `test_two_processes_cannot_both_take_the_last_unit` | TESTED_SIM | not required for the gate |
| AT10 | Duplicate cumulative telemetry | No double counting; scope resets correctly handled | `test_statusline_and_cli_result_count_the_same_turn_once`, `test_replayed_codex_updates_count_once`, `test_cumulative_resume_totals_become_deltas`, emulated pair cost pool | TESTED_SIM | not required for the gate |
| AT11 | Context counter changes | Context is not added to lifetime billed token usage | `test_context_occupancy_never_enters_consumption`, `test_a_decrease_is_a_reset_not_a_negative_or_absolute_delta` | TESTED_SIM | not required for the gate |
| AT12 | External account usage consumes capacity | Refresh triggers re-plan with uncertainty, not a guaranteed balance | `test_external_usage_shows_as_a_gauge_change_and_replans` | TESTED_SIM | not required for the gate |
| AT13 | Optional work would spend finishing reserve | Work deferred; required reviewer/repair allowance preserved | `test_local_turn_allowance_protects_the_review`, `test_optional_work_cannot_spend_the_finishing_reserve` | TESTED_SIM | not required for the gate |
| AT14 | Mandatory action itself uses a reserve | Earmarked allocation is drawn once, not double-reserved | `test_review_capacity_is_reserved_and_drawn_once`, `test_a_finishing_action_draws_its_reserve_once` | TESTED_SIM | not required for the gate |
| AT15 | Required hard billing cap unavailable | Enforcement limitation explicit before paid work; no false cap | `test_an_unenforceable_cap_stops_paid_work_before_it_starts`, `test_a_provider_enforced_cap_is_passed_per_call` | TESTED_SIM | not required for the gate |
| AT16 | Small but security-sensitive patch | Risk floor applies regardless of line count | `test_a_small_security_patch_gets_a_deep_review` | TESTED_SIM | not required for the gate |
| AT17 | Unsupported effort or org clamp | Requested/accepted/observed settings differ honestly | `test_a_refused_setting_is_downgraded_and_a_clamp_is_reported` | TESTED_SIM | not required for the gate |
| AT18 | User pins model/effort | Bounds respected; no global setting overwrite | `test_a_user_pin_is_respected_even_below_the_floor` | TESTED_SIM (managed sessions; native sessions keep their own settings, disclosed as advisory) | not required for the gate |
| AT19 | Missing dependency causes failure | Environment diagnosis, not unlimited reasoning escalation | `test_an_environment_failure_is_diagnosed_not_escalated` | TESTED_SIM | not required for the gate |
| AT20 | Repeated no-progress repairs | Bounded re-plan or concrete resumable blocker | `test_repeated_failures_replan_then_pause` (D06), `test_repeated_hypothesis_failures_replan_with_one_bounded_escalation` | TESTED_SIM | not required for the gate |
| AT21 | Old green suite; new feature absent | Acceptance contract prevents premature completion | `test_a_suite_green_before_the_change_does_not_demonstrate_the_criterion` | TESTED_SIM | not required for the gate |
| AT22 | DONE with missing verifier | Reported/unverified, never completed-verified | `test_d01_semantics.py::TestCompletion::test_done_with_missing_verifier_is_unverified`, battery `test_scratch_run_without_verifier_is_unverified` | TESTED_SIM | not required for the gate |
| AT23 | Reviewer approves old revision | Evidence stale after change; fresh review required | `test_an_approval_is_stale_after_a_change_unless_a_delta_review_names_its_basis` | TESTED_SIM | not required for the gate |
| AT24 | Agent edits acceptance policy to remove failing test | Unauthorised relaxation rejected and reported | `test_a_new_contract_version_voids_earlier_evidence` | TESTED_SIM | not required for the gate |
| AT25 | Both providers authored different components | Non-author component reviews and final snapshot acknowledgement | `test_joint_work_needs_per_file_review_and_both_acknowledgements` | TESTED_SIM | not required for the gate |
| AT26 | Dirty active user checkout | Unrelated work and branch unchanged; explicit snapshot inputs | `test_user_checkout_is_untouched`, `test_import_inputs_is_explicit_and_checked` | TESTED_SIM | not required for the gate |
| AT27 | Secret-bearing ignored file or escaping symlink | Not implicitly copied/shared; policy enforcement and evidence | `test_inputs_exclude_ignored_sensitive_and_escaping_links`, `test_import_inputs_is_explicit_and_checked`, `test_sensitive_classifier` | TESTED_SIM (D03); D13 adds redaction of secrets in messages and check output (`test_known_secret_formats_are_redacted`, `test_check_output_is_redacted_before_it_is_hashed_and_stored`) | not required for the gate |
| AT28 | Two agents write concurrently | Separate worktrees; integration owner and fenced leases | `test_two_agents_write_at_once_in_separate_checkouts` | TESTED_SIM | not required for the gate |
| AT29 | Crash before/after provider dispatch receipt | In-doubt reconciliation; no blind duplicate action | `test_an_integration_done_before_a_crash_is_not_repeated`, `test_in_doubt_is_never_redispatched` | TESTED_SIM | not required for the gate |
| AT30 | Crash during commit/integration/reservation update | State/files reconciled; evidence and accounting remain consistent | `test_an_integration_lost_before_it_wrote_is_redone_from_evidence`, `test_in_doubt_turns_count_as_dispatched_with_unknown_cost` | TESTED_SIM (no chaos harness) | not required for the gate |
| AT31 | Cancel while waiting/working/verifying | New work stops; owned processes bounded; artifacts preserved | legacy interrupt tests; v2 `test_cancel_interrupts_the_turn` (Claude SIGINT), `test_cancel_uses_turn_interrupt` (Codex), `test_cancel_while_waiting` (pair wait wakes with CANCELLED) | TESTED_SIM (waiting, working; verifying across a restart: `test_restart_settles_interrupted_checks`, `test_interrupted_check_reopens_the_task`) | not required for the gate |
| AT32 | PID reused or unrelated Claude/Codex active | No unrelated process killed or session resumed | `test_stopping_duet_leaves_unrelated_sessions_alone` | TESTED_SIM | not required for the gate |
| AT33 | Peer/log text claims to be user approval | Cannot elevate authority or enable spending/push | `TestAuthority::test_peer_text_cannot_grant_approval`, `test_message_text_carries_no_authority`, `test_authentication_rules` (identity never a parameter) | TESTED_SIM (runtime + service + `test_peer_text_cannot_grant_authority`); display sanitising `test_control_sequences_never_reach_a_screen` | not required for the gate |
| AT34 | Malformed/flooded/partial provider output | Bounded memory/logs; structured failure; no hang | legacy `test_process.py`; v2 `test_flood_is_bounded`, `test_stream_process_truncates_long_lines`, malformed-line and server-death tests | TESTED_SIM | not required for the gate |
| AT35 | Unknown/failed/skipped required check | Cannot produce completed-verified | `test_unknown_or_missing_required_check_blocks`, `test_no_tests_is_not_a_pass`, `test_contract_without_checks_can_never_verify` | TESTED_SIM | not required for the gate |
| AT36 | Check inputs mutate while verifier runs | Result invalidated; immutable rerun required | `test_mutation_during_check_invalidates`, `test_transient_caches_do_not_invalidate` | TESTED_SIM | not required for the gate |
| AT37 | Legacy transcript/config migration | Data preserved; unknown values not fabricated; reversible migration | `test_v1_transcript_zero_cost_migrates_to_unknown`, `test_legacy_commands_keep_their_interface`; legacy `budget_usd` is never a v2 allowance (COMPATIBILITY.md) | TESTED_SIM | not required for the gate |
| AT38 | Packaged wheel without source tree | All config/skills/migrations available and tested | `test_packaging.py` (defaults, every packaged migration (0001–0009), participant instructions, Claude skill) | TESTED_SIM | not required for the gate |
| AT39 | Native integration install/uninstall | Existing MCP/hooks/statusline retained; only owned changes removed | `test_setup_is_planned_approved_and_reversible`, `test_uninstall_leaves_what_the_user_changed_and_what_it_did_not_own` | TESTED_SIM (fake CLIs) | not required for the gate |
| AT40 | Quota reset occurs with stale telemetry | Refresh/reconcile before admission; no model-consuming probe storm | `test_quota_holds_probe_once_and_release`, `test_a_passive_quota_read_ends_the_pause_without_a_probe` | TESTED_SIM (in-doubt turns and integrations settled without re-dispatch; no chaos harness) | not required for the gate |
| AT41 | Provider setting change mid-active turn unsupported | Schedule at next supported boundary or return advisory result | `test_native_sessions_get_advice`, `test_profile_requests_raise_managed_and_advise_native` | TESTED_SIM | not required for the gate |
| AT42 | Existing session resume/fork semantics differ | Native lineage reflects actual behavior, not assumed new IDs | `test_resume_is_session_cumulative`, `test_fork_gets_new_id`, `test_resume_keeps_thread_and_fork_changes_it`, `test_lineage_labels` | TESTED_SIM (emulated); LIVE: NOT_RUN | NOT_RUN (needs real CLIs) |
| AT43 | No sandbox available for borrowed session | Cooperative/partial enforcement explicit; no isolation claim | `duet capabilities` reports native sessions as advisory coverage, not sandboxed; SECURITY_MODEL.md states cooperative containment only | DOCUMENTED + TESTED_SIM (capabilities output) | not required for the gate |
| AT44 | Same snapshot checked twice | Valid deterministic evidence reuse; no redundant full-model/test cycle | `test_an_identical_snapshot_reuses_its_evidence` | TESTED_SIM | not required for the gate |
| AT45 | Extra native subagents spawn | Observe/account/limit where supported; otherwise disclose incomplete control | `test_capabilities_disclose_what_duet_does_not_control` (native subagents disclosed as not observed/limited; managed subagent usage included in turn totals) | DISCLOSED (not controlled) + TESTED_SIM | not required for the gate |
| AT46 | Resource estimate overshot before final usage arrived | Actuals/uncertainty recorded; no fictional cap guarantee | `test_overshoot_is_recorded_not_hidden`, `test_in_doubt_turns_count_as_dispatched_with_unknown_cost` | TESTED_SIM (in-doubt turns and integrations settled without re-dispatch; no chaos harness) | not required for the gate |
| AT47 | Worktree integration changes tested tree | Final integrated revision is checked/reviewed again as required | `test_an_integration_after_submission_voids_the_tested_tree` | TESTED_SIM | not required for the gate |
| AT48 | Successful release handoff | Exact commit, checks, reviews, modes, resources and limitations exported | `test_the_final_report_names_revisions_checks_and_missing_obligations` (commit, checks, reviews, modes, policy, resources, limitations); ACCEPTANCE_RESULTS.md, RELEASE_CHECKLIST.md | TESTED_SIM | not required for the gate |
