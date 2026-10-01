"""D12: native integration setup is planned, approved and reversible; the
Stop hook asks a native session to answer its peer before it stops; the
status-line wrapper leaves the user's output untouched; no live control is
faked and no replacement agent is spawned."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from exeshim import make_exe
from pairkit import PY, coordinator, live_host, make_repo

from duet.integrations.installer import Installer
from duet.integrations.native import claude_stop_hook
from duet.runtime.identity import ProcessIdentity
from duet.runtime.service import RuntimeService, ServiceClient, ServicePaths

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry" / "claude" / "statusline-docs-full-schema.json"
FAKE_CLI = """import os, sys
state = os.environ["FAKE_MCP_STATE"]
args = sys.argv[1:]
open(os.path.join(state, "calls.log"), "a").write(" ".join(args) + "\\n")
marker = os.path.join(state, os.path.splitext(os.path.basename(sys.argv[0]))[0] + ".duet")
if args[:2] == ["mcp", "get"]:
    sys.exit(0 if os.path.exists(marker) else 1)
if args[:2] == ["mcp", "add"]:
    open(marker, "w").write(" ".join(args)); sys.exit(0)
if args[:2] == ["mcp", "remove"]:
    os.path.exists(marker) and os.remove(marker); sys.exit(0)
sys.exit(2)
"""


@pytest.fixture()
def home(tmp_path, monkeypatch):
    bins, state = tmp_path / "bin", tmp_path / "fake-state"
    bins.mkdir()
    state.mkdir()
    exes = {name: make_exe(bins, name, source=FAKE_CLI) for name in ("claude", "codex")}
    monkeypatch.setenv("DUET_CLAUDE_BIN", str(exes["claude"]))
    monkeypatch.setenv("DUET_CODEX_BIN", str(exes["codex"]))
    monkeypatch.setenv("FAKE_MCP_STATE", str(state))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    settings = tmp_path / "claude-config" / "settings.json"
    settings.parent.mkdir()
    original = {"model": "my-own-choice", "statusLine": {"type": "command", "command": "echo mine", "padding": 1},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-own-hook"}]}]}, "mcpServers": {"other": {"command": "x"}}}
    settings.write_text(json.dumps(original, indent=2))
    return {"settings": settings, "original": original, "state": state, "root": tmp_path / "duet-state"}


def test_setup_is_planned_approved_and_reversible(home):
    installer = Installer(home["root"])
    items = ("claude-mcp", "codex-mcp", "claude-skill", "claude-statusline", "claude-stop-hook")
    plan = installer.plan(items)
    assert [c.action for c in plan] == ["add"] * 5
    assert json.loads(home["settings"].read_text()) == home["original"]  # the plan changed nothing
    assert installer.install(items, yes=False) and installer.status()["installed"] == []  # without --yes: nothing
    installer.install(items, yes=True)
    settings = json.loads(home["settings"].read_text())
    assert settings["model"] == "my-own-choice" and settings["mcpServers"] == {"other": {"command": "x"}}  # the user's own settings stay
    assert "statusline" in settings["statusLine"]["command"] and settings["statusLine"]["padding"] == 1
    assert [e["hooks"][0]["command"] for e in settings["hooks"]["Stop"]][0] == "my-own-hook" and len(settings["hooks"]["Stop"]) == 2
    text = json.dumps(settings)
    assert "permissions" not in settings and "dangerously" not in text and "channel" not in text.lower()  # no bypass, no preview flag
    calls = (home["state"] / "calls.log").read_text()
    assert "mcp add --scope user duet --" in calls and "mcp add duet --" in calls
    assert (Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills" / "duet-pair" / "SKILL.md").exists()
    assert installer.plan(items)[0].detail == "already installed by DUET"
    installer.uninstall(yes=True)
    assert json.loads(home["settings"].read_text()) == home["original"]  # restored exactly
    assert not (Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills" / "duet-pair").exists()
    assert "mcp remove --scope user duet" in (home["state"] / "calls.log").read_text()
    assert installer.status()["installed"] == []


def test_uninstall_leaves_what_the_user_changed_and_what_it_did_not_own(home):
    (home["state"] / "codex.duet").write_text("the user's own duet server")  # present before DUET
    installer = Installer(home["root"])
    changes = {c.item: c for c in installer.install(("codex-mcp", "claude-statusline"), yes=True)}
    assert changes["codex-mcp"].action == "skip" and "did not install" in changes["codex-mcp"].detail
    settings = json.loads(home["settings"].read_text())
    settings["statusLine"] = {"type": "command", "command": "echo changed-later"}
    home["settings"].write_text(json.dumps(settings))
    result = {c.item: c.action for c in installer.uninstall(yes=True)}
    assert result == {"claude-statusline": "keep"}
    assert json.loads(home["settings"].read_text())["statusLine"]["command"] == "echo changed-later"
    assert (home["state"] / "codex.duet").exists()  # never DUET's to remove


def test_integrations_cli_needs_yes(home):
    env = {**os.environ, "DUET_STATE_DIR": str(home["root"])}
    proc = subprocess.run([sys.executable, "-m", "duet", "integrations", "install", "--json"], capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 1 and json.loads(proc.stdout)["applied"] is False
    assert json.loads(home["settings"].read_text()) == home["original"]


def test_statusline_wrapper_keeps_the_users_output_and_records_quota(tmp_path):
    state = tmp_path / "state"
    env = {**os.environ, "DUET_STATE_DIR": str(state)}
    proc = subprocess.run([sys.executable, "-m", "duet", "statusline", "--original", "printf 'my line'"],
                          input=FIXTURE.read_bytes(), capture_output=True, env=env, timeout=60)
    assert proc.returncode == 0 and proc.stdout == b"my line"
    shown = subprocess.run([sys.executable, "-m", "duet", "usage", "--json"], capture_output=True, text=True, env=env, timeout=60)
    gauges = json.loads(shown.stdout)["quota"]["gauges"]
    assert gauges and all(g["provider"] == "claude" and g["source"] == "claude.statusline" for g in gauges)


def test_stop_hook_asks_the_session_to_answer_its_peer(tmp_path):
    paths = ServicePaths.for_root(tmp_path / "state")
    service = RuntimeService(paths, idle_exit_seconds=None).start()
    try:
        client = ServiceClient(paths)
        repo = make_repo(tmp_path / "repo")
        started = client.call("join", provider="claude", objective="x", repo=str(repo), checks=[[PY, "check_feature.py"]], peer="invite",
                              host=str(ProcessIdentity.of(os.getppid())))
        codex = client.with_token(client.call("join", provider="codex", run_id=started["run_id"], invite=started["invite"]["code"], host=live_host())["token"])
        host = str(ProcessIdentity.of(os.getppid()))
        sessions = paths.root / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        import hashlib

        (sessions / (hashlib.sha256(host.encode()).hexdigest()[:32] + ".json")).write_text(json.dumps({"token": started["token"]}))
        assert claude_stop_hook("{}", paths) is None  # nothing pending: stop freely
        question = codex.call("send", kind="QUESTION", body="ints?")
        blocked = claude_stop_hook(json.dumps({"session_id": "s", "stop_hook_active": False}), paths)
        assert blocked["decision"] == "block" and "QUESTION" in blocked["reason"]
        assert claude_stop_hook(json.dumps({"stop_hook_active": True}), paths) is None  # never loops
        client.with_token(started["token"]).call("send", kind="ANSWER", body="ints", reply_to=question["message_id"])
        assert claude_stop_hook("{}", paths) is None
    finally:
        service.close()


def test_an_unavailable_native_peer_is_never_replaced(tmp_path):
    """No live control for the native session: when it goes away the run
    shows the peer unavailable; DUET does not launch a substitute."""
    launched = []
    co = coordinator(tmp_path / "state", peer_launcher=lambda run_id, provider: launched.append(provider))
    started = co.join(None, provider="claude", objective="x", repo=str(make_repo(tmp_path / "repo")), checks=[[PY, "check_feature.py"]], peer="invite", host=live_host())
    joined = co.join(None, provider="codex", run_id=started["run_id"], invite=started["invite"]["code"], host=str(ProcessIdentity.of(os.getppid())))
    co.disconnect(co.runtime.authenticate(joined["token"]))
    assert co.run_status(started["run_id"])["collaboration"] == "PEER_UNAVAILABLE" and launched == []


def test_capabilities_disclose_what_duet_does_not_control(home):
    """AT06/AT43/AT45: no live delivery is claimed, native model/effort is
    advisory, and native subagents are disclosed as outside DUET's control."""
    env = {**os.environ, "DUET_STATE_DIR": str(home["root"])}
    proc = subprocess.run([sys.executable, "-m", "duet", "capabilities", "--json"], capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    for provider in ("claude", "codex"):
        entry = report["providers"][provider]
        assert entry["live_delivery"].startswith("unavailable")
        assert entry["native"]["model_effort"].startswith("advisory")
        assert entry["native"]["subagents"].startswith("not observed")
        assert "subagents" in entry["managed"]
    human = subprocess.run([sys.executable, "-m", "duet", "capabilities"], capture_output=True, text=True, env=env, timeout=60)
    assert human.returncode == 0 and "live delivery: unavailable" in human.stdout and "subagents:" in human.stdout
