# D10 Report: final-revision review and completion

Completion now requires evidence that each criterion was actually
achieved, reviews that cover every changed file by someone who did not
write it, and a final report built only from records. Everything is
exercised with coordinator-level participants and emulated providers.

## What exists now

| Piece | Module | What it does |
|---|---|---|
| Baselines | `duet/verification/baseline.py`, migration `0008_final_review.sql` | Each required check runs once per run and contract version on a private `git archive` export of the base commit. |
| Criteria | `completion._criteria`, `Criterion.kind` | A `change` criterion (the default) is demonstrated by a check that did not pass on the base commit and passes now (fail-to-pass). Otherwise a non-author approval must name it (scope `criterion:ID`). A `preserve` criterion (existing behaviour) needs its checks to pass. `kind` is serialised only when not the default, so earlier contract hashes are unchanged. |
| Authorship | `CompletionGate.authorship` | Per file, from content hashes: the author is whoever first submitted, or handed off, that exact content. Earlier different versions are recorded with their writers. |
| Review coverage | `completion._reviews` | Every changed file needs an approval from someone who did not write it, from the other provider, whose scope covers the file. A co-edited file also needs each earlier writer's version approved by someone else, on the snapshot where it appeared. When several participants wrote the result, each writer acknowledges or approves the final snapshot. A change request blocks. |
| Delta reviews | scope `basis:SNAPSHOT` | An approval of an earlier snapshot carries over to unchanged files only when a new approval of this snapshot names that snapshot as its basis. |
| Reviews by the submitter | `evidence.submit_review` | The submitter may ackn














 the snapshot or review files it did not write, named explicitly. The gate decides per file whether the review counts. |
| Check reuse | `pairing._run_checks` | Passed evidence for the same snapshot, contract version and environment fingerprint is reused (the action is recorded as reused, and the status note says so). |
| Final report | `duet/runtime/final_report.py`, `duet report --run RUN [--json]` | Deterministic `duet.final-report/1`: outcome, contract version, repository, base and deliverable commit, final snapshot and tree hash, each check (command, exit code, output hash, baseline), every review (scope, and whether it applies), per-file authorship, findings, routing coverage, reserves and holds, and each missing obligation with the next action. No model writes it. |

## Exit criteria

| Criterion | Evidence | Status |
|---|---|---|
| A stale approval or altered acceptance contract cannot yield success | `test_an_approval_is_stale_after_a_change_unless_a_delta_review_names_its_basis`, `test_a_new_contract_version_voids_earlier_evidence`, `test_approval_of_an_old_snapshot_does_not_count` (gate) | TESTED_SIM |
| Green pre-existing tests do not satisfy unimplemented task acceptance criteria | `test_a_suite_green_before_the_change_does_not_demonstrate_the_criterion`, `test_fail_to_pass_demonstrates_the_criterion`, `test_green_existing_suite_without_the_feature_is_not_complete` | TESTED_SIM |
| Jointly authored changes cannot be self-approved through role relabelling | `test_joint_work_needs_per_file_review_and_both_acknowledgements`, `test_an_approval_from_a_co_author_does_not_count` (now also shows the path to completion) | TESTED_SIM |
| The final report references actual revisions and checks, and identifies every missing obligation | `test_the_final_report_names_revisions_checks_and_missing_obligations` | TESTED_SIM |

Acceptance tests: AT21, AT23, AT24, AT25 and AT44 (`test_an_identical_snapshot_reuses_its_evidence`), plus AT35 (unknown or skipped checks never pass; existing gate tests).

## Tests

- New: `tests/integrations/test_final_review.py` (8). The gate tests now
  record baselines, and their regression criterion is `preserve`. The
  co-author test was extended to the full path: once Codex reviews Claude's
  version, the run completes. A stale-approval assertion's wording changed.
- Suites: core as root, 857 passed and 6 skipped; with `mcp==2.2.0` as
  root, 862 passed and 4 skipped; whole suite as non-root with MCP, 862
  passed and 4 skipped.

## Limits

- Authorship is per file, from content. Lines are not attributed within a
  file. A co-edited file therefore needs every writer's version reviewed.
- Baselines run the checks on the base commit's tracked files. A check that
  needs build outputs or untracked state fails there, which counts as
  "did not pass". That is conservative: it may accept fail-to-pass evidence
  a stricter reading would question, and the report shows the baseline
  status of every check.
- Check reuse requires the same tree, contract version and environment
  fingerprint within one run. Evidence is not shared across runs.
- The criteria-from-objective contract (`duet pair`, native join) makes one
  `change` criterion covering all checks. Richer contracts need the user to
  write them.
