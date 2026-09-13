import asyncio
import json
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from tidal.lifecycle import (
    SCHEMA_REVISION,
    LifecycleError,
    activation_binding,
    clear_activation,
    execution_lock,
    inspect_database,
    require_activation,
    write_activation,
)
from tidal.migrations import run_migrations
from tidal.persistence.db import Database

SIGNERS = {"scan": "0x" + "1" * 40, "kick": "0x" + "2" * 40}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "tidal.db"
    run_migrations(f"sqlite:///{path}")
    return path


def test_missing_database_is_never_created_by_inspection_or_normal_open(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(LifecycleError, match="missing") as error:
        inspect_database(path)
    assert error.value.code == "MISSING_DB"
    database = Database(f"sqlite:///{path}")
    with pytest.raises(OperationalError):
        with database.engine.connect():
            pass
    database.engine.dispose()
    assert not path.exists()


def test_pool_reconnect_does_not_recreate_deleted_database(db_path):
    database = Database(f"sqlite:///{db_path}")
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT 1")) == 1
    database.engine.dispose()
    db_path.unlink()
    with pytest.raises(OperationalError):
        with database.engine.connect():
            pass
    assert not db_path.exists()


def test_authoritative_writes_are_full_synchronous_wal(db_path):
    database = Database(f"sqlite:///{db_path}")
    with database.engine.connect() as connection:
        assert connection.scalar(text("PRAGMA journal_mode")) == "wal"
        assert connection.scalar(text("PRAGMA synchronous")) == 2
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
    database.engine.dispose()


def test_read_only_database_rejects_writes(db_path):
    database = Database(f"sqlite:///{db_path}", read_only=True)
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM app_metadata")) == 1
        with pytest.raises(OperationalError, match="readonly"):
            connection.execute(text("DELETE FROM app_metadata"))
    database.engine.dispose()
    assert inspect_database(db_path)["schema_revision"] == SCHEMA_REVISION


def test_inspection_does_not_change_database_and_migration_preserves_identity(db_path):
    before = db_path.read_bytes()
    original = inspect_database(db_path)
    assert db_path.read_bytes() == before
    run_migrations(f"sqlite:///{db_path}")
    assert inspect_database(db_path)["database_identity"] == original["database_identity"]
    assert original["notification_baseline_pending"] is False


@pytest.mark.parametrize("change,code", [
    ("DELETE FROM app_metadata", "INVALID_METADATA"),
    ("UPDATE app_metadata SET database_identity='invalid'", "INVALID_DB"),
    ("UPDATE app_metadata SET notification_baseline_pending=7", "INVALID_METADATA"),
    ("UPDATE alembic_version SET version_num='future'", "INCOMPATIBLE_SCHEMA"),
    ("DROP TABLE api_keys", "INCOMPATIBLE_SCHEMA"),
])
def test_inspection_rejects_invalid_or_incompatible_state(db_path, change, code):
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(change)
        connection.commit()
    with pytest.raises(LifecycleError) as error:
        inspect_database(db_path)
    assert error.value.code == code


def test_activation_is_bound_to_database_chain_and_every_signer(db_path, tmp_path):
    path = tmp_path / "activation.json"
    identity = inspect_database(db_path)["database_identity"]
    binding = activation_binding(identity, 1, SIGNERS)
    with pytest.raises(LifecycleError) as error:
        require_activation(path, binding)
    assert error.value.code == "HELD"
    write_activation(path, binding)
    require_activation(path, binding)
    assert path.stat().st_mode & 0o777 == 0o600
    for wrong in (
        activation_binding(str(uuid.uuid4()), 1, SIGNERS),
        activation_binding(identity, 10, SIGNERS),
        activation_binding(identity, 1, {"scan": SIGNERS["scan"]}),
        activation_binding(identity, 1, {"scan": SIGNERS["kick"], "kick": SIGNERS["scan"]}),
    ):
        with pytest.raises(LifecycleError, match="does not match"):
            require_activation(path, wrong)
    clear_activation(path)
    clear_activation(path)
    with pytest.raises(LifecycleError):
        require_activation(path, binding)


def test_invalid_activation_cannot_authorize_execution(tmp_path):
    path = tmp_path / "activation.json"
    binding = activation_binding(str(uuid.uuid4()), 1, SIGNERS)
    for content in ("{interrupted", "[]", "null", json.dumps({"chain_id": 1})):
        path.write_text(content)
        with pytest.raises(LifecycleError):
            require_activation(path, binding)


def test_activation_requires_declared_signers():
    with pytest.raises(LifecycleError) as error:
        activation_binding(str(uuid.uuid4()), 1, {})
    assert error.value.code == "WRONG_SIGNER"
    with pytest.raises(LifecycleError) as error:
        activation_binding(str(uuid.uuid4()), 1, {"scan": "invalid"})
    assert error.value.code == "WRONG_SIGNER"


def test_execution_lock_survives_nesting_and_releases_after_exception(tmp_path):
    path = tmp_path / "execution.lock"
    contender = (
        "import fcntl,sys; f=open(sys.argv[1], 'a'); "
        "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        with execution_lock(path):
            inode = path.stat().st_ino
            with execution_lock(path):
                pass
            blocked = subprocess.run([sys.executable, "-c", contender, str(path)], capture_output=True)
            assert blocked.returncode != 0
            assert b"BlockingIOError" in blocked.stderr
            raise RuntimeError("interrupted")
    assert path.stat().st_ino == inode
    assert subprocess.run([sys.executable, "-c", contender, str(path)], capture_output=True).returncode == 0


@pytest.mark.asyncio
async def test_async_child_cannot_inherit_parent_lock_ownership(tmp_path):
    path = tmp_path / "execution.lock"

    async def contender():
        with execution_lock(path):
            pytest.fail("Concurrent task entered the execution lock")

    with execution_lock(path):
        with pytest.raises(LifecycleError) as error:
            await asyncio.create_task(contender())
        assert error.value.code == "BUSY"
        with execution_lock(path):
            pass


def test_lock_refuses_a_symlink(tmp_path):
    target = tmp_path / "other"
    target.touch()
    path = tmp_path / "execution.lock"
    path.symlink_to(target)
    with pytest.raises(OSError):
        with execution_lock(path):
            pytest.fail("Symlink was followed")


def test_packaged_migrations_match_source():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for source in (root / "alembic/versions").glob("*.py"):
        assert source.read_bytes() == (root / "tidal/_resources/alembic/versions" / source.name).read_bytes()


def test_offline_cli_checks_exact_database_without_dependency_configuration(db_path, monkeypatch):
    from typer.testing import CliRunner
    from tidal.cli import app
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("Offline inspection attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setenv("RPC_URL", "http://dependency.invalid")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "partial-config-must-not-be-loaded")
    monkeypatch.delenv("TELEGRAM_ADMIN_ALERT_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_OPERATIONS_ALERT_CHAT_ID", raising=False)
    output = CliRunner().invoke(app, ["db", "check", "--database", str(db_path), "--json"])
    assert output.exit_code == 0, output.output
    payload = json.loads(output.output)
    assert payload["interface_version"] == 1
    assert payload["code"] == "OK"
    assert payload["blockers"] == []
    assert payload["data"]["database_identity"] == inspect_database(db_path)["database_identity"]


def test_cli_missing_db_returns_structured_blocker_without_creation(tmp_path):
    from typer.testing import CliRunner
    from tidal.cli import app

    path = tmp_path / "absent.db"
    output = CliRunner().invoke(app, ["db", "check", "--database", str(path), "--json"])
    assert output.exit_code == 1
    assert json.loads(output.output)["blockers"][0]["code"] == "MISSING_DB"
    assert not path.exists()


def test_cli_hold_clears_activation_but_preserves_database(db_path, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from tidal.cli import app

    monkeypatch.setenv("TIDAL_HOME", str(tmp_path))
    path = tmp_path / "activation.json"
    binding = activation_binding(inspect_database(db_path)["database_identity"], 1, SIGNERS)
    write_activation(path, binding)
    before = db_path.read_bytes()
    output = CliRunner().invoke(app, ["hold", "--json"])
    assert output.exit_code == 0, output.output
    assert json.loads(output.output)["code"] == "HELD"
    assert not path.exists()
    assert db_path.read_bytes() == before
