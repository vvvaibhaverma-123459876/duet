"""Bounded, cancellable subprocess execution for agent CLIs and verifiers.

Output is bounded *at capture*: a flooding child cannot grow Duet's memory past
the configured head+tail window, however much it writes. The child runs in its
own session/process group so timeouts and cancellation can terminate the whole
tree it owns (SIGTERM, a grace period, then SIGKILL) without touching unrelated
processes that merely share a name.
"""
from __future__ import annotations

import collections
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

CHUNK = 65536
DEFAULT_MAX_STDOUT = 8 * 1024 * 1024
DEFAULT_MAX_STDERR = 1024 * 1024
TERM_GRACE_SECONDS = 2.0
DRAIN_GRACE_SECONDS = 2.0


class BoundedBuffer:
    """Keeps the first and last `limit // 2` bytes of a stream and counts the
    rest. Memory stays O(limit + CHUNK) regardless of input volume."""

    def __init__(self, limit: int) -> None:
        if limit < 2:
            raise ValueError("capture limit must be at least 2 bytes")
        self.limit = limit
        self.head_limit = limit // 2
        self.tail_limit = limit - self.head_limit
        self.head = bytearray()
        self._tail: collections.deque[bytes] = collections.deque()
        self._tail_size = 0
        self.total = 0
        self._lock = threading.Lock()

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            self.total += len(chunk)
            if len(self.head) < self.head_limit:
                take = min(len(chunk), self.head_limit - len(self.head))
                self.head += chunk[:take]
                chunk = chunk[take:]
            if not chunk:
                return
            self._tail.append(bytes(chunk))
            self._tail_size += len(chunk)
            # Drop whole chunks from the left while the remainder still covers
            # the tail window; the final trim happens in tail_bytes().
            while self._tail and self._tail_size - len(self._tail[0]) >= self.tail_limit:
                self._tail_size -= len(self._tail.popleft())

    @property
    def truncated(self) -> bool:
        return self.total > self.limit

    def tail_bytes(self) -> bytes:
        data = b"".join(self._tail)
        return data[-self.tail_limit :] if len(data) > self.tail_limit else data

    def text(self) -> str:
        with self._lock:
            head = bytes(self.head)
            tail = self.tail_bytes()
            if not self.truncated:
                return (head + tail).decode("utf-8", errors="replace")
            dropped = self.total - len(head) - len(tail)
            return (
                head.decode("utf-8", errors="replace")
                + f"\n...[{dropped} bytes dropped at capture by Duet]...\n"
                + tail.decode("utf-8", errors="replace")
            )


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    duration_s: float
    output_incomplete: bool  # a reader never reached EOF (pipe held by an escaped descendant)


def run_bounded(
    cmd: list[str],
    *,
    cwd: Path | str | None = None,
    stdin_data: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    max_stdout: int = DEFAULT_MAX_STDOUT,
    max_stderr: int = DEFAULT_MAX_STDERR,
    term_grace: float = TERM_GRACE_SECONDS,
    drain_grace: float = DRAIN_GRACE_SECONDS,
) -> ProcessResult:
    """Run `cmd` with bounded capture and an optional timeout.

    Raises FileNotFoundError/PermissionError when the executable cannot be
    started. On timeout the process tree is terminated and `timed_out` is set.
    Any BaseException while waiting (KeyboardInterrupt, SystemExit) terminates
    the tree and propagates: an interrupted caller never leaves the child
    running."""
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=(os.name != "nt"),
    )
    out_buf = BoundedBuffer(max_stdout)
    err_buf = BoundedBuffer(max_stderr)
    readers = [
        threading.Thread(target=_pump, args=(proc.stdout, out_buf), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, err_buf), daemon=True),
    ]
    for reader in readers:
        reader.start()
    writer = None
    if stdin_data is not None:
        writer = threading.Thread(target=_feed_stdin, args=(proc.stdin, stdin_data), daemon=True)
        writer.start()

    timed_out = False
    try:
        deadline = None if timeout is None else started + max(timeout, 0.0)
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                timed_out = True
                terminate_tree(proc, grace=term_grace)
                break
            try:
                proc.wait(timeout=0.25 if remaining is None else min(remaining, 0.25))
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        terminate_tree(proc, grace=term_grace)
        _join(readers, 0.5)
        raise

    _join(readers, drain_grace)
    if any(reader.is_alive() for reader in readers):
        # The leader exited but something it spawned still holds our pipes.
        # Everything left in the child's process group is ours: kill it.
        _kill_group(proc.pid, signal.SIGKILL)
        _join(readers, 1.0)
    incomplete = any(reader.is_alive() for reader in readers)
    if writer is not None:
        writer.join(timeout=0.5)
    return ProcessResult(
        returncode=proc.returncode,
        stdout=out_buf.text(),
        stderr=err_buf.text(),
        stdout_bytes=out_buf.total,
        stderr_bytes=err_buf.total,
        stdout_truncated=out_buf.truncated,
        stderr_truncated=err_buf.truncated,
        timed_out=timed_out,
        duration_s=time.monotonic() - started,
        output_incomplete=incomplete,
    )


def terminate_tree(proc: subprocess.Popen, grace: float = TERM_GRACE_SECONDS) -> None:
    """SIGTERM the child's process group, wait `grace`, then SIGKILL whatever
    remains. The group is signalled even if the leader already exited, because
    grandchildren can outlive it. The child was started in its own session, so
    its group id is its pid and no unrelated process is addressed."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        try:
            proc.kill()
        except OSError:
            pass
        return
    _kill_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    _kill_group(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL is not ignorable
        pass


def _kill_group(pgid: int, sig: int) -> None:
    if os.name == "nt":  # pragma: no cover
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _pump(stream, buf: BoundedBuffer) -> None:
    try:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            buf.feed(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _feed_stdin(stream, data: str) -> None:
    payload = data.encode("utf-8")
    try:
        view = memoryview(payload)
        while view:
            written = stream.write(view[:CHUNK])
            if not written:
                break
            view = view[written:]
    except (BrokenPipeError, OSError, ValueError):
        pass  # child exited or closed stdin early; its exit status tells the story
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _join(threads: list[threading.Thread], timeout: float) -> None:
    end = time.monotonic() + timeout
    for thread in threads:
        thread.join(timeout=max(0.0, end - time.monotonic()))
