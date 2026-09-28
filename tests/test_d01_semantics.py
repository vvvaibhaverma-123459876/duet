"""D01 regression tests: completion, accounting, error and process semantics.

Each test names the specification item it pins down. They use in-process
fakes; the CLI battery covers the same behaviours end to end."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from exeshim import make_exe

from duet.adapters import (
    AgentError,
    AgentResult,
    AgentTimeoutError,
    AuthError,
    BillingError,
    CLIAgent,
    ModelUnavailableError,
    OutputLimitError,
    QuotaError,
    classify_failure,
    parse_cost,
)
from duet.broker import exit_code_for, run_session
from duet.config import ConfigError, default_config_text, load_config, parse_config, write_config
from duet.doctor import Check, _round_trip, available_agent_names, hard_failures, run_doctor, uses_permission_bypass
from duet.prompting import default_roles, demo_roles
from duet.stopconditions import parse_control_token, strip_control_tokens
from duet.transcript import Message, Transcript
from duet.verifiers import AlwaysUnknown, PytestVerifier, VerificationResult

REPO_ROOT = Path(__file__).resolve().parents[1]


def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


@dataclass
class Scripted:
    """Agent whose replies are scripted; `edit` writes a file each turn."""

    name: str
    replies: list[object]
    edit: bool = False
    cost: float | None = None
    edit_turns: set[int] | None = None  # 1-based calls that edit; overrides `edit`
    prompts: list[str] = field(default_factory=list)
    calls: int = 0

    @property
    def display_name(self) -> str:
        return self.name.title()

    def send(self, prompt: str, workspace: Path) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else "idle [[HANDOFF]]"
        if isinstance(reply, BaseException):
            raise reply
        if (self.calls in self.edit_turns) if self.edit_turns is not None else self.edit:
            with open(workspace / f"{self.name}.txt", "a", encoding="utf-8") as handle:
                handle.write(f"{self.calls}\n")
        return AgentResult(text=str(reply), exit_code=0, duration_s=0.0, raw_stdout="", raw_stderr="", cost_usd=self.cost)


class Seq:
    """Verifier returning a scripted status sequence (last one repeats)."""

    name = "seq"

    def __init__(self, statuses: list[str]):
        self.statuses = list(statuses)
        self.calls = 0

    def verify(self, workspace: Path) -> VerificationResult:
        status = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        return VerificationResult(status, status == "passed", f"{status} #{self.calls}")


def session(ws, agents, verifier=None, **kw):
    params = dict(
        task="t",
        workspace=ws,
        agents=agents,
        start_with=next(iter(agents)),
        max_turns=kw.pop("max_turns", 6),
        wallclock_seconds=kw.pop("wallclock_seconds", 60),
        loop_threshold=kw.pop("loop_threshold", 2.0),  # >1 disables the loop detector
        verifier=verifier or AlwaysUnknown(),
    )
    params.update(kw)
    return run_session(**params)


# --- Completion semantics (AT21/AT22) ----------------------------------------


class TestCompletion:
    def test_done_with_missing_verifier_is_unverified(self, tmp_path):  # AT22
        ws = repo(tmp_path)
        result = session(ws, {"a": Scripted("a", ["work [[HANDOFF]]"]), "b": Scripted("b", ["ok [[DONE]]"])})
        assert result.outcome == "unverified"
        assert result.exit_code == 3

    def test_done_with_unknown_verifier_status_is_unverified(self, tmp_path):
        ws = repo(tmp_path)
        agents = {"a": Scripted("a", ["x [[DONE]]"])}
        result = session(ws, agents, Seq(["unknown"]), require_all_agents_for_success=False)
        assert result.outcome == "unverified"

    def test_suite_green_at_baseline_does_not_end_the_session(self, tmp_path):  # AT21 (legacy)
        # Checks already passing before any turn prove nothing about the task:
        # without a completion claim the session keeps working.
        ws = repo(tmp_path)
        agents = {"a": Scripted("a", ["step one [[HANDOFF]]", "step two [[HANDOFF]]", "step three [[HANDOFF]]"])}
        result = session(ws, agents, Seq(["passed"]), max_turns=3, require_all_agents_for_success=False)
        assert result.outcome == "halted"
        assert result.stop_condition == "MaxTurns(3)"
        assert len(result.transcript.messages) == 3

    def test_green_baseline_plus_done_is_success(self, tmp_path):
        ws = repo(tmp_path)
        agents = {"a": Scripted("a", ["impl [[HANDOFF]]"]), "b": Scripted("b", ["reviewed [[DONE]]"])}
        result = session(ws, agents, Seq(["passed"]))
        assert result.outcome == "success"
        assert result.stop_condition == "VerifierStop(seq)"

    def test_red_to_green_establishes_completion_without_done(self, tmp_path):
        ws = repo(tmp_path)
        agents = {"a": Scripted("a", ["fixing [[HANDOFF]]"]), "b": Scripted("b", ["reviewing [[HANDOFF]]"])}
        verifier = Seq(["failed", "passed"])  # baseline red, green after turn 1
        result = session(ws, agents, verifier)
        assert result.outcome == "success"
        assert [m.agent for m in result.transcript.messages] == ["a", "b"]  # deferred until both spoke
        assert result.transcript.baseline_verification["status"] == "failed"
        assert result.transcript.final_verification["status"] == "passed"

    def test_done_rejected_while_checks_fail_and_the_next_prompt_says_so(self, tmp_path):
        ws = repo(tmp_path)
        b = Scripted("b", ["looks done [[DONE]]", "fixed [[DONE]]"])
        agents = {"a": Scripted("a", ["impl [[HANDOFF]]", "patch [[HANDOFF]]"]), "b": b}
        verifier = Seq(["failed", "failed", "failed", "failed", "passed"])
        result = session(ws, agents, verifier)
        assert result.outcome == "success"
        assert len(result.transcript.messages) == 4
        # Turn 3's prompt (agent a) carries the rejection and the failing output.
        rejected = [p for p in agents["a"].prompts if "was NOT accepted" in p]
        assert rejected, "the rejected [[DONE]] must be reported to the agents"
        assert "failed #" in rejected[0]


# --- Quota solo mode keeps the partner's obligation (R14) --------------------


class TestSoloReviewObligation:
    def test_survivor_changes_leave_partner_review_pending(self, tmp_path):
        ws = repo(tmp_path)
        agents = {
            "a": Scripted("a", ["impl [[HANDOFF]]", "done now [[DONE]]"], edit=True),
            "b": Scripted("b", [QuotaError("b: usage limit")]),
        }
        result = session(ws, agents, Seq(["failed", "passed"]), on_quota="solo")
        assert result.outcome == "review_pending"
        assert "ReviewPending(b)" in result.stop_condition
        assert result.exit_code == 4
        assert any("review pending" in note for note in result.transcript.notes)

    def test_partner_that_reviewed_the_final_state_is_not_pending(self, tmp_path):
        # b reviews after the only change (turn 1) and is dropped later: it has
        # already seen the final state, so no review is outstanding.
        ws = repo(tmp_path)
        a = Scripted("a", ["impl [[HANDOFF]]", "polish, no edits [[HANDOFF]]", "done [[DONE]]"], edit_turns={1})
        b = Scripted("b", ["reviewed, fine [[HANDOFF]]", QuotaError("b: usage limit")])
        result = session(ws, {"a": a, "b": b}, Seq(["passed"]), on_quota="solo")
        assert [m.agent for m in result.transcript.messages] == ["a", "b", "a", "a"]
        assert result.outcome == "success"


# --- Unknown cost is never zero (R06, AT08 legacy) ----------------------------


class TestCost:
    @pytest.mark.parametrize(
        "value,expected",
        [(0.25, 0.25), (0, 0.0), (None, None), (True, None), ("0.1", None), (-1.0, None), (math.nan, None), (math.inf, None)],
    )
    def test_parse_cost_validation(self, value, expected):
        cost, problem = parse_cost(value)
        assert cost == expected
        if value is not None and expected is None:
            assert problem  # rejected values are reported, not silently dropped

    def test_transcript_tracks_unknown_turns_separately(self):
        transcript = Transcript("t")
        transcript.add(Message(1, "claude", "x", 0, 0.1, cost_usd=0.25))
        transcript.add(Message(2, "codex", "y", 0, 0.1, cost_usd=None))
        assert transcript.total_cost_usd == pytest.approx(0.25)
        assert transcript.cost_unknown_turns == 1
        assert not transcript.cost_complete
        assert "unknown cost" in transcript.cost_summary()

    def test_v1_transcript_zero_cost_migrates_to_unknown(self):
        legacy = {
            "task": "t",
            "messages": [
                {"turn_index": 1, "agent": "claude", "content": "x", "exit_code": 0, "duration_s": 1.0, "cost_usd": 0.3},
                {"turn_index": 2, "agent": "codex", "content": "y", "exit_code": 0, "duration_s": 1.0, "cost_usd": 0.0},
            ],
            "total_cost_usd": 0.3,
        }
        transcript = Transcript.from_dict(legacy)
        assert transcript.messages[1].cost_usd is None
        assert transcript.cost_unknown_turns == 1
        assert transcript.to_dict()["schema_version"] == 2

    def test_v2_transcript_roundtrip_keeps_real_zero(self):
        transcript = Transcript("t")
        transcript.add(Message(1, "a", "x", 0, 0.1, cost_usd=0.0))
        again = Transcript.from_dict(json.loads(json.dumps(transcript.to_dict())))
        assert again.messages[0].cost_usd == 0.0
        assert again.cost_unknown_turns == 0

    def test_unknown_fields_in_messages_are_tolerated(self):
        data = {"schema_version": 2, "task": "t", "messages": [
            {"turn_index": 1, "agent": "a", "content": "x", "exit_code": 0, "duration_s": 0.1, "future_field": 1}
        ]}
        assert Transcript.from_dict(data).messages[0].agent == "a"


# --- Budget admission and ordering -------------------------------------------


class TestBudget:
    def test_completion_on_the_spending_turn_is_recognised(self, tmp_path):
        ws = repo(tmp_path)
        agents = {"a": Scripted("a", ["impl [[HANDOFF]]"], cost=0.4), "b": Scripted("b", ["done [[DONE]]"], cost=0.4)}
        result = session(ws, agents, Seq(["passed"]), budget_usd=0.5)
        assert result.outcome == "success"

    def test_no_turn_is_dispatched_once_the_budget_is_spent(self, tmp_path):
        ws = repo(tmp_path)
        a = Scripted("a", [f"step {i} [[HANDOFF]]" for i in range(10)], cost=0.3)
        result = session(ws, {"a": a}, budget_usd=0.5, max_turns=10, require_all_agents_for_success=False)
        assert result.stop_condition == "BudgetExceeded($0.50)"
        assert a.calls == 2  # 0.3 + 0.3 crosses 0.5; the third turn is never sent

    def test_budget_notes_agents_without_cost_reporting(self, tmp_path):
        ws = repo(tmp_path)
        agents = {"a": Scripted("a", ["x [[DONE]]"])}
        result = session(ws, agents, budget_usd=1.0, require_all_agents_for_success=False)
        assert result.outcome == "unverified"
        assert any("covers reported costs only" in note and "a reports no cost" in note for note in result.transcript.notes)

    def test_unknown_cost_from_a_cost_reporting_agent_is_noted_not_zero(self, tmp_path):
        # Review finding: an agent *with* a cost path whose output lacked a
        # cost was silently counted as $0 against the budget (D-003).
        ws = repo(tmp_path)
        a = Scripted("a", ["one [[HANDOFF]]", "two [[HANDOFF]]", "done [[DONE]]"], cost=None)
        a.cost_json_path = "total_cost_usd"  # configured to report, but reported nothing
        result = session(ws, {"a": a}, budget_usd=0.5, max_turns=3, require_all_agents_for_success=False)
        assert a.calls == 3  # turns are still admitted; only the note is new
        assert result.outcome == "unverified"
        notes = [n for n in result.transcript.notes if "cannot account for" in n]
        assert len(notes) == 1 and "turn 1" in notes[0] and "unknown (not zero)" in notes[0]
        assert not any("covers reported costs only" in n for n in result.transcript.notes)
        assert result.transcript.cost_unknown_turns == 3 and result.transcript.total_cost_usd == 0.0
        assert not result.transcript.cost_complete
        assert all(m.cost_usd is None for m in result.transcript.messages)

    def test_no_unknown_cost_note_without_a_budget(self, tmp_path):
        ws = repo(tmp_path)
        a = Scripted("a", ["done [[DONE]]"], cost=None)
        a.cost_json_path = "total_cost_usd"
        result = session(ws, {"a": a}, require_all_agents_for_success=False)
        assert not any("cannot account for" in n for n in result.transcript.notes)

    @pytest.mark.parametrize("bad", [-1.0, math.nan, math.inf])
    def test_invalid_budget_rejected(self, tmp_path, bad):
        with pytest.raises(ValueError):
            session(repo(tmp_path), {"a": Scripted("a", [])}, budget_usd=bad)


class TestWallclock:
    def test_expired_deadline_dispatches_nothing(self, tmp_path, monkeypatch):
        ws = repo(tmp_path)
        a = Scripted("a", ["x [[HANDOFF]]"])
        real = time.monotonic
        start = real()
        # The first reading anchors the session; every later one is 2 hours on.
        readings = iter([start])
        monkeypatch.setattr(time, "monotonic", lambda: next(readings, start + 7200))
        result = session(ws, {"a": a}, wallclock_seconds=60, require_all_agents_for_success=False)
        assert result.stop_condition == "WallClockBudget(60)"
        assert a.calls == 0

    def test_cli_agent_timeout_is_clamped_to_the_deadline(self):
        agent = CLIAgent("x", "X", ["true"], "stdin", "", "text", timeout_seconds=300)
        agent.deadline = time.monotonic() + 5
        assert agent.effective_timeout() <= 5.5
        agent.deadline = None
        assert agent.effective_timeout() == 300


# --- Interrupts preserve artifacts (cancellation stops scheduling) ----------


class TestInterrupt:
    def test_interrupt_mid_turn_saves_artifacts_without_new_verification(self, tmp_path):
        ws = repo(tmp_path)
        verifier = Seq(["failed"])
        agents = {"a": Scripted("a", ["impl [[HANDOFF]]"], edit=True), "b": Scripted("b", [KeyboardInterrupt()])}
        result = session(ws, agents, verifier)
        assert result.outcome == "interrupted"
        assert result.exit_code == 130
        assert result.transcript_path and result.transcript_path.exists()
        assert any("interrupted during b's turn" in note for note in result.transcript.notes)
        assert verifier.calls == 2  # baseline + after turn 1; none after the interrupt


# --- Halts still record local verification evidence ---------------------------


class TestFinalisation:
    def test_agent_failure_after_changes_reruns_the_checks(self, tmp_path):
        ws = repo(tmp_path)
        verifier = Seq(["failed", "failed", "passed"])
        agents = {"a": Scripted("a", ["impl [[HANDOFF]]"], edit=True), "b": Scripted("b", [AgentError("b: boom")])}
        result = session(ws, agents, verifier)
        assert result.outcome == "halted"
        assert result.stop_condition == "AgentError"
        assert verifier.calls == 3  # baseline, after turn 1, final evidence at halt
        assert result.transcript.final_verification["status"] == "passed"

    @pytest.mark.parametrize(
        "exc,condition",
        [
            (AuthError("x"), "AuthFailed(b)"),
            (BillingError("x"), "BillingBlocked(b)"),
            (ModelUnavailableError("x"), "ModelUnavailable(b)"),
            (AgentTimeoutError("x"), "AgentTimeout(b)"),
            (OutputLimitError("x"), "OutputLimit(b)"),
        ],
    )
    def test_classified_failures_have_precise_stop_conditions(self, tmp_path, exc, condition):
        ws = repo(tmp_path)
        agents = {"a": Scripted("a", ["x [[HANDOFF]]"]), "b": Scripted("b", [exc])}
        result = session(ws, agents)
        assert result.stop_condition == condition

    def test_billing_block_is_not_treated_as_quota_under_wait(self, tmp_path, monkeypatch):
        ws = repo(tmp_path)
        monkeypatch.setattr(time, "sleep", lambda s: pytest.fail("billing must not be waited on"))
        agents = {"a": Scripted("a", [BillingError("a: credit balance is too low")])}
        result = session(ws, agents, on_quota="wait", quota_wait_seconds=1)
        assert result.stop_condition == "BillingBlocked(a)"


# --- Error classification ------------------------------------------------------


class TestClassification:
    @pytest.mark.parametrize(
        "text,cls,kind",
        [
            ("Not logged in · Please run /login", AuthError, "auth"),
            ("Error: HTTP 401 Unauthorized", AuthError, "auth"),
            ("Your credit balance is too low to access the API", BillingError, "billing"),
            ("model_not_found: claude-x", ModelUnavailableError, "model_unavailable"),
            ("You've hit your usage limit. Try again later.", QuotaError, "quota"),
            ("Error: status 429 Too Many Requests", QuotaError, "rate_limit"),
            ("rate limit exceeded", QuotaError, "rate_limit"),
            ("API Error: Overloaded", QuotaError, "overloaded"),
        ],
    )
    def test_known_failures(self, text, cls, kind):
        err = classify_failure("x", 1, "x", text, "")
        assert type(err) is cls
        assert err.kind == kind

    @pytest.mark.parametrize(
        "text",
        [
            "SyntaxError at line 429 of parser.py",
            "failed to generate the report",
            "billing.py: test failed",  # a file name is not a billing block
        ],
    )
    def test_incidental_text_is_not_misclassified(self, text):
        err = classify_failure("x", 1, "x", text, "")
        assert type(err) is AgentError
        assert err.kind == "error"

    def test_quota_errors_are_retryable_and_billing_is_not(self):
        assert QuotaError("x").retryable
        assert not BillingError("x").retryable
        assert not AuthError("x").retryable


# --- Control tokens are honoured only on the final line -----------------------


class TestControlTokens:
    @pytest.mark.parametrize(
        "text,token",
        [
            ("all done [[DONE]]", "DONE"),
            ("work\n\n[[HANDOFF]]", "HANDOFF"),
            ("I'll emit [[DONE]] once tests pass.\nMore work needed [[HANDOFF]]", "HANDOFF"),
            ("I'll emit [[DONE]] once tests pass.\nstill going", None),
            ("Note: do not write `[[DONE]]` yet", None),
            ("done\n```\n[[DONE]]\n```", None),
            ("finished [[DONE]]\n\n[Duet warning: output truncated]", "DONE"),
            ("[[DONE]] [[HANDOFF]]", "HANDOFF"),
            ("", None),
        ],
    )
    def test_parse(self, text, token):
        assert parse_control_token(text) == token

    def test_strip_removes_all_tokens_for_display(self):
        cleaned, token = strip_control_tokens("mention [[DONE]]\nnext [[HANDOFF]]")
        assert "[[" not in cleaned
        assert token == "HANDOFF"


# --- Adapter: bounded structured output and parse fallbacks -------------------


def _script(tmp_path: Path, body: str) -> str:
    """A fake agent that reads its prompt, then runs the Python `body`
    (`out`/`err` write UTF-8 bytes whatever the console encoding)."""
    prelude = "import sys\nsys.stdin.read()\nout = lambda t: sys.stdout.buffer.write(t.encode())\nerr = lambda t: sys.stderr.buffer.write(t.encode())\n"
    return str(make_exe(tmp_path, "agent", source=prelude + body))


class TestAdapter:
    def test_json_output_over_the_cap_is_a_structured_failure(self, tmp_path):
        cmd = _script(tmp_path, "out('{\"result\": \"' + 'x' * 200000 + '\"}\\n')\n")
        agent = CLIAgent("a", "A", [cmd], "stdin", "", "json", 30, result_json_path="result", max_output_bytes=4096)
        with pytest.raises(OutputLimitError):
            agent.send("hi", tmp_path)

    def test_invalid_cost_is_unknown_with_warning(self, tmp_path):
        cmd = _script(tmp_path, "out('{\"result\": \"ok\", \"total_cost_usd\": -3}\\n')\n")
        agent = CLIAgent("a", "A", [cmd], "stdin", "", "json", 30, result_json_path="result", cost_json_path="total_cost_usd")
        result = agent.send("hi", tmp_path)
        assert result.cost_usd is None
        assert "invalid reported cost" in result.text

    def test_text_last_line_never_returns_the_echoed_prompt(self, tmp_path):
        stdout = "user\nControl protocol: emit [[DONE]] when complete\n\nthinking...\n\nfinal answer line one\nfinal answer [[HANDOFF]]"
        cmd = _script(tmp_path, f"out({stdout + chr(10)!r})\n")
        agent = CLIAgent("codex", "Codex", [cmd], "stdin", "", "text-last-line", 30)
        result = agent.send("hi", tmp_path)
        assert "Control protocol" not in result.text
        assert result.text.startswith("final answer line one")

    def test_nonzero_exit_is_classified(self, tmp_path):
        cmd = _script(tmp_path, "err('Not logged in · Please run /login\\n')\nsys.exit(1)\n")
        agent = CLIAgent("a", "A", [cmd], "stdin", "", "text", 30)
        with pytest.raises(AuthError):
            agent.send("hi", tmp_path)

    def test_timeout_is_a_timeout_error(self, tmp_path):
        agent = CLIAgent("a", "A", [sys.executable, "-c", "import time; time.sleep(30)"], "stdin", "", "text", 1)
        with pytest.raises(AgentTimeoutError, match="timed out"):
            agent.send("hi", tmp_path)


# --- Doctor: isolated probes, agent-scoped root check --------------------------


class TestDoctor:
    def test_probe_session_never_leaks_into_the_session_agent(self, tmp_path):
        cmd = _script(tmp_path, "out('{\"result\": \"CLAUDE_DOCTOR_OK\", \"session_id\": \"probe-1\"}\\n')\n")
        agent = CLIAgent("claude", "Claude", [cmd], "stdin", "", "json", 30, result_json_path="result", session_json_path="session_id")
        config = parse_config("")
        config.agents = {"claude": agent}
        check = _round_trip("claude", config, "Reply with exactly: CLAUDE_DOCTOR_OK", "CLAUDE_DOCTOR_OK")
        assert check.ok
        assert agent.last_session_id == ""
        assert agent.session_id == ""

    def test_bypass_detection(self):
        assert uses_permission_bypass(["claude", "-p", "--dangerously-skip-permissions"])
        assert uses_permission_bypass(["claude", "--permission-mode", "bypassPermissions"])
        assert uses_permission_bypass(["claude", "--permission-mode=bypassPermissions"])
        assert not uses_permission_bypass(["claude", "--permission-mode", "acceptEdits"])
        assert not uses_permission_bypass(["codex", "--ask-for-approval", "never"])

    def test_root_disables_only_bypass_agents(self, monkeypatch, tmp_path):
        import duet.doctor as doctor

        monkeypatch.setattr(doctor, "_is_root", lambda: True)
        monkeypatch.delenv("IS_SANDBOX", raising=False)
        config = parse_config(
            f"""
[agents.claude]
command = ["{sys.executable}", "--dangerously-skip-permissions"]
[agents.codex]
command = ["{sys.executable}"]
"""
        )
        checks = run_doctor(config, live=False)
        assert available_agent_names(checks) == {"codex"}
        assert hard_failures(checks) == []  # one usable agent means solo, not abort

    def test_is_sandbox_lets_the_cli_decide(self, monkeypatch):
        import duet.doctor as doctor

        monkeypatch.setattr(doctor, "_is_root", lambda: True)
        monkeypatch.setenv("IS_SANDBOX", "1")
        config = parse_config(f'[agents.claude]\ncommand = ["{sys.executable}", "--dangerously-skip-permissions"]\n')
        assert available_agent_names(run_doctor(config, live=False)) == {"claude"}

    def test_no_usable_agent_is_a_hard_failure(self):
        checks = [Check("claude on PATH", False, "missing", agent="claude")]
        assert available_agent_names(checks) == set()
        assert hard_failures(checks)


# --- Configuration --------------------------------------------------------------


class TestConfig:
    @pytest.mark.parametrize(
        "toml",
        [
            "[session]\nmax_turns = true\n",
            "[session]\nmax_turns = 0\n",
            "[session]\nmax_turns = 2.5\n",
            "[session]\nwallclock_seconds = 0\n",
            "[session]\nbudget_usd = -1\n",
            "[session]\nbudget_usd = nan\n",
            "[session]\nbudget_usd = inf\n",
            "[session]\nloop_threshold = 0\n",
            "[session]\nquota_wait_seconds = -5\n",
            "[session]\nstart_with = 3\n",
            '[agents.x]\ncommand = ["x"]\ntimeout_seconds = 0\n',
            '[agents.x]\ncommand = ["x"]\nquota_markers = "limit"\n',
            '[agents.x]\ncommand = ["x"]\nmax_output_bytes = 10\n',
            "agents = 3\n",
        ],
    )
    def test_invalid_values_fail_closed(self, toml):
        with pytest.raises(ConfigError):
            parse_config(toml)

    def test_packaged_defaults_match_the_repository_example(self):
        packaged = parse_config(default_config_text())
        example = load_config(REPO_ROOT / "duet.toml")
        assert packaged.session == example.session
        assert {n: a.command for n, a in packaged.agents.items()} == {n: a.command for n, a in example.agents.items()}

    def test_defaults_load_without_any_config_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        config = load_config()
        assert set(config.agents) == {"claude", "codex"}
        assert config.source == "<built-in defaults>"

    def test_init_refuses_to_overwrite(self, tmp_path):
        target = tmp_path / "duet.toml"
        target.write_text("# mine\n")
        with pytest.raises(ConfigError, match="already exists"):
            write_config(target)
        assert target.read_text() == "# mine\n"
        write_config(target, force=True)
        assert "[agents.claude]" in target.read_text()


# --- Roles and verifiers ----------------------------------------------------------


def test_default_roles_are_task_agnostic():
    roles = default_roles("claude", ["claude", "codex"])
    assert "Implementer" in roles["claude"] and "Reviewer" in roles["codex"]
    assert all("test_roman" not in text for text in roles.values())
    assert "test_roman.py" in demo_roles("claude", ["claude", "codex"])["codex"]


def test_pytest_with_no_tests_is_unknown_not_failed(tmp_path):
    if not PytestVerifier and not os.environ.get("PATH"):
        pytest.skip("no PATH")
    import shutil

    if shutil.which("pytest") is None:
        pytest.skip("pytest executable not on PATH")
    result = PytestVerifier(timeout_seconds=60).verify(tmp_path)
    assert result.status == "unknown"
    assert "collected no tests" in result.output


def test_exit_codes_are_stable():
    assert [exit_code_for(o) for o in ("success", "halted", "unverified", "review_pending", "interrupted")] == [0, 2, 3, 4, 130]


COST_SRC = '''
import json, os, sys
state = {state!r}
{version_block}sys.stdin.read()
try:
    n = int(open(state).read().strip() or 0)
except OSError:
    n = 0
n += 1
open(state, "w").write(str(n))
costs = {costs!r}
print(json.dumps({{"result": "ok", "session_id": "s1", "total_cost_usd": costs[n - 1]}}))
'''


class TestResumedSessionCost:
    """Claude Code reports a resumed session's cumulative spend; summing it
    per turn double counts (found in the cost-tracking docs during D04)."""

    def _agent(self, tmp_path, costs):
        script = make_exe(tmp_path, "cumulative", source=COST_SRC.format(state=str(tmp_path / "n"), version_block="", costs=list(costs)))
        return CLIAgent(
            "claude", "Claude", [str(script)], "stdin", "", "json", 30, result_json_path="result",
            session_json_path="session_id", cost_json_path="total_cost_usd", resume_command=[str(script), "{session_id}"],
            chain_sessions=True, cost_json_scope="session_cumulative_on_resume",
        )

    def test_chained_turns_report_deltas(self, tmp_path):
        agent = self._agent(tmp_path, [0.25, 0.60, 1.00])
        costs = [agent.send("x", tmp_path).cost_usd for _ in range(3)]
        assert costs == [pytest.approx(0.25), pytest.approx(0.35), pytest.approx(0.40)]

    def test_attached_session_first_turn_is_unknown(self, tmp_path):
        agent = self._agent(tmp_path, [5.00, 5.30])
        agent.session_id = "s1"  # attached: spend from before Duet is unknown
        first = agent.send("x", tmp_path)
        assert first.cost_usd is None and "cumulative spend" in first.text
        assert agent.send("x", tmp_path).cost_usd == pytest.approx(0.30)

    def test_default_claude_config_uses_auto_scope(self):
        # Review finding: the packaged default hard-coded the cumulative scope,
        # so Claude < 2.1.277 (per-call cost) produced wrong deltas.
        from duet.config import default_config_text, parse_config

        assert parse_config(default_config_text()).agents["claude"].cost_json_scope == "auto"
        assert parse_config(default_config_text()).agents["codex"].cost_json_scope == "call"
        assert (REPO_ROOT / "duet.toml").read_text() == default_config_text()

    @pytest.mark.parametrize("scope", ["call", "session_cumulative_on_resume", "auto"])
    def test_cost_scopes_accepted(self, scope):
        config = f'[agents.x]\ncommand = ["x"]\ncost_json_scope = "{scope}"\n'
        assert parse_config(config).agents["x"].cost_json_scope == scope

    def test_unknown_cost_scope_rejected(self):
        with pytest.raises(ConfigError, match="cost_json_scope"):
            parse_config('[agents.x]\ncommand = ["x"]\ncost_json_scope = "sometimes"\n')


class TestAutoCostScope:
    """cost_json_scope = "auto": the reading of `total_cost_usd` follows the
    installed Claude Code version, read once per process from `--version`."""

    @pytest.fixture(autouse=True)
    def fresh_detection(self):
        from duet.adapters import detect_cost_scope

        detect_cost_scope.cache_clear()
        yield
        detect_cost_scope.cache_clear()

    def _agent(self, tmp_path, costs, version_cmd, name="claude"):
        version_out, version_code = version_cmd
        version_block = (
            'if sys.argv[1:2] == ["--version"]:\n'
            f'    open({str(tmp_path / "version-calls")!r}, "a").write("x\\n")\n'
            + (f"    print({version_out!r})\n" if version_out is not None else "")
            + f"    sys.exit({version_code})\n"
        )
        script = make_exe(tmp_path, name, source=COST_SRC.format(state=str(tmp_path / f"{name}.n"), version_block=version_block, costs=list(costs)))
        return CLIAgent(
            name, name.title(), [str(script)], "stdin", "", "json", 30, result_json_path="result",
            session_json_path="session_id", cost_json_path="total_cost_usd", resume_command=[str(script), "{session_id}"],
            chain_sessions=True, cost_json_scope="auto",
        )

    def version_calls(self, tmp_path) -> int:
        path = tmp_path / "version-calls"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def test_new_claude_reports_session_cumulative_cost(self, tmp_path):
        agent = self._agent(tmp_path, [0.25, 0.60, 1.00], ("2.1.283 (Claude Code)", 0))
        costs = [agent.send("x", tmp_path).cost_usd for _ in range(3)]
        assert costs == [pytest.approx(0.25), pytest.approx(0.35), pytest.approx(0.40)]
        assert self.version_calls(tmp_path) == 1  # once per process, not per turn

    def test_old_claude_reports_per_call_cost(self, tmp_path):
        # Before 2.1.277 each call reports its own cost; deltas would be wrong.
        agent = self._agent(tmp_path, [0.25, 0.30, 0.20], ("2.1.276 (Claude Code)", 0))
        results = [agent.send("x", tmp_path) for _ in range(3)]
        assert [r.cost_usd for r in results] == [pytest.approx(0.25), pytest.approx(0.30), pytest.approx(0.20)]
        assert not any("Duet warning" in r.text for r in results)

    def test_unknown_version_makes_resumed_cost_unknown(self, tmp_path):
        agent = self._agent(tmp_path, [0.25, 0.60, 1.00], ("no version here", 0))
        first, second, third = (agent.send("x", tmp_path) for _ in range(3))
        assert first.cost_usd == pytest.approx(0.25)  # a new session: the call's own cost either way
        assert second.cost_usd is None and third.cost_usd is None
        assert "could not read" in second.text and "unknown" in second.text

    def test_failing_version_command_is_unknown_not_zero(self, tmp_path):
        agent = self._agent(tmp_path, [0.25, 0.60], (None, 1))
        agent.session_id = "s1"  # attached session: every turn is a resume
        assert agent.send("x", tmp_path).cost_usd is None

    def test_detection_is_shared_across_agents_in_a_process(self, tmp_path):
        from duet.adapters import detect_cost_scope

        agent = self._agent(tmp_path, [0.25, 0.60], ("2.1.283 (Claude Code)", 0))
        agent.send("x", tmp_path)
        assert detect_cost_scope(agent.command[0]) == "session_cumulative_on_resume"
        again = CLIAgent(
            "claude", "Claude", list(agent.command), "stdin", "", "json", 30, result_json_path="result",
            cost_json_path="total_cost_usd", cost_json_scope="auto",
        )
        again.send("x", tmp_path)
        assert self.version_calls(tmp_path) == 1

    def test_version_logic_matches_the_provider_adapter(self):
        from duet.adapters import CUMULATIVE_RESUME_COST_SINCE, cumulative_resume_cost, parse_version
        from duet.providers import claude_cli

        assert claude_cli.cumulative_resume_cost is cumulative_resume_cost
        assert claude_cli.parse_version is parse_version
        assert CUMULATIVE_RESUME_COST_SINCE == (2, 1, 277)
        assert not cumulative_resume_cost(parse_version("2.1.276")) and cumulative_resume_cost(parse_version("2.1.277"))
