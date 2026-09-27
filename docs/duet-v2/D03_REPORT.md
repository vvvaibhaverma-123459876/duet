# D03 Report: safe workspace and initial evidence/completion gate

| Module | Responsibility |
|---|---|
| `workspaces/repo.py` | Repository identity from the canonical git common dir: worktrees share an id, clones of the same origin do not |
| `workspaces/manager.py` | Strict worktrees in the state dir; never switches, stashes or copies from the user's checkout; one writer via fenced `workspace:` lease; explicit, checked `import_inputs`; teardown |
| `workspaces/snapshots.py` | Input manifests with a tree hash; sensitive/transient/symlink policy; base-tree hashing through one `git cat-file --batch`; protected-path diff; read-only materialisation for reviewers |
| `runtime/artifacts.py` | Content-addressed, read-only blobs referenced only by `sha256:` ids (no path-based reads) |
| `runtime/migrations/0002_evidence.sql` + reducer | `snapshots`, `evidence`, `reviews`, `findings`, `checkpoints`, all event-sourced and replay-checked |
| `verification/acceptance.py` | Versioned contract: criteria → checks (argv, or explicit shell mode), protected paths; strict validation |
| `verification/runner.py` | Bounded checks (D01 process runner) with an allowlisted environment and fingerprint; before/after snapshots give `invalidated`; no-tests policy; timeouts fail |
| `verification/evidence.py` | Controller-only evidence keyed to (snapshot, contract hash); non-author and cross-provider reviews; raiser-only finding closure |
| `verification/completion.py` | The eight-item predicate; COMPLETED_VERIFIED / IMPLEMENTED_REVIEW_PENDING / REPORTED_UNVERIFIED; checkpoint export; controller-only finalisation |

## Exit criteria

| Criterion | Evidence |
|---|---|
| An active user checkout remains unchanged | `test_user_checkout_is_untouched`: status (including ignored files), HEAD, branch, index, file bytes and stash list are identical after creating a workspace and committing in it |
| Secrets and ignored files are not copied by default in strict mode | the same test (no `.env`, notes or build output in the worktree); `test_inputs_exclude_ignored_sensitive_and_escaping_links`; `test_explicit_ignored_secret_still_needs_approval`; `test_import_inputs_is_explicit_and_checked` |
| Changes during or after verification invalidate evidence | `test_mutation_during_check_invalidates` (AT36); `test_evidence_must_match_snapshot_inputs`; `test_approval_of_an_old_snapshot_does_not_count` (AT23) |
| No required check or review obligation can be removed by an agent | contract changes are user-only (`test_participants_cannot_change_the_contract`); `test_weakened_protected_check_blocks_completion` (AT24); participants cannot record controller evidence; author and same-provider review refused; raiser-only finding closure; `test_contract_without_checks_can_never_verify` |

Also covered: AT21 (a green existing suite with the feature absent does not
complete), AT35 (a missing, unknown or failed required check blocks),
in-doubt actions block completion, explicit solo keeps the review
requirement, and the exported checkpoint carries the COMPLETED_VERIFIED report
and evidence references.

Tests: `tests/workspaces/` (21) and `tests/verification/` (37). Full suite:
445 passed, 1 skipped as root.

## Limits

- Snapshot hashing reads every input file on each capture. That is fine for
  these repositories; D10 adds caching keyed on complete input fingerprints.
- `finalize` evaluates from autocommit reads, then transitions. A concurrent
  writer between the two could slip in; D10/D11 move the final check and the
  transition into one transaction under the integration lease.
- Joint authorship (per-component reviews) and delta reviews are D10.
- Containment is cooperative: a worktree isolates the working copy, but an
  agent running as the same user can still reach the common git dir and the
  state dir.

Independent review: **pending** (no Codex available).
