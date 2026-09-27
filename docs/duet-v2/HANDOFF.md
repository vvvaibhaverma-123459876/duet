# Handoff

## Current state

- Branch: `claude/adoring-babbage-0ys7ui`. See `git log` for the exact HEAD
  (updated with each milestone commit).
- Last completed milestone: **D06** (see `D01_REPORT.md` to `D06_REPORT.md`), pushed as `619ca53`.
- `feat/isolate-modes` is merged in (`9db29f3`).
- Suite after D06, as root: 651 passed, 6 skipped without the MCP SDK; 656
  passed, 4 skipped with `mcp==2.2.0`. Integration, verification and runtime
  tests as non-root: 291 passed, 2 skipped.
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

1. Merge the core-module review fixes (process groups, no subprocess inside
   write transactions, strict worktrees leaving `.git/info/exclude` alone,
   symlinked import parents, the legacy budget note, the legacy cost scope,
   provider API keys removed from managed peers' environment, Codex
   notification ordering and token scope). They are being prepared as a
   separate change.
2. D07: merge the pure usage-ledger modules (`duet/usage`, the Claude
   status-line parser), then add persisted, transactional reservations across
   runs, the usage CLI/JSON, and ingestion from provider turns.
3. A person with both CLIs logged in runs `PEER_ALPHA_TEST.md` (AT01–AT03).

## Uncommitted files

None after each milestone commit.

## Open items needing a human

- Real pair tests (AT01 native Claude, AT02 native Codex, AT03 managed), per
  `PEER_ALPHA_TEST.md`, on a machine with both CLIs logged in, as non-root.
- Independent Codex review of D01–D05 (no Codex login here).
