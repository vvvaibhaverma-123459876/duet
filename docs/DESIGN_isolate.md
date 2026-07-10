# Design: isolation modes for repo sessions

## Problem

`duet run --repo <path>` runs a session against an existing repository. Today it
does so *in place*: it cuts a `duet/session-<stamp>` branch in the user's real
repo and lets the agents edit the real working tree. `--worktree` softens this by
running in a linked git worktree, so the user's checkout is never switched — but
the branch still lands in the real repo, and untracked files (`.env`, installed
dependencies, build artifacts) are absent, so many repos are not runnable there.

We want a third option: a faithful, *runnable* replica of the repo that agents can
edit freely, where nothing they do can reach the source repo.

## Chosen semantics: `--isolate {none, worktree, snapshot}`

`--isolate` is only meaningful together with `--repo`. Passing `--isolate` (or
`DUET_ISOLATE`) as anything but `none` without `--repo` is a hard error rather than a
silent no-op: from-scratch sessions already create a fresh empty workspace, so every
isolation mode would be vacuously true there, and accepting the flag would imply a
distinction that does not exist. An `isolate` key in `duet.toml` is *ignored* in
from-scratch mode rather than fatal — a project-wide default must not make
`duet run "task"` stop working — while a flag or env var set for this one run names a
specific intent that is worth failing on.

| Mode | Where agents work | History | Untracked files | Source repo |
| --- | --- | --- | --- | --- |
| `none` (default) | the real repo | full | present | **mutated** (branch cut, tree edited) |
| `worktree` | linked git worktree | full (shared object store) | **absent** — use `--carry` | branch added; checkout untouched |
| `snapshot` | `copytree` replica in a temp session dir | full | present | **never touched** |

- **`none` is the default.** Bare `--repo` is exactly `--repo --isolate none`, and is
  byte-for-byte today's behavior. `--allow-dirty` and `--rollback-on-failure` keep
  their current meaning here.
- **`worktree`** is the existing `--worktree` path. `--worktree` remains as an alias
  for `--isolate worktree`; the two are interchangeable and combining `--worktree`
  with a *different* `--isolate` value is an error rather than a silent precedence
  rule.
- **`snapshot`** copies the whole repo directory — `.git`, tracked, untracked, and
  ignored files — into Duet's normal isolated session directory, with the normal
  lock and cleanup. The replica therefore has full history, a real `origin` remote,
  installed dependencies, and `.env`. `--exclude <glob>` (repeatable, snapshot only)
  skips paths during the copy; nothing is excluded by default.

Duet's own `.duet/` bookkeeping directory is always excluded from the snapshot: it
holds the source session's lock, and copying a live lock into the replica would
make the replica appear locked by a foreign pid.

**Isolation is an invariant, not a best effort.** For `worktree` and `snapshot`, no
commit, branch creation, or file operation inside the workspace may alter the source
repo's HEAD, refs, or working tree. This is asserted directly in the test suite
against real git repositories, not mocks.

### `--carry <path>`

Copies named untracked files or directories from the source into the workspace.
It exists for `worktree`, which starts from a clean checkout and therefore omits
`.env` and friends. Under `snapshot` those files are already present, so `--carry`
is a verified no-op rather than an error — the same command line works under either
mode, which is the point.

### `--base <ref>` and `--branch <name>`

When either is given, they override the automatic `duet/session-<stamp>` naming:
the working branch is created off the base before turn 1 (`git checkout -b <name>
<base>`, or `git worktree add -b <name> <dir> <base>`). If `<name>` already exists,
Duet fails with a clear message and never silently resets or reuses it. When
omitted, the existing `duet/session-*` scheme is kept unchanged.

Because Duet creates the branch itself, a task prompt used with `--branch` must not
also instruct the agents to create it; they should commit onto the current branch.

## Commit modes: `--commit-mode {default, agent-driven}`

- **`default`** preserves whatever the selected path does today. In every existing
  mode the Broker runs `git add -A && git commit` after each turn, authored by the
  agent and committed by `Duet Broker`.
- **`agent-driven`** injects no Broker commits at all. Instead each agent subprocess
  is spawned with `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` / `GIT_COMMITTER_NAME` /
  `GIT_COMMITTER_EMAIL` set to that agent's display name and `<agent>@duet.local`,
  so the commits the agent makes *itself* carry its identity.

Rationale: repo-remediation tasks arrive with their own required commit sequence and
exact commit messages (a migration split across reviewable steps, a fix that must be
cherry-pickable, a conventional-commit trailer). A Broker commit after each turn
either buries that sequence under `"Claude turn"` squashes or mis-messages it. Under
`agent-driven`, `git log`, `/log`, `/diff`, and the transcript all reflect the commits
the agent actually intended, and provenance stays truthful because the identity is
set by the environment the agent cannot forge from inside its own prompt.

The tradeoff is that a lazy agent may leave the tree dirty; nothing auto-commits for
it. That is deliberate — under `agent-driven` an uncommitted change is a signal, not
something to paper over.

## Configuration

`isolate` and `commit_mode` join the existing `[session]` config table and follow the
project's established precedence chain, extended with the env tier the spec calls for:

    CLI flag  >  DUET_ISOLATE / DUET_COMMIT_MODE  >  duet.toml [session]  >  default

Duet had no general env tier before this change (only ad-hoc `DUET_LOG` /
`DUET_LOG_FILE` inside `logging_setup`), so this introduces a small resolver rather
than slotting into an existing one. Invalid values are rejected at parse time with
the same `ConfigError` / argparse behavior as the surrounding options.

## Rejected alternatives

**Repurposing `--repo` to mean snapshot-copy.** An earlier draft proposed making
`--repo` default to an isolated copy and moving today's in-place behavior behind a new
`--in-place` flag. Rejected: `--repo` is public surface with the opposite meaning, and
the failure mode is silent. Every existing `duet run --repo ./app` script would keep
exiting 0 while operating on a throwaway directory, and the user's real repo would
simply never change — no error, no branch, no diff, just work that evaporates. Roughly
ten CLI battery tests encode the current contract, and `connect` / `resume` reuse
`--repo` to reattach live agent sessions, which is incoherent against a copy: you
cannot resume a real Claude session into a directory it has never seen. An additive
enum defaulting to today's behavior gets the same capability with no silent breakage.

**Making `--isolate` a no-op without `--repo`.** Rejected in favor of a hard error;
see above.

**`git clone --no-hardlinks` instead of `copytree` for snapshot.** Clone yields a
clean history and a correct `origin` in one command, but drops exactly what makes the
replica *runnable* — untracked and ignored files, i.e. `node_modules`, `.venv`, `.env`,
build output. Recovering those means re-adding a `--carry` list per repo, which is the
worktree ergonomics we are trying to escape. `copytree` copies the real `.git`, so the
true `origin` and full history come along for free. The cost is copying large ignored
directories, which `--exclude` addresses opt-in.

**Snapshot refusing a dirty source.** Rejected: the source is never touched under
snapshot, so uncommitted work there is harmless and copying it is the faithful thing
to do. `--allow-dirty` stays meaningful only for `--isolate none`.
