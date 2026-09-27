# D07 Report: normalised telemetry and shared usage ledger

Built in two parts: a pure ledger (developed in parallel by a separate agent,
commit `2b390ea`) and persistence and integration in the runtime (this
change).

## What exists now

| Piece | Module | What it does |
|---|---|---|
| Observations | `duet/usage/observations.py` | One normalised `Observation` (dimension, scope, source, dedupe id, epoch, baseline, quality, UTC time; money as `Decimal`; unknown is `None`, never zero). Adapters from D04 turn results, Codex token-usage and rate-limit notifications, and Claude stream events. |
| Ledger | `duet/usage/ledger.py` | Pure and order-independent: dedupe, one primary source per metric (others validate), cumulative counters turned into deltas within an epoch (a decrease is a reset), context and quota as gauges, pools, run attribution, disagreement kept. |
| Status line | `duet/integrations/claude_statusline.py` | Allowlisted parsing of the documented status-line JSON, keyed by session id; a wrapper that passes the user's own status-line output through unchanged. Installing it comes in D12. |
| Pools | `duet/runtime/pools.py`, migration `0005_usage_pools.sql` | User-defined allowances, capacity checked inside the reserving transaction, deduplicated usage records, `uncertain` when sizes are unknown. |
| Managed turns | `duet/runtime/peers.py` | Reserve from matching pools before each turn and record its usage afterwards. Cost comes from the ledger, as an exact delta or unknown. |
| CLI | `duet usage [--run ID] [--json]`, `duet usage pool set …` | The versioned `duet.usage/1` JSON and a human view. |

## Exit criteria

| Criterion | Evidence | Status |
|---|---|---|
| The same work seen by multiple telemetry paths is not counted twice | `test_statusline_and_cli_result_count_the_same_turn_once`, `test_parallel_tool_messages_sharing_an_id_count_once`, `test_replayed_codex_updates_count_once`, `test_the_same_update_via_turn_result_and_raw_stream_has_one_identity`, `test_usage_records_are_deduplicated_and_unknown_is_not_zero` | TESTED_SIM |
| Missing, reset, stale, shared-pool and decreasing observations have explicit semantics | `test_unknown_usage_is_never_zero`, `test_absent_final_usage_after_cancellation_stays_unknown`, `test_a_decrease_is_a_reset_not_a_negative_or_absolute_delta`, `test_stale_observations_are_flagged`, `test_two_models_and_two_runs_share_one_pool_without_double_counting`, `test_an_account_change_leaves_the_delta_unattributed`, `test_out_of_order_updates_give_the_same_answer` | TESTED_SIM |
| Two simultaneous runs cannot locally reserve the same remaining allowance twice | `test_two_processes_cannot_both_take_the_last_unit` (four processes race for one unit; exactly one wins), `test_reservations_draw_down_and_refuse_atomically` (two runs share a pool; a refused reservation leaves nothing half-created) | TESTED_SIM |
| Context occupancy is never presented as cumulative billed usage | `test_context_occupancy_never_enters_consumption`, `test_context_occupancy_cannot_be_cumulative_usage`, `test_quota_percentages_never_become_tokens_or_an_average` | TESTED_SIM |

End to end: `test_duet_pair_with_emulated_managed_sessions` runs a managed
pair with turns pools for both providers and a Claude cost pool. The
Claude emulator reports session-cumulative cost as real Claude Code 2.1.277+
does (0.01, 0.02, 0.03). The pool records exact per-turn deltas totalling
0.03, as estimates, with nothing uncertain. `test_local_turn_allowance_is_reserved_and_recorded`
shows a managed peer refused its second turn by a one-turn allowance, with
the reason reported and no fallback.

## Tests

- New: `tests/usage` (71), `tests/runtime/test_usage_pools.py` (7), one
  service integration test, and cost pools in the emulated pair.
- Suite results are in `HANDOFF.md`.

## Limits

- Provider quota windows (the Codex rate limits, the Claude status line's
  rate limits) are observed gauges only. Nothing yet reads them live from a
  running native session (the status-line installer is D12), and a local
  pool cannot lock provider-side quota.
- Codex reports no cost, so Codex cost pools can only ever be uncertain.
  Turns pools work for both providers.
- A cost pool refuses a turn only once it is exhausted. One turn can
  overshoot. Pre-dispatch estimates and finishing reserves are D08.
- The ledger runs inside each managed peer, from its turn results. Native
  sessions' usage is not observed until the status-line integration is
  installed (D12).
- Fixtures for success-path telemetry follow the official documentation and
  the generated schema. No real provider traffic was observed.
