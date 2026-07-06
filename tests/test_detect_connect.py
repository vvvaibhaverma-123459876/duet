from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from duet.cli import _resolve_connect_session
from duet.detect import Activity, DetectedSession, detect_activity, format_activity
from duet.sessions import SessionInfo, claude_project_dir


def seed_claude(home: Path, repo: Path, session_id: str, age_seconds: float, now: float) -> None:
    project = claude_project_dir(repo, claude_home=home)
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "work on it"}}) + "\n")
    stamp = now - age_seconds
    os.utime(path, (stamp, stamp))


def seed_codex(home: Path, session_id: str, cwd: str, age_seconds: float, now: float) -> None:
    day = home / "sessions" / "2026" / "07" / "06"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-07-06T10-00-00-{session_id}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": session_id, "cwd": cwd}}) + "\n")
    stamp = now - age_seconds
    os.utime(path, (stamp, stamp))


def info(session_id: str = "s1") -> SessionInfo:
    return SessionInfo(session_id=session_id, modified=datetime.now(), size_bytes=1, preview="")


def activity_with(*sessions: DetectedSession) -> Activity:
    return Activity(repo=Path("."), sessions=list(sessions), processes={})


class TestDetectActivity:
    def test_live_and_idle_classification(self, tmp_path):
        now = 1_800_000_000.0
        repo = tmp_path / "repo"
        repo.mkdir()
        claude_home, codex_home = tmp_path / "ch", tmp_path / "xh"
        seed_claude(claude_home, repo, "fresh", age_seconds=10, now=now)
        seed_codex(codex_home, "old", str(repo), age_seconds=4000, now=now)
        activity = detect_activity(repo, claude_home=claude_home, codex_home=codex_home, now=now, processes={})
        by_agent = {d.agent: d for d in activity.sessions}
        assert by_agent["claude"].live is True
        assert by_agent["codex"].live is False

    def test_codex_ancestor_cwd_matches_repo(self, tmp_path):
        now = 1_800_000_000.0
        repo = tmp_path / "parent" / "repo"
        repo.mkdir(parents=True)
        codex_home = tmp_path / "xh"
        seed_codex(codex_home, "anc", str(tmp_path / "parent"), age_seconds=10, now=now)
        seed_codex(codex_home, "other", str(tmp_path / "elsewhere"), age_seconds=10, now=now)
        (tmp_path / "elsewhere").mkdir()
        activity = detect_activity(repo, claude_home=tmp_path / "ch", codex_home=codex_home, now=now, processes={})
        assert [d.info.session_id for d in activity.candidates("codex")] == ["anc"]

    def test_format_mentions_cold_start_and_live_warning(self, tmp_path):
        live = DetectedSession("codex", info("live-1"), live=True)
        rendered = format_activity(Activity(repo=tmp_path, sessions=[live], processes={"codex": 1}))
        assert "claude: no sessions found" in rendered
        assert "[LIVE] live-1" in rendered
        assert "--fork-live" in rendered
        assert "codex x1" in rendered


class TestResolveConnectSession:
    def test_explicit_id_found_in_detection_keeps_live_flag(self):
        activity = activity_with(DetectedSession("codex", info("s1"), live=True))
        assert _resolve_connect_session(activity, "codex", "s1") == ("s1", True)

    def test_explicit_id_not_detected_is_trusted_as_idle(self):
        assert _resolve_connect_session(activity_with(), "codex", "manual") == ("manual", False)

    def test_defaults_to_newest_candidate(self):
        activity = activity_with(
            DetectedSession("claude", info("newest"), live=False),
            DetectedSession("claude", info("older"), live=False),
        )
        assert _resolve_connect_session(activity, "claude", None) == ("newest", False)

    def test_no_candidates_means_cold_start(self):
        assert _resolve_connect_session(activity_with(), "claude", None) == (None, False)
