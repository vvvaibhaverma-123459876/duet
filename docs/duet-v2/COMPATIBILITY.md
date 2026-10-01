# Compatibility

## Platforms

| Tier | Platform | Evidence |
|---|---|---|
| Supported | Linux (Ubuntu), Python 3.11 and 3.13, as a normal user and as root, with and without the MCP SDK | CI on every push: `test (3.11)`, `test (3.13)`, `test-as-root`. Local runs of the whole suite as root and as non-root (umask 002). |
| Tested (CI) | macOS (GitHub `macos-latest`, arm64), Python 3.13, with the MCP SDK | CI job `test-macos`, which is still non-blocking. It passed on `8f69b81` with 894 passed and 6 skipped. The first two runs failed and led to two fixes, both kept:
- **Process identities over the id limit:** a 70-character hostname plus the raw `kern.boottime` text. The boot time is now stored as `boottime:<sec>` and long hostnames are hashed.
- **Short service socket placed under `$TMPDIR`:** MCP proxies and managed peers start without `TMPDIR`, so they looked in `/tmp`. It now always lives under `/tmp/duet-<uid>`, checked to be private and owned by the user.

Differences from Linux:
- Without `/proc`, liveness checks inside store transactions are conservative. A live pid counts as alive, and the exact check runs outside the transaction.
- The host-ancestry check is skipped, because there are no peer credentials.
- The Stop hook's session lookup uses `/proc`, so on macOS it finds no session and lets the stop proceed.

Real CLIs have not been run on macOS. |
| Tested (CI) | Native Windows (GitHub `windows-latest`, Windows Server 2025), Python 3.13, with the MCP SDK | CI job `test-windows`, which is still non-blocking. It passed on `4ef4359` with 877 passed and 33 skipped. The 33 skips are POSIX-mechanics tests (umask, signals, process groups, Unix sockets); each mechanism has a Windows counterpart test. The earlier runs failed and led to fixes that are kept, including two real bugs that also affected Linux and macOS:
- **A failed first turn deadlocked a pair:** the peer now retries once, then gives up with a reason.
- **The deliverable was committed after the run was marked complete:** it is now committed first.

Differences from Linux (see `WINDOWS_PORT.md` and `SECURITY_MODEL.md`):
- **Service transport:** loopback TCP on `127.0.0.1` plus a per-start access key, instead of a private Unix socket. Other local users can reach the port but not use it without the key. The host-ancestry check is skipped, as on macOS.
- **Private state:** `%LOCALAPPDATA%\duet` relies on its inherited per-user ACL, which DUET does not verify.
- **Batch launchers:** with `claude.cmd` / `codex.cmd`, free text never goes on the command line. Extra instructions go on stdin and the MCP config in a file; other unsafe arguments are refused.
- **Service lifetime:** an MCP client that starts the service inside a kill-on-close job object (the MCP Python SDK does this) takes the service down with it. The next call restarts it and the run's state is kept. Whether Claude Code and Codex do the same is step 7 of `WINDOWS_LAPTOP_TEST.md`.
- **Stopping a turn:** `CTRL_BREAK_EVENT` to the process group, then a tree kill.

Real CLIs have not been run on Windows. `WINDOWS_LAPTOP_TEST.md` is the procedure. |
| Untested | WSL | Expected to behave as Linux; no evidence yet. A Windows `duet` driving WSL-side CLIs (or the reverse) is not supported. |

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
