# Operations

## Where state lives

`$DUET_STATE_DIR`, otherwise `$XDG_STATE_HOME/duet`, otherwise
`~/.local/state/duet` (macOS: `~/Library/Application Support/duet`). It is
private (0700), and every missing parent is created private too.

- `v2/runtime.db`: the event-sourced store. `duet` verifies replay in its tests; every row is derived from events.
- `v2/artifacts/`: snapshot blobs, check output, diffs, patches, checkpoints and reports.
- `v2/worktrees/<run>/`: the run's workspace and any isolated task worktrees.
- `v2/service.sock`, `service.info`, `service.secret` (0600): the local service.
- `v2/sessions/`: native MCP proxy sessions, keyed by the client process.
- `integrations.json`: what `duet integrations install` owns.

## Run lifecycle, day to day

| Want to | Command |
|---|---|
| Start a managed pair | `duet pair "task" --check "pytest -q"` |
| Pair from your own Claude/Codex | install once (`duet integrations install --yes`), then call `duet_join` in the session |
| See a run | `duet status --run RUN [--json]` |
| See usage, quota, reserves | `duet usage [--run RUN] [--json]` |
| Bound spending | `duet usage pool set NAME --provider P --metric turns\|cost.estimated_usd --allowance N` |
| See or steer model routing | `duet routing [--run RUN]`, `duet routing pin/map` |
| Resume a paused run | `duet resume --run RUN` (quota pauses resume on their own after the reset) |
| Stop a run | `duet stop --run RUN` (stops DUET's own peers only) |
| Final report | `duet report --run RUN [--json]` |
| What DUET controls here | `duet capabilities [--probe]` |
| The service | `duet service status\|stop\|run` (starts on demand, exits when idle) |

## Exit codes (v2 commands)

`0` verified success (or the command itself succeeded, for setup and
report commands); `1` usage, setup or refused operation; `2` halted,
failed or not complete; `130` cancelled by the user. A paused run is
never `0`.

## Recovery

On start, the service:
1. reconciles leases whose owners are provably dead or expired;
2. marks their in-flight actions IN_DOUBT;
3. settles interrupted checks (re-requested) and provider turns (counted as one dispatched turn with unknown cost, never re-dispatched);
4. settles integrations (from the files);
5. marks orphaned managed peers unavailable.

Nothing in doubt is repeated blindly.

## Logs and redaction

Output capture is bounded. Messages and check output are redacted before
persistence. Peer text is stripped of control sequences before display.
The service log is under the state directory.

## Backups and removal

The store is a single SQLite file plus content-addressed artifacts. Copy
the state directory while the service is stopped (`duet service stop`).
Uninstall client integrations with `duet integrations uninstall --yes`
first, then delete the state directory. DUET branches (`duet/run-*`) in
your repositories are yours to keep or delete.
