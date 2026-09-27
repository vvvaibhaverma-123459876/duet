# Architecture Decisions

Short records of implementation decisions. Status is `accepted` unless stated
otherwise. New decisions are appended; superseded ones are marked, not
deleted.

## D-001: Merge `feat/isolate-modes` before v2 work

Merged at `9db29f3`, with the accidental `build/` tree dropped. Reasons and
risk notes: `BRANCH_RECONCILIATION.md`.

## D-002: Legacy outcome vocabulary and exit codes (D01)

v1 reported `success` for completions that nothing verified. The legacy broker
now uses these outcomes:

| Outcome | Meaning | `duet run` exit |
|---|---|---|
| `success` | A configured verifier passed on the final workspace **and** completion was established (an agent claimed `[[DONE]]`, or the verifier went from failing at baseline to passing), with all required participants contributing | 0 |
| `unverified` | An agent claimed completion, but no verifier is configured or its result is `unknown` | 3 |
| `review_pending` | Completion reached while a participant dropped for quota (solo mode) still owes review of the final state | 4 |
| `halted` | Stopped before completion (max turns, wallclock, loop, budget, quota, agent/workspace error) | 2 |
| `interrupted` | SIGINT/SIGTERM; artifacts and resume manifest are saved | 130 |

Exit 1 remains reserved for usage, config and preflight errors. This is a
deliberate behaviour change under R15: the old `success` for unverified
completion was a misleading claim, not a compatibility feature. Scripts that
treated "exit 0 or 2" as the only outcomes must handle 3 and 4.

## D-003: Usage values are nullable with provenance

At the legacy adapter boundary, `cost_usd` is `float | None`. `None` means
unknown and is never coerced to zero. Negative, non-finite and boolean values
are rejected as invalid, which makes them unknown and adds a warning.
Transcripts gain `schema_version`. When a pre-v2 transcript (no version) is
loaded, `cost_usd == 0.0` becomes `None`, because the old writer used `0.0` for
"unknown" and the two cannot be told apart. The v2 ledger (D07) stores integer
micro-units with provenance, not floats.

## D-004: Default configuration is package data

The built-in defaults live at `duet/resources/default_config.toml` and are
loaded through `importlib.resources`, so wheel installs work (AT38). The
repository-root `duet.toml` remains as the project example. A test asserts that
both parse to the same configuration.

## D-005: Root check is agent-scoped

Claude Code refuses its permission-bypass mode as root. Running as root now
disables only an agent whose configured command contains a bypass flag (it
fails that agent's checks) instead of aborting every run. Duet no longer
refuses root globally. Containers commonly run as root; the containment claims
are unchanged (cooperative).

## D-006: Runtime store is stdlib `sqlite3`

The v2 runtime uses the standard-library `sqlite3`, adding no dependency. It
enables foreign keys, uses `BEGIN IMMEDIATE` for writes, and applies versioned
SQL migrations shipped as package data. WAL is enabled only when the database
path is on a local filesystem; network filesystems are refused. The event log
and the materialised state share one database, so they commit atomically.

## D-007: MCP via the official SDK as an optional extra

`duet[mcp]` depends on `mcp>=2.2,<3` (tested with 2.2.0). The core runtime
never imports it. The MCP server is a thin proxy that forwards to the runtime
service over its local endpoint and holds no state of its own.

## D-008: Local endpoint is a Unix-domain socket

The runtime service listens on a Unix socket in a per-user `0700` directory,
using newline-delimited JSON requests. Each participant connection is
authenticated with a per-participant token issued by the runtime. Tokens are
stored hashed and passed to proxies through the environment or a `0600` file,
never on argv. Native Windows is unsupported until a named-pipe transport is
built and tested (D13).

## D-009: State location

Resolution order: `DUET_STATE_DIR`, then `$XDG_STATE_HOME/duet` (default
`~/.local/state/duet`) on Linux/WSL, then `~/Library/Application Support/duet`
on macOS. The directory is created `0700`, and startup refuses one that is
group- or world-writable. Repositories hold at most a non-secret run pointer
under `.duet/`. The existing `runs.json` registry stays where it is for v1.

## D-010: Event-sourced reducer over materialised tables (D02)

Every change is an `Event` applied by the pure `duet.runtime.reducer.apply`,
which reads current rows through a getter and returns full rows. The store
writes those rows and appends the event in one `BEGIN IMMEDIATE` transaction.
IDs, sequence numbers and timestamps are produced by the API layer and carried
in the event, so `Store.verify_replay()` can rebuild the tables from the log
and diff them. `idempotency_keys` is the only non-replayed table: it caches
responses.

## D-011: Principals (D02)

There are three principal kinds. `user` is the local operator, from the CLI.
`controller` is Duet's own dispatcher. `participant` is an agent and exists
only as the result of `authenticate(token)`. User-only operations: create
runs, grant or revoke approvals, change the acceptance contract, cancel
required work, and switch to explicit solo. Controller-only operations: mark
tasks VERIFIED, establish COMPLETED_VERIFIED, plan, dispatch and record
actions. Participants act only within their own run; the sender, owner and run
are derived from their token.

## D-012: Leases, fencing and process identity (D02)

Leases carry a fencing token that increases by one on every acquisition.
Writes that depend on ownership (task owner transitions, action outcomes)
present the token, and a superseded or expired token raises `StaleLease`.
Lease owners that are Duet processes are recorded as `host|boot_id|pid|start`,
so a reused PID or a reboot never looks like the old owner. `reconcile()`
releases dead or expired leases and moves in-flight actions to `IN_DOUBT`.
IN_DOUBT is settled only by `resolve_in_doubt` with a reconciliation record,
and there is no transition back to dispatch.

## D-013: Strict-mode input policy (D03)

Strict workspaces are `git worktree`s under the private state directory, on a
new `duet/run-*` branch from an explicit base commit. Nothing untracked or
ignored is copied from the user's checkout. Dirty work arrives only through
`import_inputs` with an explicit list, which refuses secrets (unless
approved), `.git` internals, `..` paths, escaping symlinks and oversized
files. Snapshots hash tracked files as they are on disk. They include
untracked files that pass the input policy, and ignored files only on
explicit request. Sensitive untracked files are excluded and reported;
committed templates (`*.example`, `*.sample`) are not treated as secrets.
Symlinks are hashed by target and never followed. Transient outputs
(`__pycache__`, `.pytest_cache`, …) are not inputs.
`feat/isolate-modes` snapshot mode keeps its copy-everything semantics as an
explicit classic option; strict mode does not adopt them.

## D-014: Evidence, reviews and the completion predicate (D03)

Check evidence is recorded only by the controller, keyed to (snapshot, current
acceptance hash). It is refused if the check ran on inputs other than the
snapshot's. A check whose inputs change while it runs is `invalidated`.
Reviews are keyed the same way, and the reviewer must not be the author or
the author's provider. An approval cannot carry blocking findings. Blocking
findings are closed only by their raiser or the controller. The final
predicate evaluates all eight spec items. `finalize` exports the checkpoint as
the report stands once exported, re-evaluates from the store, and only then
moves the run to COMPLETED_VERIFIED. A contract with no required checks can
never verify.

## D-015: Provider paths (D04)

- Managed Claude peers use `claude -p --output-format stream-json --verbose`
  on the user's login. `--bare` is never used (it requires an API key, which
  would be paid API billing). Permission profiles: `acceptEdits` for writers
  and `dontAsk` for read-only reviewers, plus `--permission-prompts none` where
  supported. There is no bypass mode in strict mode.
- Managed Codex peers use `codex app-server` (stdio) with
  `approvalPolicy: "never"` and a sandbox from the profile; approval and input
  requests are declined. `codex exec --json` is an explicit, lower-capability
  fallback.
- Model and effort are validated before dispatch against what the provider
  advertises (Claude help, Codex `model/list`). Unsupported values raise
  `UnsupportedSetting`; they are never sent in the hope they work.
- Cost observations carry their scope. Claude's resumed-session figure is
  `session_cumulative`; the ledger (D07) derives deltas.
