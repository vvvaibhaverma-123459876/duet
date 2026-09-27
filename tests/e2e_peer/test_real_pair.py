"""Optional real pair test (spec D05: "a real pair test is required for the
peer-alpha label; fixtures alone do not earn it").

`duet pair` launches the real Claude Code and Codex CLIs on a tiny repository
with the user's existing logins, and the two sessions must agree on and ship
one reviewed patch. It consumes plan usage from both accounts and never
enables paid fallback (no API keys, no --bare). It is NOT_RUN unless a person
sets DUET_REAL_PAIR=1 on a machine where both CLIs are installed, logged in
and able to reach their providers.

This covers AT03 (DUET-originated) only. The native-origin tests AT01/AT02
need a person driving an existing Claude or Codex session; see
docs/duet-v2/PEER_ALPHA_TEST.md."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DUET_REAL_PAIR") != "1", reason="set DUET_REAL_PAIR=1 to run a real, account-consuming pair"),
]
RECORD = os.environ.get("DUET_REAL_PAIR_RECORD")


def test_real_managed_pair_ships_one_reviewed_patch(tmp_path):
    for binary in ("claude", "codex"):
        if shutil.which(binary) is None:
            pytest.skip(f"{binary} not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "u@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "User"], cwd=repo, check=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "check_feature.py").write_text("import calc\nassert calc.mul(3, 4) == 12\nassert calc.mul(-2, 5) == -10\nprint('ok')\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    env = dict(os.environ, DUET_STATE_DIR=str(tmp_path / "state"))
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DUET_CLAUDE_BIN", "DUET_CODEX_BIN", "DUET_MANAGED_PEER"):
        env.pop(key, None)  # never let an API key turn this into paid API usage
    proc = subprocess.run(
        [sys.executable, "-m", "duet", "pair", "Add mul(a, b) to calc.py returning the product.", "--repo", str(repo),
         "--check", f"{sys.executable} check_feature.py", "--protect", "check_feature.py", "--json"],
        env=env, capture_output=True, text=True, timeout=1800,
    )
    if RECORD:
        Path(RECORD).mkdir(parents=True, exist_ok=True)
        (Path(RECORD) / "real-pair.json").write_text(json.dumps({"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr[-20000:]}, indent=2))
    assert proc.returncode == 0, proc.stderr[-4000:]
    status = json.loads(proc.stdout)
    assert status["lifecycle"] == "COMPLETED_VERIFIED"
    assert {p["provider"] for p in status["participants"]} == {"claude", "codex"}
    assert {p["origin"] for p in status["participants"]} == {"managed"}
