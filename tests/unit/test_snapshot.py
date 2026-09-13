import hashlib
import sqlite3
from contextlib import closing

import pytest

from tidal.lifecycle import LifecycleError, inspect_database
from tidal.migrations import run_migrations
from tidal.snapshot import snapshot


def test_snapshot_is_coherent_during_a_wal_writer_and_never_copies_activation(tmp_path):
    path, output = tmp_path / "tidal.db", tmp_path / "copy.sqlite3"
    run_migrations(f"sqlite:///{path}")
    activation = tmp_path / "activation.json"
    activation.write_text("local-only")
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE app_metadata SET notification_baseline_pending=1")
        writer.commit()
        writer.execute("UPDATE app_metadata SET notification_baseline_pending=0")
        # Snapshot sees a complete committed version despite an open writer.
        report = snapshot(path, output)
        writer.rollback()
    assert report["data"]["database_identity"] == inspect_database(path)["database_identity"]
    assert report["data"]["notification_baseline_pending"] is True
    assert report["data"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert output.stat().st_mode & 0o777 == 0o600
    assert activation.read_text() == "local-only"
    assert not list(tmp_path.glob(".tidal-snapshot-*"))
    assert not output.with_name(output.name + "-wal").exists()


def test_snapshot_refuses_overwrite_and_malformed_sources(tmp_path):
    path, output = tmp_path / "tidal.db", tmp_path / "copy.sqlite3"
    run_migrations(f"sqlite:///{path}")
    original = path.read_bytes()
    for target in (path, tmp_path / "link"):
        if target != path:
            target.symlink_to(path)
        with pytest.raises(LifecycleError, match="new file"):
            snapshot(path, target)
    assert path.read_bytes() == original
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"interrupted data")
    with pytest.raises(LifecycleError):
        snapshot(bad, output)
    assert not output.exists()
