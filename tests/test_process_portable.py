"""Process-tree guarantees on every platform (the Windows port relies on
them; tests/test_process.py covers the POSIX signal details). The trees are
built in Python so the same test runs on Linux, macOS and Windows: DUET
must never leave a descendant of a child it started running."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from duet import oscompat
from duet.providers.process import run_bounded, stream_process
from duet.runtime.identity import process_start

PY = sys.executable


def spawner(pidfile: Path, *, then: str) -> list[str]:
    """A child that starts a long-lived grandchild (detached from its
    output), records the grandchild's pid, then runs `then`."""
    code = (
        "import subprocess, sys, time\n"
        f"g = subprocess.Popen([{PY!r}, '-c', 'import time; time.sleep(60)'],"
        " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(pidfile)!r}, 'w').write(str(g.pid))\n"
        "print('started', flush=True)\n"
        f"{then}\n"
    )
    return [PY, "-c", code]


def grandchild(pidfile: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pidfile.exists() and pidfile.read_text().strip():
            return int(pidfile.read_text())
        time.sleep(0.05)
    raise AssertionError("the grandchild never started")


def gone(pid: int, within: float = 10.0) -> bool:
    """Exited: no such pid, or (POSIX) a zombie. An orphan's zombie stays
    until PID 1 reaps it, and in a container PID 1 may never do so."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not oscompat.pid_exists(pid) or process_start(pid) is None:
            return True
        time.sleep(0.1)
    return False


def test_timeout_kills_the_whole_tree(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    result = run_bounded(spawner(pidfile, then="time.sleep(60)"), cwd=tmp_path, timeout=3, term_grace=0.5)
    assert result.timed_out
    assert gone(grandchild(pidfile)), "the grandchild outlived the timeout"


def test_a_descendant_of_an_exited_child_is_killed(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    result = run_bounded(spawner(pidfile, then="sys.exit(0)"), cwd=tmp_path, timeout=30, drain_grace=0.5)
    assert result.returncode == 0
    assert gone(grandchild(pidfile)), "a descendant outlived its exited parent"


def test_streaming_timeout_kills_the_whole_tree(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    lines: list[str] = []
    result = stream_process(spawner(pidfile, then="time.sleep(60)"), on_line=lines.append, cwd=tmp_path, timeout=3, term_grace=0.5)
    assert result.timed_out and any("started" in line for line in lines)
    assert gone(grandchild(pidfile))


def test_pid_exists_never_signals(tmp_path):
    """On Windows os.kill(pid, 0) would terminate the process."""
    import subprocess

    proc = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
    try:
        for _ in range(3):
            assert oscompat.pid_exists(proc.pid)
        time.sleep(0.3)
        assert proc.poll() is None, "checking existence killed the process"
    finally:
        proc.kill()
        proc.wait()
    assert not oscompat.pid_exists(-1) and not oscompat.pid_exists(0)


def test_file_lock_is_exclusive(tmp_path):
    import subprocess

    lock = tmp_path / "lock"
    with open(lock, "a+") as handle:
        oscompat.lock_file(handle, blocking=False)
        probe = (
            "import sys\nfrom duet import oscompat\n"
            f"h = open({str(lock)!r}, 'a+')\n"
            "try:\n    oscompat.lock_file(h, blocking=False)\nexcept oscompat.LockBusy:\n    sys.exit(3)\nsys.exit(0)\n"
        )
        assert subprocess.run([PY, "-c", probe], timeout=30).returncode == 3
        oscompat.unlock_file(handle)
    assert subprocess.run([PY, "-c", probe], timeout=30).returncode == 0


def test_split_and_join_round_trip_paths_with_spaces(tmp_path):
    argv = [str(tmp_path / "dir with space" / "prog"), "--flag", "value with space"]
    assert oscompat.split_command(oscompat.join_command(argv)) == argv
