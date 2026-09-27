# Handoff (final, D14)

## What this answers

The specification asks: *what works, at which exact revision, under which
client, platform and control modes, with which real verification evidence,
and what remains unproven.*

- **Revision:** the head of branch `claude/adoring-babbage-0ys7ui`, draft PR
  https://github.com/vvvaibhaverma-123459876/duet/pull/2. The commit that
  adds this file is the D14 revision. `git log -1` gives the exact SHA, and
  the PR's checks show CI for it. Nothing is merged to `main`.
- **What works (implemented and simulation-tested):**
  - D00–D14 as described in `D01_REPORT.md`–`D13_REPORT.md`.
  - An event-sourced runtime with a local service.
  - Native and managed pairing over MCP.
  - Task scheduling.
  - Usage pools, admission and finishing reserves.
  - Adaptive routing.
  - Final-revision review with a deterministic report.
  - Isolated parallel work.
  - Reversible client integrations.
  - Hardening.
  - Every acceptance test has automated evidence or explicit disclosure (`ACCEPTANCE_RESULTS.md`).
- **Where:** Linux, Python 3.11 and 3.13, root and non-root (CI and local). macOS: a non-blocking CI job, result recorded in `COMPATIBILITY.md`. Windows is unsupported.
- **Control modes:**
  - Managed sessions: push delivery, with model and effort enforced per turn where the CLI exposes them.
  - Native sessions: checkpoint delivery (plus the optional Stop hook), with advisory model and effort. Subagents are not controlled.
  - Live delivery: unavailable. (`duet capabilities`)
- **Real verification evidence:** none. Real-provider tests are gated and NOT_RUN. No row is `LIVE_PROVEN`.
- **Independent review:** the Codex review is pending for D01–D14. Internal Claude review agents are not independent.
- **Release label:** none met (`RELEASE_CHECKLIST.md`).

## Documents

- `ACCEPTANCE_RESULTS.md`: every AT, with its evidence and status.
- `EVALUATION.md`: no comparative results; the fixed protocol.
- `RELEASE_CHECKLIST.md`: gates, the operator walkthrough, migration.
- `CAPABILITY_MATRIX.md`: R and AT traceability.
- `DECISIONS.md`: D-001 to D-035.
- `SECURITY_MODEL.md`, `OPERATIONS.md`, `COMPATIBILITY.md`.
- `PEER_ALPHA_TEST.md`: the real pair test procedure.

## Commits

Milestone commits are on the branch (`git log --oneline`): D13 is
`b0f80cb`; D14 is the commit that adds `ACCEPTANCE_RESULTS.md`. Earlier
reference points: D06 `619ca53`, the core review fixes `e71d957`, the D07
ledger `2b390ea`, D07 persistence `5814b11`, the D06 review fixes
`6a9f5bd`, and `feat/isolate-modes` merged at `9db29f3`.

## How to run the tests

```bash
pip install -e ".[test,mcp]"
python3 -m pytest -q                       # real-provider tests skip unless gated
```

To check non-root behaviour when working as root, mirror the tree to an
unprivileged user:

```bash
tar -C /home/user/duet --exclude=.git -cf - . | (mkdir -p /home/ubuntu/duet-ci && tar -C /home/ubuntu/duet-ci -xf -)
chown -R ubuntu /home/ubuntu/duet-ci
su ubuntu -s /bin/bash -c "cd /home/ubuntu/duet-ci && python3 -m pytest -q -p no:cacheprovider"
```

## Next steps (all need a person)

1. Run the real pair tests on a machine with both CLIs logged in, as non-root, per `PEER_ALPHA_TEST.md`: `DUET_REAL_PAIR=1` (AT01 and AT02 in both directions, AT03) and `DUET_REAL_PROVIDERS=1`. These use the account's quota; that is why they are gated.
2. Run the independent Codex review of the branch and address its findings.
3. Script the `EVALUATION.md` tasks and run the protocol before promoting pairing to a default.
4. Record the `test-macos` result. If it fails, fix it or keep macOS unsupported.

## Known risks

- `test_detached_descendant_is_killed_after_normal_exit` failed twice on CI
  non-root runners early on. It has been hardened and has been green since.
  If it recurs, its diagnostic output names the surviving process.
- There is no chaos harness. Crash recovery is tested at defined points only.

## Uncommitted files

None.
