import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import insert, select

from test_legacy_import import retained, report, run, HASH, TARGET
from test_migrations import _alembic_config
from tidal.lifecycle import LifecycleError, SCHEMA_REVISION, inspect_database
from tidal.migrate_state import migrate_state
from tidal.persistence import models


def tables(path):
    with closing(sqlite3.connect(path)) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_populated_alembic_upgrade_cannot_silently_retire_action_history(retained):
    settings, _, _, _ = retained
    with pytest.raises(RuntimeError, match="protected --source-database"):
        command.upgrade(_alembic_config(settings.resolved_db_path), "head")
    assert "api_actions" in tables(settings.resolved_db_path)


def test_native_migration_requires_pair_and_never_uses_target_as_original(retained):
    settings, _, original, outbox = retained
    before = settings.resolved_db_path.read_bytes()
    for kwargs, code in [({}, "LEGACY_SOURCES_REQUIRED"),
                         ({"source_database": original}, "LEGACY_SOURCES_REQUIRED"),
                         ({"source_database": settings.resolved_db_path, "outbox": outbox}, "INVALID_LEGACY_SOURCE")]:
        with pytest.raises(LifecycleError) as caught:
            migrate_state(settings, **kwargs)
        assert caught.value.code == code
    assert settings.resolved_db_path.read_bytes() == before


def test_native_transition_imports_outbox_then_retires_jobs_without_changing_originals(retained):
    settings, session, original, outbox = retained
    report(outbox)
    session.close()
    # Exercise the actual pre-ledger start, not only the interrupted midpoint.
    with closing(sqlite3.connect(original)) as source, closing(sqlite3.connect(settings.resolved_db_path)) as target:
        source.backup(target)
    originals = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (original, outbox)]
    result = migrate_state(settings, source_database=original, outbox=outbox)
    assert result["code"] == "MIGRATED_AND_HELD"
    assert result["data"]["schema_revision"] == SCHEMA_REVISION
    assert result["data"]["transactions_imported"] == 1
    assert "api_actions" not in tables(settings.resolved_db_path)
    assert "api_action_transactions" not in tables(settings.resolved_db_path)
    row = session.execute(select(models.transactions)).mappings().one()
    assert row["tx_hash"] == HASH and row["nonce"] == 7 and row["status"] == "PENDING"
    session.close()
    assert migrate_state(settings)["code"] == "MIGRATED_AND_HELD"
    assert not (settings.resolved_home_path / "activation.json").exists()
    assert originals == [hashlib.sha256(path.read_bytes()).hexdigest() for path in (original, outbox)]


def test_interrupted_import_is_retryable_and_unsigned_previews_are_discarded(retained, monkeypatch):
    import tidal.migrate_state as transition
    settings, session, original, outbox = retained
    report(outbox)
    session.close()
    actual = transition.run_migrations

    def interrupted(url, revision="head", **kwargs):
        if revision == "head":
            raise KeyboardInterrupt("interruption after durable import")
        return actual(url, revision, **kwargs)

    monkeypatch.setattr(transition, "run_migrations", interrupted)
    with pytest.raises(KeyboardInterrupt):
        migrate_state(settings, source_database=original, outbox=outbox)
    assert "api_actions" in tables(settings.resolved_db_path)
    monkeypatch.setattr(transition, "run_migrations", actual)
    session.execute(insert(models.transactions).values(
        operation="kick", legacy=1, created_at="unsigned", updated_at="unsigned",
    ))
    session.commit()
    migrate_state(settings, source_database=original, outbox=outbox)
    assert session.execute(select(models.transactions.c.tx_hash)).scalars().all() == [HASH]
    inspect_database(settings.resolved_db_path)


def test_unknown_submission_with_business_links_survives_retirement(retained):
    settings, session, original, outbox = retained
    transaction_id = session.execute(insert(models.transactions).values(
        operation="kick", status="REVIEW_REQUIRED", legacy=1, created_at="original", updated_at="original",
    )).lastrowid
    session.execute(insert(models.kick_txs).values(
        run_id="old-scanner", operation_type="kick", token_address=TARGET, auction_address=TARGET,
        status="SUBMITTED", created_at="original", transaction_id=transaction_id,
    ))
    session.commit()
    migrate_state(settings, source_database=original, outbox=outbox)
    row = session.execute(select(models.transactions)).mappings().one()
    assert row["id"] == transaction_id and row["status"] == "REVIEW_REQUIRED" and row["tx_hash"] is None


@pytest.mark.parametrize("preview", [{"preparedOperations": []}, {"decision": "skip", "preparedOperations": []}])
def test_unproven_existing_api_business_rows_stop_import_before_retirement(retained, preview):
    import json
    settings, session, original, outbox = retained
    report(outbox)
    with closing(sqlite3.connect(original)) as connection:
        connection.execute("UPDATE api_actions SET preview_json=?", (json.dumps(preview),))
        connection.commit()
    session.execute(insert(models.kick_txs).values(
        run_id="api-action:retained", operation_type="kick", token_address=TARGET, auction_address=TARGET,
        status="SUBMITTED", tx_hash=HASH, created_at="original",
    ))
    session.commit()
    with pytest.raises(LifecycleError, match="provable scope"):
        migrate_state(settings, source_database=original, outbox=outbox)
    assert "api_actions" in tables(settings.resolved_db_path)
    assert session.execute(select(models.transactions.c.tx_hash)).scalar_one() is None
    assert not (settings.resolved_home_path / "activation.json").exists()


def test_packaged_retirement_migration_matches_source():
    root = Path(__file__).resolve().parents[2]
    name = "versions/0031_retire_action_protocol.py"
    assert (root / "alembic" / name).read_bytes() == (root / "tidal/_resources/alembic" / name).read_bytes()
