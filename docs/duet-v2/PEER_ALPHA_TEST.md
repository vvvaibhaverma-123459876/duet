# Peer-alpha acceptance: real pair tests

The peer-alpha label needs real two-way collaboration between original
sessions in both directions (spec §13). Fixtures and emulators do not earn
it. These steps are for a person on a machine where Claude Code and Codex are
installed and logged in. Each run uses plan usage from both accounts. Run as
a non-root user, and do not set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`:
DUET must use the existing logins, never paid API billing.

## Setup (once)

```bash
pip install 'duet[mcp]'
claude mcp add duet -- duet mcp serve        # add -s user for every project
codex mcp add duet -- duet mcp serve
```

Register `duet mcp serve` directly, not through a shell wrapper. DUET
identifies a native session by the process that launched the MCP server.

Prepare a small repository with a failing check, for example `calc.py` with
`add()` and a `check_feature.py` asserting `calc.mul(3, 4) == 12`, committed
on a branch.

## AT01: launch from an existing Claude session

1. Start `claude` in the repository and ask: "Use DUET to pair with Codex on:
   add mul(a, b) to calc.py. Check with `python check_feature.py`. Start a
   managed Codex peer; you write the code."
2. Expect Claude to call `duet_join` (`peer="managed"`), then keep calling
   `duet_wait`.
3. Ask Claude, in the same session, to put a question to Codex before
   coding. Record that Codex's reply arrives through `duet_wait` without you
   relaying anything.
4. If Codex asks Claude a question, record that the same Claude session
   answers it.
5. Expect `duet_submit`, a Codex review, and `COMPLETED_VERIFIED` reported in
   the Claude session.

Evidence to record: `duet status --run RUN_ID --json`, the message sequence
(`duet status` shows open requests; the runtime database in the DUET state
directory holds the log), and the Claude session id before and after (it must
be the same session: no substitute Claude).

## AT02: launch from an existing Codex session

The same steps, starting in `codex`: `duet_join(provider="codex", ...)` with
a managed Claude peer. Record that the original Codex session answers a
Claude-originated question.

## Native + native (optional)

Start in Claude with `peer="invite"`, give the returned invite to a separate
Codex session ("join DUET run RUN_ID with invite CODE"), and repeat the
question exchange.

## AT03: DUET-launched pair

```bash
DUET_REAL_PAIR=1 DUET_REAL_PAIR_RECORD=./pair-evidence pytest -m e2e tests/e2e_peer/test_real_pair.py
```

or run `duet pair "..." --check "python check_feature.py"` by hand. Both
sessions are labelled `managed`. This mode does not substitute for AT01 or
AT02.

## What to report

For each test: the date; Claude Code and Codex versions; pass or fail; the
run id; the status JSON; the exact failure if any; and whether any message
needed your relay. It must not. Add the results to `CAPABILITY_MATRIX.md`
(AT01–AT03) and `PROGRESS.md` ("Live-proven").
