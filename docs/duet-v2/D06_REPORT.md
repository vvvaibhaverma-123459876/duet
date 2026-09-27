# D06 Report: shared task graph and bounded scheduler

Commit `619ca53`, which also carries the fixes for the internal review
findings (see "Review findings" below).

## What exists now

| Piece | Module | What it does |
|---|---|---|
| Contracts | `duet/runtime/taskplan.py` | `TaskSpec`, `GraphState`, `GraphTask`, `ParticipantView`, `Recommendation`, `ProgressSample`, `FailureRecord`, `LoopVerdict`; task kinds; bounds: 12 tasks per plan, 32 per run, 3 ancestors, 2 active claims. |
| Scheduler | `duet/runtime/scheduler.py` | Pure and deterministic. `validate_plan` (cycles through dependencies and parents, bounds, acceptance ids, cancelled references), `ready_tasks`, `critical_path`, `assign` (a maximum matching so no task is duplicated and both participants are busy; the non-writer is preferred for non-code work), `recommend` (answer peer requests, then continue, then claim, then wait with a reason), `progress_fingerprint` (task states and revisions, snapshot trees, check results, open findings; never message text), `detect_loop`. |
| Integration | `duet/runtime/taskgraph.py`, migration `0004_taskgraph.sql` | Shared plans (propose → the *other* participant accepts, rejects, or the proposer withdraws), task results decided by the non-author, claim rules, writer handoff and takeover, contribution records from evidence, scheduler nudges (controller TASK_PROPOSAL, which also starts a managed peer's turn), loop control. |
| Gate | `duet/verification/completion.py` | `both_contributions` counts authored changes, reviews and contribution records; messages no longer count (R01). |
| Tools | `duet/integrations/mcp_server.py` | `duet_propose_plan`, `duet_decide_plan`, `duet_complete_task`, `duet_decide_task`, `duet_handoff`; `duet_propose_task` is now a one-task plan; `next` appears in `duet_status` and `duet_wait`. |

## Exit criteria

| Criterion | Evidence | Status |
|---|---|---|
| Both providers contribute meaningful work without duplicating every task | `test_both_contribute_without_duplicating_work` (Codex investigates while Claude codes; the task cannot be claimed twice; contributions from both, from evidence). Emulated full path: `test_duet_pair_shares_a_plan_and_both_contribute` (a managed writer's plan routes an investigation to the managed reviewer, who completes it; the writer accepts it and implements). Simulation: 100 random whole runs, every task finishes and both participants complete work; `assign` never duplicates. | TESTED_SIM |
| Dependency cycles, invalid claims and unbounded delegation are rejected | `test_bad_plans_are_rejected` (cycle, oversize, unknown acceptance id, 4-deep delegation, unknown kind, second pending plan), `test_active_claims_are_bounded`, `test_rejected_result_goes_back_to_its_owner`, non-writer code claims refused; 300 random plans in the simulation suite. | TESTED_SIM |
| Repeated failed hypotheses cause a focused re-plan or honest pause, not infinite ping-pong | `test_repeated_failures_replan_then_pause`: two identical failed checks block the task and ask for a re-plan; accepting a new plan unblocks it; two more failures pause the run (`PAUSED_APPROVAL`). `test_message_ping_pong_stalls_then_pauses`, `test_real_progress_resets_the_stall_count`. | TESTED_SIM |
| The original objective and acceptance contract remain intact | `test_plan_is_shared_and_only_adds_tasks` (objective and acceptance hash unchanged); a plan has no field that could change them; unknown acceptance ids are rejected; participants still cannot make acceptance work optional. | TESTED_SIM |

## Review findings (D01–D05, internal Claude review)

Two review agents audited D01–D05 and reproduced each finding. That is a
Claude review, not the Codex review the specification requires.

| Finding | Fix | Test |
|---|---|---|
| Checks ran in the live workspace, where files the snapshot excluded could make them pass (escaping links, ignored packages, stale bytecode), and edits made during a check went unseen | Checks run on a fresh copy of exactly the snapshot, which must reproduce the snapshot's tree hash. In-tree links are stored and recreated. | `tests/verification/test_snapshot_checks.py` |
| The deliverable commit used the live worktree on top of the writer's own commits | The commit is built from the snapshot's blobs on the base tree, in a private index, with a compare-and-swap ref update. A failure is reported honestly. | `test_deliverable_ignores_work_the_writer_committed_but_the_snapshot_lacks`, `test_edits_after_verification_never_reach_the_commit` |
| "Submit again" was impossible after a stale submission or an interrupted check | The task is reopened (CHANGES_REQUESTED) with a BLOCKER. | `test_a_stale_submission_can_be_resubmitted`, `test_interrupted_check_reopens_the_task` |
| Reverting to an earlier tree left completion stuck on the newer snapshot | Submissions are ordered records. | `test_reverting_to_an_earlier_tree_makes_it_current_again`, `test_resubmitting_the_approved_tree_reuses_its_evidence` |
| Action leases (900 s) were shorter than check timeouts | The lease is the timeout plus 300 s, for checks and for managed turns. | `test_check_leases_outlive_the_check_timeout` |
| The managed driver died when the model acknowledged further than it had | The ack never moves backwards. | `test_driver_survives_the_model_acknowledging_further` |
| Managed peers looked connected after a service restart | They are marked unavailable at stop and at start, and the native peer is told. | `test_restart_marks_orphaned_managed_peers_unavailable` |
| A native session could start only one run | A finished run's token is forgotten. | `test_a_native_session_can_start_a_new_run_after_its_run_ends` |
| The no-tests policy could relabel a failing exit | The policy applies only to exit codes 0 and 5. | `test_no_tests_policy_never_hides_a_failing_exit` |
| `--protect tests` matched nothing; some secret files were not recognised | Directory patterns are supported, and the sensitive-name list is extended. | `test_protected_directories_with_or_without_slash`, `test_more_secret_files_are_recognised` |
| A task a participant proposed could block completion forever | In D06, proposed tasks become verifiable: the peer accepts the result. They are required only if tied to acceptance ids, which both sides agreed to. | `test_both_contribute_without_duplicating_work` |

The core-module findings (process groups, subprocesses inside transactions,
the strict-worktree exclude file, symlinked import parents, the legacy budget
note, the legacy cost scope, provider API keys in managed peers' environment,
Codex notification ordering and token scope) are being fixed in a separate
change. See `HANDOFF.md`.

## Tests

- New: `tests/runtime/test_scheduler_sim.py` (82),
  `tests/integrations/test_taskgraph.py` (14),
  `tests/integrations/test_review_fixes.py` (7),
  `tests/verification/test_snapshot_checks.py` (8), 4 service regression
  tests, and one emulated `duet pair` plan run.
- Full suite as root: 651 passed, 6 skipped (core only); 656 passed, 4
  skipped with `mcp==2.2.0`. Integration, verification and runtime tests as
  non-root: 291 passed, 2 skipped.

## Limits

- One writer per run. Code tasks cannot run in parallel until D11 adds
  parallel workspaces.
- Stall detection keeps its message window in the service's memory, so a
  restart resets it. Failure history is read from the store and survives.
- The scheduler suggests, and participants decide. Nothing forces a native
  session to take the task it was offered. A managed peer is prompted with
  the suggestion.
- Checks run on a copy of the snapshot, which is not a git checkout of the
  user's repository. A check that needs ignored local state (a virtualenv
  inside the repository, build outputs) or git history fails and must be
  written to install or build what it needs. That is the intended fail-closed
  behaviour.
