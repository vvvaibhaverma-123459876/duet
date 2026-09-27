# Handoff

## Current state

- Branch: `claude/adoring-babbage-0ys7ui`. See `git log` for the exact HEAD
  (updated with each milestone commit).
- Last completed milestone: **D08** (see `D01_REPORT.md` to `D08_REPORT.md`). D06 is `619ca53`, the core review fixes are `e71d957`, the D07 ledger is `2b390ea`, and D07's persistence is `5814b11`. D08 is the commit that adds `docs/duet-v2/D08_REPORT.md`.
- Changed in D08: `duet/usage/{estimation,admission,reservations}.py`, `duet/runtime/budgeting.py`, migration `0006_admission.sql`, and `duet/runtime/{api,pools,reducer,peers,pairing,service}.py`, `duet/cli.py`, `duet/cli_v2.py`. Tests: `tests/usage/test_admission.py`, `tests/runtime/test_finishing_reserves.py`, `tests/integrations/test_admission_service.py`; one service test was rewritten for D08 semantics.
- `feat/isolate-modes` is merged in (`9db29f3`).
- Suite after D08 plus the merged D06 review fixes (`fix-d06-review`, `6a9f5bd`) and the process/umask hardening, all on the final tree:
  core only as root, 817 passed and 6 skipped (`python3 -m pytest -q -p no:cacheprovider tests`); with `mcp==2.2.0` (scratch `mcpvenv`) as root, 822 passed and 4 skipped; the **whole suite** as non-root (`ubuntu`, umask 002, own venv with `.[test,mcp]` in `/home/ubuntu/venv`, copy in `/home/ubuntu/duet-ci`), 822 passed and 4 skipped.
- Review status: the internal Claude review of D06/D07 is done (13 reproductions) and every confirmed finding is fixed. The usage findings are in D08 (`D08_REPORT.md`) and the task-graph findings in `6a9f5bd` (`D06_REPORT.md`). The independent Codex review is still pending for D01–D08.
- CI: `test_detached_descendant_is_killed_after_normal_exit` failed twice on non-root runners (`e71d957`, `5814b11`). It never reproduced locally (300+ runs, under load, as non-root with the MCP tests). The final group kill now re-sends until the group is empty, and the test reports the survivor's state, parent, group and session if it recurs. If it does, that output is the next lead.
- The MCP tests need the extra: `pip install -e ".[test,mcp]"`. In this
  environment a venv in the session scratchpad has it (`mcpvenv`).

## How to run the tests here

Since D01 the suite passes as root (`python3 -m pytest -q`). To also check
non-root behaviour, mirror the tree into the ubuntu user's home:

```bash
tar -C /home/user/duet --exclude=.git -cf - . | (mkdir -p /home/ubuntu/duet-ci && tar -C /home/ubuntu/duet-ci -xf -)
chown -R ubuntu /home/ubuntu/duet-ci
su ubuntu -s /bin/bash -c "cd /home/ubuntu/duet-ci && python3 -m pytest -q -p no:cacheprovider"
```

## Next safe step

1. Confirm CI is green on the pushed head (see the CI note above).
2. D09: adaptive model and effort routing (task risk and uncertainty
   assessment, logical profiles, capability-specific mapping, user pins,
   decision records). Admission (D08) gives it the resource side: estimates
   per provider, and pressure signals it must respect.
3. A person with both CLIs logged in runs `PEER_ALPHA_TEST.md` (AT01–AT03).

## Uncommitted files

None after each milestone commit.

## Open items needing a human

- Real pair tests (AT01 native Claude, AT02 native Codex, AT03 managed), per
  `PEER_ALPHA_TEST.md`, on a machine with both CLIs logged in, as non-root.
- Independent Codex review of D01–D05 (no Codex login here).
