"""D02: transactional store, migrations, crash atomicity, replay determinism."""
from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from duet.runtime import store as store_mod
from duet.runtime.contracts import Event, SchemaError, StoreBusy, ValidationError, utc_now
from duet.runtime.paths import ensure_private_dir, state_dir
from duet.runtime.reducer import MemoryState
from duet.runtime.store import Store, migrations

REPO_ROOT = Path(__file__).resolve().parents[2]


def _policy_event(n: int = 0) -> Event:
    return Event("policy.registered", {"policy_hash": f"sha256:{n:064d}", "body": {"n": n}}, "user:user", utc_now())


class TestMigrations:
    def test_fresh_database_is_migrated_with_foreign_keys(self, tmp_path):
        store = Store(tmp_path / "d.db")
        assert store.schema_version() == len(migrations()) >= 1
        assert store.connection().execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_reopen_does_not_reapply(self, tmp_path):
        Store(tmp_path / "d.db")
        store = Store(tmp_path / "d.db")
        rows = store.connection().execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert rows == len(migrations())

    def test_newer_database_is_refused(self, tmp_path):
        store = Store(tmp_path / "d.db")
        store.connection().execute("INSERT INTO schema_migrations VALUES (99, 'future', 'x')")
        store.close()
        with pytest.raises(SchemaError, match="newer than this Duet"):
            Store(tmp_path / "d.db")

    def test_wal_only_on_local_filesystem(self, tmp_path, monkeypatch):
        store = Store(tmp_path / "local.db")
        if store.filesystem == "local":
            assert store.connection().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        monkeypatch.setattr(store_mod, "filesystem_kind", lambda path: "unknown")
        other = Store(tmp_path / "unknown.db")
        assert other.connection().execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal"

    def test_network_filesystem_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store_mod, "filesystem_kind", lambda path: "network:nfs")
        with pytest.raises(SchemaError, match="network filesystem"):
            Store(tmp_path / "n.db")
        assert Store(tmp_path / "n.db", allow_network_fs=True).schema_version() >= 1

    def test_migration_files_are_contiguous(self):
        assert [v for v, _, _ in migrations()] == list(range(1, len(migrations()) + 1))


class TestTransactions:
    def test_exception_rolls_back_rows_and_events(self, tmp_path):
        store = Store(tmp_path / "d.db")
        with pytest.raises(RuntimeError):
            with store.transaction() as tx:
                tx.emit(_policy_event(1))
                raise RuntimeError("boom")
        assert store.connection().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert store.connection().execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0

    def test_unknown_event_type_rejected(self, tmp_path):
        store = Store(tmp_path / "d.db")
        with pytest.raises(ValidationError):
            with store.transaction() as tx:
                tx.emit(Event("nope.nothing", {}, "user:user", utc_now()))

    def test_nested_transaction_refused(self, tmp_path):
        store = Store(tmp_path / "d.db")
        with store.transaction():
            with pytest.raises(SchemaError):
                with store.transaction():
                    pass

    def test_lock_contention_yields_structured_busy_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store_mod, "BUSY_TIMEOUT_MS", 300)
        store = Store(tmp_path / "d.db")
        blocker = sqlite3.connect(tmp_path / "d.db", isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(StoreBusy):
                with store.transaction() as tx:
                    tx.emit(_policy_event(2))
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        with store.transaction() as tx:  # the lock is free again
            tx.emit(_policy_event(3))

    def test_crash_mid_transaction_leaves_no_partial_state(self, tmp_path):
        db = tmp_path / "d.db"
        Store(db)
        script = textwrap.dedent(
            f"""
            import os, sys
            sys.path.insert(0, {str(REPO_ROOT)!r})
            from duet.runtime.store import Store
            from duet.runtime.contracts import Event, utc_now
            store = Store({str(db)!r})
            with store.transaction() as tx:
                tx.emit(Event("policy.registered", {{"policy_hash": "sha256:" + "a" * 64, "body": {{}}}}, "user:user", utc_now()))
                tx.emit(Event("policy.registered", {{"policy_hash": "sha256:" + "b" * 64, "body": {{}}}}, "user:user", utc_now()))
                os._exit(9)  # process dies before COMMIT
            """
        )
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 9, proc.stderr
        store = Store(db)
        assert store.connection().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert store.connection().execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0
        assert store.verify_replay() == []


class TestReplay:
    def test_replay_matches_stored_state(self, tmp_path):
        store = Store(tmp_path / "d.db")
        with store.transaction() as tx:
            for n in range(3):
                tx.emit(_policy_event(n))
        assert store.verify_replay() == []

    def test_replay_detects_tampering(self, tmp_path):
        store = Store(tmp_path / "d.db")
        with store.transaction() as tx:
            tx.emit(_policy_event(7))
        store.connection().execute("UPDATE policies SET body_json = '{\"forged\":true}'")
        assert store.verify_replay()

    def test_memory_state_is_deterministic(self, tmp_path):
        store = Store(tmp_path / "d.db")
        with store.transaction() as tx:
            tx.emit(_policy_event(1))
        a, b = MemoryState(), MemoryState()
        for event in store.events():
            a.apply(event)
            b.apply(event)
        assert a.tables == b.tables


class TestStatePaths:
    def test_state_dir_override_and_private_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DUET_STATE_DIR", str(tmp_path / "state"))
        path = state_dir()
        assert path == tmp_path / "state"
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o700

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_world_writable_state_dir_refused(self, tmp_path):
        path = tmp_path / "open"
        path.mkdir()
        os.chmod(path, 0o777)
        with pytest.raises(SchemaError, match="writable by group or others"):
            ensure_private_dir(path)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_group_readable_dir_is_tightened(self, tmp_path):
        path = tmp_path / "loose"
        path.mkdir()
        os.chmod(path, 0o750)
        ensure_private_dir(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
