# Security model

DUET coordinates two AI coding agents on the user's machine, under the
user's account. Its containment is **cooperative**. It keeps honest
agents and their inputs from overstepping by accident or by manipulation.
It is **not** isolation against malicious code running as the same OS
user, which can read DUET's store, tokens and worktrees directly.

## What DUET protects

| Asset | Protection | Where |
|---|---|---|
| The user's checkout | All agent writes go to DUET-owned worktrees on DUET branches; the checkout, its branch and its index are never touched. Nothing is pushed or merged. | D03 strict worktrees; D11 isolated task worktrees |
| Spending authority | Only the user principal defines usage pools and pins; agents and the controller cannot widen them. There is no paid fallback, no account rotation and no API-key path. Provider credentials are stripped from managed peers' environment. | D07, D08, D04 |
| Completion truth | Only the controller can mark a run verified, from recorded evidence: checks on an exact snapshot copy, per-file non-author reviews and a demonstrated criterion. | D03, D10 |
| Authority | Principals (user, controller, participant) are bound to hashed tokens. Participants cannot call controller operations. Peer text, repository files, tool output and logs are data: a message claiming the user approved something grants nothing (AT33). | D02, D05 |
| The service endpoint | Unix socket 0600 in a 0700 directory, a single instance (flock), peer-credential uid check, host-ancestry check, requests bounded to 1 MiB. Unauthenticated calls are limited to ping and join. **Windows:** loopback TCP on an ephemeral port. Every request must carry a per-start access key from `%LOCALAPPDATA%\duet` (per-user ACL). There are no peer credentials, so the host-ancestry check is skipped, as on macOS. | D05, D-036 |
| Secrets | Sensitive files (`.env*`, keys, kubeconfig, credential stores, and more) are excluded from snapshots, and escaping symlinks are refused. Recognisable secrets in messages and check output are redacted before persistence (`runtime/hygiene.py`). | D03, D13 |
| The user's terminal | Terminal control sequences are stripped from peer text before it is shown or handed to a model. | D13 |
| Resources | Output capture, message sizes, discussion volume, outstanding requests, tasks, delegation depth, invocations and elapsed time are all bounded. | D01, D02, D06 |
| User configuration | Integration setup is planned, needs `--yes`, records what DUET owns, and uninstall removes only that. There are no permission bypasses and no preview flags. | D12 |

## Threat-model tests

`tests/test_threat_model.py` covers:
- redaction of known credential formats, in messages and in check output;
- control-sequence stripping;
- bounded floods (outstanding requests and the discussion budget);
- peer text claiming authority;
- legacy command stability.

It is complemented by the D02 authorisation tests, the D03 snapshot and
symlink tests, the D05 endpoint tests, and the D12 installer tests.

## Not protected (stated, not hidden)

- **Same-user malicious code.** Any process running as the user can read
  `~/.local/state/duet` (tokens, database, artifacts), write the worktrees,
  or call the service with a token it read. There is no sandbox.
  Verification checks and agents run with the user's permissions (an
  allowlisted environment, but no sandbox).
- **Redaction is pattern-based.** It catches common credential formats,
  not every secret. Keeping secrets out of the workspace (D03) is the
  primary control.
- **Code under review.** Checks execute repository code. A malicious
  repository can do anything the user can when its checks run.
- **Native sessions.** Their own tools, subagents and edits outside DUET's
  tools are neither controlled nor observed. Coverage reports this.
- **Providers.** What Anthropic and OpenAI receive is decided by their
  CLIs. DUET shares the task and code context with both providers; the user
  consents to this by starting a pair.
