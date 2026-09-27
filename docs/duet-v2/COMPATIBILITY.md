# Compatibility

## Platforms

| Tier | Platform | Evidence |
|---|---|---|
| Supported | Linux (Ubuntu), Python 3.11 and 3.13, as a normal user and as root, with and without the MCP SDK | CI on every push: `test (3.11)`, `test (3.13)`, `test-as-root`. Local runs of the whole suite as root and as non-root (umask 002). |
| Being measured | macOS, Python 3.13 | CI job `test-macos` (non-blocking). Its first run (on `b0f80cb`) **failed**: 22 failures and 54 errors, 815 passed. Almost all came from process identities exceeding the 128-character id limit (a 70-character runner hostname plus the raw `kern.boottime` text). One test assumed exact liveness without `/proc`. Both are fixed: the boot time is stored as `boottime:<sec>`, long hostnames are hashed, and the `/proc`-only test is skipped elsewhere. The second run (`e9ace30`) had 890 passed and 3 failed. MCP proxies and managed peers are started with a minimal environment (no `TMPDIR`), so they looked for the service's short socket under `/tmp` while the service listened under `$TMPDIR` (`/var/folders/...`). The short socket now always lives under `/tmp/duet-<uid>` (checked private and owned). macOS is not claimed as supported until a run passes. Known gap: the Stop hook's session lookup uses `/proc`, so on macOS it finds no session and lets the stop proceed. |
| Untested | WSL | Expected to behave as Linux; no evidence yet. |
| Unsupported | Native Windows | Unix sockets, process groups, `flock` and `/proc` are assumed throughout. |

No platform is claimed on the strength of mocked platform tests.

## Commands

The legacy commands keep their documented behaviour. D01 changed their
outcome and exit-code semantics deliberately (see `DECISIONS.md` D-002).
Every legacy command still parses and documents itself
(`tests/test_threat_model.py::test_legacy_commands_keep_their_interface`).

| Legacy (unchanged interface) | v2 additions (explicit, opt-in) |
|---|---|
| `doctor`, `run`, `exec`, `sessions`, `ps`, `status`, `connect`, `resume`, `stop`, `talk`, `peek`, `replay`, `init` | `pair`, `mcp serve`, `service`, `usage`, `routing`, `report`, `integrations`, `hook`, `statusline`, `capabilities` |
| | `status --run`, `stop --run` and `resume --run` extend the legacy commands only when `--run` is given |

The spec's proposed `duet run --engine peer`, `duet pause`, `duet explain`
and `duet export` are not implemented. Their roles are covered by
`duet pair`, the automatic pauses, `duet routing`/`duet report` and
`duet report --json`. They are not documented as available.

## Data

- **v1 transcripts:** migrated by the legacy reader (D01).
- **Legacy `budget_usd`:** still read by legacy commands, with a note that
  the figure is estimated. It never becomes a v2 allowance: v2 spending is
  bounded only by pools the user defines.
- **Runtime store:** migrations are packaged and contiguous (0001–0009),
  applied in order, and verified in the wheel.
- **Acceptance contracts:** new optional fields are omitted when at their
  default, so contract hashes from earlier versions stay valid.

## Installs

Editable, source and wheel installs are tested. The wheel test builds a
wheel and runs it outside the source tree, checking that every migration,
instruction file and skill is packaged. There are no source-relative paths
at runtime (`importlib.resources`).
