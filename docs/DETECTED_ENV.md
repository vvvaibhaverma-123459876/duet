# Detected Environment

Detected on 2026-07-01.

## Versions

- Claude Code: `2.1.197 (Claude Code)`
- Codex CLI: `codex-cli 0.142.5`

## Help Commands Read

- `claude --help`
- `claude -p --help`
- `codex --help`
- `codex exec --help`

## Confirmed Claude Invocation

Command shape:

```bash
printf 'Reply with exactly: CLAUDE_PROBE_OK' | claude -p --output-format json --dangerously-skip-permissions --add-dir /private/tmp/duet-probe-claude
```

Observed JSON fields:

- Final text: `result`
- Session id: `session_id`

Inside the managed Codex filesystem sandbox, the local Claude CLI reached headless mode but reported:

```text
Not logged in · Please run /login
```

Outside that harness sandbox, `duet doctor` completed a real Claude round-trip successfully:

```text
CLAUDE_DOCTOR_OK
```

## Confirmed Codex Invocation

Command shape:

```bash
printf 'Reply with exactly: CODEX_PROBE_OK' | codex --ask-for-approval never --sandbox workspace-write exec --skip-git-repo-check -C /private/tmp/duet-probe-codex -
```

Findings:

- `--ask-for-approval never` and `--sandbox workspace-write` must be passed as global `codex` options before `exec` for this installed version.
- `codex exec -` reads the prompt from stdin.
- `-C <dir>` sets the working root.
- This Codex build prints a run transcript to stdout and repeats the final agent message as the last non-empty line, so the default parser uses `text-last-line`.
- Running Codex from the managed filesystem sandbox failed with `Operation not permitted` during app-server initialization; running the real CLI outside that sandbox succeeded, including `duet doctor` with `CODEX_DOCTOR_OK`.
