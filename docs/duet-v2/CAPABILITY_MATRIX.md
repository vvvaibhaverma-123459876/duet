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
| R01 | Partnership with evidenced contributions | D05, D06, D10 | TESTED_SIM (pair runs need a registered Claude and Codex; completion requires authored snapshot + cross-provider review; peer loss holds completion); substantive-contribution scheduling: D06 |
| R02 | Original-session continuity | D05, D12 | TESTED_SIM (native session stays the participant: rejoin is idempotent, restart reconnects via host identity, a dead host becomes `gone` and its token stops working); LIVE: NOT_RUN |
| R03 | Two-way initiative | D05 | TESTED_SIM (either side asks at any time; non-blocking send; waits wake on questions) — real models: NOT_RUN |
| R04 | One objective/contract | D02, D06 | TESTED_SIM (runtime: one run, versioned acceptance contract, user-only changes); scheduler: D06 |
| R05 | Bounded autonomy | D02, D03 | TESTED_SIM (runtime: principal-bound authority, narrowing-only project policy); workspace enforcement: D03 |
| R06 | Honest resources | D01, D07 | LEGACY: TESTED_SIM (nullable cost, unknown turns counted separately, invalid costs rejected); v2 ledger NOT_STARTED |
| R07 | Completion reserve | D08 | NOT_STARTED |
| R08 | Supported controls only | D04, D09 | TESTED_SIM (discovery from help/model catalogue; requested/accepted/observed recorded; unsupported rejected); routing: D09 |
| R09 | Evidence-based completion | D01, D03, D10 | TESTED_SIM: legacy (D01) and v2 predicate with controller-only finalisation (D03); joint authorship: D10 |
| R10 | Revision integrity | D03, D10 | TESTED_SIM (evidence and reviews keyed to snapshot + contract hash; mutation invalidates); delta reviews and caching: D10 |
| R11 | Safe writes | D03, D11 | TESTED_SIM (strict worktree leaves the user checkout byte-identical; one fenced writer; explicit checked imports); integration locking: D11 |
| R12 | Recoverability | D02, D11 | TESTED_SIM (runtime: atomic transactions, IN_DOUBT reconciliation, no blind re-dispatch); full crash matrix: D11 |
| R13 | No silent paid fallback | D04, D08 | TESTED_SIM (no `--bare`, no API-key paths, billing errors never retried as quota); admission: D08 |
| R14 | No weakened standards | D01, D08 | LEGACY: TESTED_SIM (solo mode yields `review_pending`); v2 NOT_STARTED |
| R15 | Compatibility | D01, D13 | LEGACY_PARTIAL (documented outcome/exit-code change D-002; v1 transcript migration) |

## Acceptance tests

| ID | Scenario | Milestone | Test(s) | Status |
|---|---|---|---|---|
| AT01 | Launch from existing Claude | D05 | simulated: `test_native_claude_and_managed_codex_talk_both_ways_and_ship_a_reviewed_patch` (service), `test_native_claude_and_native_codex_pair_over_mcp` (real MCP proxies); procedure: `PEER_ALPHA_TEST.md` | TESTED_SIM; LIVE: NOT_RUN |
| AT02 | Launch from existing Codex | D05 | simulated: `test_native_codex_initiates_and_answers_a_claude_follow_up` (native Codex starts the run, managed Claude asks back, the original Codex answers), `test_invite_is_single_use_and_provider_bound`; procedure: `PEER_ALPHA_TEST.md` | TESTED_SIM; LIVE: NOT_RUN |
| AT03 | DUET launches both managed | D05 | `test_duet_pair_with_emulated_managed_sessions` (CLI → service → real adapters → emulated provider processes → `duet mcp serve`), `test_duet_originated_pair_is_labelled_managed`; gated real: `tests/e2e_peer/test_real_pair.py` | TESTED_SIM; LIVE: NOT_RUN |
| AT04 | Both ask while waiting | D05 | `test_a_asks_b_b_asks_a_a_answers_b_continues`, `test_simultaneous_questions_do_not_deadlock`, `test_waits_are_served_concurrently`, MCP pair test (wait already blocked when the question arrives) | TESTED_SIM |
| AT05 | Redelivery/reconnect | D02/D05 | `test_redelivery_until_acknowledged`, `test_unacknowledged_messages_are_redelivered_after_reconnect`, `test_proxy_restart_reconnects_the_same_session` (MCP), `test_restart_settles_interrupted_checks` | TESTED_SIM |
| AT06 | Live delivery unsupported | D12 | | NOT_STARTED |
| AT07 | Peer quota before mandatory review | D08 | | NOT_STARTED |
| AT08 | Unknown usage | D07 | | NOT_STARTED |
| AT09 | Two runs share pool | D07 | | NOT_STARTED |
| AT10 | Duplicate cumulative telemetry | D07 | | NOT_STARTED |
| AT11 | Context counter changes | D07 | | NOT_STARTED |
| AT12 | External usage consumes capacity | D08 | | NOT_STARTED |
| AT13 | Optional work vs finishing reserve | D08 | | NOT_STARTED |
| AT14 | Mandatory action draws reserve once | D08 | | NOT_STARTED |
| AT15 | Hard billing cap unavailable | D08 | | NOT_STARTED |
| AT16 | Small security-sensitive patch | D09 | | NOT_STARTED |
| AT17 | Unsupported effort / org clamp | D09 | adapter-level rejection tests (D04) | PARTIAL: rejection TESTED_SIM; clamp detection needs live observation: NOT_RUN |
| AT18 | User pins model/effort | D09/D12 | | NOT_STARTED |
| AT19 | Missing dependency failure | D09 | | NOT_STARTED |
| AT20 | Repeated no-progress repairs | D06/D09 | | NOT_STARTED |
| AT21 | Old green suite, feature absent | D01/D10 | legacy `test_suite_green_at_baseline_does_not_end_the_session`; v2 `test_green_existing_suite_without_the_feature_is_not_complete` | TESTED_SIM |
| AT22 | DONE with missing verifier | D01 | `test_d01_semantics.py::TestCompletion::test_done_with_missing_verifier_is_unverified`, battery `test_scratch_run_without_verifier_is_unverified` | TESTED_SIM |
| AT23 | Approval of old revision | D10 | `test_approval_of_an_old_snapshot_does_not_count` | TESTED_SIM (D03); delta-review rules: D10 |
| AT24 | Agent edits acceptance policy | D03/D10 | `test_participants_cannot_change_the_contract`, `test_weakened_protected_check_blocks_completion`, `test_protected_paths_detect_weakened_tests` | TESTED_SIM |
| AT25 | Joint authorship reviews | D10 | | NOT_STARTED |
| AT26 | Dirty active checkout | D03 | `test_user_checkout_is_untouched`, `test_import_inputs_is_explicit_and_checked` | TESTED_SIM |
| AT27 | Secret-bearing ignored file / escaping symlink | D03/D13 | `test_inputs_exclude_ignored_sensitive_and_escaping_links`, `test_import_inputs_is_explicit_and_checked`, `test_sensitive_classifier` | TESTED_SIM (D03); threat-model pass: D13 |
| AT28 | Concurrent writers | D11 | | NOT_STARTED |
| AT29 | Crash around dispatch | D02/D11 | `test_crash_mid_transaction_leaves_no_partial_state`, `test_in_doubt_is_never_redispatched`, `test_reconcile_uses_process_identity_not_just_expiry` | TESTED_SIM (runtime); provider-level: D11 |
| AT30 | Crash during commit/integration | D11 | | NOT_STARTED |
| AT31 | Cancel while waiting/working/verifying | D04/D11 | legacy interrupt tests; v2 `test_cancel_interrupts_the_turn` (Claude SIGINT), `test_cancel_uses_turn_interrupt` (Codex), `test_cancel_while_waiting` (pair wait wakes with CANCELLED) | TESTED_SIM (waiting, working); verifying across a crash: D11 |
| AT32 | PID reuse / unrelated sessions | D11 | | LEGACY_PARTIAL (`stop` confirms; name matching) |
| AT33 | Peer text claims user approval | D02/D13 | `TestAuthority::test_peer_text_cannot_grant_approval`, `test_message_text_carries_no_authority`, `test_authentication_rules` (identity never a parameter) | TESTED_SIM (runtime + service); installer threat model: D13 |
| AT34 | Malformed/flooded provider output | D04 | legacy `test_process.py`; v2 `test_flood_is_bounded`, `test_stream_process_truncates_long_lines`, malformed-line and server-death tests | TESTED_SIM |
| AT35 | Unknown/failed/skipped required check | D03/D10 | `test_unknown_or_missing_required_check_blocks`, `test_no_tests_is_not_a_pass`, `test_contract_without_checks_can_never_verify` | TESTED_SIM |
| AT36 | Inputs mutate during verification | D03 | `test_mutation_during_check_invalidates`, `test_transient_caches_do_not_invalidate` | TESTED_SIM |
| AT37 | Legacy transcript/config migration | D13 | | NOT_STARTED |
| AT38 | Wheel without source tree | D13 | `test_packaging.py` (defaults, migrations 0001–0003, participant instructions, Claude skill) | TESTED_SIM |
| AT39 | Integration install/uninstall | D12/D13 | | NOT_STARTED |
| AT40 | Quota reset with stale telemetry | D08/D11 | | NOT_STARTED |
| AT41 | Mid-turn setting change unsupported | D04/D09 | settings applied only at invocation (Claude) or `turn/start` (Codex); no steering-based switching | PARTIAL: adapters TESTED_SIM; routing boundary: D09 |
| AT42 | Resume/fork semantics differ | D04/D12 | `test_resume_is_session_cumulative`, `test_fork_gets_new_id`, `test_resume_keeps_thread_and_fork_changes_it`, `test_lineage_labels` | TESTED_SIM (emulated); LIVE: NOT_RUN |
| AT43 | No sandbox for borrowed session | D13 | | NOT_STARTED |
| AT44 | Same snapshot checked twice | D10 | | NOT_STARTED |
| AT45 | Native subagents spawn | D11 | | NOT_STARTED |
| AT46 | Estimate overshoot before final usage | D08/D11 | | NOT_STARTED |
| AT47 | Integration changes tested tree | D10/D11 | | NOT_STARTED |
| AT48 | Release handoff | D14 | | NOT_STARTED |
