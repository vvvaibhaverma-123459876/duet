## DUET pairing (Codex)

When the user asks you to work with Claude through DUET, use the `duet` MCP
tools. This session stays the Codex participant. Never run `duet pair`
yourself: it would start two new managed agents instead of using this
session.

- Start: `duet_join(provider="codex", objective=..., repo=..., checks=[...], peer="managed"|"invite", writer="self"|"peer")`
- Join an invitation: `duet_join(provider="codex", run_id=..., invite=...)`
- Then loop: call `duet_wait`, answer the peer's questions and review
  requests first, do your part, submit or review, and stop when the run is
  COMPLETED_VERIFIED, CANCELLED or FAILED.
