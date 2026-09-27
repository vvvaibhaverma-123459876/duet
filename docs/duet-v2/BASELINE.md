# D00 Baseline

Recorded 2026-09-27 in a Claude Code cloud container. Everything below was
observed directly. Items not run are marked **NOT_RUN** with the reason.

## Source state

| Item | Value |
|---|---|
| Implementation branch | `claude/adoring-babbage-0ys7ui` (tracks `origin/claude/adoring-babbage-0ys7ui`) |
| Branch HEAD at start | `cd32d25` (docs-only review commit on top of main) |
| `origin/main` | `c2431d1b4f5b91eb8c7a8e41ea6ae995a3307d5e`; matches the specification's observation |
| `origin/feat/isolate-modes` | `3c8630f91102ba0dec1e9355ef040e8c92418b96`; matches the specification |
| After reconciliation | `9db29f3` (merge of `feat/isolate-modes`; see `BRANCH_RECONCILIATION.md`) |
| Remotes | `origin` → `https://github.com/vvvaibhaverma-123459876/duet` |
| Worktrees | one (`/home/user/duet`) |
| Dirty state at start | clean; no untracked files |
| Repo instructions | no `CLAUDE.md`, `AGENTS.md`, ADRs or handoff files exist; `docs/PRODUCTION_HARDENING.md`, `docs/DETECTED_ENV.md` and `docs/DESIGN_isolate.md` (from the merged branch) were read |
| Open PRs | #2 (this branch, draft). PR #1 (`harden/production-live-mode`) is closed, but its commit is an ancestor of main. |

## Environment

| Tool | Version / state |
|---|---|
| OS | Linux 6.18.44 x86_64 (container), running as **root** (uid 0); a non-root `ubuntu` user exists |
| Python | 3.11.15 |
| SQLite (stdlib) | 3.45.1 |
| Git | 2.43.0 |
| pytest | 9.1.1 |
| Claude Code | 2.1.283 at `/opt/node22/bin/claude`; help text saved to `tests/fixtures/provider_protocols/claude/2.1.283-help.txt` |
| Codex CLI | **not installed**. `@openai/codex` 0.157.1 is available on npm. |
| Node / npm | 22.22.2 / 10.9.7 |
| MCP Python SDK | not installed; `mcp` 2.2.0 is available on PyPI (requires pydantic, starlette, uvicorn, httpx2, …) |
| Active agent processes | one `claude` process (this session's own harness, pid 166). No `codex` or `duet` processes. Tests must never signal the harness process. |

## Installation

- `pip install -e ".[test]"` (the repository's documented development path)
  succeeds.
- **Wheel install is broken.** `pip wheel .` at `9db29f3` produces a wheel
  that contains only `duet/*.py`. `duet.toml` is not packaged, and
  `config.default_config_path()` resolves to `site-packages/duet.toml`. After
  installing into a clean venv, every command, including `duet init` and
  `duet ps`, exits 1 with `Config error: cannot read config
  …/site-packages/duet.toml`. The quickstart (`pipx install duet`) is affected.

## Test baseline

The tests were reviewed before running. The CLI battery drives the real CLI
against bash fake agents in temp dirs. `test_e2e.py` is gated by `DUET_E2E=1`.
`test_control.py` monkeypatches `os.kill`, so no real process is signalled.

Commands ran from `git archive` exports of each ref, so the working checkout
was untouched, with `python3 -m pytest -q -p no:cacheprovider`.

| Ref | User | Exit | Result | Duration |
|---|---|---|---|---|
| `origin/main` c2431d1 | root | 1 | 21 failed, 117 passed, 1 skipped | 9.2 s |
| `origin/main` c2431d1 | ubuntu (non-root) | 0 | 138 passed, 1 skipped | 14.4 s |
| `origin/feat/isolate-modes` 3c8630f | root | 1 | 42 failed, 136 passed, 1 skipped | 19.4 s |
| `origin/feat/isolate-modes` 3c8630f | ubuntu (non-root) | 0 | 178 passed, 1 skipped | 20.4 s |
| merged `9db29f3` | ubuntu (non-root) | 0 | 178 passed, 1 skipped | 20.8 s |

**Failure analysis.** Every root failure (21 in `test_cli_battery.py`, plus 21
in `test_isolate.py` on the isolate branch) has the same cause. `doctor`'s
global hard check `not running as root` aborts the run before any turn, even
though the agents are fakes. The one skip in every run is
`test_e2e.py::test_seeded_session_real_clis` (`DUET_E2E` unset).

### NOT_RUN

| What | Why | Local next step |
|---|---|---|
| Real Claude + Codex end-to-end (`DUET_E2E=1`) | Codex is not installed or authenticated here; real runs consume account usage and need an explicit gate | On a machine with both CLIs logged in: `DUET_E2E=1 pytest -m e2e` as non-root |
| Any authenticated Claude invocation | Uses the user's account; also, bypass mode refuses root | Run `duet doctor` as a non-root user with Claude logged in |
| Any Codex invocation or app-server handshake | Codex is absent | `npm i -g @openai/codex`, `codex login`, then `codex app-server generate-json-schema` |
| macOS and native Windows | Only Linux is available here | Run the suite on those platforms |

## Contradictions between source and docs, and test gaps

Source findings (review `docs/CAPABILITY_REVIEW_AND_PLAN.md`, plus the
specification's findings, re-checked against source):

1. **Completion.** `StopPolicy` honours `[[DONE]]` when verification is
   `unknown`, and runs without `--verify` report `success`. The tagline says
   the orchestrator decides.
2. **Pass before acceptance.** A verifier that already passes at baseline stops
   the session as `success` after both agents speak, whatever the task asked
   for.
3. **Solo mode drops review.** With `--on-quota solo`, the dropped agent is
   removed from `_all_agents_spoke`, and the survivor alone yields `success`.
4. **Budget ordering.** The budget check runs after `agent.send` and *before*
   `policy.check`. A turn that makes the tests pass is reported as
   `BudgetExceeded`, and the verifier never runs.
5. **Unknown cost becomes zero.** `AgentResult.cost_usd`,
   `Message.cost_usd` and the parse fallbacks all default to `0.0`. Codex turns
   are indistinguishable from free turns. `bool` values pass the numeric check.
6. **Unbounded capture.** `proc.communicate()` buffers all output in memory;
   `_cap` truncates only when the transcript is saved.
7. **Weak process cancellation.** `_kill_process_tree` returns early if the
   leader has exited, which leaves grandchildren in the group. A background
   grandchild that holds the pipes makes `communicate()` wait until timeout.
8. **Error classification.** Quota detection matches substrings such as `429`
   or `billing` anywhere in output. Auth, billing, model-unavailable and
   timeout failures are not distinguished.
9. **Doctor probe leak.** The probe shares the `CLIAgent` object with the
   session, so the probe session id can end up in `resume.json` (reproduced:
   `{'claude': 'doc'}`).
10. **Interrupts.** Ctrl-C/SIGTERM save neither the transcript nor the resume
    manifest, contradicting `PRODUCTION_HARDENING.md` A.4.
11. **Demo leakage.** Headless roles mention `test_roman.py` for every task,
    and `duet run` with no task runs the demo task.
12. **Prompt context.** Verifier output never reaches the agents, and the
    "uncommitted diff" section is always empty because every turn is
    committed.
13. **Codex text scraping.** `text-last-line` parsing yields no session id and
    no usage.
14. **Registry race.** The run registry does read-modify-write without a lock.

Test gaps: nothing checks outcome honesty under unknown verification, budget
versus verification ordering, solo-mode review obligations, cost
unknown-ness, bounded capture, wheel packaging, or running as root.
