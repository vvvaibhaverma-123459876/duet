# v2 Progress

Status per milestone. "Implemented / Tested / Reviewed / Live-proven" are
tracked separately; see `CAPABILITY_MATRIX.md` for per-requirement evidence.

| Milestone | State | Commit | Implemented | Tested | Independent review | Live-proven |
|---|---|---|---|---|---|---|
| D00 Inventory & baseline | done | see HANDOFF | docs | baseline recorded | n/a | n/a |
| D01 Completion/accounting/process semantics | done | see HANDOFF | legacy layers | 291 passed/1 skipped (root), 289/3 (non-root) | pending (no Codex) | n/a |
| D02 Runtime store & authorisation | done | see HANDOFF | `duet/runtime` | 98 runtime/packaging tests | pending (no Codex) | n/a |
| D03 Workspace & evidence gate | done | see HANDOFF | `duet/workspaces`, `duet/verification` | 58 new tests; suite 445/1 skipped | pending (no Codex) | n/a |
| D04 Provider adapters | done | see HANDOFF | `duet/providers` (+ legacy cumulative-cost fix) | 46 new tests; suite 491/1 skipped | pending (no Codex) | NOT_RUN (no Codex login; api.openai.com blocked) |
| D05 MCP pair slice | done | see HANDOFF | `duet/runtime/{pairing,service,peers}.py`, `duet/integrations/mcp_server.py`, `duet/cli_v2.py`, instructions/skill | 48 new integration tests; suite 537/6 skipped (no SDK), 541/4 (mcp 2.2.0) | pending (no Codex) | NOT_RUN (peer-alpha **not** earned: no real pair run) |
| D06 Task graph & scheduler | done | `619ca53` | `runtime/{taskplan,scheduler,taskgraph}.py`, migration 0004, 5 tools, evidence-only contributions; plus review fixes | 82 sim + 22 integration + 8 verification tests; suite 651/6 (core), 656/4 (mcp) | internal Claude review of D01–D05 done (findings fixed or in progress); Codex review pending | n/a |
| D07 Usage telemetry & ledger | done | `2b390ea`, `5814b11` | `duet/usage` (observations, ledger), status-line parser, `runtime/pools.py`, migration 0005, `duet usage` | 71 ledger + 7 pool + integration tests | internal Claude review of D06/D07 done: usage findings fixed in D08, task-graph findings being fixed; Codex review pending | n/a |
| D08 Completion-aware admission | done | see HANDOFF | `duet/usage/{estimation,admission,reservations}.py`, `runtime/budgeting.py`, migration 0006, admitted managed turns, quota pause/resume, `duet resume --run` | 19 pure + 11 store + 7 service tests; suite 802/6 (core) | Codex review pending | NOT_RUN (no real quota exhaustion observed) |
| D09–D14 | not started | | | | | |

Independent review: Codex is not installed or authenticated in this
environment, so no Claude–Codex review has taken place. Every milestone
carries **review pending** until one is recorded. Two internal Claude review
agents audited D01–D05 (reproduced findings; see `D06_REPORT.md`). That is
not the cross-provider review the specification requires.
