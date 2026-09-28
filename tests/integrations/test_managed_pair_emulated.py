"""D05, AT03 shape, simulated providers: `duet pair` launches a managed
Claude (ClaudeCLIAdapter, stream-json) and a managed Codex (app-server
JSON-RPC) whose binaries are emulators. Each emulator acts through the DUET
MCP server DUET configured for it, so the whole path is real except the
model: CLI -> service -> peer drivers -> provider adapters -> provider
process -> `duet mcp serve` -> service.

This earns no peer-alpha label: fixtures and emulators never do."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from exeshim import make_exe

pytest.importorskip("mcp")
from pairkit import MUL, PY, git, make_repo  # noqa: E402

from duet.runtime.service import ServiceClient, ServicePaths, service_running  # noqa: E402
from duet.runtime.store import Store  # noqa: E402

EMULATORS = Path(__file__).resolve().parents[1] / "providers" / "emulators"


def shim(bin_dir: Path, name: str, script: str) -> Path:
    return make_exe(bin_dir, name, script=EMULATORS / script, python=PY)


def test_duet_pair_with_emulated_managed_sessions(tmp_path):
    repo = make_repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    env = dict(
        os.environ,
        DUET_STATE_DIR=str(state),
        DUET_CLAUDE_BIN=str(shim(bin_dir, "claude", "fake_claude.py")),
        DUET_CODEX_BIN=str(shim(bin_dir, "codex", "fake_codex_appserver.py")),
        FAKE_CLAUDE_MODE="peer",
        FAKE_CODEX_MODE="peer",
        FAKE_BRAIN_STATE=str(tmp_path / "brain"),
    )
    env.pop("DUET_MANAGED_PEER", None)
    paths = ServicePaths.for_root(state / "v2")
    for pool in (
        ["claude-turns", "--provider", "claude", "--metric", "turns", "--allowance", "10"],
        ["codex-turns", "--provider", "codex", "--metric", "turns", "--allowance", "10"],
        ["claude-cost", "--provider", "claude", "--metric", "cost.estimated_usd", "--allowance", "1.00"],
    ):
        defined = subprocess.run([sys.executable, "-m", "duet", "usage", "pool", "set", *pool], env=env, capture_output=True, text=True, timeout=60)
        assert defined.returncode == 0, defined.stderr
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "duet", "pair", "add mul(a, b) to calc.py", "--repo", str(repo),
             "--check", f'"{PY}" check_feature.py', "--writer", "claude", "--json"],
            env=env, capture_output=True, text=True, timeout=300, cwd=tmp_path,
        )
        assert proc.returncode == 0, proc.stderr[-3000:] + proc.stdout[-3000:]
        status = json.loads(proc.stdout)
        assert status["lifecycle"] == "COMPLETED_VERIFIED"
        assert {p["origin"] for p in status["participants"]} == {"managed"}
        assert {p["provider"]: p["role"] for p in status["participants"]} == {"claude": "writer", "codex": "reviewer"}
        assert all(item["ok"] for item in status["verification"]["completion"]["items"])
        assert "DUET-managed" in proc.stderr  # the command says it is not a native-origin test

        run_id = status["run_id"]
        store = Store(paths.db)
        tx = store.read()
        messages = tx.query("SELECT kind, sender, recipient, reply_to, body FROM messages WHERE run_id = ? AND sender != 'controller' ORDER BY seq", (run_id,))
        providers = {r["participant_id"]: r["provider"] for r in tx.query("SELECT participant_id, provider FROM participants WHERE run_id = ?", (run_id,))}
        convo = [(providers[m["sender"]], m["kind"]) for m in messages]
        # A asks B; B asks A; A answers; B answers; A implements; B reviews.
        assert convo == [
            ("claude", "QUESTION"), ("codex", "QUESTION"), ("claude", "ANSWER"), ("codex", "ANSWER"),
            ("claude", "REVIEW_REQUEST"), ("codex", "REVIEW_RESULT"),
        ], convo
        assert not any(m["body"].startswith("[DUET: delivered from the end") for m in messages)
        turns = tx.query("SELECT participant_id, state, result_json FROM actions WHERE run_id = ? AND type = 'provider_turn' ORDER BY created_at", (run_id,))
        assert [providers[t["participant_id"]] for t in turns] == ["claude", "codex", "claude", "codex", "claude", "codex"]
        assert all(t["state"] == "SUCCEEDED" for t in turns)
        claude_sessions = {json.loads(t["result_json"])["session_id"] for t in turns if providers[t["participant_id"]] == "claude"}
        assert len(claude_sessions) == 1  # every Claude turn resumed the same managed session
        codex_lineage = [json.loads(t["result_json"])["lineage"] for t in turns if providers[t["participant_id"]] == "codex"]
        assert codex_lineage[0] == "new" and set(codex_lineage[1:]) == {"resumed_same"}
        branch = git("for-each-ref", "--format=%(refname:short)", "refs/heads/duet/", cwd=repo).splitlines()[0]
        assert git("show", f"{branch}:calc.py", cwd=repo) == MUL.rstrip("\n")
        assert git("status", "--porcelain", cwd=repo) == ""
        assert store.verify_replay() == []
        store.close()
        usage = json.loads(subprocess.run([sys.executable, "-m", "duet", "usage", "--run", run_id, "--json"], env=env, capture_output=True, text=True, timeout=60).stdout)
        pools = {p["pool_id"]: p for p in usage["pools"]}
        assert pools["claude-turns"]["used"] == "3" and pools["codex-turns"]["used"] == "3"
        # Cumulative session cost (0.01, 0.02, 0.03) became per-turn deltas, recorded as estimates.
        assert Decimal(pools["claude-cost"]["used"]) == Decimal("0.03") and not pools["claude-cost"]["uncertain"]
        assert {r["quality"] for r in usage["run"]["records"] if r["pool_id"] == "claude-cost"} == {"estimated"}
    finally:
        if service_running(paths):
            ServiceClient.as_controller(paths).call("shutdown")


def test_duet_pair_shares_a_plan_and_both_contribute(tmp_path):
    """D06 through the full path: the managed writer proposes a plan giving
    the reviewer an investigation task; the reviewer accepts the plan, is
    routed the task by the scheduler, completes it; the writer accepts the
    result, implements, and the reviewer approves the snapshot."""
    repo = make_repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    env = dict(
        os.environ, DUET_STATE_DIR=str(state),
        DUET_CLAUDE_BIN=str(shim(bin_dir, "claude", "fake_claude.py")), DUET_CODEX_BIN=str(shim(bin_dir, "codex", "fake_codex_appserver.py")),
        FAKE_CLAUDE_MODE="peer", FAKE_CODEX_MODE="peer", FAKE_BRAIN_MODE="plan", FAKE_BRAIN_STATE=str(tmp_path / "brain"),
    )
    env.pop("DUET_MANAGED_PEER", None)
    paths = ServicePaths.for_root(state / "v2")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "duet", "pair", "add mul(a, b) to calc.py", "--repo", str(repo), "--check", f'"{PY}" check_feature.py', "--json"],
            env=env, capture_output=True, text=True, timeout=300, cwd=tmp_path,
        )
        assert proc.returncode == 0, proc.stderr[-3000:] + proc.stdout[-3000:]
        status = json.loads(proc.stdout)
        assert status["lifecycle"] == "COMPLETED_VERIFIED"
        kinds = {(c["provider"], c["kind"]) for c in status["contributions"]}
        assert {("claude", "plan"), ("claude", "code"), ("codex", "investigate"), ("codex", "review")} <= kinds, kinds
        tasks = {t["kind"]: t for t in status["tasks"]}
        assert tasks["investigate"]["state"] == "VERIFIED" and tasks["code"]["state"] == "VERIFIED"
        owners = {p["participant_id"]: p["provider"] for p in status["participants"]}
        assert owners[tasks["investigate"]["owner"]] == "codex" and owners[tasks["code"]["owner"]] == "claude"  # no duplicated work
        assert status["plans"][0]["state"] == "ACCEPTED" and status["interventions"] == []
    finally:
        if service_running(paths):
            ServiceClient.as_controller(paths).call("shutdown")
