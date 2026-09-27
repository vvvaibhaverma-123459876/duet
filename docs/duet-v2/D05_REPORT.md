# D05 Report: MCP peer tools and native-session pairing

## What exists now

| Piece | Module | What it does |
|---|---|---|
| Pair coordinator | `duet/runtime/pairing.py` | The controller behind the tools: join (create run, invite, managed peer), send, inbox, bounded `wait`, propose_task, claim, submit, request_review, request_profile, status. Strict worktree per run, one writer, checks as durable actions, completion through the D03 gate, deliverable committed on the DUET branch, deadline pause, peer-loss detection. |
| Runtime service | `duet/runtime/service.py` | One process per state directory (exclusive lock). Unix socket 0600 in a 0700 directory; peer uid checked with SO_PEERCRED on Linux; one newline-delimited JSON request per connection (1 MiB); participant token or per-start service secret; unauthenticated callers may only `ping` and `join`. Starts on demand (spawn lock), reconciles on start, settles checks interrupted by a crash, pauses overdue runs, and exits when idle with no active run. |
| Managed peers | `duet/runtime/peers.py` | A driver thread per DUET-launched session: runs a provider turn when an actionable message arrives (or at kickoff for a writer), with the D04 adapters and an MCP config pointing at `duet mcp serve --token-file`. Each turn is a durable action. The turn budget is enforced. Auth, billing and quota failures stop the peer (no fallback). A labelled fallback answer is sent only when the peer sent nothing during the turn. |
| MCP proxy | `duet/integrations/mcp_server.py` | `duet mcp serve` over stdio with the official SDK (`mcp` 2.2.0, `MCPServer`). Ten tools, `duet_join` … `duet_status`. Keeps only the session token (never shown to the model; 0600 file keyed by the host process for reconnects) and the delivery watermark. A managed proxy cannot create or join runs, or start the service. |
| Instructions | `duet/resources/instructions/participant.md`, `.../codex-agents.md`, `duet/resources/skills/duet-pair/SKILL.md` | The participant loop (answer peer questions before waiting, never ask the user to relay, never start another pair), served as MCP `instructions`, used in managed prompts, and packaged as a Claude Code skill and a Codex AGENTS snippet. |
| CLI | `duet/cli_v2.py` | `duet pair`, `duet mcp serve`, `duet service run/status/stop`, `duet status --run ID [--json]`, `duet stop --run ID`. The legacy commands are unchanged. |
| Schema | `runtime/migrations/0003_pairing.sql` | Invites (hash only, single use, provider-bound, expiring). |

Related changes: `Runtime.claim_action` (claim one specific planned action);
the controller may post a closing STATUS to a terminal run;
`ProcessIdentity.of(pid)`; the Claude adapter passes the tool allowlist in the
read-only profile too, so a read-only reviewer can call DUET's own tools; the
Codex adapter translates `mcp_config` into thread `config.mcp_servers`
(observed on 0.157.1; fixture `thread-mcp-config-observation.json`); `Store`
creates its directory 0700.

## Exit criteria

| Criterion | Evidence | Status |
|---|---|---|
| A asks B; B asks A; A answers; B continues, with no user relay | Coordinator: `test_a_asks_b_b_asks_a_a_answers_b_continues` (A's wait wakes for B's question, not an answer). Real MCP: `test_native_claude_and_native_codex_pair_over_mcp` (two `duet mcp serve` processes; B's wait was already blocked when A asked). Managed over socket: `test_native_claude_and_managed_codex_talk_both_ways_and_ship_a_reviewed_patch` (Claude-originated) and `test_native_codex_initiates_and_answers_a_claude_follow_up` (Codex-originated). Full path through `duet pair`, real adapters, provider processes and MCP: `test_duet_pair_with_emulated_managed_sessions` (exact sequence Q, Q-back, A, A, REVIEW_REQUEST, REVIEW_RESULT). | TESTED_SIM |
| No duplicate initiator, nested runtime, recursive pair spawn or waiting deadlock | `test_no_duplicate_initiator_from_the_same_session`, `test_rejoin_is_idempotent`, `test_socket_is_private_and_single_instance` (second service refused), `test_managed_credentials_cannot_start_or_join_runs`, `test_managed_proxy_cannot_start_runs`, `test_cli_refuses_inside_a_managed_session`, `test_simultaneous_questions_do_not_deadlock`, `test_waits_are_served_concurrently`, `test_cancel_while_waiting`. | TESTED_SIM |
| Delivery and identity labels match observed behaviour | Native sessions are `receive=checkpoint` (messages arrive only through `duet_wait`/`duet_inbox`), `native_identity=connection_bound` (the host process is checked as an ancestor of the connecting process; a pid-reused or dead host is refused), `control_*=advisory`, `containment=cooperative`. Managed sessions are `receive=push` (DUET starts a turn), `native_identity=verified` (DUET observed the session or thread id), Codex `containment=unverified` (sandbox requested, not tested). Tests: `test_invite_forms_the_pair_with_honest_labels`, `test_managed_peer_is_launched_and_labelled`, `test_claimed_host_must_be_an_ancestor`, `test_peer_loss_wakes_the_waiter_and_holds_completion` (a dead host is `gone`; its token stops working, so a resumed conversation cannot pose as the original). | TESTED_SIM |
| A real pair test is required for the peer-alpha label | `tests/e2e_peer/test_real_pair.py` (gated by `DUET_REAL_PAIR=1`) and the manual AT01/AT02 procedure in `PEER_ALPHA_TEST.md`. | **NOT_RUN**: no Codex login, `api.openai.com` blocked here, and an account-consuming run needs an explicit local gate. **Peer-alpha is not earned.** |

One reviewed patch: every flow above ends with the writer's snapshot checked
by DUET on the exact files, approved by the other provider, the D03 predicate
satisfied item by item, a checkpoint exported, and the change committed on
the DUET-owned branch. The user's checkout is unchanged, and nothing is
pushed or merged. `test_changes_requested_then_repaired`,
`test_approval_of_an_older_snapshot_does_not_complete` and
`test_edits_after_submission_block_completion` cover the negative paths.

## Found while testing

- The first emulated end-to-end run hung. The reviewer's approval arrived
  inside its own provider-turn action, so the gate (correctly) refused
  completion with an action in flight, and nothing re-evaluated afterwards.
  Now the driver re-evaluates after every turn.
- The same run showed the fallback answer firing when the peer had
  deliberately asked a clarifying question instead of answering. The fallback
  now fires only when the peer sent nothing at all during the turn.
- Controller STATUS notes and peer review results no longer start a managed
  turn (each costs a model call). Actionable controller notices ("changes
  needed", "workspace changed after submission") are BLOCKER messages.
- The run deadline was recorded but not enforced, so an abandoned run kept
  the service alive. Overdue runs now pause (`PAUSED_BUDGET`) and their peers
  stop.
- `try_complete` stopped peers while holding the run lock, and a peer thread
  could be waiting for it (a 30 s stall). Peers are now stopped after the lock
  is released.
- As a non-root user (umask 002), `Store` created the state directory 0775
  before the private-directory check. It now creates it 0700.
- The adversarial read before commit found three more problems.
  - A managed launch that failed left a live run whose initiator never got
    its token, which blocked the session from retrying. The run is now
    cancelled, and a missing provider binary fails before the peer is
    registered.
  - An unexpected adapter exception left the turn action RUNNING, which
    blocked completion forever. It is now recorded as FAILED first.
  - A native join without a host claim was labelled `connection_bound`. It
    is now `unverified`.

## Tests and results

- New: `tests/integrations/test_pairing_coordinator.py` (23),
  `test_runtime_service.py` (21), `test_mcp_proxy.py` (3, needs `mcp`),
  `test_managed_pair_emulated.py` (1, needs `mcp`), 2 Codex adapter tests,
  wheel assertions for migrations and instructions, gated
  `tests/e2e_peer/test_real_pair.py`.
- Full suite as root without the MCP SDK: 537 passed, 6 skipped. With
  `mcp==2.2.0`: 541 passed, 4 skipped (only the gated real-provider tests).
  Integration, runtime, verification, workspace and provider tests as
  non-root: 243 passed, 2 skipped.
- CI installs `.[test,mcp]` in the matrix job. The root job keeps `.[test]`,
  which proves the core imports and runs without the SDK.

## Limits (honest)

- No real model took part. The "brain" in the emulated tests is a script;
  what is proven is DUET's protocol, delivery, authority and completion
  machinery, not how real models behave with these instructions.
- Native sessions receive messages only when they call the tools. DUET cannot
  wake an idle native session. The packaged instructions ask the session to
  keep calling `duet_wait` until the run ends. The limit is stated in every
  status and join response.
- Host identity verification uses `/proc` (Linux). On macOS the claim is
  recorded but not checked against the connecting process. The label stays
  `connection_bound`, never `verified`.
- Model and effort are fixed. `duet_request_profile` is always declined, with
  a reason; routing is D09.
- The acceptance contract for a native-initiated run is proposed by the
  initiating session (`acceptance_source=initiator_proposed`). The reviewer
  and the user see it; only the user can change it afterwards.
- A shell wrapper around `duet mcp serve` changes the host process, so a
  restarted wrapper cannot reconnect the session. Register the command
  directly.
