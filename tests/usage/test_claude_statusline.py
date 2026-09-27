"""D07: Claude Code status-line telemetry. The payloads are the documented
examples from https://code.claude.com/docs/en/statusline (retrieved
2026-09-27, recorded verbatim under tests/fixtures/telemetry/claude)."""
from __future__ import annotations

import builtins
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from duet.integrations.claude_statusline import (
    ALLOWED_FIELDS,
    StatusLineError,
    observe_status_line,
    parse_status_line,
    run_status_line,
)
from duet.usage import Ledger, Quality, Scope

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "telemetry" / "claude"
DOCS_EXAMPLE = (FIXTURES / "statusline-docs-full-schema.json").read_bytes()
MOCK_INPUT = (FIXTURES / "statusline-docs-mock-input.json").read_bytes()
NOW = 1_790_400_000_000


def test_fixture_provenance_is_recorded():
    sources = json.loads((FIXTURES.parent / "SOURCES.json").read_text())
    entry = sources["claude/statusline-docs-full-schema.json"]
    assert entry["origin"] == "verbatim" and entry["url"] == "https://code.claude.com/docs/en/statusline" and entry["retrieved"] == "2026-09-27"


class TestParsing:
    def test_documented_example_yields_the_allowlisted_fields(self):
        snap = parse_status_line(DOCS_EXAMPLE)
        assert (snap.session_id, snap.model_id, snap.model_display_name, snap.claude_code_version) == ("abc123...", "claude-opus-5-5", "Opus", "2.1.90")
        assert snap.cost_usd == Decimal("0.01234") and str(snap.cost_usd) == "0.01234"  # never through float
        assert (snap.total_duration_ms, snap.total_api_duration_ms) == (45000, 2300)
        assert (snap.context_input_tokens, snap.context_output_tokens, snap.context_window_size, snap.context_used_percent) == (15500, 1200, 200000, Decimal(8))
        assert [(r.name, r.used_percent, r.resets_at_s, r.window_minutes) for r in snap.rate_limits] == [
            ("five_hour", Decimal("23.5"), 1738425600, 300), ("seven_day", Decimal("41.2"), 1738857600, 10080),
            ("spend_limit", Decimal("62.8"), 1740787200, None),
        ]

    def test_documented_example_observations_have_distinct_meanings(self):
        out = observe_status_line(DOCS_EXAMPLE, observed_at_ms=NOW)
        by_metric = {}
        for o in out:
            by_metric.setdefault(o.metric, []).append(o)
        cost = by_metric["cost.estimated_usd"][0]
        assert cost.scope is Scope.SESSION_CUMULATIVE and cost.quality is Quality.ESTIMATED and cost.session_id == "abc123..."
        assert {o.metric for o in out if o.scope is Scope.SNAPSHOT} == {
            "context.input_tokens", "context.output_tokens", "context.window_size", "context.used_percent"}
        quota = by_metric["quota.used_percent"]
        assert {(o.key, o.resets_at_ms) for o in quota} == {("five_hour", 1738425600000), ("seven_day", 1738857600000), ("spend_limit", 1740787200000)}
        assert all(o.pool_id is None for o in out)  # the payload names no account
        assert set(by_metric) == {"cost.estimated_usd", "time.session_ms", "time.api_ms", "context.input_tokens", "context.output_tokens",
                                  "context.window_size", "context.used_percent", "quota.used_percent"}

    def test_documented_mock_input_leaves_absent_fields_absent(self):
        snap = parse_status_line(MOCK_INPUT)
        assert snap.session_id == "test-session-abc" and snap.context_used_percent == Decimal(25)
        assert snap.cost_usd is None and snap.total_duration_ms is None and snap.rate_limits == () and snap.model_id is None
        out = observe_status_line(MOCK_INPUT, observed_at_ms=NOW)
        assert [(o.metric, o.value) for o in out] == [("context.used_percent", Decimal(25))]

    def test_null_and_invalid_fields_are_unknown_not_zero(self):
        payload = json.loads(DOCS_EXAMPLE)
        payload["context_window"].update(used_percentage=None, current_usage=None, total_input_tokens="lots")
        payload["cost"].update(total_cost_usd=-1, total_duration_ms=True)
        payload["rate_limits"] = {"five_hour": {"used_percentage": None, "resets_at": 1738425600}}
        snap = parse_status_line(json.dumps(payload))
        assert (snap.context_used_percent, snap.context_input_tokens, snap.cost_usd, snap.total_duration_ms, snap.rate_limits) == (None, None, None, None, ())
        metrics = {o.metric for o in observe_status_line(json.dumps(payload), observed_at_ms=NOW)}
        assert not metrics & {"context.used_percent", "context.input_tokens", "cost.estimated_usd", "time.session_ms", "quota.used_percent"}

    @pytest.mark.parametrize("payload", [b"not json", b"[1, 2]", b'{"model": {}}', b'{"session_id": ""}', b'{"session_id": "a b/../c"}', b"\xff\xfe"])
    def test_unattributable_payloads_are_refused(self, payload):
        with pytest.raises(StatusLineError):
            parse_status_line(payload)


class TestAllowlist:
    def test_nothing_outside_the_allowlist_is_kept(self, tmp_path, monkeypatch):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"message": "SECRET-TRANSCRIPT-CONTENT"}\n')
        payload = json.loads(DOCS_EXAMPLE)
        payload["transcript_path"] = str(transcript)
        payload["api_key"] = "sk-ant-api03-SECRETSECRET"
        payload["cost"]["oauth_token"] = "SECRET-TOKEN"
        payload["model"]["display_name"] = "Opus\x1b]8;;http://evil\x07"  # control characters are not a label
        opened = []
        real_open = builtins.open
        monkeypatch.setattr(builtins, "open", lambda *a, **k: opened.append(a) or real_open(*a, **k))
        snap = parse_status_line(json.dumps(payload))
        out = observe_status_line(payload, observed_at_ms=NOW)
        monkeypatch.setattr(builtins, "open", real_open)
        assert opened == []  # the transcript is never read
        dumped = json.dumps(snap.to_dict()) + repr(snap) + json.dumps([o.to_dict() for o in out]) + repr(out)
        for forbidden in ("SECRET", "sk-ant", "transcript", str(tmp_path), "/current/working/directory", "/original/project",
                          "anthropics", "pull/1234", "my-session", "550e8400", "security-reviewer", "worktree", "evil", "hit_ratio"):
            assert forbidden not in dumped, forbidden
        assert snap.model_display_name is None

    def test_allowlist_is_the_documented_usage_fields_only(self):
        assert set(ALLOWED_FIELDS) >= {"session_id", "model.id", "cost.total_cost_usd", "context_window.used_percentage",
                                       "rate_limits.five_hour.used_percentage", "rate_limits.spend_limit.resets_at"}
        assert not any(f.startswith(("workspace", "transcript", "cwd", "worktree", "pr", "prompt", "session_name")) for f in ALLOWED_FIELDS)


class TestSessionKeying:
    def test_concurrent_sessions_stay_apart(self):
        ledger = Ledger()
        for i in range(3):
            for session, cost in (("sess-a", Decimal("0.10") * (i + 1)), ("sess-b", Decimal("1.00") + i)):
                payload = json.loads(DOCS_EXAMPLE)
                payload["session_id"], payload["cost"]["total_cost_usd"] = session, float(cost)
                ledger.ingest_all(observe_status_line(payload, observed_at_ms=NOW + i))
        a = ledger.consumption(session_id="sess-a").get("claude", "cost.estimated_usd")
        b = ledger.consumption(session_id="sess-b").get("claude", "cost.estimated_usd")
        assert (a.lower_bound, a.upper_bound) == (Decimal("0.20"), Decimal("0.30"))  # first value: pre-wrapper history unknown
        assert (b.lower_bound, b.upper_bound) == (Decimal("2"), Decimal("3"))
        assert all(o.source_event_id.startswith(f"claude.statusline:{o.session_id}:") for o in ledger.observations)

    def test_repeated_identical_payloads_refresh_but_do_not_add(self):
        ledger = Ledger()
        for i in range(5):
            ledger.ingest_all(observe_status_line(DOCS_EXAMPLE, observed_at_ms=NOW + i * 1000))
        cost = ledger.consumption().get("claude", "cost.estimated_usd")
        assert cost.upper_bound == Decimal("0.01234") and cost.latest_observed_at_ms == NOW + 4000


class TestWrapper:
    def test_original_output_and_exit_status_pass_through_unchanged(self):
        command = "printf '\\033[32m[Opus]\\033[0m 8%% context\\nsecond line'; exit 3"
        run = run_status_line(command, DOCS_EXAMPLE, observed_at_ms=NOW)
        assert run.stdout == b"\x1b[32m[Opus]\x1b[0m 8% context\nsecond line" and run.returncode == 3
        assert run.telemetry_error is None and any(o.metric == "cost.estimated_usd" for o in run.observations)

    def test_the_original_command_gets_the_untouched_stdin(self):
        echo = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]
        raw = DOCS_EXAMPLE.replace(b"\n", b"\r\n") + b"  "
        assert run_status_line(echo, raw, observed_at_ms=NOW).stdout == raw

    def test_the_environment_reaches_the_command(self):
        run = run_status_line('printf "%s" "$COLUMNS"', MOCK_INPUT, env={"COLUMNS": "77", "PATH": "/usr/bin:/bin"}, observed_at_ms=NOW)
        assert run.stdout == b"77"

    def test_telemetry_failures_never_change_the_output(self):
        def broken_sink(observations):
            raise RuntimeError("disk full")

        run = run_status_line("printf ok", DOCS_EXAMPLE, sink=broken_sink, observed_at_ms=NOW)
        assert (run.stdout, run.returncode) == (b"ok", 0) and "disk full" in run.telemetry_error
        run = run_status_line("printf ok", b"garbage", observed_at_ms=NOW)
        assert (run.stdout, run.returncode, run.observations) == (b"ok", 0, ()) and run.telemetry_error.startswith("StatusLineError")

    def test_no_original_command_prints_nothing_but_still_observes(self):
        seen = []
        run = run_status_line(None, DOCS_EXAMPLE, sink=seen.append, observed_at_ms=NOW)
        assert (run.stdout, run.returncode) == (b"", 0) and seen == [run.observations] and run.observations
