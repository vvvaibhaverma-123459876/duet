# Handoff

## Current state

- Branch: `claude/adoring-babbage-0ys7ui`. See `git log` for the exact HEAD
  (updated with each milestone commit).
- Last completed milestone: **D08** (see `D01_REPORT.md` to `D08_REPORT.md`). D06 is `619ca53`, the core review fixes are `e71d957`, the D07 ledger is `2b390ea`, and D07's persistence is `5814b11`. D08 is the commit that adds `docs/duet-v2/D08_REPORT.md`.
- Changed in D08: `duet/usage/{estimation,admission,reservations}.py`, `duet/runtime/budgeting.py`, migration `0006_admission.sql`, and `duet/runtime/{api,pools,reducer,peers,pairing,service}.py`, `duet/cli.py`, `duet/cli_v2.py`. Tests: `tests/usage/test_admission.py`, `tests/runtime/test_finishing_reserves.py`, `tests/integrations/test_admission_service.py`; one service test was rewritten for D08 semantics.
- `feat/isolate-modes` is merged in (`9db29f3`).
- Suite after D08, all on the final tree: core only as root, 802 passed and 6 skipped (`python3 -m pytest -q -p no:cacheprovider tests`); with `mcp==2.2.0` (scratch `mcpvenv`), 807 passed and 4 skipped; as non-root (`ubuntu`, a copy in `/home/ubuntu/duet-ci`), integrations, verification, runtime and usage give 412 passed and 2 skipped.
- Review status: internal Claude review of D06/D07 done (13 reproductions). The usage findings are fixed in D08 (see `D08_REPORT.md`). The task-graph findings are being fixed separately and are **not yet merged**. The independent Codex review is still pending for D01–D08.
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

1. Land the fixes for the D06 task-graph findings from the internal review
   of D06/D07 (findings 1–4 and 9–11 in `D08_REPORT.md`'s source review:
   verified-too-early completion, the missing completion wake-up after
   side-task acceptance, self-approval after a handoff, racing plan
   decisions, the unwoken writer after a re-plan, the permanent block at 32
   tasks, and failure signatures without output hashes). A separate change
   is in progress; merge it, re-run all suites, and then continue.
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
