"""Optional real-provider smoke tests (spec D04 exit criterion).

These contact the real Claude Code and Codex CLIs with the user's existing
login and consume a small amount of plan usage. They never enable paid
fallback: Claude runs without --bare (subscription login), Codex through its
own app-server login, and nothing sets API keys. They are NOT_RUN unless
DUET_REAL_PROVIDERS=1 is set by a person on a machine where both CLIs are
installed, logged in, and allowed to reach their providers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from duet.providers.base import TurnRequest
from duet.providers.claude_cli import ClaudeCLIAdapter
from duet.providers.codex_appserver import CodexAppServerAdapter

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DUET_REAL_PROVIDERS") != "1", reason="set DUET_REAL_PROVIDERS=1 to contact real providers"),
]
RECORD = os.environ.get("DUET_REAL_PROVIDERS_RECORD")


def _record(name: str, payload: dict) -> None:
    if RECORD:
        Path(RECORD).mkdir(parents=True, exist_ok=True)
        (Path(RECORD) / f"{name}.json").write_text(json.dumps(payload, indent=2, default=str))


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude not installed")
def test_claude_real_turn_records_native_ids_and_settings(tmp_path):
    adapter = ClaudeCLIAdapter()
    result = adapter.run_turn(TurnRequest(prompt="Reply with exactly: DUET_SMOKE_OK", cwd=_repo(tmp_path), permission_profile="read_only", timeout_seconds=300))
    _record("claude", {"caps": adapter.capabilities().to_dict(), "status": result.status, "session_id": result.session_id, "settings": result.settings.to_dict(), "usage": [u.to_dict() for u in result.usage]})
    assert result.ok, result.error
    assert "DUET_SMOKE_OK" in result.text and result.session_id


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex not installed")
def test_codex_real_turn_records_thread_and_usage(tmp_path):
    adapter = CodexAppServerAdapter()
    try:
        result = adapter.run_turn(TurnRequest(prompt="Reply with exactly: DUET_SMOKE_OK", cwd=_repo(tmp_path), permission_profile="read_only", timeout_seconds=300))
        limits, reason = adapter.read_rate_limits()
        _record("codex", {"caps": adapter.capabilities().to_dict(), "status": result.status, "thread": result.session_id, "settings": result.settings.to_dict(), "usage": [u.to_dict() for u in result.usage], "rate_limits": [l.to_dict() for l in limits], "rate_limit_reason": reason})
        assert result.ok, result.error
        assert "DUET_SMOKE_OK" in result.text and result.session_id
    finally:
        adapter.close()
