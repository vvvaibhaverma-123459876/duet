# D02 Report: typed runtime, transactional store, authorisation

New package `duet/runtime/`. Nothing in it imports a provider or the UI:

| Module | Responsibility |
|---|---|
| `contracts.py` | Enums for the four state machines and the capability fields; `Principal`, `Capabilities`, `Event`; structured `DomainError` codes; validators (length, NUL, finiteness, fail-closed enums); canonical JSON, content hashes, UTC time |
| `migrations/0001_initial.sql` | Schema v1, shipped as package data (verified in the wheel) |
| `store.py` | SQLite with foreign keys; `BEGIN IMMEDIATE` plus busy retry, giving `StoreBusy`; WAL only on local filesystems; network filesystems refused; future schema versions refused; `verify_replay()` |
| `reducer.py` | Pure transitions and the state machines; `MemoryState` for replay |
| `api.py` | Authorised commands: runs, participants (hashed tokens), approvals, messages (receipts, inbox cursor, ack, redelivery, reply closes request, request, depth and budget limits), tasks (proposal, acceptance, exclusive versioned claim with a fenced lease, owner-only transitions, non-author review, controller-only VERIFIED, user-only cancellation of required work, dependency gating, cycle rejection), actions (atomic action + reservations + outbox, single-claimer dispatch, fenced outcome recording, reservation release/reconcile), leases, recovery |
| `policy.py` | User-origin `AuthorisationPolicy` pinned by hash; `restrict()` only narrows and records ignored widening |
| `identity.py` | Participant tokens (random, shown once, stored as SHA-256); `ProcessIdentity` (host, boot id, pid, start time) |
| `paths.py` | Per-user `0700` state directory; refuses group- or world-writable directories |

## Exit criteria

| Criterion | Evidence |
|---|---|
| Repeated events do not duplicate actions, messages or accounting | `test_idempotent_send`, `test_plan_is_idempotent`, `test_idempotency_keys_are_per_principal`, `IdempotencyMismatch` on reuse |
| Concurrent schedulers cannot claim the same action; stale leases cannot commit | `test_concurrent_dispatchers_claim_each_action_once` (4 processes, 6 actions), `test_concurrent_claims_have_one_winner`, `test_stale_worker_cannot_record_after_reclaim`, `test_owner_transitions_require_a_valid_fence`, `TestLeases` |
| Unauthorised caller, workspace or policy changes are rejected with structured errors | `TestIdentity`, `TestAuthority` (including AT33 peer-claimed approval), policy widening tests; all errors carry `code` |
| Migration and replay are deterministic and failure-safe | `test_crash_mid_transaction_leaves_no_partial_state` (process killed before COMMIT), `test_plan_failure_persists_nothing`, `test_full_scenario_replays_exactly`, `test_replay_detects_tampering`, schema/reducer column agreement |

Tests: `tests/runtime/`, 98 including the packaging additions.

## Limits (by design at this milestone)

- No endpoint or service process yet. The API is in-process; the Unix-socket
  service and the MCP proxy arrive in D05.
- Reservations are stored and released atomically, but capacity accounting
  and admission come in D07/D08.
- Containment is cooperative. Any process running as the same OS user can
  open the database; the state directory only keeps it out of repositories
  and away from other users.

Independent review: **pending** (no Codex available).
