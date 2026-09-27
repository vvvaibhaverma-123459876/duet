# Handoff

## Current state

- Branch: `claude/adoring-babbage-0ys7ui`. See `git log` for the exact HEAD
  (updated with each milestone commit).
- Last completed milestone: **D03** (see `D01_REPORT.md` to `D03_REPORT.md`).
- `feat/isolate-modes` is merged in (`9db29f3`).
- Suite after D01: 291 passed, 1 skipped as root; 289 passed, 3 skipped as
  non-root. The non-root packaging tests skip because that user cannot read
  the proxy CA bundle.

## How to run the tests here

Since D01 the suite passes as root (`python3 -m pytest -q`). To also check
non-root behaviour, mirror the tree into the ubuntu user's home:

```bash
tar -C /home/user/duet --exclude=.git -cf - . | (mkdir -p /home/ubuntu/duet-ci && tar -C /home/ubuntu/duet-ci -xf -)
chown -R ubuntu /home/ubuntu/duet-ci
su ubuntu -s /bin/bash -c "cd /home/ubuntu/duet-ci && python3 -m pytest -q -p no:cacheprovider"
```

## Next safe step

D04: `duet/providers`: a capability-oriented base contract; a Claude CLI
adapter (stream-json, session start/resume, observed settings); a Codex
app-server JSON-RPC client built against the schema generated from the
installed CLI (`codex app-server generate-json-schema`; install
`@openai/codex` locally, with no login needed for schema generation); the
`codex exec --json` fallback; emulator fixtures; and the error taxonomy.

## Uncommitted files

None after each milestone commit.

## Open items needing a human

- A real Claude + Codex pair test (peer-alpha gate) needs a machine with both
  CLIs installed and logged in, run as non-root.
