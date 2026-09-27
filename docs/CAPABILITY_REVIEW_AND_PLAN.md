# Capability Review and Plan

Reviewed at `c2431d1` (2026-09-27). Scope: all of `duet/`, the tests, CI, docs,
and a wheel install. Items marked **reproduced** were confirmed by running them.
The others come from reading the code, with file:line references.

## 1. What Duet can do today

| Area | Capability | Where |
|---|---|---|
| Core loop | Sequential Claude ⇄ Codex turns, a git commit per turn authored by that agent, a rolling-summary prompt with size limits | `broker.py`, `prompting.py` |
| Stopping | `VerifierStop`, `ControlToken` (`[[DONE]]` is ignored while the gate fails), `MaxTurns`, `WallClockBudget`, `LoopDetector` (Jaccard) | `stopconditions.py` |
| Verification | `pytest`, `cmd:<shell>`, and repeated `--verify` flags that must all pass | `verifiers.py` |
| Workspaces | Scratch temp repo; live repo on an isolated `duet/session-*` branch; `--worktree`; `--rollback-on-failure`; `--allow-dirty` that saves prior work in a stash | `workspace.py` |
| Session reuse | `sessions`, `peek` (read-only), `status` (live vs. idle), `--attach`, `--chain-sessions`, `connect` (resume both agents, refuse LIVE ones) | `sessions.py`, `detect.py`, `cli.py` |
| Resilience | Quota detection with `halt`/`solo`/`wait` policies; `resume` from `.duet/resume.json`; `--wait-ready` polling | `adapters.py`, `resumestate.py` |
| Cost | Per-turn USD for Claude (`total_cost_usd`), `--budget-usd` cap, `duet ps` run list | `registry.py` |
| Lifecycle | `stop` (SIGINT/SIGTERM with confirmation), `talk` (one solo turn) | `control.py` |
| Hardening | Agent timeouts that kill the whole process tree, capped output capture, stale-lock reclaim, SIGTERM→cleanup, config validation, structured logging | throughout |
| Front-ends | Headless `run`/`exec` (text or JSON output), a REPL | `cli.py`, `repl.py` |
| Tests | 138 tests, including a hermetic CLI battery that uses fake agent binaries; CI on Python 3.11/3.13 | `tests/`, `.github/workflows/ci.yml` |

The core has zero dependencies and a clean engine/front-end split, and the
live-repo guardrails are careful. Most of the gaps below concern **what the
agents are told between turns** and **packaging and Codex parity**. The
architecture itself holds up.

## 2. Findings

### Bugs (reproduced)

- **B1. A normal install cannot run any command.** `duet.toml` is not shipped
  in the wheel, but `default_config_path()` (`config.py:37`) points at
  `site-packages/duet.toml`. In a clean venv, `pip install .` followed by
  `duet init --project` or `duet ps` exits with `Config error: cannot read
  config …/site-packages/duet.toml`. The README's `pipx install duet` quickstart
  is broken. Only editable installs work.
- **B2. The doctor's probe session leaks into the resume manifest.**
  `doctor._round_trip` (`doctor.py:94`) calls `send()` on the same `CLIAgent`
  object the session later uses, and `send()` records `last_session_id`
  (`adapters.py:138`). If an agent never completes a turn (for example, the
  first turn hits quota), `resume.json` stores the throwaway probe id.
  Reproduced with the fake agents: `{'claude': 'doc', 'codex': ''}`.
  `duet resume` would then `--resume` a session that belongs to a deleted temp
  directory, so this breaks the exact halt→resume path it exists for.
- **B3. The test suite fails when run as root.** Doctor's global hard check
  "not running as root" (`doctor.py:26`) aborts every battery run, even with
  fake agents: 21 failures as root, all 138 pass as a non-root user. Root is
  the default in many containers and devcontainers.

### Bugs (from reading the code)

- **B4. Headless roles are specific to the demo.** `cli.py:687` tells the
  Verifier to "Add edge-case tests to test_roman.py" for every `run`,
  `connect`, and `resume`, including live repos. The REPL copy
  (`repl.py` `_roles`) has the generic wording.
- **B5. `duet run` with no task silently runs the roman-numeral demo task**
  (`cli.py:313`), even against `--repo`.
- **B6. An interrupt saves neither the transcript nor the resume manifest.**
  `run_session` saves artifacts only when it exits normally, and `cli.py:396`
  swallows `KeyboardInterrupt`. Committed turns survive in git, but
  `duet resume` has nothing to load. This contradicts
  `PRODUCTION_HARDENING.md` A.4 ("artifacts are saved").
- **B7. `duet resume` from inside a scratch workspace fails.** The default
  `--repo .` goes through `create_workspace(".")` (`cli.py:520`), and
  `assert_safe_workspace` refuses the current directory.
- **B8. Control tokens are matched anywhere in the text**
  (`stopconditions.py:18`). Prose such as "I'll emit [[DONE]] once tests pass"
  ends the session. With no verifier configured, that counts as success.
  Codex's `text-last-line` fallback returns the *whole* stdout whenever it
  contains a token, so an echoed prompt (which quotes the protocol) is also a
  risk.
- **B9. REPL `/ask` doesn't catch `AgentError`** (`repl.py:114`): one failed
  call crashes the REPL.

### Gaps in capability

- **G1. Agents never see verifier results.** `build_prompt` takes no verifier
  input, and `VerifierStop.last_result` is thrown away. An agent learns about
  test failures only by re-running the tests itself, and a `[[DONE]]` rejected
  by a failing gate is dropped without any feedback to the agent. This is the
  biggest quality gap in the "execution-grounded" loop.
- **G2. The partner never sees the previous turn's diff.** `commit_after_turn`
  commits everything, so the "Uncommitted diff" section of `workspace_state`
  (`workspace.py:404`) is always `(none)` when the next prompt is built. On
  large live repos, the full `git ls-files` listing also crowds out useful
  context.
- **G3.** An agent doesn't see its own previous message in the prompt: the
  summary skips `messages[-2]` unless sessions are chained.
- **G4. Codex integration relies on scraping text.** Output is parsed by
  `text-last-line`, and no session id is captured. As a result, a cold-started
  Codex can't be chained, `resume` always cold-starts it (reproduced: `'codex':
  ''`), and it reports no token or cost usage.
- **G5. By default, "done" is not grounded in execution.** Without `--verify`
  (the default outside `--seed-demo`), `[[DONE]]` from the agents is enough for
  `success`. The tagline says the orchestrator decides.
- **G6. The wallclock budget is only checked between turns.** Agent timeouts
  (300s) and verifier timeouts (600s) aren't clamped to the remaining budget,
  so runs can overshoot it by minutes.
- **G7. The verifier runs after every turn.** A 10-minute suite runs N times.
  There is no option to verify only on DONE, or to run a fast gate per turn and
  the full gate on DONE.
- **G8. The REPL lags headless mode.** It has no `--verify`, live repo,
  worktree, quota policy, budget, or attach support. Its verifier is chosen by
  whether `test_roman.py` exists (`repl.py:80`). `/stop` does nothing, and
  `/budget` actually sets `max_turns`.
- **G9. The agent set is fixed at claude and codex.** CLI `choices`
  (`--start`, `talk`, `peek`, `stop`) and `_roles` assume those two, although
  the config accepts any `[agents.*]` and the broker already cycles through N
  agents.
- **G10.** Doctor makes a real model call per agent before every headless run
  and on every `--wait-ready` probe.
- **G11.** MCP peer consult appears in the architecture diagram but isn't
  implemented.

### Risks and hygiene

- **R1.** Codex session matching accepts *ancestor* working directories
  (`detect.py:114`). A Codex session started in `~` matches every repo under
  `~`, so `connect` can resume an unrelated conversation.
- **R2.** The run registry does a read-modify-write with no lock
  (`registry.py:79-100`). Parallel duets, which are an advertised feature, can
  drop entries.
- **R3.** CI has no lint or type-check step, no wheel-install smoke test, no
  job running as root, and no macOS job, although the docs reference macOS
  paths.
- **R4.** Check whether the `duet` name is available on PyPI before
  documenting `pipx install duet`.

## 3. Plan

Phases are ordered by leverage and dependency. Sizes: S ≈ under half a day,
M ≈ 1–2 days, L ≈ multi-day.

### Phase 0: Make it installable and testable (first PR)

| # | Change | Fixes | Size |
|---|---|---|---|
| 0.1 | Ship the default config as package data (`duet/defaults/duet.toml`, loaded with `importlib.resources`); `duet init` copies it from there. Keep the root `duet.toml` as the project example. | B1 | S |
| 0.2 | Scope the root check to agents that run in bypass mode: mark only that agent unavailable (an agent-scoped check) instead of failing the whole run, and honor the sandbox override Claude Code supports, after verifying it. The battery no longer depends on the uid. | B3 | S |
| 0.3 | Run doctor probes on a copy of each agent (`dataclasses.replace`) so probe session ids never reach the real agents; add a regression test from the B2 reproduction. | B2 | S |
| 0.4 | CI: a wheel build + clean-venv smoke test (`duet --version`, `duet init`, `duet doctor` against the fake agents), a job running as root in a container, and `ruff`. | R3 | S |

### Phase 1: Correctness

| # | Change | Fixes | Size |
|---|---|---|---|
| 1.1 | Move role text into one generic source (`prompting.py`), overridable in `duet.toml` under `[roles]`. Use demo-specific roles only with `--seed-demo`. | B4 | S |
| 1.2 | Require a task for `run`/`exec`/`connect` unless `--seed-demo` is given. | B5 | S |
| 1.3 | On interrupt, mark the outcome `interrupted`, save the transcript and resume manifest, then re-raise; the CLI saves the manifest in `finally`. | B6 | S |
| 1.4 | `resume` reopens an existing Duet workspace (checks that `.duet/resume.json` exists) instead of going through the new-workspace guard. | B7 | S |
| 1.5 | Honor a control token only on the final non-empty line and outside code fences; drop the whole-stdout fallback in `text-last-line`. | B8 | S |
| 1.6 | REPL `/ask`: catch `AgentError` and report it. | B9 | S |

### Phase 2: A smarter collaboration loop (the biggest quality lever)

| # | Change | Fixes | Size |
|---|---|---|---|
| 2.1 | Put a **Verification** section in every prompt: gate name, pass/fail/unknown, and a clipped tail of the output. Say explicitly when a `[[DONE]]` was rejected by a failing gate. | G1 | M |
| 2.2 | Replace the always-empty uncommitted diff with the **last turn's change** (`git show --stat` plus a clipped patch of the partner's commit) and a cumulative `--stat` since the base commit. Summarize `ls-files` on large repos. | G2 | M |
| 2.3 | Include the agent's own previous message in the prompt context. | G3 | S |
| 2.4 | Add `--verify auto` to detect pytest, `npm test`, `cargo test`, or `go test`. If no verifier is configured, warn at start and report `success (unverified)`. | G5 | M |
| 2.5 | Add `--verify-on every-turn\|done\|every:N`. Optionally pair a fast per-turn gate with the full gate on DONE. | G7 | M |
| 2.6 | Clamp agent and verifier timeouts to the remaining wallclock budget. | G6 | S |

Add a battery case for each item that asserts on the prompt the fake agent
receives. The fakes already log their args; extend them to log stdin.

### Phase 3: Codex parity

| # | Change | Fixes | Size |
|---|---|---|---|
| 3.1 | Add an `output_format = "jsonl"` event parser and switch Codex to `codex exec --json` (or `--output-last-message`). Capture the final message, the thread/session id (which enables chaining and `resume`), and token usage. Check event names against the installed Codex (see `DETECTED_ENV.md`) and keep `text-last-line` as a fallback. Add a JSONL mode to the fake Codex. | G4 | M |
| 3.2 | Record tokens per turn for every agent and add `--budget-tokens`, since Codex reports no USD. | G4 | S |

### Phase 4: Hygiene and reach

| # | Change | Fixes | Size |
|---|---|---|---|
| 4.1 | Codex session matching: rank exact or descendant working directories first, and use ancestors only with a flag. | R1 | S |
| 4.2 | Lock the registry file (`fcntl`) around read-modify-write. | R2 | S |
| 4.3 | Build CLI `choices` and roles from the configured agents so N agents work. | G9 | M |
| 4.4 | Route the REPL through the same session-options object as headless mode, and implement `/stop` or remove it. | G8 | M |
| 4.5 | Cache a passing doctor result for about 10 minutes, and add `--skip-doctor`. | G10 | S |
| 4.6 | MCP peer-consult server (`consult(agent, question)` and `run_session` as tools). Scope it in its own design doc. | G11 | L |

### Suggested sequencing

1. **Phase 0** as one PR. Each item already has a failing reproduction, and it
   unblocks installs and CI in containers.
2. **Phase 1** as one or two small PRs.
3. **Phase 2.1 + 2.2** together. This is where session outcomes should improve
   the most; measure it with the seeded demo plus one real-repo task before
   and after.
4. Phase 3, then Phase 2.4–2.6 and Phase 4 as capacity allows. Phase 4.6
   (MCP) is a separate project.

## 4. Open questions for the owner

- Should root be supported at all (container use), or only warned about?
- For runs with no verifier: warn, refuse, or auto-detect by default?
- Keep `duet` as the distribution name, given PyPI availability?
- Is MCP peer consult still planned, and before or after Codex parity?
