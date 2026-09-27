# Handoff

## Current state

- Branch: `claude/adoring-babbage-0ys7ui`. See `git log` for the exact HEAD
  (updated with each milestone commit).
- Last completed milestone: **D05** (see `D01_REPORT.md` to `D05_REPORT.md`).
- `feat/isolate-modes` is merged in (`9db29f3`).
- Suite after D05, as root: 537 passed, 6 skipped without the MCP SDK; 541
  passed, 4 skipped with `mcp==2.2.0` (the skips are the gated real-provider
  and real-pair tests). As non-root, the integration, runtime, verification
  workspace and provider tests pass (243 passed, 2 skipped). Non-root packaging tests
  skip because that user cannot read the proxy CA bundle.
- The MCP tests need the extra: `pip install -e ".[test,mcp]"`. In this
  environment a venv at the session scratchpad has it (`mcpvenv`).

## How to run the tests here

Since D01 the suite passes as root (`python3 -m pytest -q`). To also check
non-root behaviour, mirror the tree into the ubuntu user's home:

```bash
tar -C /home/user/duet --exclude=.git -cf - . | (mkdir -p /home/ubuntu/duet-ci && tar -C /home/ubuntu/duet-ci -xf -)
chown -R ubuntu /home/ubuntu/duet-ci
su ubuntu -s /bin/bash -c "cd /home/ubuntu/duet-ci && python3 -m pytest -q -p no:cacheprovider"
```

## Next safe step

1. A person with both CLIs logged in runs `PEER_ALPHA_TEST.md` (AT01–AT03).
   Peer-alpha is not earned until then.
2. D06: shared plan proposals, validated task dependencies, bounded
   ownership, substantive contribution records, role reassignment, and
   progress/loop detection from task and evidence changes (`runtime/
   scheduler.py`, scheduler simulation suite). Build on the D05 coordinator:
   today participant tasks are accepted automatically up to a cap and there
   is one writer.

## Uncommitted files

None after each milestone commit.

## Open items needing a human

- Real pair tests (AT01 native Claude, AT02 native Codex, AT03 managed), per
  `PEER_ALPHA_TEST.md`, on a machine with both CLIs logged in, as non-root.
- Independent Codex review of D01–D05 (no Codex login here).
