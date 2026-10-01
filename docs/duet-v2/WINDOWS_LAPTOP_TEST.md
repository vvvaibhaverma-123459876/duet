# Windows laptop test (real Claude Code and Codex)

CI proves DUET on Windows against emulated agents only. This procedure
proves it with the real CLIs on a real Windows 10/11 machine. Each step
records PASS, FAIL or NOT_RUN, with evidence. A skipped step is never a
pass.

The real steps (4 onwards) use your Claude and Codex plan quota. Run them
only when you mean to.

## Prerequisites

- Windows 10 or 11, PowerShell, Python 3.11+ (`py -3 --version`), Git for
  Windows (it provides Git Bash, which Claude Code uses).
- Claude Code installed and logged in (`claude --version`), and Codex
  installed and logged in (`codex --version`). Either the native installers or
  npm (`claude.cmd` / `codex.cmd`) works; record which.
- No `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` set. DUET removes them from the
  agents' environment anyway, but a real test should not have them.

## Steps

1. **Install.**
   ```powershell
   git clone https://github.com/vvvaibhaverma-123459876/duet; cd duet
   git checkout claude/adoring-babbage-0ys7ui   # until PR #3 is merged
   py -3 -m venv .venv; .venv\Scripts\Activate.ps1
   pip install -e ".[test,mcp]"
   ```
   Record `git rev-parse HEAD`, `py -3 --version`, `claude --version` and
   `codex --version`, and whether each CLI is an `.exe` or a `.cmd`
   (`where.exe claude codex`).

2. **Offline suite:** `python -m pytest -q`. Record passed, failed and
   skipped. Every failure is a finding.

3. **What DUET sees:** `duet capabilities --probe`. Save the output.

4. **Managed pair (real, uses quota).** In a small throwaway repo:
   ```powershell
   duet usage pool set claude-turns --provider claude --metric turns --allowance 6
   duet usage pool set codex-turns --provider codex --metric turns --allowance 6
   duet pair "add a function mul(a, b) to calc.py with a test" --check "python -m pytest -q"
   duet report --run <RUN_ID>
   ```
   PASS means `COMPLETED_VERIFIED`, a non-author review exists, and your
   checkout and branch were not touched.

5. **Native pairing, Claude first (AT01).**
   `duet integrations plan`, then `duet integrations install --yes`. Open
   Claude Code in the repo and ask it to start a DUET pair with Codex
   (`duet_join`). In a Codex session, join with the invite. Have Codex ask
   Claude a question and check that the *original* Claude session answers.

6. **Native pairing, Codex first (AT02).** The same, the other way round.

7. **Service lifetime.** During step 5, close the Claude session that
   started the pair, then call `duet_status` from the Codex session.
   - Record whether the service survived, or whether it restarted and the
     run was still there.
   - The MCP Python SDK kills the service with the client that started it.
     Whether Claude Code or Codex do the same is unknown until this step.

8. **Stop and resume.** Run `duet pair ... --no-wait`, then `duet stop --run
   <RUN_ID>`. Check in Task Manager that no `claude` or `codex` process
   started by DUET is left. Then `duet resume --run <RUN_ID>`.

9. **Clean up.** `duet integrations uninstall --yes`. Check that your Claude
   settings are as before (`%USERPROFILE%\.claude\settings.json`).

## Report

A table (step, command, result, evidence), then:
- each failure, with a reproduction;
- quota used, from `duet usage --run <RUN_ID>`;
- which acceptance rows this moves to LIVE_PROVEN on Windows
  (AT01, AT02, AT03), and what is still unproven.
