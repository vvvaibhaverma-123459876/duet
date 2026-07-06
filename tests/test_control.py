from __future__ import annotations

import os
import signal
from pathlib import Path

from duet.control import StopTarget, running_targets, stop_target

PS = """\
  101 ttys001    01:02:03 /Users/x/.local/bin/claude
  202 ttys002       05:10 /usr/local/bin/codex --ask-for-approval never
  303 ttys003       00:01 vim notes.txt
  404 ??           10:00 /Users/x/.local/bin/claude -p
"""


def write_lock(repo: Path, pid: int) -> None:
    duet_dir = repo / ".duet"
    duet_dir.mkdir(parents=True, exist_ok=True)
    (duet_dir / "session.lock").write_text(f"pid={pid}\ncreated=123\n")


class TestRunningTargets:
    def test_finds_agents_and_duet_lock(self, tmp_path):
        write_lock(tmp_path, os.getpid())  # alive pid so the lock counts
        targets = running_targets(tmp_path, ps_output=PS, self_pid=999999)
        kinds = [(t.kind, t.pid) for t in targets]
        assert ("duet", os.getpid()) in kinds
        assert ("claude", 101) in kinds
        assert ("codex", 202) in kinds
        assert ("claude", 404) in kinds
        assert all(kind != "vim" for kind, _ in kinds)

    def test_excludes_own_process(self, tmp_path):
        targets = running_targets(tmp_path, ps_output=PS, self_pid=101)
        assert all(t.pid != 101 for t in targets)

    def test_dead_lock_pid_ignored(self, tmp_path):
        write_lock(tmp_path, 2 ** 30)  # certainly not alive
        targets = running_targets(tmp_path, ps_output="", self_pid=1)
        assert targets == []

    def test_no_lock_no_processes(self, tmp_path):
        assert running_targets(tmp_path, ps_output="", self_pid=1) == []


class TestStopTarget:
    def test_sends_sigint_by_default(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(os, "kill", lambda pid, sig: sent.update(pid=pid, sig=sig))
        message = stop_target(StopTarget("codex", 4242, "tty=x"))
        assert sent == {"pid": 4242, "sig": signal.SIGINT}
        assert "SIGINT" in message

    def test_force_sends_sigterm(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(os, "kill", lambda pid, sig: sent.update(pid=pid, sig=sig))
        stop_target(StopTarget("codex", 4242, "tty=x"), force=True)
        assert sent["sig"] == signal.SIGTERM

    def test_gone_process_reported(self, monkeypatch):
        def raise_lookup(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(os, "kill", raise_lookup)
        assert "already gone" in stop_target(StopTarget("claude", 4242, "tty=x"))
