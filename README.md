# Duet

Make Claude and Codex collaborate in your terminal, with the orchestrator deciding when they're actually done.

## Requirements

- Claude Code installed and authenticated: `claude`
- OpenAI Codex installed and authenticated: `codex`
- Git
- Python 3.11+

Duet does not bundle, install, or manage Claude Code, Codex, or their accounts. It is a conductor; the performers must already be present and logged in, like a tool that wraps `git` or `ffmpeg`.

## Quickstart

```bash
pipx install duet
duet doctor
duet
```

For development from this checkout:

```bash
pip install -e .
duet doctor
duet run --seed-demo --max-turns 6 --verify pytest
```

Piped stdin auto-routes to headless mode:

```bash
echo "Implement the task in this workspace" | duet
```

## Architecture

```mermaid
flowchart LR
    TTY[duet REPL] --> Engine[Broker engine]
    Run[duet run/exec] --> Engine
    Future[Future MCP peer consult] --> Engine
    Engine --> Transcript[Canonical transcript]
    Engine --> Workspace[Isolated locked git workspace]
    Engine --> Claude[Claude Code CLI]
    Engine --> Codex[Codex CLI]
    Claude --> Workspace
    Codex --> Workspace
    Workspace --> Verifier[Verifier: pytest or unknown]
    Verifier --> Engine
    Engine --> Artifacts[JSON + Markdown logs]
```

## CLI

```bash
duet                         # REPL when stdin is a TTY
duet run "Fix the bug"       # headless one-shot in a scratch workspace
duet exec "Fix the bug"      # alias for run
duet doctor                  # preflight
duet status --repo PATH      # detect agent sessions/processes for a repo (live vs idle)
duet connect --repo PATH "…" # resume existing claude+codex sessions as one duet
duet sessions --repo PATH    # list Claude Code sessions available to attach
duet sessions codex          # list Codex sessions (most recent first, with cwd)
duet resume --repo PATH      # continue the last duet run there, re-attaching both agents
duet ps                      # list recent duet runs on this machine (status + cost)
duet talk claude "..."       # one solo turn, resuming that agent's newest session
duet stop                    # list running duet/claude/codex sessions, pick one to stop
duet stop codex --yes        # stop without prompting (SIGINT; --force for SIGTERM)
duet peek codex              # read-only tail of the most recent Codex session
duet peek claude ID --repo P # same for a Claude Code session of repo P
duet replay transcript.json  # render markdown
duet init --project          # write ./duet.toml
duet init --user             # write ~/.config/duet/config.toml
duet --version
```

Global flags: `--log-level DEBUG|INFO|WARNING|ERROR` (or `DUET_LOG`) and
`--log-file PATH` (or `DUET_LOG_FILE`) enable structured logging.

## Working on an existing repo (live mode)

By default `duet run` operates in a disposable scratch workspace. To let the
agents work on a real repository, point `--repo` at it:

```bash
duet run --repo /path/to/project "Add retry logic to the HTTP client" --verify pytest
```

Live mode is safe by default:

- The target must be a git work tree, and by default its tree must be **clean**
  so unrelated uncommitted work is never swept into Duet's commits. Pass
  `--allow-dirty` to override.
- Duet checks out a fresh `duet/session-<timestamp>` branch and records the base
  commit. **All agent commits land on that branch only** — your original branch
  is never modified.
- Duet never merges. When the session ends you review the branch
  (`git log`, `git diff`) and merge deliberately.
- `--rollback-on-failure` discards the Duet branch if the session does not
  succeed (or is interrupted); otherwise the branch is left for inspection.
- `--branch NAME` overrides the generated branch name, and `--base REF` chooses
  what it branches off. If `NAME` already exists Duet fails rather than resetting
  it. When you pass `--branch`, your task prompt should **not** also tell the
  agents to create a branch — Duet already did, and they should commit onto it.

`--repo` edits your real repo in place. To work inside an isolated replica
instead, see [Isolation modes](#isolation-modes) below.

Because the bundled agent commands run non-interactively
(`--dangerously-skip-permissions` for Claude, `--ask-for-approval never` for
Codex), the branch isolation and rollback are the guardrails on a live repo. If
you want an interactive approval gate instead, edit the agent `command` in your
`duet.toml`; branch isolation still applies.

## Attaching to an existing agent session

Duet can resume an agent CLI session that already exists — for example a Claude
Code session another operator (or you) previously ran against the same repo —
so the Duet performer starts with that session's full context instead of cold.

```bash
duet sessions --repo /path/to/project          # find the session id
duet run --repo /path/to/project \
  --attach claude=<session-id> \
  "Continue the audit you were running; close out the remaining findings"
```

- `--attach AGENT=SESSION_ID` (repeatable) resumes that agent via its
  `resume_command` from `duet.toml` (`claude -p --resume`, `codex exec resume`).
- Attaching implies **session chaining**: each turn returns a fresh session id
  and Duet feeds it into the next turn's resume, so the conversation stays one
  continuous thread across the whole Duet run.
- `--chain-sessions` enables the same chaining for cold-started agents, giving
  performers real cross-turn memory instead of relying only on the re-fed
  transcript.
- The final summary prints each agent's last session id so a later
  `duet run --attach` can pick up exactly where the run stopped.

Honest limits: resuming *forks* the stored conversation state. If the original
session is still open in another terminal, that terminal will not see Duet's
messages — this is "continue that agent's memory", not "type into its window".
Attach to sessions that are idle or finished. Run with `--repo` pointing at the
same directory the session was recorded in, since Claude Code stores sessions
per project directory.

To observe a session that is still running, use `duet peek` instead of
attaching. Peek only reads the session's transcript file (recent messages and
tool calls, newest last) and never locks, mutates, or forks it:

```bash
duet sessions codex                 # find the live session and its cwd
duet peek codex                     # tail the most recent one
duet peek codex <session-id> --lines 50
```

## Connecting independent sessions into a duet

When Claude Code and Codex have each been working on the same repo in separate,
unrelated sessions, `duet connect` turns them into one brokered duet without
losing either agent's context:

```bash
duet status --repo /path/to/project    # who's there? live or idle?
duet connect --repo /path/to/project "Reconcile your work and finish the task"
```

`connect` detects each agent's newest session for the repo (Claude sessions are
matched by project directory; Codex sessions by recorded cwd, including
ancestors) and resumes both into the broker loop. Detection is automatic but
overridable with `--claude SESSION_ID` / `--codex SESSION_ID`. An agent with no
session for the repo is cold-started — so `connect` also covers the "launch the
missing partner" case.

Safety: a session whose transcript was written in the last three minutes is
classified **LIVE**, and `connect` refuses to resume it — resuming would fork
the conversation out from under the running agent. Peek at it instead, wait for
it to go idle, or pass `--fork-live` to accept the fork deliberately.

Multiple duets can run in parallel: each `duet run`/`connect` takes a workspace
lock and its own `duet/session-*` branch, so concurrent sessions on different
repos (or worktrees of one repo) do not interfere. Duet does not open terminal
windows for you; run each session in its own terminal or under `tmux`.

## Verification gates

`--verify` accepts `pytest`, `none`, or `cmd:<shell command>`, and can be
repeated to form an all-must-pass composite. While the gate fails, an agent's
`[[DONE]]` is not honored — "done" means the checks actually pass, not that
the agents agree they're finished:

```bash
duet run --repo . \
  --verify "cmd:cd frontend && npm test" \
  --verify "cmd:cd frontend && npx tsc --noEmit" \
  "Fix the dashboard health check"
```

## Isolation modes

`--isolate` chooses how far a `--repo` session is separated from your real
repository. It only applies together with `--repo`; from-scratch runs already work
in a fresh scratch workspace.

| `--isolate` | Agents work in | History | Untracked/ignored files | Your repo |
| --- | --- | --- | --- | --- |
| `none` *(default)* | the real repo | full | present | branch cut, tree edited |
| `worktree` | a linked git worktree | full | **absent** — use `--carry` | branch added, checkout untouched |
| `snapshot` | a full copy in a temp dir | full | present | **never touched** |

Bare `--repo` is exactly `--repo --isolate none`, unchanged from earlier versions.
`--worktree` remains an alias for `--isolate worktree`.

```bash
# Faithful, runnable replica: deps, .env, full history, real origin — source untouched.
duet run --repo ./app --isolate snapshot "Fix the flaky retry test" --verify pytest

# Skip the expensive ignored directories.
duet run --repo ./app --isolate snapshot --exclude node_modules --exclude .venv "..."

# Worktree starts from a clean checkout, so bring the files it omits.
duet run --repo ./app --worktree --carry .env "..."
```

**`worktree` omits untracked and ignored files.** It is a clean checkout of a
commit, so `.env`, `node_modules/`, `.venv/`, and build output are simply not
there, and a repo that needs them will not run. `--carry <path>` (repeatable)
copies named untracked files or directories in from the source. Under `snapshot`
those files are already present, so `--carry` is a harmless no-op — the same
command line works under either mode.

**`snapshot` copies everything, including what you ignore.** `shutil.copytree`
brings `.git`, tracked, untracked, and ignored files across, which is what makes
the replica runnable and preserves the true `origin` and full history. The cost is
that a repo with a 900 MB `node_modules/` copies 900 MB. Use `--exclude <glob>`
(repeatable, snapshot only) to skip those paths; nothing is excluded by default.
A dirty source is fine under `snapshot` — it is never touched, so `--allow-dirty`
is only meaningful for `--isolate none`.

**Pushing.** Duet never pushes and never merges, in any mode. Under `snapshot` the
replica keeps your real `origin`, so a `git push` from the workspace *would* reach
your remote — that is deliberate, so a finished branch can be published from the
replica, but it means the replica is isolated from your *working copy*, not from
your remote. Review before you push:

```bash
git -C <workspace> log <branch>
git -C <workspace> diff main...<branch>
```

The workspace path, branch, and a reminder that the source is unmodified are all
printed at the end of the run. Worktrees are kept for inspection (with a cleanup
command); `--rollback-on-failure` removes both worktree and branch. Snapshots have
nothing to roll back — the source never changed — so the replica is simply left in
place for you to inspect or discard.

## Commit modes

By default the Broker commits the whole workspace after each turn, authored by the
agent that spoke. Pass `--commit-mode agent-driven` and Duet injects **no commits
at all**; instead each agent subprocess is spawned with `GIT_AUTHOR_NAME` /
`GIT_AUTHOR_EMAIL` / `GIT_COMMITTER_NAME` / `GIT_COMMITTER_EMAIL` set to that
agent's name and `<agent>@duet.local`, so the commits it makes itself carry its
identity.

```bash
duet run --repo ./app --isolate snapshot --commit-mode agent-driven \
  "Split the migration into three reviewable commits, conventional-commit messages"
```

Use it when the task has its own required commit sequence and exact messages: a
per-turn `Claude turn` squash would bury or mis-message them. The tradeoff is that
nothing auto-commits — if an agent leaves the tree dirty, the transcript says so
rather than papering over it.

Both options follow Duet's precedence chain, so they can be set once per project:

    CLI flag  >  DUET_ISOLATE / DUET_COMMIT_MODE  >  duet.toml [session]  >  default

```toml
[session]
isolate = "snapshot"
commit_mode = "agent-driven"
```

`connect` and `resume` always run against the real repo. They reattach live Claude
and Codex CLI sessions, which cannot be resumed into a replica directory they have
never seen, so they ignore an `isolate` default and stay in place.

## Cost tracking and budgets

Agents that report spend (Claude Code's JSON output includes
`total_cost_usd`; configured via `cost_json_path`) have per-turn cost recorded
in the transcript and summed in the summary. `--budget-usd X` (or
`[session] budget_usd`) halts the session once reported spend reaches the cap
— resumable exactly like a quota halt. Codex CLI reports no cost, so the cap
tracks reported costs only; turn/wallclock caps remain the backstop.

`duet ps` lists recent runs on the machine (from `~/.local/state/duet`), with
live status (running / success / halted / died) and reported cost.

## When an agent runs out of usage limit

Quota/rate-limit failures are detected (the CLI's error output is scanned for
usage-limit signals) and handled per the `--on-quota` policy, configurable in
`[session]` or per run on `duet run`/`exec`/`connect`:

- **`halt`** (default): stop cleanly. Completed turns are already committed on
  the duet branch, the transcript is saved, and each agent's session id is
  printed — when the limit resets, `duet connect` resumes both agents with
  full context.
- **`solo`**: drop the exhausted agent from the rotation and let the surviving
  agent finish alone. The transcript records a note that the partner's
  review/verification is pending, and success no longer waits for the dropped
  agent to have spoken.
- **`wait`**: sleep `--quota-wait-seconds` (default 300) and retry the same
  agent, as long as the next wait still fits inside the wallclock budget;
  otherwise halt with the same resumable state as `halt`.

`duet doctor` (run automatically before headless sessions) does an
authenticated round-trip per agent, so a limit that is already exhausted
aborts the run before any turns are spent.

### Resuming after the limit resets

Every run saves a resume manifest (`.duet/resume.json` in the workspace) with
the task, outcome, stop condition, and each agent's last session id. When the
limit is back:

```bash
duet resume --repo /path/to/project              # re-attach both agents, continue the task
duet resume --repo /path/to/project --wait-ready # poll doctor every 10 min until agents
                                                 # pass their round-trip, then auto-resume
```

`resume` re-attaches every agent that has a saved session id (cold-starting
any that don't), reuses the same workspace so committed turns are visible, and
prepends a note telling the agents this is a continuation, not a fresh start.
`--wait-ready [SECONDS]` turns it into the full loop for a quota halt: park,
probe (each probe is one doctor round-trip per agent — rate-limited calls are
rejected without drawing usage), and continue automatically the moment the
limit resets.

## Controlling sessions individually

`duet talk` continues one agent on its own — no broker, no partner, no branch
isolation (the agent acts directly in `--repo` with the same permissions as a
duet turn). It resumes the agent's newest session for the repo (live guard
applies), sends one message, prints the reply and the session id to continue
from. `--new` starts fresh; `--session ID` picks a specific session.

`duet stop` handles shutdown. With no arguments it lists everything stoppable —
a duet run holding the repo's lock (by pid), and any claude/codex CLI processes
(by tty and runtime, since an outside process cannot be tied to a session file)
— and asks which to stop. It sends SIGINT (what Ctrl-C in that terminal would
do) so the agent exits cleanly; `--force` escalates to SIGTERM. `--yes` skips
the prompt and is required when stdin is not a TTY. Stopping an agent's TUI
does not destroy its session: the transcript stays on disk and `duet connect`,
`duet talk`, or the agent's own resume command can pick it up later.

## Production hardening

- Subprocess timeouts with process-tree kill for agents, and a bounded timeout
  for the pytest verifier so a hanging test cannot wedge the session.
- Git failures are surfaced as clean halts, not crashes; agent/verifier output
  stored in transcripts is size-bounded to protect memory.
- The workspace lock records its PID and is reclaimed automatically if the
  owning process has died; SIGINT/SIGTERM trigger a graceful shutdown that
  releases the lock and honors rollback intent.
- Malformed config files fail fast with a clear `ConfigError`.

Config precedence is deterministic: `--config PATH`, then `./duet.toml`, then `$XDG_CONFIG_HOME/duet/config.toml` or `~/.config/duet/config.toml`, then the built-in detected defaults.

## Design Decisions

Sequential turns are the concurrency-safety model. Duet never runs Claude and Codex at the same time, so there is no file-write race to resolve.

Git is the audit trail. Duet initializes the workspace as a git repository, sets repo-local committer identity, and commits after each turn with the active agent as author.

Termination is execution-grounded when a verifier is configured. For coding tasks, `PytestVerifier` runs `pytest -q` after every turn; passing tests can stop the session without trusting an agent's claim.

The engine/interface split is deliberate. The REPL, headless `run/exec`, and a future MCP peer-consult server are thin front-ends over the same broker API.

Solo mode is supported. If exactly one agent is installed and authenticated, Duet still launches as a front-end to that agent and clearly reports that collaboration is disabled.

The runtime core has zero third-party dependencies. `pytest` is only a development/test dependency.

## Stop Policy

Duet checks these conditions after every turn: `VerifierStop`, `ControlToken`, `MaxTurns`, `WallClockBudget`, and `LoopDetector`. The final summary reports the outcome and the condition that fired.

## Limitations

Each turn calls a frontier coding CLI and consumes from the configured account or plan. Keep `max_turns` low.

Agent behavior is nondeterministic. Duet provides bounded prompts, git auditability, and verifier-backed stopping, but convergence is not guaranteed for arbitrary tasks.

The practical audience is people who already use Claude Code and Codex locally. Headless auth and sandbox semantics are owned by those CLIs.

On Windows, prefer WSL for consistent subprocess and sandbox behavior.
