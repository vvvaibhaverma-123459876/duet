"""Newline-delimited JSON-RPC over a child process's stdio.

Used for `codex app-server` (stdio transport, JSON objects one per line, no
`jsonrpc` field; observed with codex-cli 0.157.1). A reader thread parses
lines into a queue; the caller pumps the queue, so notifications and
server-initiated requests are handled in the caller's thread while it waits
for a response. Lines are size-bounded; malformed lines are counted and
skipped, never fatal on their own."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .. import oscompat
from .process import BoundedBuffer, _mark_launch, _pump, terminate_tree

MAX_LINE_BYTES = 8 * 1024 * 1024


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message
        self.data = data


class ProcessGone(RuntimeError):
    pass


class JsonRpcProcess:
    def __init__(
        self,
        argv: list[str],
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        on_notification: Callable[[dict], None] | None = None,
        on_server_request: Callable[[dict], dict | JsonRpcError] | None = None,
        max_line_bytes: int = MAX_LINE_BYTES,
    ) -> None:
        self.proc = subprocess.Popen(
            oscompat.resolve_argv(argv),
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            **oscompat.own_group_kwargs(),
        )
        _mark_launch(self.proc)
        self.on_notification = on_notification
        self.on_server_request = on_server_request
        self.max_line_bytes = max_line_bytes
        self._next_id = 0
        self._queue: "queue.Queue[dict | None]" = queue.Queue()
        self._write_lock = threading.Lock()
        self.malformed = 0
        self.oversized = 0
        self.stderr = BoundedBuffer(256 * 1024)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        threading.Thread(target=_pump, args=(self.proc.stderr, self.stderr), daemon=True).start()

    # -- io -------------------------------------------------------------------------

    def _read(self) -> None:
        pending = bytearray()
        skipping = False
        try:
            while True:
                chunk = self.proc.stdout.read(65536)
                if not chunk:
                    break
                start = 0
                while True:
                    newline = chunk.find(b"\n", start)
                    piece = chunk[start:] if newline < 0 else chunk[start:newline]
                    if not skipping:
                        if len(pending) + len(piece) > self.max_line_bytes:
                            skipping = True
                            pending = bytearray()
                        else:
                            pending += piece
                    if newline < 0:
                        break
                    if skipping:
                        self.oversized += 1
                    elif pending.strip():
                        self._dispatch_raw(bytes(pending))
                    pending = bytearray()
                    skipping = False
                    start = newline + 1
        except (OSError, ValueError):
            pass
        finally:
            self._queue.put(None)

    def _dispatch_raw(self, raw: bytes) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            self.malformed += 1
            return
        if not isinstance(message, dict):
            self.malformed += 1
            return
        self._queue.put(message)

    def _send(self, message: dict) -> None:
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise ProcessGone(f"app-server is not accepting input: {exc}") from exc

    # -- api ------------------------------------------------------------------------

    def notify(self, method: str, params: dict | None = None) -> None:
        message: dict = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(self, method: str, params: dict | None = None, *, timeout: float = 60.0) -> dict:
        self._next_id += 1
        request_id = self._next_id
        message: dict = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        response = self.pump(timeout=timeout, until=lambda m: m.get("id") == request_id and ("result" in m or "error" in m))
        if response is None:
            raise TimeoutError(f"no response to {method} within {timeout}s")
        if "error" in response:
            err = response["error"] if isinstance(response["error"], dict) else {}
            raise JsonRpcError(int(err.get("code", -32603)), str(err.get("message", "unknown error")), err.get("data"))
        result = response.get("result")
        return result if isinstance(result, dict) else {"value": result}

    def pump(self, *, timeout: float, until: Callable[[dict], bool] | None = None, stop: Callable[[], bool] | None = None) -> dict | None:
        """Process incoming messages until `until(message)` matches (returned),
        `stop()` is true, or `timeout` elapses (None). Notifications and
        server requests seen on the way are handled, not dropped."""
        deadline = time.monotonic() + timeout
        while True:
            if stop is not None and stop():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                message = self._queue.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                continue
            if message is None:
                self._queue.put(None)  # keep EOF visible to later pumps
                raise ProcessGone(f"app-server exited (code {self.proc.poll()}): {self.stderr.text()[-400:]}")
            if until is not None and until(message):
                return message
            self._handle(message)

    def _handle(self, message: dict) -> None:
        if "method" in message and "id" in message:
            answer = self.on_server_request(message) if self.on_server_request else JsonRpcError(-32601, "not supported by this client")
            if isinstance(answer, JsonRpcError):
                self._send({"id": message["id"], "error": {"code": answer.code, "message": answer.message}})
            else:
                self._send({"id": message["id"], "result": answer})
        elif "method" in message:
            if self.on_notification:
                self.on_notification(message)
        # Responses to requests nobody is waiting for are ignored.

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self, grace: float = 2.0) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            terminate_tree(self.proc, grace=grace)
