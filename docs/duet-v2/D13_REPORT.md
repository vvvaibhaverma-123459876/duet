# D13 report: hardening, security and compatibility

## Implemented
- `duet/runtime/hygiene.py`:
  - `redact()`: private keys, Anthropic/OpenAI/GitHub/AWS/DUET tokens, bearer headers, and `SECRET=value` assignments;
  - `for_display()`: strips ANSI/OSC and control characters, keeps newlines and tabs.
- Messages are redacted before they are stored (`api.send_message`).
- Check output is redacted before it is hashed and stored (`verification/runner.py`).
- Peer text is sanitised wherever it is displayed: inbox views, `duet status`, and the report markdown.
- The final report includes the resolved policy and its hash.
- The packaging test now asserts that every packaged migration ships in the wheel.
- CI gains a `test-macos` job (non-blocking) so platform claims rest on real runs.
- Docs: `SECURITY_MODEL.md`, `OPERATIONS.md`, `COMPATIBILITY.md`, D-034.

## Tests
`tests/test_threat_model.py` (20 cases):
- redaction formats;
- control sequences;
- stored-message redaction with a sanitised inbox;
- check-output redaction;
- bounded floods;
- peer text granting no authority;
- every legacy command still parsing and documenting itself.

## Status
- Implemented and tested (simulated) on Linux.
- macOS: the first two CI runs failed (identity length; socket placement under TMPDIR); both are fixed, and the suite passes on macOS in CI from `8f69b81`.
- Independent Codex review: pending.
- Nothing live-proven.

## Limitations
- Redaction is pattern-based.
- There is no sandbox; see `SECURITY_MODEL.md`.
- The Stop hook's session lookup is Linux-only (`/proc`).
