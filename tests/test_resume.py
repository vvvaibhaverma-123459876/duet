from __future__ import annotations

import json
from pathlib import Path

import pytest

from duet.resumestate import ResumeError, ResumeState, load_resume_state, resume_path, save_resume_state


def state(**overrides) -> ResumeState:
    defaults = dict(
        task="fix the bug",
        outcome="halted",
        stop_condition="QuotaExhausted(codex)",
        mode="live",
        workspace="/tmp/x",
        sessions={"claude": "c-1", "codex": "x-1"},
        branch="duet/session-1",
    )
    defaults.update(overrides)
    return ResumeState(**defaults)


class TestResumeState:
    def test_save_load_roundtrip(self, tmp_path):
        save_resume_state(tmp_path, state())
        loaded = load_resume_state(tmp_path)
        assert loaded.task == "fix the bug"
        assert loaded.stop_condition == "QuotaExhausted(codex)"
        assert loaded.sessions == {"claude": "c-1", "codex": "x-1"}
        assert loaded.mode == "live"
        assert loaded.branch == "duet/session-1"
        assert loaded.saved_at > 0

    def test_attach_specs_skip_empty_ids(self):
        specs = state(sessions={"claude": "c-1", "codex": ""}).attach_specs()
        assert specs == ["claude=c-1"]

    def test_missing_manifest_raises(self, tmp_path):
        with pytest.raises(ResumeError, match="no resume manifest"):
            load_resume_state(tmp_path)

    def test_corrupt_manifest_raises(self, tmp_path):
        path = resume_path(tmp_path)
        path.parent.mkdir()
        path.write_text("{not json")
        with pytest.raises(ResumeError, match="cannot read"):
            load_resume_state(tmp_path)

    def test_manifest_without_task_raises(self, tmp_path):
        path = resume_path(tmp_path)
        path.parent.mkdir()
        path.write_text(json.dumps({"outcome": "halted"}))
        with pytest.raises(ResumeError, match="malformed"):
            load_resume_state(tmp_path)


class TestLastSessionId:
    def test_send_records_last_session_id_without_chaining(self, tmp_path):
        from duet.adapters import CLIAgent

        script = tmp_path / "fake"
        script.write_text('#!/bin/sh\ncat > /dev/null\necho \'{"result":"ok","session_id":"sid-9"}\'\n')
        script.chmod(0o755)
        agent = CLIAgent(
            name="claude",
            display_name="Claude",
            command=[str(script)],
            prompt_via="stdin",
            workspace_flag="",
            output_format="json",
            timeout_seconds=30,
            result_json_path="result",
            session_json_path="session_id",
            chain_sessions=False,
        )
        agent.send("hi", tmp_path)
        assert agent.last_session_id == "sid-9"
        assert agent.session_id == ""  # chaining off: not adopted for reuse
