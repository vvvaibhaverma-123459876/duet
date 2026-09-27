# D11 Report: selective parallel implementation and failure recovery

Both participants can now write at the same time without sharing a
checkout. Integration has one owner and is fenced. An integration
interrupted by a crash is settled from the files, and stopping DUET stops
only what DUET started. Exercised with coordinator-level participants and
scripted providers.

## What exists now

| Piece | Module | What it does |
|---|---|---|
| Isolated code tasks | `taskplan.ISOLATED_KINDS` (`code_isolated`), scheduler, `taskgraph.check_claim` | Independent code work for the participant who is not the writer. The writer is refused, and the scheduler never offers it to them. |
| Task worktrees | `duet/runtime/parallel.py`, migration `0009_parallel.sql`, `WorkspaceManager.create(subdir=)` | At the first claim: a worktree of the task's own, on its own branch from the run's base commit, with its own write lease for the claimant. Recorded (`task_workspace.created` and `task_workspace.state`), and shown in `status` as `isolated_workspaces`. |
| Results | `ParallelWork.capture` | `duet_complete_task` records a snapshot authored by the task's owner, so D10 attributes its files to them, plus the patch against the base commit (stored as an artifact). |
| Integration | `ParallelWork.integrate`, inside `duet_decide_task(accept)` | The writer is the integration owner. Its fence on the run's workspace is checked, then an `integration` action applies the patch. The patch is checked (`git apply --check`) before anything is written. |
| Conflicts | `ParallelWork._conflict` | A patch that does not apply writes nothing. A `code` conflict task is created, carrying the original task's requiredness and acceptance ids, and the writer gets a BLOCKER with `git apply`'s explanation and the patch artifact. |
| Recovery | `ParallelWork.settle_in_doubt`, at service start | An integration left in doubt is settled by inspecting the files. If the patch reverse-applies, it happened (SUCCEEDED). If it applies cleanly, it did not, so it is integrated again from that evidence. Otherwise a conflict task is created. Provider turns in doubt are settled as before (D08: one turn, unknown cost, never re-dispatched). |

## Exit criteria

| Criterion | Evidence | Status |
|---|---|---|
| No concurrent writes to one checkout or integration by stale workers | `test_two_agents_write_at_once_in_separate_checkouts`: separate worktrees and leases, the non-writer holds no lease on the run's workspace, and a finished isolated task cannot submit again. Integration checks the writer's fence. | TESTED_SIM |
| A crash after an unobserved provider action does not blindly repeat it | `test_an_integration_done_before_a_crash_is_not_repeated`; provider turns: `test_in_doubt_turns_count_as_dispatched_with_unknown_cost` (D08) and `test_in_doubt_is_never_redispatched` (D02) | TESTED_SIM |
| Integration creates a new verification snapshot and invalidates stale evidence | `test_two_agents_write_at_once_in_separate_checkouts`: after integration the submission is a new snapshot with per-file authorship, and both non-author reviews are needed. D10 keys evidence and approvals to the snapshot. | TESTED_SIM |
| Stopping DUET never kills unrelated native sessions | `test_stopping_duet_leaves_unrelated_sessions_alone`: a look-alike `claude` process survives a stop; DUET's own managed peer is stopped. Process-group termination of owned processes only: D01 and D04 tests. | TESTED_SIM |

Acceptance tests: AT28 and AT32; D11 parts of AT29, AT30 and AT31.

## Tests

- New: `tests/integrations/test_parallel_work.py` (5). The scheduler
  simulation now models the isolated kind (`can_work`).
- Suites: core as root, 862 passed and 6 skipped; with `mcp==2.2.0` as
  root, 867 passed and 4 skipped; whole suite as non-root with MCP, 867
  passed and 4 skipped.

## Limits

- Parallelism is two root participants: the writer, plus isolated tasks
  for the other participant. The writer's own code work stays in the run's
  workspace.
- Isolated worktrees start from the run's base commit, not the writer's
  current work. Overlapping edits surface as conflicts at integration.
- Integration happens inside the writer's accept call. There is no
  background integration queue.
- Crash recovery is exercised by forcing leases to expire in-process. There
  is no chaos harness that kills the service mid-integration yet.
- Checkpoints are exported at completion (D03/D10), not after every state
  transition. The event log itself is the durable record.
- Native provider subagents are not observed or counted. That is a D12
  concern.
