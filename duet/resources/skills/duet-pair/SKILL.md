---
name: duet-pair
description: Pair with a Codex peer through DUET on the current task. Starts or joins a DUET run, exchanges questions with Codex, and submits a patch that Codex reviews and DUET verifies. Use when the user asks to work with Codex, to get a Codex review, or to "use duet".
---

# Pair with Codex through DUET

This session stays the Claude participant. Do not run `duet pair` from here:
that command starts two new managed agents instead of using this session.

## Start a run

Call `duet_join` with:

- `provider`: `"claude"`
- `objective`: the user's task, in their words
- `repo`: the repository root
- `checks`: one or more commands that prove the task is done, usually the
  test command, for example `["python -m pytest -q"]`
- `peer`: `"managed"` to have DUET start a Codex peer, or `"invite"` to get
  an invite code the user gives to their own Codex session
- `writer`: `"self"` if this session edits the code, `"peer"` if Codex does

## Join someone else's run

`duet_join(provider="claude", run_id=..., invite=...)`

## Then

Follow the participant loop in the DUET server instructions. Answer your
peer's questions before resuming your own wait. Stop when the run reports
COMPLETED_VERIFIED, CANCELLED or FAILED, and tell the user the outcome and
the DUET branch that holds the verified change.
