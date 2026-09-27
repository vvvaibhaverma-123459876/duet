# D00 Branch Reconciliation

All remote refs were fetched on 2026-09-27. Ancestry was checked with
`git merge-base --is-ancestor`, and diffs were read in full.

| Branch | Tip | Relation to `origin/main` (c2431d1) | Decision |
|---|---|---|---|
| `main` | c2431d1 | — | Base |
| `feature/session-attach` | 4478cbf | ancestor of main (main is 6 commits ahead) | Already merged; nothing to do |
| `harden/production-live-mode` | e985048 | ancestor of main (main is 10 ahead); PR #1 closed, but the commit is in main | Already merged; nothing to do |
| `feat/isolate-modes` | 3c8630f | **5 commits ahead of main, 0 behind**, no PR | **Merged into the implementation branch** (merge commit `9db29f3`) |
| `claude/adoring-babbage-0ys7ui` | cd32d25 → | implementation branch (draft PR #2) | Where the work lands |

## `feat/isolate-modes`: what it adds

- `--isolate {none,worktree,snapshot}` for `run`/`exec` and the REPL. `none`
  (the default) is byte-for-byte the previous in-place behaviour. `--worktree`
  is an alias for `worktree`. `snapshot` copies the whole repo (`.git`,
  tracked, untracked, **ignored**) into a temp dir, excluding `.duet/` and any
  `--exclude` globs.
- `--carry PATH` copies named untracked paths into a worktree or snapshot.
- `--base REF` and `--branch NAME`. An explicit branch must not already exist;
  Duet never resets it.
- `--commit-mode {default,agent-driven}`. Under `agent-driven`, the Broker makes
  no commits, and agent subprocesses get `GIT_AUTHOR_*`/`GIT_COMMITTER_*` set to
  the agent's identity.
- Precedence: CLI flag > `DUET_ISOLATE`/`DUET_COMMIT_MODE` > `duet.toml
  [session]` > default. `connect`/`resume` pin isolation to `none`/`worktree`,
  so they can never be diverted onto a replica.
- 21 new tests (`tests/test_isolate.py`) that use real git. They pass as
  non-root.

## Why merge rather than leave it

The branch is the owner's most recent work (2026-07-10). It is additive and
tested, and it touches the same modules v2 must change (`cli.py`,
`workspace.py`, `broker.py`, `config.py`). Building v2 beside it would
guarantee a painful three-way reconciliation later, so it was merged
deliberately after reading the full diff. It was not an indiscriminate merge.

What the merge changed:

- The branch accidentally committed a `build/lib/duet/*` tree (stale copies of
  the package). It was **dropped** in the merge commit, and `build/` and
  `dist/` were added to `.gitignore`.
- There were no textual conflicts. Merged-tree tests as non-root: 178 passed,
  1 skipped.

## Risk notes carried into v2

These rules are recorded for D03 and D13:

1. **A snapshot is a replica, not a sandbox.** `copytree` brings `.env`,
   credentials files, ignored build output, `.git/config` (including the real
   `origin` URL and any credential helpers), and hooks. An agent inside the
   snapshot still holds the user's full OS permissions and can push through
   the inherited remote. Snapshot mode must never be described as isolation
   against the agent.
2. **Strict pair mode (v2) must not adopt snapshot's copy-everything default.**
   v2 snapshots are *input manifests*: tracked files plus explicitly approved
   untracked inputs. Secrets, ignored files and escaping symlinks are excluded
   unless approved (AT27).
3. **Classic mode keeps these semantics unchanged** (R15). `--isolate
   snapshot` stays opt-in, and its documentation will gain the risk statement
   above.
4. **`agent-driven` commit mode.** Author identity is set through the
   environment, which is convenient but not unforgeable. An agent can run
   `git -c user.name=… commit` or override the environment. It is provenance
   by cooperation, not proof. v2 evidence must be keyed to snapshot/tree
   hashes, not commit authorship.
5. **Branch-name collisions.** Explicit `--branch` refuses existing names.
   Automatic names are made unique with `_unique_branch`. This is reused as is.
