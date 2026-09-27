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
| D06–D14 | not started | | | | | |

Independent review: Codex is not installed or authenticated in this
environment, so no Claude–Codex review has taken place. Every milestone
carries **review pending** until one is recorded.
