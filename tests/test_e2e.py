from __future__ import annotations

import os
import subprocess

import pytest


@pytest.mark.e2e
def test_seeded_session_real_clis(tmp_path):
    if os.environ.get("DUET_E2E") != "1":
        pytest.skip("set DUET_E2E=1 to run real Claude and Codex CLIs")
    workspace = tmp_path / "real"
    proc = subprocess.run(
        ["duet", "run", "--workspace", str(workspace), "--seed-demo", "--max-turns", "6", "--verify", "pytest"],
        text=True,
        capture_output=True,
        timeout=1800,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Outcome: success" in proc.stdout
    assert "Stop condition:" in proc.stdout
    assert "Claude <claude@duet.local>" in proc.stdout
    assert "Codex <codex@duet.local>" in proc.stdout
    assert list((workspace / ".duet").glob("*.json"))
    assert list((workspace / ".duet").glob("*.md"))
    pytest_proc = subprocess.run(["pytest", "-q"], cwd=workspace, text=True, capture_output=True)
    assert pytest_proc.returncode == 0, pytest_proc.stdout + pytest_proc.stderr
