# Handoff

## Current state

- Branch: `claude/adoring-babbage-0ys7ui`. See `git log` for the exact HEAD
  (updated with each milestone commit).
- Last completed milestone: **D00**.
- `feat/isolate-modes` is merged in (`9db29f3`).
- Baseline: 178 passed, 1 skipped as non-root. As root, the doctor root check
  fails the battery (see `BASELINE.md`).

## How to run the tests here

The container runs as root, and on the pre-D01 baseline the battery fails
under root. Run the suite as a non-root user by mirroring the tree into their
home:

```bash
tar -C /home/user/duet --exclude=.git -cf - . | (mkdir -p /home/ubuntu/duet-ci && tar -C /home/ubuntu/duet-ci -xf -)
chown -R ubuntu /home/ubuntu/duet-ci
su ubuntu -s /bin/bash -c "cd /home/ubuntu/duet-ci && python3 -m pytest -q -p no:cacheprovider"
```

## Next safe step

D01: add failing regression tests for the D01 list in the specification, then
fix the legacy layers (`broker`, `adapters`, `stopconditions`, `verifiers`,
`config`, `transcript`, `cli`).

## Uncommitted files

None after each milestone commit.

## Open items needing a human

- A real Claude + Codex pair test (peer-alpha gate) needs a machine with both
  CLIs installed and logged in, run as non-root.
