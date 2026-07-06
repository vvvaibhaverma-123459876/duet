# Production Hardening Plan

Goal: make Duet production-grade (robust error handling, no leaked resources,
graceful failure) and give it a **safe** way to operate on an existing ("live")
git repository instead of only a throwaway temp workspace.

## Guiding principle

For a tool that drives two nondeterministic AI CLIs with
`--dangerously-skip-permissions` / `--ask-for-approval never`, "works on live
systems" means *adding* safety machinery, not removing the existing guards.
Operating on a real repo is **opt-in and guardrailed by default**: dedicated
branch, recorded rollback point, clean-tree requirement, and the user reviews
and merges — Duet never lands changes on the user's main branch itself.

## A. Error-handling hardening

1. **Verifier timeout + missing-binary handling** — `pytest -q` runs with a
   timeout and a clear result if `pytest` is absent; a hanging test can no
   longer wedge the whole session.
2. **Lock robustness** — acquire the lock only after the workspace is fully
   prepared; release on any preparation failure; treat a lock whose recorded
   PID is no longer alive as stale and reclaim it (crashed sessions self-heal).
3. **Git failures are handled, not fatal** — `workspace._run` wraps
   `CalledProcessError` in `WorkspaceError` with context; `commit_after_turn`
   failures halt the session cleanly instead of crashing `run_session`.
4. **Signal handling** — SIGINT/SIGTERM trigger a graceful shutdown: current
   turn is abandoned, artifacts are saved, lock and branch state are released.
5. **Bounded output capture** — per-message raw stdout/stderr is capped
   (head+tail with a truncation marker) so a chatty agent or huge repo cannot
   exhaust memory or bloat transcripts.
6. **Structured logging** — a real logger (level via `DUET_LOG`/`--log-level`)
   records turns, decisions, and errors to stderr and an optional file.
7. **Config validation** — malformed agent config yields a clear
   `ConfigError`, not a raw `KeyError`/`TypeError`.

## B. Live-repo capability (opt-in, safe-by-default)

- `duet run --repo PATH ...` operates on an existing git repo.
- Preconditions: path is a git work tree, not a system path, and (unless
  `--allow-dirty`) the tree is clean so unrelated uncommitted work is never
  swept into Duet's commits.
- Duet checks out a fresh `duet/session-<stamp>` branch and records the base
  commit. All agent commits land on that branch only.
- On failure/interrupt, `--rollback-on-failure` resets the branch to the base
  commit; otherwise the branch is left intact for inspection.
- Duet never merges to the original branch — the user reviews the diff/branch
  and merges deliberately.

## User-owned decision (default chosen)

The bundled agent commands keep `--dangerously-skip-permissions` (Claude) and
`--ask-for-approval never` (Codex). On a live repo this is bounded by the
dedicated-branch + rollback design. If you want an approval gate instead, adjust
the agent `command` in `duet.toml`; the branch isolation remains regardless.
