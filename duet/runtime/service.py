"""Local runtime service and client (D05).

One service process per state directory owns the database, the coordinator
and any managed peers. Native integrations (`duet mcp serve`) and the CLI talk
to it over a Unix-domain socket:

- the socket lives in a 0700 directory and is created 0600; on Linux the
  connecting process must also belong to the same uid (SO_PEERCRED);
- one JSON request per connection, newline-terminated, at most 1 MiB;
- a participant authenticates with its bearer token, the CLI with the
  per-start service secret (a 0600 file); unauthenticated connections may
  only `ping` and `join`;
- an exclusive lock file guarantees a single service per state directory, so
  an MCP proxy can start the service on demand without creating a second,
  competing runtime.

Everything here runs as the same OS user as the agents: this is isolation
from other users and from accidents, not a security boundary against an
unrestricted process of the same user (containment is labelled cooperative)."""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import secrets
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from .api import Runtime
from .artifacts import ArtifactStore
from .contracts import (
    CONTROLLER,
    TERMINAL_RUN,
    USER,
    Conflict,
    DomainError,
    IdempotencyMismatch,
    InvalidTransition,
    NotFound,
    PolicyDenied,
    RunLifecycle,
    SchemaError,
    StaleLease,
    StoreBusy,
    Unauthorized,
    ValidationError,
)
from .identity import ProcessIdentity
from .pairing import MAX_WAIT_SECONDS, PairCoordinator
from .paths import ensure_private_dir, runtime_dir
from .store import Store

log = logging.getLogger("duet.service")

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
READ_TIMEOUT_SECONDS = 30.0
MONITOR_SECONDS = 1.0
DEFAULT_IDLE_EXIT_SECONDS = 900.0
UNIX_PATH_LIMIT = 100  # sun_path is 104 (macOS) or 108 (Linux) bytes

ERRORS: dict[str, type[DomainError]] = {
    cls.code: cls
    for cls in (ValidationError, NotFound, Unauthorized, Conflict, InvalidTransition, StaleLease, PolicyDenied, IdempotencyMismatch, StoreBusy, SchemaError)
}


class ServiceUnavailable(DomainError):
    code = "service_unavailable"


class ServiceRunning(DomainError):
    code = "service_running"


ERRORS[ServiceUnavailable.code] = ServiceUnavailable


# --- paths --------------------------------------------------------------------------------


@dataclass(frozen=True)
class ServicePaths:
    root: Path
    db: Path
    artifacts: Path
    pairs: Path
    lock: Path
    spawn_lock: Path
    info: Path
    secret: Path
    log: Path
    socket: Path

    @classmethod
    def for_root(cls, root: Path | None = None) -> "ServicePaths":
        root = ensure_private_dir(Path(root) if root else runtime_dir())
        sock = root / "service.sock"
        if len(os.fsencode(str(sock))) > UNIX_PATH_LIMIT:
            # Deep state dirs exceed sun_path; use a short private directory
            # keyed by the state dir instead.
            short = ensure_private_dir(Path(tempfile.gettempdir()) / f"duet-{os.getuid() if hasattr(os, 'getuid') else 'u'}")
            sock = short / (hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:20] + ".sock")
        return cls(
            root=root, db=root / "duet.db", artifacts=root / "artifacts", pairs=root / "pairs", lock=root / "service.lock",
            spawn_lock=root / "service.spawn.lock", info=root / "service.json", secret=root / "service.secret",
            log=root / "service.log", socket=sock,
        )


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


# --- client -------------------------------------------------------------------------------


class ServiceClient:
    """One request per connection. Errors come back as the same DomainError
    classes the runtime raises, so callers handle them uniformly."""

    def __init__(self, paths: ServicePaths, *, token: str | None = None, secret: str | None = None) -> None:
        self.paths = paths
        self.token = token
        self.secret = secret

    def with_token(self, token: str) -> "ServiceClient":
        return ServiceClient(self.paths, token=token, secret=self.secret)

    @classmethod
    def as_controller(cls, paths: ServicePaths) -> "ServiceClient":
        try:
            secret = paths.secret.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise ServiceUnavailable("the DUET service is not running (no secret file)") from None
        return cls(paths, secret=secret)

    def call(self, op: str, rpc_timeout: float = 30.0, **args: Any) -> Any:
        auth = {}
        if self.token:
            auth["token"] = self.token
        if self.secret:
            auth["secret"] = self.secret
        payload = (json.dumps({"op": op, "args": args, "auth": auth}) + "\n").encode("utf-8")
        if len(payload) > MAX_REQUEST_BYTES:
            raise ValidationError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(rpc_timeout)
        try:
            sock.connect(str(self.paths.socket))
        except OSError as exc:
            sock.close()
            raise ServiceUnavailable(f"cannot reach the DUET service at {self.paths.socket}: {exc.strerror or exc}") from None
        try:
            sock.sendall(payload)
            data = _read_line(sock, MAX_RESPONSE_BYTES)
        except socket.timeout:
            raise ServiceUnavailable(f"the DUET service did not answer {op!r} within {rpc_timeout:.0f}s") from None
        finally:
            sock.close()
        if not data:
            raise ServiceUnavailable(f"the DUET service closed the connection during {op!r}")
        response = json.loads(data)
        if response.get("ok"):
            return response.get("result")
        error = response.get("error") or {}
        cls = ERRORS.get(error.get("code"), DomainError)
        raise cls(error.get("message", "service error"), details=error.get("details") or {})

    def ping(self, timeout: float = 5.0) -> dict:
        return self.call("ping", rpc_timeout=timeout)


def _read_line(sock: socket.socket, limit: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunks.append(chunk[:newline])
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ValidationError(f"message exceeds {limit} bytes")
    return b"".join(chunks)


def service_running(paths: ServicePaths) -> bool:
    try:
        ServiceClient(paths).ping(timeout=2.0)
        return True
    except DomainError:
        return False


def ensure_service(paths: ServicePaths, *, spawn: bool = True, timeout: float = 15.0, extra_env: dict[str, str] | None = None) -> None:
    """Make sure a service answers on `paths.socket`, starting one detached
    if allowed. Concurrent callers serialise on a spawn lock, and the service
    itself holds an exclusive lock, so at most one service ever runs."""
    if service_running(paths):
        return
    if not spawn:
        raise ServiceUnavailable("the DUET service is not running and this process may not start one")
    with open(paths.spawn_lock, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if service_running(paths):
            return
        env = dict(os.environ, DUET_STATE_DIR=str(paths.root.parent) if paths.root.name == "v2" else str(paths.root))
        env.update(extra_env or {})
        fd = os.open(paths.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            subprocess.Popen(
                [sys.executable, "-m", "duet", "service", "run", "--state-root", str(paths.root)],
                stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, env=env, start_new_session=True, close_fds=True,
            )
        finally:
            os.close(fd)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if service_running(paths):
                return
            time.sleep(0.1)
    raise ServiceUnavailable(f"the DUET service did not start within {timeout:.0f}s; see {paths.log}")


# --- server -------------------------------------------------------------------------------

PARTICIPANT_OPS = frozenset(
    {
        "join", "send", "inbox", "wait", "propose_task", "propose_plan", "decide_plan", "claim", "complete_task", "decide_task",
        "handoff", "submit", "request_review", "request_profile", "status", "disconnect",
    }
)
CONTROLLER_OPS = frozenset({"pair", "run_status", "cancel", "resume", "runs", "shutdown"})
OPEN_OPS = frozenset({"ping", "join"})

PeerFactory = Callable[["RuntimeService", str, str], Any]  # (service, run_id, provider) -> started peer


class RuntimeService:
    def __init__(
        self,
        paths: ServicePaths,
        *,
        peer_factory: PeerFactory | None = None,
        idle_exit_seconds: float | None = DEFAULT_IDLE_EXIT_SECONDS,
        check_env: dict[str, str] | None = None,
        verify_host: bool = True,
    ) -> None:
        self.paths = paths
        self._lock_file = open(paths.lock, "a+")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_file.close()
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise ServiceRunning(f"a DUET service already runs for {paths.root}") from None
            raise
        self.identity = ProcessIdentity.current()
        self.store = Store(paths.db)
        self.runtime = Runtime(self.store, identity=self.identity)
        self.artifacts = ArtifactStore(paths.artifacts)
        self.peer_factory = peer_factory
        self.peers: dict[str, list[Any]] = {}
        self._peers_lock = threading.Lock()
        self.coordinator = PairCoordinator(
            self.runtime, self.artifacts, state_root=paths.pairs,
            peer_launcher=self._launch_peer if peer_factory else None, peer_stopper=self.stop_peers, check_env=check_env,
        )
        self.secret = secrets.token_urlsafe(32)
        self.idle_exit_seconds = idle_exit_seconds
        self.verify_host = verify_host
        self._stop = threading.Event()
        self._server: socketserver.ThreadingUnixStreamServer | None = None
        self._threads: list[threading.Thread] = []
        self._active = 0
        self._active_lock = threading.Lock()
        self._last_activity = time.monotonic()
        self._last_event_seq = 0

    # -- lifecycle -----------------------------------------------------------------------

    def start(self) -> "RuntimeService":
        report = self.runtime.reconcile(CONTROLLER)
        self._settle_in_doubt_checks(report["in_doubt"])
        self.coordinator.budget.settle_in_doubt_turns(report["in_doubt"])
        # Managed sessions do not survive their service: their drivers are
        # gone, so they must not keep looking connected.
        self._release_orphaned_managed_peers("the DUET service restarted; managed sessions are not relaunched automatically")
        if self.paths.socket.exists() or self.paths.socket.is_symlink():
            self.paths.socket.unlink()  # stale: we hold the lock, so no service owns it
        service = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:  # noqa: D401 - socketserver API
                service._handle_connection(self.connection)

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            allow_reuse_address = True

        old_umask = os.umask(0o177)
        try:
            self._server = Server(str(self.paths.socket), Handler)
        finally:
            os.umask(old_umask)
        os.chmod(self.paths.socket, 0o600)
        _write_private(self.paths.secret, self.secret)
        _write_private(
            self.paths.info,
            json.dumps({"pid": os.getpid(), "identity": str(self.identity), "socket": str(self.paths.socket), "version": __version__, "started_at": time.time()}),
        )
        for target, name in ((self._server.serve_forever, "duet-service-accept"), (self._monitor, "duet-service-monitor")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("service listening on %s", self.paths.socket)
        return self

    def serve_forever(self) -> None:
        self.start()
        try:
            while not self._stop.wait(0.5):
                pass
        finally:
            self.close()

    def request_stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self._stop.set()
        self.stop_peers(None)
        try:
            self._release_orphaned_managed_peers("the DUET service stopped")
        except Exception:  # closing must not fail on bookkeeping
            log.exception("could not mark managed peers unavailable")
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        for path in (self.paths.socket, self.paths.info, self.paths.secret):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self.store.close()
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
        finally:
            self._lock_file.close()

    def _settle_in_doubt_checks(self, action_ids: list[str]) -> None:
        """A check interrupted by a service crash wrote no evidence; checks are
        read-only and repeatable, so settle it as FAILED, reopen the task and
        tell the writer to submit again. Other in-doubt actions (provider
        turns) stay IN_DOUBT."""
        tx = self.store.read()
        reopen: set[str] = set()
        for action_id in action_ids:
            row = tx.get("actions", action_id)
            if row and row["type"] == "check":
                self.runtime.resolve_in_doubt(CONTROLLER, action_id, "FAILED", reconciliation="service restarted during the check; no evidence was recorded")
                reopen.add(row["run_id"])
        for run_id in reopen:
            try:
                latest = self.coordinator._latest_snapshot(run_id)
                if latest is not None:
                    self.coordinator._changes_requested(run_id, latest["snapshot_id"], f"a check on {latest['snapshot_id']} was interrupted by a service restart and recorded no evidence")
            except DomainError:
                log.exception("could not reopen run %s after an interrupted check", run_id)

    def _release_orphaned_managed_peers(self, reason: str) -> None:
        with self._peers_lock:
            driven = {peer.participant_id for peers in self.peers.values() for peer in peers if getattr(peer, "alive", False)}
        rows = self.store.read().query(
            "SELECT p.participant_id, p.run_id, p.liveness, r.lifecycle FROM participants p JOIN runs r ON r.run_id = p.run_id WHERE p.origin = 'managed'"
        )
        for row in rows:
            if row["participant_id"] in driven or row["liveness"] in ("unavailable", "gone") or RunLifecycle(row["lifecycle"]) in TERMINAL_RUN:
                continue
            self.runtime.update_participant(CONTROLLER, row["participant_id"], liveness="unavailable")
            self.coordinator._peer_lost(row["run_id"], row["participant_id"], reason)

    def _monitor(self) -> None:
        while not self._stop.wait(MONITOR_SECONDS):
            try:
                self.coordinator.check_hosts()
                self.coordinator.expire_overdue()
                self.coordinator.budget.check_quota()  # reset-aware resume (D08)
                seq = self.store.read().scalar("SELECT MAX(seq) FROM events") or 0
                if seq != self._last_event_seq:
                    self._last_event_seq = seq
                    self.coordinator.notify()  # also wakes waiters for writes made by other processes
                if self.idle_exit_seconds is not None and self._idle_for() > self.idle_exit_seconds and not self._live_runs():
                    log.info("idle with no live runs; exiting")
                    self._stop.set()
            except Exception:  # never let the monitor die silently
                log.exception("monitor iteration failed")

    def _idle_for(self) -> float:
        with self._active_lock:
            if self._active:
                return 0.0
            return time.monotonic() - self._last_activity

    def _live_runs(self) -> list[str]:
        """Runs that keep the service alive: active ones, and runs paused for
        quota (the service resumes them after the reset). Any other paused run
        waits for the user and does not."""
        rows = self.store.read().query("SELECT run_id, lifecycle FROM runs")
        return [
            r["run_id"] for r in rows
            if RunLifecycle(r["lifecycle"]) not in TERMINAL_RUN
            and (not r["lifecycle"].startswith("PAUSED_") or r["lifecycle"] == RunLifecycle.PAUSED_QUOTA.value)
        ]

    # -- managed peers -------------------------------------------------------------------

    def _launch_peer(self, run_id: str, provider: str) -> dict:
        assert self.peer_factory is not None
        peer = self.peer_factory(self, run_id, provider)
        with self._peers_lock:
            self.peers.setdefault(run_id, []).append(peer)
        return peer.participant

    def stop_peers(self, run_id: str | None) -> None:
        with self._peers_lock:
            if run_id is None:
                targets = [p for peers in self.peers.values() for p in peers]
                self.peers.clear()
            else:
                targets = self.peers.pop(run_id, [])
        for peer in targets:
            peer.stop()

    # -- requests ------------------------------------------------------------------------

    def _handle_connection(self, conn: socket.socket) -> None:
        with self._active_lock:
            self._active += 1
            self._last_activity = time.monotonic()
        try:
            conn.settimeout(READ_TIMEOUT_SECONDS)
            peer_pid = _peer_pid(conn)
            try:
                data = _read_line(conn, MAX_REQUEST_BYTES)
                request = json.loads(data) if data else None
                if not isinstance(request, dict):
                    raise ValidationError("request must be a JSON object on one line")
                conn.settimeout(MAX_WAIT_SECONDS + 30)
                result = self.dispatch(request, peer_pid=peer_pid)
                response = {"ok": True, "result": result}
            except DomainError as exc:
                response = {"ok": False, "error": exc.to_dict()}
            except json.JSONDecodeError as exc:
                response = {"ok": False, "error": ValidationError(f"invalid JSON: {exc}").to_dict()}
            except Exception as exc:  # report, never crash the service
                log.exception("request failed")
                response = {"ok": False, "error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}", "details": {}}}
            payload = (json.dumps(response, default=str) + "\n").encode("utf-8")
            if len(payload) > MAX_RESPONSE_BYTES:
                payload = (json.dumps({"ok": False, "error": {"code": "validation", "message": "response too large", "details": {}}}) + "\n").encode()
            try:
                conn.sendall(payload)
            except OSError:
                pass
        finally:
            with self._active_lock:
                self._active -= 1
                self._last_activity = time.monotonic()

    def dispatch(self, request: dict, *, peer_pid: int | None = None) -> Any:
        op = request.get("op")
        args = request.get("args") or {}
        auth = request.get("auth") or {}
        if not isinstance(op, str) or not isinstance(args, dict) or not isinstance(auth, dict):
            raise ValidationError("request needs op (string), args (object) and auth (object)")
        if op == "ping":
            return {"pid": os.getpid(), "version": __version__, "root": str(self.paths.root)}
        if auth.get("secret") is not None:
            if not secrets.compare_digest(str(auth["secret"]), self.secret):
                raise Unauthorized("bad service secret")
            return self._controller_op(op, args)
        token = auth.get("token")
        if token is None:
            if op != "join":
                raise Unauthorized(f"{op!r} needs a participant token; call join first")
            if self.verify_host and args.get("host"):
                _verify_host(args["host"], peer_pid)
            return self.coordinator.join(None, **_only(args, JOIN_ARGS))
        principal = self.runtime.authenticate(token)
        if op not in PARTICIPANT_OPS:
            raise Unauthorized(f"participants cannot call {op!r}")
        if op == "disconnect":
            self.coordinator.disconnect(principal, reason=str(args.get("reason", "connection closed"))[:200])
            return {"ok": True}
        self.coordinator.touch(principal)
        co = self.coordinator
        if op == "join":
            if any(args.get(k) for k in ("objective", "run_id", "invite")):
                raise PolicyDenied("this connection already belongs to a run; one session cannot start or join another run")
            return co.join(principal, provider=args.get("provider") or principal.provider or "")
        if op == "send":
            return co.send(principal, **_only(args, {"kind", "body", "to", "reply_to", "task_id", "snapshot_id", "review", "idempotency_key"}))
        if op == "inbox":
            return co.inbox(principal, **_only(args, {"ack_through", "since", "limit"}))
        if op == "wait":
            return co.wait(principal, **_only(args, {"since", "watch", "timeout", "ack_through"}))
        if op == "propose_task":
            return co.propose_task(principal, **_only(args, {"description", "depends_on", "acceptance_ids", "kind"}))
        if op == "propose_plan":
            return co.graph.propose_plan(principal, **_only(args, {"tasks", "rationale"}))
        if op == "decide_plan":
            return co.graph.decide_plan(principal, **_only(args, {"plan_id", "decision", "reason"}))
        if op == "complete_task":
            return co.graph.complete_task(principal, **_only(args, {"task_id", "summary", "artifact"}))
        if op == "decide_task":
            return co.graph.decide_task(principal, **_only(args, {"task_id", "decision", "reason"}))
        if op == "handoff":
            return co.graph.handoff(principal, **_only(args, {"reason"}))
        if op == "claim":
            return co.claim(principal, **_only(args, {"task_id"}))
        if op == "submit":
            return co.submit(principal, **_only(args, {"task_id", "summary", "request_review", "note"}))
        if op == "request_review":
            return co.request_review(principal, **_only(args, {"snapshot_id", "note", "criteria"}))
        if op == "request_profile":
            return co.request_profile(principal, **_only(args, {"model", "effort", "reason"}))
        if op == "status":
            return co.status(principal)
        raise ValidationError(f"unknown op {op!r}")

    def _controller_op(self, op: str, args: dict) -> Any:
        if op not in CONTROLLER_OPS:
            raise ValidationError(f"unknown controller op {op!r}")
        if op == "shutdown":
            self._stop.set()
            return {"stopping": True}
        if op == "runs":
            rows = self.store.read().query("SELECT run_id, objective, lifecycle, collaboration, created_at FROM runs ORDER BY created_at DESC LIMIT 50")
            return [dict(r) for r in rows]
        if op == "run_status":
            return self.coordinator.run_status(_text(args, "run_id"))
        if op == "cancel":
            return self.coordinator.cancel(_text(args, "run_id"), reason=str(args.get("reason") or "stopped by the user")[:500], principal=USER)
        if op == "resume":
            return self.coordinator.budget.resume(_text(args, "run_id"), reason=str(args.get("reason") or "resumed by the user")[:500], principal=USER)
        if op == "pair":
            if self.peer_factory is None:
                raise PolicyDenied("this service cannot launch managed peers")
            return self.coordinator.create_managed_pair(**_only(args, {"objective", "repo", "checks", "protected", "writer"}))
        raise ValidationError(f"unknown controller op {op!r}")


JOIN_ARGS = frozenset({"provider", "objective", "repo", "checks", "protected", "peer", "writer", "run_id", "invite", "host", "native_session_id", "client"})


def _only(args: dict, allowed: set[str] | frozenset[str]) -> dict:
    unknown = set(args) - set(allowed)
    if unknown:
        raise ValidationError(f"unknown arguments {sorted(unknown)}")
    return dict(args)


def _text(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{key} is required")
    return value


# --- peer credentials ---------------------------------------------------------------------


def _peer_pid(conn: socket.socket) -> int | None:
    """Linux only: pid and uid of the connecting process. A different uid is
    refused outright (the socket permissions should already prevent it)."""
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except OSError:
        return None
    pid, uid, _gid = struct.unpack("3i", raw)
    if hasattr(os, "getuid") and uid != os.getuid():
        raise Unauthorized("connections from other users are refused")
    return pid


def _parent_pid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (IndexError, ValueError):
        return None


def _verify_host(host: str, peer_pid: int | None) -> None:
    """The proxy claims its host (the agent CLI that launched it). Where the
    kernel tells us who connected, the claimed host must be an ancestor of
    that process and still be the same process (pid reuse is caught by the
    start time)."""
    identity = ProcessIdentity.parse(host)
    if identity is None:
        raise ValidationError("host must be a process identity (host|boot|pid|start)")
    if not identity.is_alive():
        raise Unauthorized("the claimed host process is not running")
    if peer_pid is None or not Path("/proc").is_dir():
        return
    current: int | None = peer_pid
    for _ in range(16):
        if current is None or current <= 1:
            break
        if current == identity.pid:
            return
        current = _parent_pid(current)
    raise Unauthorized("the claimed host is not an ancestor of the connecting process")
