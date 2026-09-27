# Capability and Traceability Matrix

This matrix maps each requirement (R) and acceptance test (AT) in the v2
specification to its milestone, the tests that cover it, and its evidence
status. It is updated at each milestone.

**Status legend** (highest level reached):
`NOT_STARTED` · `LEGACY_PARTIAL` (exists in v1 with gaps) · `IMPLEMENTED` (code
merged, not yet tested) · `TESTED_SIM` (automated tests against
fixtures/emulators) · `REVIEWED` (independent non-author review recorded) ·
`LIVE_PROVEN` (real authenticated provider run recorded) · `NOT_RUN` (a
required real test that has not been executed).

A skipped real-provider test is `NOT_RUN`. It is never evidence of
compatibility.

## Client capability evidence (observed, not assumed)

| Client | Version | How observed | Relevant capabilities | Not established |
|---|---|---|---|---|
| Claude Code | 2.1.283 | `claude --help` (fixture) | `-p`; `--output-format json\|stream-json`; `--input-format stream-json`; `--model`; `--effort low..max`; `--resume`; `--session-id`; `--fork-session`; `--mcp-config`; `--strict-mcp-config`; `--permission-mode`; `--allowed-tools`; `--max-budget-usd` (provider-enforced, `--print` only); `--no-session-persistence` | Authenticated output schema on this machine; whether effort/model are applied (observed settings); hook/status-line payloads |
| Codex CLI | — | not installed | — | Everything, including the app-server schema, exec JSONL events, resume semantics and rate-limit reads |
| MCP Python SDK | 2.2.0 (PyPI) | package metadata | stdio server/client | Compatibility beyond the tested version |

## Requirements

| ID | Requirement | Milestones | Status |
|---|---|---|---|
| R01 | Partnership with evidenced contributions | D05, D06, D10 | LEGACY_PARTIAL (v1 requires "spoke", not substantive) |
| R02 | Original-session continuity | D05, D12 | NOT_STARTED (v1 `--attach` resumes, which may fork) |
| R03 | Two-way initiative | D05 | NOT_STARTED |
| R04 | One objective/contract | D02, D06 | NOT_STARTED |
| R05 | Bounded autonomy | D02, D03 | LEGACY_PARTIAL (branch isolation only) |
| R06 | Honest resources | D01, D07 | NOT_STARTED |
| R07 | Completion reserve | D08 | NOT_STARTED |
| R08 | Supported controls only | D04, D09 | NOT_STARTED |
| R09 | Evidence-based completion | D01, D03, D10 | NOT_STARTED |
| R10 | Revision integrity | D03, D10 | NOT_STARTED |
| R11 | Safe writes | D03, D11 | LEGACY_PARTIAL (lock + branch/worktree) |
| R12 | Recoverability | D02, D11 | LEGACY_PARTIAL (resume manifest) |
| R13 | No silent paid fallback | D04, D08 | LEGACY_PARTIAL (no fallback exists) |
| R14 | No weakened standards | D01, D08 | NOT_STARTED (solo mode drops review) |
| R15 | Compatibility | D01, D13 | NOT_STARTED |

## Acceptance tests

| ID | Scenario | Milestone | Test(s) | Status |
|---|---|---|---|---|
| AT01 | Launch from existing Claude | D05 | | NOT_STARTED |
| AT02 | Launch from existing Codex | D05 | | NOT_STARTED |
| AT03 | DUET launches both managed | D05 | | NOT_STARTED |
| AT04 | Both ask while waiting | D05 | | NOT_STARTED |
| AT05 | Redelivery/reconnect | D02/D05 | | NOT_STARTED |
| AT06 | Live delivery unsupported | D12 | | NOT_STARTED |
| AT07 | Peer quota before mandatory review | D08 | | NOT_STARTED |
| AT08 | Unknown usage | D07 | | NOT_STARTED |
| AT09 | Two runs share pool | D07 | | NOT_STARTED |
| AT10 | Duplicate cumulative telemetry | D07 | | NOT_STARTED |
| AT11 | Context counter changes | D07 | | NOT_STARTED |
| AT12 | External usage consumes capacity | D08 | | NOT_STARTED |
| AT13 | Optional work vs finishing reserve | D08 | | NOT_STARTED |
| AT14 | Mandatory action draws reserve once | D08 | | NOT_STARTED |
| AT15 | Hard billing cap unavailable | D08 | | NOT_STARTED |
| AT16 | Small security-sensitive patch | D09 | | NOT_STARTED |
| AT17 | Unsupported effort / org clamp | D09 | | NOT_STARTED |
| AT18 | User pins model/effort | D09/D12 | | NOT_STARTED |
| AT19 | Missing dependency failure | D09 | | NOT_STARTED |
| AT20 | Repeated no-progress repairs | D06/D09 | | NOT_STARTED |
| AT21 | Old green suite, feature absent | D01/D10 | | NOT_STARTED |
| AT22 | DONE with missing verifier | D01 | | NOT_STARTED |
| AT23 | Approval of old revision | D10 | | NOT_STARTED |
| AT24 | Agent edits acceptance policy | D03/D10 | | NOT_STARTED |
| AT25 | Joint authorship reviews | D10 | | NOT_STARTED |
| AT26 | Dirty active checkout | D03 | | LEGACY_PARTIAL (clean-tree check, `--allow-dirty` stash) |
| AT27 | Secret-bearing ignored file / escaping symlink | D03/D13 | | NOT_STARTED |
| AT28 | Concurrent writers | D11 | | NOT_STARTED |
| AT29 | Crash around dispatch | D02/D11 | | NOT_STARTED |
| AT30 | Crash during commit/integration | D11 | | NOT_STARTED |
| AT31 | Cancel while waiting/working/verifying | D04/D11 | | NOT_STARTED |
| AT32 | PID reuse / unrelated sessions | D11 | | LEGACY_PARTIAL (`stop` confirms; name matching) |
| AT33 | Peer text claims user approval | D02/D13 | | NOT_STARTED |
| AT34 | Malformed/flooded provider output | D04 | | NOT_STARTED |
| AT35 | Unknown/failed/skipped required check | D03/D10 | | NOT_STARTED |
| AT36 | Inputs mutate during verification | D03 | | NOT_STARTED |
| AT37 | Legacy transcript/config migration | D13 | | NOT_STARTED |
| AT38 | Wheel without source tree | D13 | | NOT_STARTED (baseline: **fails**) |
| AT39 | Integration install/uninstall | D12/D13 | | NOT_STARTED |
| AT40 | Quota reset with stale telemetry | D08/D11 | | NOT_STARTED |
| AT41 | Mid-turn setting change unsupported | D04/D09 | | NOT_STARTED |
| AT42 | Resume/fork semantics differ | D04/D12 | | NOT_STARTED |
| AT43 | No sandbox for borrowed session | D13 | | NOT_STARTED |
| AT44 | Same snapshot checked twice | D10 | | NOT_STARTED |
| AT45 | Native subagents spawn | D11 | | NOT_STARTED |
| AT46 | Estimate overshoot before final usage | D08/D11 | | NOT_STARTED |
| AT47 | Integration changes tested tree | D10/D11 | | NOT_STARTED |
| AT48 | Release handoff | D14 | | NOT_STARTED |
