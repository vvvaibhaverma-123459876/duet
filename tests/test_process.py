"""D01: bounded capture at source and owned-process-tree termination."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from duet.providers.process import BoundedBuffer, run_bounded, stream_process

pytestmark = pytest.mark.skipif(os.name == "nt", reason="process-group semantics are POSIX-only here")


def _wait_dead(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _alive(pid)


def _describe(pid: int) -> str:
    """State, parent, process group and session of a survivor, so a failure
    explains itself (it has never reproduced locally)."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
        return f"{comm} state={fields[0]} ppid={fields[1]} pgrp={fields[2]} sid={fields[3]}"
    except OSError as exc:
        return f"unreadable: {exc}"


def _reap(pid: int) -> None:
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); treat reaped-or-zombie as dead.
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            return "State:\tZ" not in handle.read()
    except OSError:
        return True


class TestBoundedBuffer:
    def test_small_input_is_kept_verbatim(self):
        buf = BoundedBuffer(100)
        buf.feed(b"hello ")
        buf.feed(b"world")
        assert buf.text() == "hello world"
        assert not buf.truncated

    def test_flood_keeps_head_and_tail_only(self):
        buf = BoundedBuffer(1000)
        for i in range(10000):
            buf.feed(f"line-{i:06d}\n".encode())
        text = buf.text()
        assert buf.truncated
        assert text.startswith("line-000000")
        assert text.rstrip().endswith("line-009999")
        assert "bytes dropped at capture" in text
        assert len(text) < 1200
        # Internal storage never grows past the window plus one chunk.
        assert len(buf.head) <= 500
        assert sum(len(chunk) for chunk in buf._tail) <= 500 + len(b"line-009999\n")

    def test_invalid_utf8_does_not_raise(self):
        buf = BoundedBuffer(100)
        buf.feed(b"\xff\xfeok")
        assert "ok" in buf.text()


class TestRunBounded:
    def test_captures_stdout_stderr_and_exit_code(self, tmp_path):
        result = run_bounded(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
            cwd=tmp_path,
            timeout=30,
        )
        assert result.returncode == 3
        assert result.stdout.strip() == "out"
        assert result.stderr.strip() == "err"
        assert not result.timed_out

    def test_stdin_is_delivered(self, tmp_path):
        result = run_bounded(
            [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
            cwd=tmp_path,
            stdin_data="ping " * 50000,  # larger than a pipe buffer
            timeout=30,
        )
        assert result.stdout.startswith("PING PING")

    def test_flooding_child_is_bounded_at_capture(self, tmp_path):
        # 64 MiB of output against a 256 KiB window.
        code = "import sys\nchunk = b'x' * 65536\nfor _ in range(1024): sys.stdout.buffer.write(chunk)\n"
        result = run_bounded([sys.executable, "-c", code], cwd=tmp_path, timeout=60, max_stdout=256 * 1024)
        assert result.returncode == 0
        assert result.stdout_bytes == 64 * 1024 * 1024
        assert result.stdout_truncated
        assert len(result.stdout) < 300 * 1024

    def test_timeout_terminates_the_whole_tree(self, tmp_path):
        pidfile = tmp_path / "grandchild.pid"
        script = f"sleep 60 & echo $! > {pidfile}; wait"
        started = time.monotonic()
        result = run_bounded(["/bin/sh", "-c", script], cwd=tmp_path, timeout=1, term_grace=0.5)
        assert result.timed_out
        assert time.monotonic() - started < 10
        grandchild = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(grandchild), "grandchild survived the timeout kill"

    def test_orphan_holding_the_pipe_does_not_block(self, tmp_path):
        # The leader exits at once, but a background grandchild inherits stdout
        # and would keep the pipe open for 60 s. communicate() would wait for it.
        pidfile = tmp_path / "orphan.pid"
        script = f"sleep 60 & echo $! > {pidfile}; echo done"
        started = time.monotonic()
        result = run_bounded(["/bin/sh", "-c", script], cwd=tmp_path, timeout=30, drain_grace=0.5)
        assert time.monotonic() - started < 10
        assert result.returncode == 0
        assert "done" in result.stdout
        orphan = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while _alive(orphan) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(orphan)

    def test_detached_grandchild_is_killed_after_normal_exit(self, tmp_path):
        # Review finding: the group was only killed on timeout or while a
        # reader was blocked. A background grandchild with its stdio redirected
        # away never holds our pipes, so it survived a normal leader exit.
        pidfile = tmp_path / "bg.pid"
        script = f"sleep 30 </dev/null >/dev/null 2>&1 & echo $! > {pidfile}; echo done"
        result = run_bounded(["/bin/sh", "-c", script], cwd=tmp_path, timeout=30, drain_grace=0.5)
        grandchild = int(pidfile.read_text())
        try:
            assert result.returncode == 0 and "done" in result.stdout
            assert not result.output_incomplete  # the pipes were never held
            assert _wait_dead(grandchild), "background grandchild outlived run_bounded"
        finally:
            _reap(grandchild)

    def test_keyboard_interrupt_kills_child_and_propagates(self, tmp_path, monkeypatch):
        pidfile = tmp_path / "child.pid"
        calls = {"n": 0}
        real_wait = subprocess.Popen.wait

        def interrupting_wait(self, timeout=None):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
            return real_wait(self, timeout=timeout)

        monkeypatch.setattr(subprocess.Popen, "wait", interrupting_wait)
        with pytest.raises(KeyboardInterrupt):
            run_bounded(["/bin/sh", "-c", f"echo $$ > {pidfile}; sleep 60"], cwd=tmp_path, timeout=30, term_grace=0.5)
        monkeypatch.undo()
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child)

    def test_missing_executable_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            run_bounded([str(tmp_path / "does-not-exist")], cwd=tmp_path, timeout=5)

    def test_child_runs_in_its_own_process_group(self, tmp_path):
        result = run_bounded(
            [sys.executable, "-c", "import os; print(os.getpgid(0) == os.getpid())"], cwd=tmp_path, timeout=10
        )
        assert result.stdout.strip() == "True"  # the child leads its own process group


def test_stdin_defaults_to_devnull(tmp_path: Path):
    # Arg-mode agents must not inherit (and block on) Duet's own stdin.
    result = run_bounded([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], cwd=tmp_path, timeout=10)
    assert result.stdout.strip() == "''"


class TestStreamProcess:
    """Review finding: with interrupt_first, escalation required a live
    leader, so a SIGINT-ignoring descendant holding stdout after the leader
    exited kept stream_process (and its timeout) waiting indefinitely."""

    @staticmethod
    def _script(pidfile: Path) -> str:
        # The leader prints a line and exits; a descendant that ignores SIGINT
        # inherits stdout and keeps it open for 30 s.
        return f"(trap '' INT; sleep 30) & echo $! > {pidfile}; echo started"

    def test_lingering_descendant_is_killed_after_drain_grace(self, tmp_path):
        pidfile = tmp_path / "desc.pid"
        lines: list[str] = []
        started = time.monotonic()
        result = stream_process(
            ["/bin/sh", "-c", self._script(pidfile)], on_line=lines.append, cwd=tmp_path,
            timeout=30, interrupt_first=True, term_grace=0.5, drain_grace=0.5,
        )
        descendant = int(pidfile.read_text())
        try:
            assert time.monotonic() - started < 10
            assert lines == ["started"] and result.returncode == 0 and not result.timed_out
            assert _wait_dead(descendant)
        finally:
            _reap(descendant)

    def test_timeout_escalates_after_leader_exit(self, tmp_path):
        # With a long drain grace the only way out is the deadline: SIGINT is
        # ignored, the leader is already gone, and escalation must still happen.
        pidfile = tmp_path / "desc.pid"
        started = time.monotonic()
        result = stream_process(
            ["/bin/sh", "-c", self._script(pidfile)], on_line=lambda line: None, cwd=tmp_path,
            timeout=2, interrupt_first=True, term_grace=0.5, drain_grace=60,
        )
        descendant = int(pidfile.read_text())
        try:
            assert result.timed_out
            assert time.monotonic() - started < 10, "timeout was ignored"
            assert _wait_dead(descendant)
        finally:
            _reap(descendant)

    def test_detached_descendant_is_killed_after_normal_exit(self, tmp_path):
        pidfile = tmp_path / "bg.pid"
        script = f"sleep 30 </dev/null >/dev/null 2>&1 & echo $! > {pidfile}; echo done"
        lines: list[str] = []
        result = stream_process(["/bin/sh", "-c", script], on_line=lines.append, cwd=tmp_path, timeout=30)
        grandchild = int(pidfile.read_text())
        try:
            assert lines == ["done"] and result.returncode == 0
            assert _wait_dead(grandchild), f"grandchild survived: {_describe(grandchild)}"
        finally:
            _reap(grandchild)
