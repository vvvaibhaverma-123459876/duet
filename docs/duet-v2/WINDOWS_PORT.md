# Native Windows support: plan

Goal: DUET runs natively on Windows 10/11 (no WSL) with Python 3.11+, with the
same guarantees it gives on Linux and macOS, or with any weaker guarantee
stated plainly. Evidence comes only from real runs: a `windows-latest` CI
job, and then a person's real laptop. A green run with emulators is not a
real-CLI result.

## What is Unix-only today, and the replacement

| Area | Today (POSIX) | Windows replacement | Guarantee on Windows |
|---|---|---|---|
| Process existence | `os.kill(pid, 0)` | `OpenProcess` + exit code, via `psutil` | Same. **On Windows `os.kill(pid, 0)` terminates the process**, so every call site moves to one helper. |
| Process identity (start time, boot, parent) | `/proc`, `ps`, `sysctl` | `psutil` (`create_time`, `boot_time`, `ppid`) | Same: pid reuse is caught by the start time. |
| Stopping a process tree | process groups, `killpg(SIGTERM/SIGKILL)` | `CREATE_NEW_PROCESS_GROUP`; tree kill of the process and its descendants (`psutil`) | Same scope: DUET's own trees only. |
| Interrupting a turn | `SIGINT` to the group | `CTRL_BREAK_EVENT` to the group | Best effort, as on POSIX; the tree kill follows the grace period. |
| Single-instance and spawn locks | `fcntl.flock` | `msvcrt.locking` on the lock file | Same. |
| Service endpoint | Unix socket, 0600, in a 0700 dir; peer uid check | TCP on `127.0.0.1`, ephemeral port. Every request must carry a per-start **access key** read from the private state dir. | Weaker transport isolation (loopback is reachable by other local users), compensated by the access key. There is no peer-pid check, so the host-ancestry check is skipped, as on macOS. |
| Private state dir | `0700` and owner checks | `%LOCALAPPDATA%\duet`, which is per-user by default ACL | The ACL is inherited, not verified. Documented. |
| Detached service | `start_new_session=True` | `DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP \| CREATE_NO_WINDOW` | Same. |
| Launching `claude.cmd` / `codex.cmd` shims | direct exec | Arguments to a `.cmd`/`.bat` pass through `cmd.exe`: DUET never passes free text as an argument to a batch launcher (the prompt already goes on stdin; extra instructions move into the prompt). | Same content, and no command-line injection. |
| Check commands | `shlex.split` (POSIX rules) | Windows command-line rules (`shlex` in non-POSIX mode) | Same: still no shell. |
| Integrations (Stop hook, status line) | `/proc` ancestor walk; commands joined with spaces | `psutil` ancestors; commands quoted with `subprocess.list2cmdline` | Same. |

The new dependency is `psutil`, for Windows only (`sys_platform == "win32"`). It
is widely used, and doing without it would mean hand-written `ctypes` calls
that no Windows machine here can test.

## Order of work

1. **Platform layer** (`duet/platform.py`): one module owning every OS-specific
   call. Everything else calls it, and the POSIX behaviour stays byte-for-byte
   the same (the Linux and macOS suites must stay green).
2. **Service transport:** the endpoint abstraction (Unix socket or loopback TCP
   plus access key), the lock helper and detached spawn.
3. **Processes:** pid existence, identity, tree kill, interrupt, and the
   provider adapters (batch-launcher argument rule).
4. **Paths, checks, integrations:** state dir, command splitting, command
   quoting, ancestors.
5. **Tests:** fake-CLI shims become `.cmd` wrappers on Windows. POSIX-mechanics
   tests (umask, signals, process groups) are marked POSIX-only, each with a
   Windows counterpart that tests the Windows mechanism.
6. **CI:** add a `test-windows` job (Python 3.13, with MCP), non-blocking until it
   passes, then iterate on real failures.
7. **Docs:** `COMPATIBILITY.md` tier, `SECURITY_MODEL.md` (loopback plus access
   key), `OPERATIONS.md` (Windows paths), a decision entry, and the
   laptop test prompt.

## Not in scope

- Windows without Python 3.11+.
- Running DUET across a Windows/WSL boundary (a Windows `duet` with WSL-side
  CLIs).
