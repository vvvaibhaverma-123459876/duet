"""Task risk and uncertainty assessment (spec 8.1). Pure and deterministic.

assess(task, *, role, purpose, agent=None, policy=RoutingPolicy()) -> Assessment

Rules to implement:
- Risk-bearing areas raise risk regardless of line count (AT16):
  authentication/authorisation, secrets/credentials/tokens/crypto, payment
  and billing, database/schema migrations, concurrency (locks, threads,
  async, race), recovery/backup/restore, permission/sandbox/security
  policy, and deletion of data. Detect them from changed paths (path
  segments and file names, case-insensitive; e.g. auth/, login, oauth,
  session, password, secret, token, credential, crypt, key (as a whole
  segment or in names like api_key), payment, billing, invoice, migration(s)/,
  *.sql, alembic, lock, mutex, thread, async, concurrency, recovery,
  backup, restore, permission, sandbox, policy) AND from the task
  description (whole words). Each hit is a Reason(code="area:<name>",
  detail=<the matching path or word>, weight="raises").
  Two or more distinct areas, or any area plus a migration/deletion, is
  "critical"; one area is "high".
- Otherwise risk comes from size and blast radius: > 400 changed lines or
  > 15 changed files -> "medium"; a change touching protected paths ->
  "high" (Reason code "protected"); tiny or unknown size -> "low".
- No required checks (task.has_checks False) raises uncertainty one step and
  risk at least to "medium" (untestable).
- Failures: each prior failure of class "hypothesis" raises uncertainty one
  step (max "high"); two or more raise risk one step (max "critical").
  Environment failures do NOT raise risk or uncertainty (they are not about
  reasoning; see failures.py).
- Kinds: investigate/test_design start with uncertainty "medium"; a
  description with words like "unclear", "maybe", "investigate", "why",
  "flaky", "intermittent" raises uncertainty one step.
- The agent assessment can raise risk/uncertainty/floor (Reason
  "agent_raised") but never lower them; a lower agent proposal is recorded
  as Reason(code="agent_lower_ignored", weight="info").
- floor = max(policy.floor_for_risk[risk], agent profile if higher); for
  purpose "review" of a code change: at least policy.review_floor, and at
  least policy.risky_review_floor when risk is high or critical. For
  uncertainty "high" the floor is at least "deep".
- Reasons are ordered deterministically (sort by code, then detail)."""
from __future__ import annotations

from .contracts import AgentAssessment, Assessment, RoutingPolicy, TaskFacts


def assess(task: TaskFacts, *, role: str, purpose: str, agent: AgentAssessment | None = None, policy: RoutingPolicy = RoutingPolicy()) -> Assessment:
    raise NotImplementedError
