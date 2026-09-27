# D04 Report: capability-aware Claude and Codex adapters

## What was observed (not assumed)

| Source | How | Recorded as |
|---|---|---|
| Claude Code 2.1.283 help | `claude --help` (local, no model call) | `tests/fixtures/provider_protocols/claude/2.1.283-help.txt` |
| Claude stream-json, cost semantics | Claude Code docs: programmatic use, cost tracking | module docstrings; emulator `tests/providers/emulators/fake_claude.py` |
| Codex CLI 0.157.1 | `npm install @openai/codex@0.157.1` into the session scratchpad only (not the repo, not global), for inspection | — |
| Codex app-server v2 protocol | `codex app-server generate-json-schema` (local, no login) | `…/codex/0.157.1/schema-subset.json`, `methods.json`, approval response schemas |
| Codex app-server live handshake | `initialize`, `account/read`, `account/rateLimits/read`, `model/list`, unknown method against the real binary with an empty CODEX_HOME: no login, no model call | `…/codex/0.157.1/app-server-handshake-unauthenticated.jsonl` (redacted) |
| Codex exec JSONL failure path | `codex exec --json` attempted; the environment's egress proxy refused api.openai.com (HTTP 403), so no model call happened | `…/codex/0.157.1/exec-json-network-denied.jsonl` |
| Codex exec event types | codex-rs `exec/src/exec_events.rs` (GitHub raw) | `CodexExecAdapter` docstring |

Facts that changed the design:

- Claude's `--bare` switches authentication to `ANTHROPIC_API_KEY` (paid API),
  so it is never used; managed peers run on the user's existing login (R13).
- A resumed Claude session reports **session-cumulative** `total_cost_usd`
  (≥ 2.1.277). The legacy adapter summed it per turn under `--attach` and
  `--chain-sessions`: a real over-count. Fixed in the legacy layer with
  `cost_json_scope = "session_cumulative_on_resume"`: the delta against the
  last figure seen for that session, or *unknown* for the first turn of an
  attached session.
- A Claude crash result may carry zeroed costs; those are treated as unknown.
- Claude reports typed retry categories (`authentication_failed`,
  `billing_error`, `rate_limit`, `overloaded`, `model_not_found`, …). These
  are used before any text matching.
- Codex app-server: messages carry no `jsonrpc` field. Unsolicited
  notifications and undeclared fields (`emittedAtMs`) arrive during the
  handshake. `ReasoningEffort` is a free string advertised per model by
  `model/list` (0.157.1 lists `ultra` on some models). Rate limits require an
  authenticated account (-32600 otherwise). Approval policies are `untrusted`,
  `on-request`, `never`.
- `codex exec resume` takes no `-C`/`--sandbox`; they must be global options.

## Modules

| Module | Responsibility |
|---|---|
| `providers/base.py` | `ProviderCapabilities`, `TurnRequest`, `SettingsRecord` (requested/accepted/observed), `UsageObservation` (metric, scope, source, quality; Decimal cost; nothing unknown becomes 0), `TurnResult` (status, lineage, denials, warnings), `UnsupportedSetting` |
| `providers/process.py` | `stream_process`: per-line dispatch in the caller's thread, bounded line and total size, cancellation event, SIGINT-first interrupt then TERM/KILL, orphan cleanup |
| `providers/claude_cli.py` | Capability discovery from help/version; stream-json parser; settings validation (effort must be advertised); read-only (`dontAsk`) and workspace-write (`acceptEdits`) profiles; `--permission-prompts none`; resume/fork/pre-assigned ids; strict MCP config; provider budget cap; typed error mapping; cumulative-scope cost labelling |
| `providers/jsonrpc.py` | Bounded NDJSON JSON-RPC client over a child's stdio; notifications and server requests handled while waiting |
| `providers/codex_appserver.py` | initialize/initialized, paginated `model/list`, account and rate limits, thread start/resume/fork, `turn/start` with per-turn model/effort validated against the catalogue, turn collection (agent message, token usage turn/thread, rate-limit updates, errors), `turn/interrupt` cancellation (server stopped if the interrupt is not acknowledged), approval/input requests declined |
| `providers/codex_exec.py` | Lower-capability fallback: effort advisory, no catalogue, no cost or quota windows, labelled as such |

## Exit criteria

| Criterion | Evidence |
|---|---|
| Chunked, malformed or partial output cannot hang or exhaust memory | `test_flood_is_bounded` (Claude stream over cap gives `OutputLimitError`), `test_stream_process_truncates_long_lines`, malformed-line tolerance (both providers), `test_server_death_is_a_structured_failure`, `test_no_result_is_a_failure` |
| Permission-denial, quota, auth, model-unavailable and timeout errors are distinguished | `test_typed_failures` (auth/billing/model/rate_limit), `test_permission_denials_are_reported_not_hidden`, `test_failed_turn_is_classified`, `test_turn_failed_classified`, timeouts on both providers |
| Unsupported settings are not reported as applied | `test_unsupported_effort_rejected_before_dispatch`, `test_effort_not_advertised_by_model_is_rejected`, unobserved effort absent from `settings.observed`, exec effort labelled advisory |
| An optional bounded real smoke test records native IDs and settings without paid fallback | `tests/e2e_peer/test_real_providers.py`: **NOT_RUN** here (Codex has no login and api.openai.com is blocked by the environment; an authenticated Claude call would consume the user's plan). Run with `DUET_REAL_PROVIDERS=1` (optionally `DUET_REAL_PROVIDERS_RECORD=dir`) |

Tests: `tests/providers/` (43) plus 3 legacy cumulative-cost tests. Full
suite: 491 passed, 1 skipped (real e2e) as root.

## Limits

- No real provider turn has been observed in this environment. Success-path
  event shapes come from the generated schema (Codex) and the docs (Claude),
  exercised through emulators. Evidence level: TESTED_SIM, not LIVE_PROVEN.
- Claude's effective effort is not reported by the CLI, so it stays
  unobserved.
- Claude's model availability cannot be discovered in advance (no catalogue);
  an unavailable model surfaces as `ModelUnavailableError` from the turn.
