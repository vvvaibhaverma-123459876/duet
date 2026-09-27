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
| D07 Usage telemetry & ledger | done | `2b390ea`, `5814b11` | `duet/usage` (observations, ledger), status-line parser, `runtime/pools.py`, migration 0005, `duet usage` | 71 ledger + 7 pool + integration tests | internal Claude review of D06/D07 done: usage findings fixed in D08, task-graph findings fixed in `6a9f5bd`; Codex review pending | n/a |
| D08 Completion-aware admission | done | see HANDOFF | `duet/usage/{estimation,admission,reservations}.py`, `runtime/budgeting.py`, migration 0006, admitted managed turns, quota pause/resume, `duet resume --run` | 19 pure + 11 store + 7 service tests; suite 817/6 (core), 822/4 (mcp, root and non-root) | Codex review pending | NOT_RUN (no real quota exhaustion observed) |
| D09 Adaptive routing | done | see HANDOFF | `duet/routing`, `runtime/routing_control.py`, migration 0007, routed managed turns, `duet_request_profile`, `duet routing` | 26 policy + 6 service tests; suite 849/6 (core), 854/4 (mcp, root and non-root) | Codex review pending | NOT_RUN |
| D10 Final review & completion | done | see HANDOFF | baselines (migration 0008), criteria kinds, content authorship, per-file coverage, delta reviews, acknowledgements, check reuse, `duet report` | 8 new tests; suite 857/6 (core), 862/4 (mcp, root and non-root) | Codex review pending | NOT_RUN |
| D11 Parallel work & recovery | done | see HANDOFF | `code_isolated` tasks, task worktrees (migration 0009), fenced integration, conflict tasks, in-doubt integration settlement | 5 new tests; suite 862/6 (core), 867/4 (mcp, root and non-root) | Codex review pending | NOT_RUN |
| D12 Native enhancements & coverage | done | see HANDOFF | `duet integrations`, Stop hook, status-line wrapper, `duet capabilities` | 6 new tests; suite 868/6 (core), 873/4 (mcp, root and non-root) | Codex review pending | NOT_RUN (not installed into a real client) |
| D13 Hardening & compatibility | done | see HANDOFF | `runtime/hygiene.py` (redaction, display sanitising), resolved policy in reports, wheel migration check, macOS CI (non-blocking), SECURITY_MODEL/OPERATIONS/COMPATIBILITY | 20 threat-model/compat tests (7 functions, legacy command parametrised ×13) | Codex review pending | Linux only (CI) |
| D14 | not started | | | | | |

Independent review: Codex is not installed or authenticated in this
environment, so no Claude–Codex review has taken place. Every milestone
carries **review pending** until one is recorded. Two internal Claude review
agents audited D01–D05 (reproduced findings; see `D06_REPORT.md`). That is
not the cross-provider review the specification requires.
