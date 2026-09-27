# Handoff

## Current state

- Branch: `claude/adoring-babbage-0ys7ui`. See `git log` for the exact HEAD
  (updated with each milestone commit).
- Last completed milestone: **D07** (see `D01_REPORT.md` to `D07_REPORT.md`). D06 is `619ca53`, the core review fixes are `e71d957`, and the D07 ledger is `2b390ea`.
- `feat/isolate-modes` is merged in (`9db29f3`).
- Suite after D07: see the latest commit message. Both configurations (core only, and with `mcp==2.2.0`) pass as root, and the integration, verification and runtime tests pass as non-root.
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

1. D08: completion-aware admission. Pre-dispatch estimates per provider and
   pool with matching scopes, finishing reserves for the required review,
   repair and checks (a Claude review must be funded from Claude capacity),
   reset-aware pause and resume (`PAUSED_QUOTA`), and a label saying how
   each pool is enforced. Build on `runtime/pools.py` (the atomic check) and
   `duet.usage.Ledger` (observed consumption and quota gauges).
2. Run an internal review agent over D06–D07 before D08 grows on them.
3. A person with both CLIs logged in runs `PEER_ALPHA_TEST.md` (AT01–AT03).

## Uncommitted files

None after each milestone commit.

## Open items needing a human

- Real pair tests (AT01 native Claude, AT02 native Codex, AT03 managed), per
  `PEER_ALPHA_TEST.md`, on a machine with both CLIs logged in, as non-root.
- Independent Codex review of D01–D05 (no Codex login here).
