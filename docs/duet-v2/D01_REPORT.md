# D01 Report: completion, accounting and process semantics

These changes are to the legacy (v1) layers. The v2 runtime starts in D02.
Every item below has a regression test. The behaviour changes are recorded in
`DECISIONS.md` D-002 to D-005.

| Spec item | Change | Tests |
|---|---|---|
| Unknown verification + DONE | `unverified` outcome (exit 3); `success` only with a passing verifier | `TestCompletion`, battery `test_scratch_run_*` |
| Pass before acceptance | Baseline verification recorded. A pass ends the session only with `[[DONE]]` or red→green | `test_suite_green_at_baseline_does_not_end_the_session`, `test_red_to_green_*` |
| Quota solo removes review | Dropped agents keep a review obligation, giving `review_pending` (exit 4) when changes follow their last turn | `TestSoloReviewObligation`, battery `test_solo_completion_keeps_partner_review_pending` |
| Unknown cost | `cost_usd: float \| None`; invalid values rejected; transcript `schema_version: 2` with v1 migration | `TestCost`, `test_missing_cost_path_is_unknown_not_zero` |
| Budget before verification | Completion is checked before the budget. Budget and wallclock gate **admission** of the next turn, not finalisation | `TestBudget`, battery `test_completion_on_budget_spending_turn_is_recognised` |
| Local finalisation after inference halt | Quota, agent or git halts re-run the checks and record `final_verification` | `TestFinalisation::test_agent_failure_after_changes_reruns_the_checks` |
| Cancellation stops scheduling | Interrupt returns `interrupted` (exit 130). The transcript and resume manifest are saved; no new verification runs | `TestInterrupt`, battery `test_interrupt_leaves_a_resumable_manifest` |
| Validate budgets, timeouts, config | Strict typed config, argparse validators, broker argument checks, turn timeout clamped to the wallclock deadline | `TestConfig`, `test_invalid_numeric_flags_rejected`, `TestWallclock` |
| Bound streaming capture at source | `duet/providers/process.py` head+tail `BoundedBuffer`; JSON over the cap gives `OutputLimitError` | `test_process.py`, `TestAdapter` |
| Owned-process cancellation | Own session/process group; SIGTERM, grace, SIGKILL of the group even after the leader exits; orphans holding pipes are killed | `test_process.py` |
| Error classification | `AuthError`, `BillingError`, `ModelUnavailableError`, `AgentTimeoutError`, `OutputLimitError`, `QuotaError(kind=quota\|rate_limit\|overloaded)`; bare `429`/`billing` markers removed | `TestClassification` |

Also fixed in D01, because they block D02 onward or are small correctness
bugs from the capability review:

- The doctor probe no longer leaks into `resume.json` (reproduced; fixed with
  a probe copy).
- The root check is agent-scoped, so the suite passes as root (D-005).
- Defaults are packaged, so the wheel runs outside the source tree (AT38).
  `[build-system]` now requires `setuptools>=77`; the PEP 639 license string
  broke older backends.
- Roles are task-agnostic; the demo roles apply only with `--seed-demo`.
- `run`/`connect` require a task.
- `resume` works from inside a scratch workspace.
- Control tokens count only on the final line.
- REPL `/ask` catches `AgentError`.
- The registry uses file locking and tolerates unknown fields.
- `init` refuses to overwrite without `--force`.
- Verifiers run through the bounded runner. "No tests collected" is `unknown`.
- Rejected `[[DONE]]` claims and verifier output are fed into the next prompt.
- CI gains a job that runs the suite as root in a container.

Deliberately **not** changed, with a pointer to where it belongs:

- The REPL still lacks `--verify`, quota and budget options (REPL parity is
  not part of D01; D13 reviews legacy front-ends).
- Codex output is still scraped as text (D04: exec JSONL / app-server).
- The legacy broker still rotates by pointer. Task-driven dispatch is v2 (D06).

Independent review: **pending**. Codex is not installed or authenticated
here, so no non-Claude review was possible. The only review so far is an
adversarial re-read of the diff by the author (Claude), which does not count
as independent review.
