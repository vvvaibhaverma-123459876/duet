"""Transactional SQLite store for the v2 runtime.

Every state change is an event applied through the deterministic reducer and
appended to the event log in the *same* transaction as the materialised rows
it produces, so history and state can never commit independently. Writes use
BEGIN IMMEDIATE, which serialises writers across processes; no network call or
subprocess wait may happen while a transaction is open (callers commit before
dispatching and record results in a later transaction)."""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Iterator

from . import reducer
from .contracts import (
    Event,
    IdempotencyMismatch,
    NotFound,
    SchemaError,
    StoreBusy,
    ValidationError,
    canonical_json,
    content_hash,
    utc_now,
)

BUSY_TIMEOUT_MS = 5000
NETWORK_FILESYSTEMS = {
    "nfs", "nfs4", "cifs", "smbfs", "smb3", "sshfs", "fuse.sshfs", "afpfs", "9p", "ceph", "glusterfs",
    "fuse.glusterfs", "davfs", "fuse.davfs2", "lustre", "gpfs", "fuse.s3fs", "fuse.rclone", "virtiofs",
}


def migrations() -> list[tuple[int, str, str]]:
    """Packaged migrations as (version, name, sql), ordered by version."""
    found = []
    folder = resources.files("duet.runtime").joinpath("migrations")
    for entry in folder.iterdir():
        match = re.fullmatch(r"(\d{4})_([a-z0-9_]+)\.sql", entry.name)
        if match:
            found.append((int(match.group(1)), match.group(2), entry.read_text(encoding="utf-8")))
    found.sort()
    versions = [v for v, _, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise SchemaError(f"packaged migrations are not contiguous from 1: {versions}")
    return found


SCHEMA_VERSION = len(migrations())


def filesystem_kind(path: Path) -> str:
    """Best-effort classification of the filesystem holding `path`:
    'local', 'network:<type>' or 'unknown'."""
    target = str(path.resolve())
    if sys.platform.startswith("linux"):
        try:
            mounts = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
        except OSError:
            return "unknown"
        best = ("", "")
        for line in mounts:
            parts = line.split()
            if len(parts) < 3:
                continue
            mount_point = parts[1].replace("\\040", " ")
            if (target == mount_point or target.startswith(mount_point.rstrip("/") + "/")) and len(mount_point) > len(best[0]):
                best = (mount_point, parts[2])
        fstype = best[1]
        if not fstype:
            return "unknown"
        return f"network:{fstype}" if fstype in NETWORK_FILESYSTEMS else "local"
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        best = ("", "")
        for line in out.splitlines():
            match = re.match(r"^.+ on (.+) \((.+)\)$", line)
            if not match:
                continue
            mount_point, opts = match.group(1), match.group(2)
            if (target == mount_point or target.startswith(mount_point.rstrip("/") + "/")) and len(mount_point) > len(best[0]):
                best = (mount_point, opts)
        if not best[1]:
            return "unknown"
        return "local" if "local" in best[1].split(", ") else f"network:{best[1].split(',')[0]}"
    return "unknown"


class Store:
    """One SQLite database. Safe to open from several processes at once."""

    def __init__(self, path: Path | str, *, allow_network_fs: bool = False) -> None:
        self.path = Path(path)
        # Private from the start: tokens' hashes and run state live here.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        kind = filesystem_kind(self.path.parent)
        if kind.startswith("network:") and not allow_network_fs:
            raise SchemaError(
                f"refusing to place the runtime database on a network filesystem ({kind}): {self.path}. "
                "Set DUET_STATE_DIR to a local directory."
            )
        self.filesystem = kind
        self._local = threading.local()
        conn = self._connect()
        try:
            self._migrate(conn)
        finally:
            conn.close()

    # -- connections ------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if self.filesystem == "local":
            # WAL only where it is known to work: never on network filesystems.
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- migrations ------------------------------------------------------------

    def _migrate(self, conn: sqlite3.Connection) -> None:
        packaged = migrations()
        with _immediate(conn):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            newest = max(applied, default=0)
            if newest > len(packaged):
                raise SchemaError(
                    f"database {self.path} has schema version {newest}, newer than this Duet ({len(packaged)}); "
                    "upgrade Duet instead of downgrading the database"
                )
            for version, name, sql in packaged:
                if version in applied:
                    continue
                for statement in _statements(sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, utc_now()),
                )

    def schema_version(self) -> int:
        row = self.connection().execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    # -- transactions ------------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self) -> Iterator["Tx"]:
        conn = self.connection()
        if conn.in_transaction:
            raise SchemaError("nested transactions are not supported")
        with _immediate(conn):
            yield Tx(conn)

    def read(self) -> "Tx":
        """A read-only view outside a write transaction (autocommit reads)."""
        return Tx(self.connection(), readonly=True)

    # -- replay ------------------------------------------------------------------

    def events(self, run_id: str | None = None) -> list[Event]:
        conn = self.connection()
        if run_id is None:
            rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        else:
            rows = conn.execute("SELECT * FROM events WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
        return [
            Event(
                type=row["type"],
                payload=json.loads(row["payload_json"]),
                actor=row["actor"],
                at=row["at"],
                run_id=row["run_id"],
                event_id=row["event_id"],
            )
            for row in rows
        ]

    def snapshot_tables(self) -> dict[str, dict[str, dict]]:
        conn = self.connection()
        out: dict[str, dict[str, dict]] = {}
        for table, (pk, columns) in reducer.TABLES.items():
            rows = conn.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
            out[table] = {row[pk]: {c: row[c] for c in columns} for row in rows}
        return out

    def verify_replay(self) -> list[str]:
        """Rebuild state from the event log and diff it against the tables.
        Returns human-readable differences; empty means consistent."""
        state = reducer.MemoryState()
        for event in self.events():
            state.apply(event)
        actual = self.snapshot_tables()
        problems = []
        for table in reducer.TABLES:
            want, have = state.tables[table], actual[table]
            for key in sorted(set(want) | set(have)):
                if want.get(key) != have.get(key):
                    problems.append(f"{table}[{key}]: replay={want.get(key)!r} stored={have.get(key)!r}")
        return problems


class Tx:
    """Reads and event application inside one transaction."""

    def __init__(self, conn: sqlite3.Connection, readonly: bool = False) -> None:
        self.conn = conn
        self.readonly = readonly
        self.events: list[Event] = []

    def get(self, table: str, key: str) -> dict | None:
        pk, columns = reducer.TABLES[table]
        row = self.conn.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE {pk} = ?", (key,)).fetchone()
        return {c: row[c] for c in columns} if row is not None else None

    def require(self, table: str, key: str) -> dict:
        row = self.get(table, key)
        if row is None:
            raise NotFound(f"{table} {key} not found")
        return row

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: tuple = ()):
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def emit(self, event: Event) -> None:
        """Apply one event through the reducer and persist rows + event."""
        if self.readonly:
            raise SchemaError("cannot emit events outside a write transaction")
        if event.type not in reducer.EVENT_TYPES:
            raise ValidationError(f"unknown event type {event.type!r}")
        for table, row in reducer.apply(event, self.get):
            _, columns = reducer.TABLES[table]
            if set(row) != set(columns):
                raise SchemaError(f"reducer produced columns {sorted(row)} for {table}; expected {sorted(columns)}")
            placeholders = ", ".join("?" for _ in columns)
            updates = ", ".join(f"{c} = excluded.{c}" for c in columns[1:])
            pk = reducer.TABLES[table][0]
            self.conn.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT({pk}) DO UPDATE SET {updates}",
                tuple(row[c] for c in columns),
            )
        record = event.to_row()
        self.conn.execute(
            "INSERT INTO events (event_id, run_id, type, actor, at, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (record["event_id"], record["run_id"], record["type"], record["actor"], record["at"], record["payload_json"]),
        )
        self.events.append(event)

    # -- idempotency --------------------------------------------------------------

    def idempotent_response(self, principal: str, key: str, command: str, request: object) -> dict | None:
        """Return the stored response for (principal, key) or None. A reused key
        with a different command or request is an error, never a silent replay."""
        row = self.conn.execute(
            "SELECT command, request_hash, response_json FROM idempotency_keys WHERE principal = ? AND key = ?",
            (principal, key),
        ).fetchone()
        if row is None:
            return None
        if row["command"] != command or row["request_hash"] != content_hash(request):
            raise IdempotencyMismatch(
                f"idempotency key {key!r} was already used for a different request",
                details={"command": row["command"]},
            )
        return json.loads(row["response_json"])

    def remember_response(self, principal: str, key: str, command: str, request: object, response: dict) -> None:
        self.conn.execute(
            "INSERT INTO idempotency_keys (principal, key, command, request_hash, response_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (principal, key, command, content_hash(request), canonical_json(response), utc_now()),
        )


@contextlib.contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE with bounded retries on lock contention."""
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise StoreBusy("runtime database is busy; another Duet process holds the write lock") from exc
            time.sleep(0.05)
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        try:
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            conn.execute("ROLLBACK")
            raise StoreBusy(f"commit failed: {exc}") from exc


def _statements(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return [statement.strip() for statement in body.split(";") if statement.strip()]


def default_db_path() -> Path:
    from .paths import state_dir

    return state_dir() / "v2" / "duet.db"


def open_default_store() -> Store:
    return Store(default_db_path())

