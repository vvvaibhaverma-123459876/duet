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

## D-016: MCP SDK (D05)

`duet mcp serve` uses the official MCP Python SDK (`mcp`) behind an optional
extra: `pip install 'duet[mcp]'`, pinned `mcp>=2.2,<3` and tested with 2.2.0.
The 2.x API (`mcp.server.mcpserver.MCPServer`) replaces 1.x `FastMCP`, hence
the major-version cap. The core imports nothing from the SDK, and the CI root
job runs without it. A legacy `initialize` handshake (what Claude Code and
Codex send) was checked against the server directly.

## D-017: Local runtime service (D05)

- One service per state directory (exclusive `flock`), started on demand by
  the first MCP proxy or CLI (spawn lock). The MCP proxy never opens the
  database itself: one scheduler, one state store.
- Transport: a Unix-domain socket, 0600, in a 0700 directory, with a short
  fallback path when the state path exceeds `sun_path`. The connecting uid is
  checked with SO_PEERCRED on Linux. Each connection carries one
  newline-delimited JSON request, at most 1 MiB. There is no network listener
  and no browser-reachable API.
- Authentication: a participant bearer token (hash stored) or the per-start
  service secret (0600 file) for the user's CLI. Unauthenticated callers may
  only `ping` and `join`. Identity, recipients and workspace always come from
  the token, and unknown arguments are rejected.
- Same-user processes can read the secret and reach the socket. This is
  isolation from other users and from accidents, labelled
  `containment=cooperative`; it is not a sandbox.

## D-018: Delivery, identity and turn rules (D05)

- Native sessions: `receive=checkpoint`, `native_identity=connection_bound`.
  The claimed host (the agent CLI that launched the proxy) must be a live
  ancestor of the connecting process (Linux). A host that exits makes the
  participant `gone`, and its token stops working: a later resume is a
  different session (R02).
- Managed sessions: `receive=push` (a turn starts when an actionable message
  arrives), `native_identity=verified` (session or thread id observed), Codex
  `containment=unverified`.
- `send` never waits. `wait` is bounded (≤ 50 s, below Codex's default MCP
  tool timeout) and wakes for any new message, run or peer change, or
  cancellation. Delivery is at least once: the proxy's watermark resets to
  the durable cursor on restart.
- A managed turn starts only for peer questions, answers, review requests,
  findings, proposals, blockers, or a controller BLOCKER. Informational
  notices ride along with the next turn.
- The fallback answer (the turn's final text, labelled) is sent only when the
  managed peer sent nothing at all during a turn that carried a question.

## D-019: Vertical-slice run shape (D05)

- One writer per run (`self` or `peer`, fixed at creation), holding the
  fenced workspace lease. The reviewer reads a read-only materialised
  snapshot. The review request carries a diff built from that snapshot,
  never from the live workspace.
- The initiating native session proposes the checks (argv only, never a
  shell). At least one check is required, since nothing else can verify
  completion. Only the user changes the contract afterwards.
- Checks run as durable `check` actions. A check interrupted by a service
  crash is settled FAILED at restart (read-only and repeatable); an
  interrupted provider turn stays IN_DOUBT.
- On COMPLETED_VERIFIED the snapshot's recorded paths (never excluded
  secrets) are committed to the DUET-owned branch. Nothing is pushed or
  merged.
- Overdue runs pause (`PAUSED_BUDGET`) and their managed peers stop. A paused
  run does not keep the service alive.

## D-020: Shared task graph (D06)

- The scheduler (`runtime/scheduler.py`) is a pure function of a
  `GraphState` snapshot: no database, clock or randomness. It holds no
  authority; the coordinator turns its answers into commands.
- Plans are shared. One participant proposes (at most 12 tasks, one pending
  plan per run); the other accepts or rejects; the proposer may withdraw. A
  plan only adds tasks. It cannot change the objective, the acceptance
  contract or existing tasks. `duet_propose_task` is a one-task plan.
- Bounds: 32 tasks per run, 3 ancestors per task, 2 active claims per
  participant. Proposals create tasks, never agents.
- Task kinds: `code` (the single writer only, until parallel workspaces in
  D11), and `investigate`, `test_design`, `review` (either participant).
  A finished non-main task goes to the other participant for acceptance; the
  controller then marks it VERIFIED. The main task keeps DUET's checks plus
  a cross-provider snapshot review.
- `CHANGES_REQUESTED` work is reclaimed by its owner (for code, the current
  writer), not by the other participant.

## D-021: Contributions from evidence only (D06)

A contribution record is written by the controller only from evidence: an
authored snapshot with changes (`code`), a review of an exact snapshot or a
decision on a task result (`review`), an accepted task result (its kind), or
an accepted plan (`plan`). Ids are derived from (run, participant, kind,
ref), so nothing counts twice. The completion gate's `both_contributions`
item uses snapshots, reviews and these records; message counts no longer
count (R01). This tightens the D03 gate.

## D-022: Loop control (D06)

- Progress is a fingerprint of task states and revisions, snapshot trees,
  check results and open findings. Message text never changes it.
- Repeated failed hypotheses (failed required checks, blocking reviews,
  rejected results; `max_repair_attempts`, default 2, of the same failure,
  or one more of any kind) block the task and ask for a re-plan. The task
  unblocks only when the other participant accepts a new plan. The same
  after a re-plan pauses the run (`PAUSED_APPROVAL`): the user decides.
- Twelve peer messages without a fingerprint change are a stall: both
  participants get a BLOCKER. If the stall continues for another window, the
  run pauses. Samples live in the service's memory, so a restart resets the
  stall window, but not the failure history, which is read from the store.
- The role of writer moves by `duet_handoff`: the writer hands it over, or
  the reviewer takes it over when the writer is unavailable. The outgoing
  writer's active code tasks return to READY.

## D-023: Usage observations and ledger semantics (D07)

- `duet.usage.Observation` is the one normalised record: provider,
  account/pool (None = unknown), session, run, metric (and so its
  dimension), scope, source, `source_event_id`, epoch, baseline, quality
  (`observed`, `estimated`, `unknown`) and a UTC time. Money is `Decimal`,
  never float. Unknown is `value=None` with quality `unknown`, never zero.
  Gauge dimensions (quota windows, context occupancy, elapsed deadline)
  only take point-in-time scopes, so they cannot become consumption.
- The ledger (`duet.usage.Ledger`) is pure and keeps only deduplicated raw
  observations. Everything else is derived on demand and does not depend
  on arrival order. A replay keeps the earliest timestamp; the same
  identity with a different value is a recorded conflict.
- For each (provider, session, metric), the first source in the policy that
  is present is primary and is the only one counted. Other sources
  validate it (agree, lagging, ahead, disagree, incomparable) and never
  add to it. Defaults: the Claude CLI result, then the status line; Claude
  `modelUsage` for tokens; the Codex thread `total`. An undesignated source
  alone yields unknown.
- Cumulative counters become deltas only within one declared epoch, per
  key. The first value counts from its baseline: zero for a session DUET
  saw created, the parent's level for a fork, otherwise unknown and bounded
  by the value. A decrease is a recorded reset, and its interval is
  unknown. An unknown marker (for example a cancelled turn) is resolved
  only by a later value that covers its time.
- Deltas are attributed to the pool of their observation. A pool change
  within a counter, or an unknown pool, leaves the delta unattributed.
  Quota windows are latest-value gauges per pool and window. They are
  never converted into tokens, and never combined across providers.
- Token totals follow a per-provider algebra. Codex: `total`, or
  input + output, since cached input is inside input and reasoning inside
  output; `cacheWriteInputTokens` is never added because its relation is
  unverified. Claude: the four fields are disjoint.
- The pure layer keeps exact `Decimal` values. Converting them to D-003's
  integer micro-units, including how to round sub-micro per-model costs,
  is the persistence layer's decision.

## D-024: Claude status-line telemetry (D07)

`duet.integrations.claude_statusline` reads only allowlisted fields of the
documented payload: session id, version, model id/name, cost and durations,
context-window figures and rate-limit windows. JSON floats are parsed as
`Decimal`. Paths, names, prompt ids, PR data and unknown fields are never
copied, and the transcript is never opened. Everything is keyed by
`session_id`, never by a process id. The cost is a session-cumulative
estimate whose first value has an unknown baseline. Context figures are
gauges. Rate-limit windows are capacity with an unknown pool, because the
payload names no account. `run_status_line(original_command, stdin)` runs
the user's command with the untouched stdin and returns its output and
exit status byte for byte. Telemetry errors are reported, never shown.
Installing the wrapper is a later milestone.

## D-025: Codex `tokenUsage.last` is one update, not one turn (D07)

In codex-rs, each `thread/tokenUsage/updated` adds its `last` to `total`
(`TokenUsageInfo::append_last_usage`), so a turn with several model
requests has several `last` values. The D04 collector keeps only the final
update, so its `turn`-scoped observations are not the turn's usage. The
ledger therefore uses the thread `total` as primary. Adapters from a
`TurnResult` drop `last`. Raw notifications keep it, as call-scoped
validators. D04 code is unchanged.

## D-026: Local usage pools and reservations (D07)

- A pool (`usage_pools`, migration 0005) is a user-defined allowance for one
  provider metric (`turns` or `cost.estimated_usd`), with an optional rolling
  window. Only the user principal can define or change one (`duet usage pool
  set`); agents and the controller cannot create or widen spending authority
  through DUET. The protection is cooperative: the CLI writes the local
  database as the user, so any process running as that OS user could do the
  same (internal review, D08).
  Enforcement is `local_bound` (refuses), `best_effort` (reports only) or
  `provider_cap` (mirrors a provider-enforced limit).
- The capacity check runs inside `plan_action`'s write transaction. SQLite's
  write lock serialises it across processes, so two runs cannot both take
  the last of an allowance. Consumption is the known usage records in the
  window plus reservations still HELD. Held reservations count until an
  explicit outcome, and in-doubt ones until reconciled.
- Usage records are keyed by a dedupe identity (`<action>:<pool>`), so
  replays and delayed duplicates count once. The same identity with a
  different quantity is a conflict. A record of unknown size makes the pool
  `uncertain`, never zero.
- Managed turns reserve 1 from each matching turns pool. Cost pools get a
  zero reservation, which refuses a turn once the pool is exhausted but
  cannot stop one turn from overshooting; per-turn estimates are D08's.
  The turn's cost delta comes from the usage ledger, which turns a resumed
  session's cumulative figure into a delta. It is recorded as `estimated`,
  or `unknown` when it cannot be derived (Codex reports no cost).
- A pool is not a view of the provider's own quota. A local reservation
  cannot lock provider-side capacity, and usage outside DUET is invisible
  here. Quota windows are observed gauges in `duet.usage`.

## D-027: Completion-aware admission (D08)

- Every managed turn is admitted in the same write transaction that plans
  it (`ReservationBook.admit`). The pure `admission.decide()` compares, per
  local_bound pool of the turn's own provider and metric: estimate +
  outstanding reservations + remaining finishing reserves + a margin
  (unknown-size records x the estimate) with allowance - used. Outstanding
  and finishing reservations are both HELD rows, so one sum carries both.
- Turns are classified by purpose: finishing (the review, repairs while the
  task has changes requested), required (implementing the open task,
  questions, blockers), optional (everything else). Optional work is deferred
  first and never draws on a reserve. A refused required or finishing turn
  pauses the run for the user (`PAUSED_BUDGET`); nothing is silently skipped.
- Unknown quantities get a bounded policy, not a fictional inequality. A
  turn of unknown size runs alone per pool while the pool has room, and
  optional work waits for a measured turn. Without quota telemetry, optional
  turns are capped per provider per run (12), on top of the run's
  invocation cap.
- Enforcement labels are per pool: provider_cap only when the provider
  enforces the limit per call (Claude `--max-budget-usd`, money only);
  local_bound for work DUET schedules; best_effort for tracking pools. A
  pool that asks for a cap the provider cannot enforce pauses the run
  (`PAUSED_APPROVAL`) before any work.

## D-028: Finishing reserves (D08)

- When the pair forms (and again at the first admission, if missing), DUET
  reserves two review turns on the reviewer's provider and two repair turns
  on the writer's, in each bounded pool of that provider. A native
  participant gets no reserve: DUET does not schedule its turns. Cost
  reserves are units x the current high estimate, resized after each turn,
  and start at zero (with the size unknown) until a turn has been measured.
  A reserve that cannot be fully held records its shortfall.
- A finishing turn draws on its run's reserve for the same purpose and pool:
  `action.planned` carries `draw_from`, and the reducer moves that quantity
  from the reserve to the action's reservation, so it is held once. Only
  the part the reserve does not cover is checked against free capacity. A
  terminal run releases what is left.

## D-029: Quota pauses and reset-aware resume (D08)

- Quota readings (`quota.observed`) are provider windows as observed. An
  older reading never replaces a newer one, and the previous level is kept
  so a jump from use outside DUET is visible. Readings are compared with
  thresholds only, never converted into turns or averaged.
- A reading at 100%, or a quota or rate-limit failure, places a provider
  hold. It resumes at the window's reset (plus a grace minute) or after a
  bounded backoff of 5, 10, 20, 40, then 60 minutes. After the resume time,
  a passive read (Codex `account/rateLimits/read`) can end the hold without
  a model call. Otherwise exactly one real turn is admitted as the probe
  while the others wait for its outcome.
- The run pauses (`PAUSED_QUOTA`) only when the held participant owns the
  current obligation and every other participant is managed. With a native
  participant, the run stays active and the native session is told what is
  pending and until when. The review stays required either way. The service
  stays alive for `PAUSED_QUOTA` runs, and its monitor resumes them
  (RECONCILING, then the state the main task implies).


## D-030: Adaptive model and effort routing (D09)

- Routing is a pure policy (`duet/routing`): the same request always gives
  the same decision. The runtime (`duet/runtime/routing_control.py`) builds
  the request from the store: the task's changed and protected paths, its
  checks, its failures with their output, the participant's previous
  decisions and outcomes, the provider's discovered controls, the user's
  pins and profile maps, and pressure from admission (D08).
- There are four logical profiles: routine, standard, deep and
  critical_review. Each is mapped per provider at run time: first from the
  user's own maps (`duet routing map`), otherwise from that provider's own
  discovered effort labels, ranked on its own documented scale. There are no
  built-in model names, and effort labels are never compared across
  providers. The provider default always remains a valid candidate.
- Risk comes from what the change touches, not its size (AT16). An agent
  can raise scrutiny and never lower it. Budget pressure lowers a target
  towards the floor, never below it. A user pin is respected even below the
  floor; the decision then records `floor_met: false`.
- Failures are classified before any escalation. An environment failure
  is diagnosed, not escalated (AT19); an unclear requirement asks for an
  assumption or a question; repeated hypothesis failures trigger a re-plan
  and at most one escalation per task.
- Settings change only at a turn boundary (a managed turn). For a native
  session the decision is advice (coverage `advisory`), because DUET cannot
  change a native client's model (AT41). A setting the provider refuses
  before dispatch is excluded with its evidence and the turn is re-routed
  (an explicit downgrade). What the provider accepted and observed is
  recorded next to what was requested, and differences are flagged (AT17).
- Routing never selects another provider or participant, so it can never
  replace the second provider's obligation to save cost.

## D-031: Final-revision review and completion (D10)

- Required checks are baselined on the base commit. A `change` criterion
  counts only with fail-to-pass evidence or an explicit non-author
  attestation (review scope `criterion:ID`). A `preserve` criterion needs
  its checks to pass. `kind` is omitted from the contract when it is the
  default, so earlier contract hashes stay valid.
- Authorship is per file, from content hashes, never from roles: who first
  submitted or handed off the exact content wrote it. Review coverage is per
  file, by a non-author of the other provider. Co-edited files need each
  earlier version reviewed on its own snapshot. Several writers each
  acknowledge the final snapshot (a new `acknowledge` disposition). The
  submitter may review only files it did not write, named explicitly.
- Delta reviews name their basis (`basis:SNAPSHOT`). Unchanged files carry
  the basis approval; changed files need the new review.
- Passed evidence is reused only for the same snapshot, contract version and
  environment fingerprint.
- The final report (`duet.final-report/1`) is deterministic: built from
  records, with every missing obligation and its next action.

## D-032: Selective parallel implementation (D11)

- One run workspace, one writer. Independent code work is a
  `code_isolated` task for the other participant, in its own worktree
  (branch `<run branch>-task-<id>`, from the base commit) under its own
  lease.
- The writer is the only integration owner. Accepting an isolated result
  applies its patch inside that call, after checking the writer's fence, as
  an `integration` action. The patch is checked before anything is written.
  A conflict writes nothing and becomes a `code` task with the patch as
  evidence.
- An integration in doubt after a crash is settled from the workspace
  contents (reverse-applies, applies, or neither). A second application
  happens only when the files prove the first did not.
