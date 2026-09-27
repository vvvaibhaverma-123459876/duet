# Release checklist

Release revision: the head of `claude/adoring-babbage-0ys7ui`, as recorded
in `HANDOFF.md`. Nothing is merged to `main` or released by this work.

## Label status

| Label (spec §14) | Gate | Status |
|---|---|---|
| Peer alpha | D00–D05 gates; real two-way native-origin collaboration in both directions; fixed safe controls; known limitations | **Not met.** The simulated gates pass. The real runs in both directions (AT01, AT02) are NOT_RUN. |
| Adaptive beta | scheduling, usage/reserves, routing and final review gates demonstrated | **Not met.** They are simulated only, and the independent review is pending. |
| Operational v1 | crash, cancel, isolation, migration, packaging and security gates, plus real evidence on the supported platform | **Not met.** Linux CI evidence exists. There is no chaos harness and no real-provider evidence. |
| Live-control supported | separate live tests per client and version | **Not claimed.** Live delivery is reported as unavailable. |

Until peer alpha is met, the honest description is: **implemented and
simulation-tested, not yet live-proven or independently reviewed.**

## Gates

- [x] Every acceptance test has automated evidence or explicit disclosure (`ACCEPTANCE_RESULTS.md`).
- [x] CI is green on Linux (3.11, 3.13, root) at the release revision. Check the PR's checks for the exact head.
- [ ] macOS: the `test-macos` CI job is non-blocking. Record its result in `COMPATIBILITY.md` before claiming support.
- [x] The wheel installs and runs outside the source tree, with every migration packaged (`test_packaging.py`).
- [x] No paid fallback, API-key path or permission bypass (R13 tests; `SECURITY_MODEL.md`).
- [x] Every legacy command keeps its interface. The changed semantics are documented (D-002, `COMPATIBILITY.md`).
- [ ] Real pair, Claude-initiated (AT01): `DUET_REAL_PAIR=1 pytest tests/e2e_peer/test_real_pair.py`, run by a person.
- [ ] Real pair, Codex-initiated (AT02): same, other direction.
- [ ] Real provider contract tests: `DUET_REAL_PROVIDERS=1 pytest tests/e2e_peer/test_real_providers.py`.
- [ ] Independent Codex review of D01–D14, with findings addressed.
- [ ] Comparative evaluation (`EVALUATION.md` protocol).

## Operator walkthrough (a bounded task, end to end)

A new operator can reproduce this with no real providers by using the
emulators in `tests/providers/emulators`. It is exercised by
`tests/integrations/test_managed_pair_emulated.py` and
`test_admission_service.py::test_managed_pair_pauses_for_quota_and_resumes_after_the_reset`.
With real CLIs:

```bash
duet capabilities                                   # what DUET controls here
duet usage pool set turns-claude --provider claude --metric turns --allowance 20
duet usage pool set turns-codex  --provider codex  --metric turns --allowance 20
duet pair "fix the failing date parser" --check "pytest -q" --no-wait   # prints the run id
duet status --run RUN                               # participants, tasks, checks, reviews
duet usage --run RUN                                # reserves, holds, consumption
duet stop --run RUN                                 # stops DUET's own peers only
duet resume --run RUN                               # continues; nothing in doubt is redone
duet report --run RUN                               # revisions, evidence, missing obligations
```

Evidence lives under the state directory (`OPERATIONS.md`). The
deliverable is the DUET branch named in the report, never your checkout.

## Migration

- v1 transcripts are read as-is. A v1 zero cost becomes "unknown".
- Legacy `budget_usd` is not converted into a v2 allowance. Define pools
  explicitly.
- The runtime store migrates itself (0001–0009) on first use.
- Client integrations are opt-in: `duet integrations plan`, then
  `install --yes`. `uninstall --yes` reverses them.

## Remaining limitations

See `SECURITY_MODEL.md` (no sandbox; pattern-based redaction),
`COMPATIBILITY.md` (Linux only proven), `CAPABILITY_MATRIX.md` (NOT_RUN
rows), and the final report's own limitations list.
