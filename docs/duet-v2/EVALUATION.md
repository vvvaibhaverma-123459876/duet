# Evaluation

**No comparative evaluation has been run.** This document therefore
reports no model-performance, usage-saving or completion-rate figures.
Those need real, account-consuming runs of both CLIs. This environment has
neither the authenticated CLIs nor the authorisation to spend the user's
quota. The plan forbids substituting simulated results.

What exists:
1. the protocol below, fixed in advance so that results cannot be chosen after the fact;
2. deterministic evidence from simulations, which shows the runtime *behaves* as specified, not that it is *better*.

## Measured so far (deterministic, simulated)

| Property | Evidence | What it shows | What it does not show |
|---|---|---|---|
| Completion is refused without evidence | `tests/integrations/test_final_review.py`, gate tests | a suite already green before the change, a stale approval, an unreviewed file or a changed tree never yields `COMPLETED_VERIFIED` | how often real pairs reach verified completion |
| Scheduling | `tests/runtime` scheduler simulation suite (D06) | no starvation or deadlock across scripted interleavings; bounded discussion | real coordination overhead |
| Admission and reserves | `tests/usage`, `tests/integrations/test_admission_service.py` | review and repair turns stay protected under pressure; quota pauses resume after the reset with at most one probe | real usage or savings |
| Routing | `tests/routing` | risk sets the floor; pressure never goes below it; escalation is bounded | whether routing improves outcomes |
| Recovery | runtime service and parallel-work tests | in-doubt turns and integrations are settled from evidence and never re-dispatched | behaviour under real crashes (no chaos harness) |

## Protocol for the real evaluation (not yet run)

**Tasks.** Original small repositories, one task each, covering:
- a mechanical change;
- a bounded feature;
- an unfamiliar bug;
- retry/idempotency;
- a migration;
- concurrency;
- session recovery (kill the service mid-turn);
- ambiguous requirements.

No proprietary data and no destructive live actions.

**Arms.** Each arm gets the same snapshot, checks, acceptance contract,
permissions and resource ceiling (a turn pool per provider):
1. Claude only (`duet exec` / solo);
2. Codex only;
3. the legacy alternating strategy (`duet run`);
4. a fixed-profile true pair (`duet pair` with `duet routing pin` on both sides);
5. adaptive DUET (`duet pair`, default routing).

Order is counterbalanced per task. Each (task, arm) runs at least three
times. No conclusion is drawn from one run.

**Report, in this order:**
1. verified completion and remaining blockers, per arm, with denominators;
2. regression rate, meaning the baseline-preserved checks that fail;
3. reviewer findings that were confirmed by evidence;
4. repair success after `changes_requested`;
5. resources, from `duet report --json` → `resources`: turns and estimated cost with their quality labels. Unknowns are reported as unknown;
6. coordination overhead (messages and turns that are not work);
7. elapsed time;
8. pause and recovery behaviour.

Distributions, not just means. Savings without equal completion quality
are not a win.

**Records.** Each run's `duet report --json` output and the commit of DUET
used. The gated tests write records when `DUET_REAL_PAIR_RECORD` or
`DUET_REAL_PROVIDERS_RECORD` names a file.

**How to run** (on a machine with both CLIs authenticated, with
permission to use their quota): see `PEER_ALPHA_TEST.md` for the gated pair
tests. The comparison tasks are not scripted yet. Scripting them is the
first evaluation task.

## Default strategy

Adaptive pairing is **not** promoted to the default execution strategy.
`duet run` keeps its legacy behaviour and `duet pair` is opt-in. Promotion
waits for the protocol above to show verified completion at least equal to
the baselines.
