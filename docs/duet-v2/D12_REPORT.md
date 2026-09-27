# D12 Report: native live enhancements and transparent control coverage

Your own Claude Code and Codex can now be set up for DUET with one planned,
approved and reversible command. A native Claude session gets a
checkpoint that asks it to answer its peer before it stops. What DUET can
and cannot control is stated per provider. Live push delivery into a
native session is explicitly unavailable, not faked.

## What exists now

| Piece | Module | What it does |
|---|---|---|
| Setup | `duet/integrations/installer.py`, `duet integrations plan \| install \| uninstall \| status` | Items: `claude-mcp` and `codex-mcp` (through `claude mcp add --scope user` and `codex mcp add`, so DUET never edits their config files itself), `claude-skill`, and opt-in `claude-statusline` and `claude-stop-hook` (precise edits to `settings.json`, with a one-time `.duet-backup` of the original). `plan` changes nothing. `install` and `uninstall` need `--yes`. A manifest records exactly what DUET owns and the original values. |
| Uninstall | same | Removes only owned items. The status line is restored only if it is still DUET's; a later user edit is kept and reported. An MCP server named `duet` that DUET did not install is never touched. |
| Stop hook | `duet/integrations/native.py`, `duet hook claude-stop` | Finds the native session the way the MCP proxy does (by walking up to the client process). If the peer is waiting on that session, it blocks the stop once with the reason. It never loops (`stop_hook_active`), and any error lets the stop proceed. |
| Status line | `duet statusline` | Runs the user's own command with untouched input and output, and records Claude's quota windows (changes only) as D08 gauges. |
| Coverage | `duet capabilities [--json] [--probe]` | Per provider: managed (push delivery, model and effort set per turn when exposed) and native (checkpoint delivery, model and effort advisory, connection-bound identity). Live delivery: unavailable. Claude Channels is a preview feature DUET does not enable without approval, and there is no supported live endpoint for an existing Codex session. |

## Exit criteria

| Criterion | Evidence | Status |
|---|---|---|
| Live delivery is proved on an eligible session or explicitly unavailable; no fake transport success | `duet capabilities` reports live delivery as unavailable with the reason. The MCP proxy labels native receive as `checkpoint` (D05). No channel adapter claims delivery. | TESTED_SIM (label); live channel: NOT_RUN (not enabled) |
| Native-origin bidirectional checkpoint mode remains functional without preview features | D05 native tests (`test_native_claude_and_native_codex_pair_over_mcp`), plus `test_stop_hook_asks_the_session_to_answer_its_peer` | TESTED_SIM |
| An unavailable live control does not silently spawn a replacement original agent | `test_an_unavailable_native_peer_is_never_replaced` | TESTED_SIM |
| No global settings, permission bypasses or preview flags are enabled without approval | `test_setup_is_planned_approved_and_reversible` (no changes without `--yes`; the user's own settings, hooks and MCP servers are preserved; no permissions, bypass or channel keys; exact restoration), `test_uninstall_leaves_what_the_user_changed_and_what_it_did_not_own`, `test_integrations_cli_needs_yes` | TESTED_SIM |

Acceptance tests: AT39 (install and uninstall keep existing MCP servers, hooks and status lines; only owned changes are removed) and AT06 (checkpoint mode stays useful; push is not claimed). AT18's "no global setting overwrite" is covered by D09 pins, and none of this writes model settings.

## Tests

- New: `tests/integrations/test_native_integrations.py` (6), using fake
  `claude` and `codex` CLIs and a temporary `CLAUDE_CONFIG_DIR`.
- Suites: core as root, 868 passed and 6 skipped; with `mcp==2.2.0` as
  root, 873 passed and 4 skipped; whole suite as non-root with MCP, 873
  passed and 4 skipped.

## Limits

- Not tried against the real CLIs here: `claude mcp add` exists in this
  environment, but it was not run against the user's real configuration.
  `codex` is not installed here. The installer relies on the clients'
  documented `mcp add/get/remove` subcommands.
- Session lookup for hooks walks up to six ancestor processes through
  `/proc` (Linux). On macOS the hook finds no session and lets the stop
  proceed.
- Claude Channels (live push into a native session) is not implemented. It
  is a preview feature that needs the user's explicit approval and a
  supported client. It is reported as unavailable.
- Native provider subagents are neither observed nor counted. Coverage says
  DUET controls only what it schedules.
