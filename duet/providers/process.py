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

from .. import oscompat

CHUNK = 65536
DEFAULT_MAX_STDOUT = 8 * 1024 * 1024
DEFAULT_MAX_STDERR = 1024 * 1024
TERM_GRACE_SECONDS = 2.0
DRAIN_GRACE_SECONDS = 2.0

# Environment variables that make a provider CLI authenticate with an API key
# or a cloud account, i.e. bill metered API usage, instead of the user's own
# CLI login. A managed peer must never switch to paid usage on its own (R13),
# so provider adapters remove them from the child environment by default.
PROVIDER_CREDENTIAL_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "AZURE_OPENAI_API_KEY",
)


def provider_child_env(
    *overlays: dict[str, str] | None, allow_api_key_env: bool = False
) -> tuple[dict[str, str], tuple[str, ...]]:
    """The environment for a provider CLI child: Duet's own environment plus
    `overlays`, minus provider credential variables unless the user explicitly
    opted into API billing. Returns (env, removed variable names). Only names
    are reported; values never leave this function."""
    env = dict(os.environ)
    for overlay in overlays:
        env.update(overlay or {})
    if allow_api_key_env:
        return env, ()
    removed = tuple(name for name in PROVIDER_CREDENTIAL_ENV if name in env)
    for name in removed:
        del env[name]
    return env, removed


def credential_env_warning(removed: tuple[str, ...]) -> str:
    return (
        f"removed provider credential variable(s) {', '.join(removed)} from the child environment so the CLI "
        "uses its own login, not API billing (allow_api_key_env=True opts into API billing)"
    )


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
    running. After a normal exit of the leader, whatever is left in its
    process group is killed as well, so no background descendant outlives
    the call."""
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        **oscompat.own_group_kwargs(),
    )
    _mark_launch(proc)
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
    # The leader exited. Anything still in the child's process group is ours
    # and must not outlive the call: a descendant holding our pipes, and also
    # one that redirected its stdio away (`cmd >/dev/null 2>&1 &`), which no
    # pipe would ever reveal.
    _clear_group(proc.pid, since=getattr(proc, "_duet_launched_at", None))
    if any(reader.is_alive() for reader in readers):
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
    if oscompat.IS_WINDOWS:  # pragma: no cover - exercised on Windows only
        # No SIGTERM on Windows: kill the child's whole tree, including
        # descendants of an already-exited leader (never re-parented there).
        oscompat.kill_tree(proc.pid, since=getattr(proc, "_duet_launched_at", None), timeout=grace)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
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


def _clear_group(pgid: int, within: float = 0.5, since: float | None = None) -> None:
    """SIGKILL what is left of an exited leader's process group, and keep
    re-sending (bounded by `within`) while members remain, so one that was
    not yet scheduled, or mid-fork, when the first signal went out cannot
    survive. Call only after the leader was reaped: members awaiting their
    reaper (zombies) still count, and the loop then just runs out."""
    if oscompat.IS_WINDOWS:  # pragma: no cover
        # The leader was reaped: kill what it left behind.
        oscompat.kill_tree(pgid, since=None if since is None else since, timeout=within)
        return
    deadline = time.monotonic() + within
    while True:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(0.02)


def _kill_group(pgid: int, sig: int) -> None:
    oscompat.signal_group(pgid, sig)


def _mark_launch(proc: subprocess.Popen) -> None:
    """Remember when the child started, so a Windows tree kill never takes a
    process older than the child (a reused pid) for one of its descendants."""
    try:
        proc._duet_launched_at = time.time() - 1.0  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover
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


@dataclass(frozen=True)
class StreamResult:
    returncode: int | None
    stderr: str
    timed_out: bool
    cancelled: bool
    lines: int
    bytes_read: int
    truncated_lines: int
    over_limit: bool  # stopped dispatching lines after max_total_bytes
    duration_s: float


def stream_process(
    cmd: list[str],
    *,
    on_line,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    stdin_data: str | None = None,
    timeout: float | None = None,
    cancel_event: threading.Event | None = None,
    max_line_bytes: int = 4 * 1024 * 1024,
    max_total_bytes: int = 64 * 1024 * 1024,
    max_stderr: int = DEFAULT_MAX_STDERR,
    interrupt_first: bool = False,
    term_grace: float = TERM_GRACE_SECONDS,
    drain_grace: float = DRAIN_GRACE_SECONDS,
) -> StreamResult:
    """Run `cmd` and hand each stdout line to `on_line` as it arrives, in the
    caller's thread. Lines longer than `max_line_bytes` are truncated (and
    counted); after `max_total_bytes` lines stop being dispatched but output
    is still drained so the child cannot block on a full pipe.

    On timeout or cancellation the child's process group gets SIGINT first
    when `interrupt_first` (CLIs such as Claude Code end the current turn
    cleanly on SIGINT and still emit a final result), then SIGTERM/SIGKILL
    after `term_grace`, whether or not the leader is still alive: a
    descendant that ignores SIGINT can hold stdout after the leader exited.
    Lines that arrive during that grace period are still dispatched.

    When the leader exits, descendants get `drain_grace` to finish writing;
    then the rest of the process group is killed so a lingering descendant
    that holds stdout cannot keep the call (and its timeout) waiting."""
    import queue

    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        **oscompat.own_group_kwargs(),
    )
    _mark_launch(proc)
    lines: "queue.Queue[tuple[bytes, bool] | None]" = queue.Queue(maxsize=1024)
    counters = {"bytes": 0, "truncated": 0}
    err_buf = BoundedBuffer(max_stderr)

    def read_lines() -> None:
        pending = bytearray()
        overflowing = False
        try:
            while True:
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    break
                counters["bytes"] += len(chunk)
                start = 0
                while True:
                    newline = chunk.find(b"\n", start)
                    piece = chunk[start:] if newline < 0 else chunk[start:newline]
                    if not overflowing:
                        room = max_line_bytes - len(pending)
                        if len(piece) > room:
                            pending += piece[:room]
                            overflowing = True
                        else:
                            pending += piece
                    if newline < 0:
                        break
                    lines.put((bytes(pending), overflowing))
                    if overflowing:
                        counters["truncated"] += 1
                    pending = bytearray()
                    overflowing = False
                    start = newline + 1
        except (OSError, ValueError):
            pass
        finally:
            if pending:
                lines.put((bytes(pending), overflowing))
                if overflowing:
                    counters["truncated"] += 1
            lines.put(None)

    reader = threading.Thread(target=read_lines, daemon=True)
    err_reader = threading.Thread(target=_pump, args=(proc.stderr, err_buf), daemon=True)
    reader.start()
    err_reader.start()
    if stdin_data is not None:
        threading.Thread(target=_feed_stdin, args=(proc.stdin, stdin_data), daemon=True).start()

    deadline = None if timeout is None else started + max(timeout, 0.0)
    timed_out = cancelled = over_limit = False
    stop_requested_at: float | None = None
    leader_exited_at: float | None = None
    group_killed_at: float | None = None
    dispatched_bytes = 0
    count = 0
    try:
        while True:
            now = time.monotonic()
            if stop_requested_at is None:
                if deadline is not None and now >= deadline:
                    timed_out = True
                elif cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                if timed_out or cancelled:
                    stop_requested_at = now
                    if interrupt_first:
                        _kill_group(proc.pid, signal.SIGINT)
                    else:
                        terminate_tree(proc, grace=term_grace)
                        group_killed_at = time.monotonic()
            elif group_killed_at is None and now - stop_requested_at > term_grace:
                # Escalate regardless of the leader: if it already exited, a
                # descendant that ignored SIGINT may still hold stdout.
                terminate_tree(proc, grace=term_grace)
                group_killed_at = time.monotonic()
            if leader_exited_at is None and proc.poll() is not None:
                leader_exited_at = now
            if group_killed_at is None and leader_exited_at is not None and now - leader_exited_at > drain_grace and reader.is_alive():
                # The leader is gone but something it spawned still holds
                # stdout. Everything left in the child's process group is ours.
                _kill_group(proc.pid, signal.SIGKILL)
                group_killed_at = now
            try:
                item = lines.get(timeout=0.1)
            except queue.Empty:
                if proc.poll() is not None and not reader.is_alive():
                    break
                if group_killed_at is not None and time.monotonic() - group_killed_at > drain_grace:
                    # Killed the whole group and stdout is still open: a
                    # process that left the group holds it. Stop waiting.
                    break
                continue
            if item is None:
                break
            raw, _ = item
            dispatched_bytes += len(raw) + 1
            if dispatched_bytes > max_total_bytes:
                over_limit = True
                continue
            count += 1
            on_line(raw.decode("utf-8", errors="replace"))
    except BaseException:
        terminate_tree(proc, grace=term_grace)
        raise
    if stop_requested_at is not None:
        wait_for = term_grace * 2
    elif deadline is None:
        wait_for = 30.0
    else:
        # stdout closed but the leader may still run: keep honouring the deadline.
        wait_for = min(30.0, max(1.0, deadline - time.monotonic()))
    try:
        proc.wait(timeout=wait_for)
    except subprocess.TimeoutExpired:
        if stop_requested_at is None and deadline is not None and time.monotonic() >= deadline:
            timed_out = True
        terminate_tree(proc, grace=term_grace)
    if proc.poll() is None:  # pragma: no cover - defensive
        terminate_tree(proc, grace=term_grace)
    # Anything still in the group after the leader exited is ours.
    _clear_group(proc.pid, since=getattr(proc, "_duet_launched_at", None))
    err_reader.join(timeout=1.0)
    return StreamResult(
        returncode=proc.returncode,
        stderr=err_buf.text(),
        timed_out=timed_out,
        cancelled=cancelled,
        lines=count,
        bytes_read=counters["bytes"],
        truncated_lines=counters["truncated"],
        over_limit=over_limit,
        duration_s=time.monotonic() - started,
    )
