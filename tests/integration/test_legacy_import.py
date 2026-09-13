import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, update

from tidal.config import Settings
from tidal.legacy_import import import_legacy
from tidal.lifecycle import LifecycleError, activation_binding, inspect_database, write_activation
from tidal.persistence import models
from tidal.persistence.db import Database

HASH = "0x" + "ab" * 32
SIGNER = "0x" + "1" * 40
TARGET = "0x" + "2" * 40


@pytest.fixture
def retained(tmp_path, monkeypatch):
    monkeypatch.setenv("TIDAL_HOME", str(tmp_path))
    original, target, outbox = (tmp_path / name for name in ("original.db", "tidal.db", "outbox.db"))
    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{original}")
    command.upgrade(cfg, "0028_application_identity")
    with closing(sqlite3.connect(original)) as connection:
        connection.execute("""INSERT INTO api_actions
            (action_id, action_type, status, operator_id, sender, request_json, preview_json, created_at, updated_at)
            VALUES ('retained', 'kick', 'PREPARED', 'fixture', ?, '{}', '{}', '2026-09-01', '2026-09-01')""", (SIGNER,))
        connection.execute("""INSERT INTO api_action_transactions
            (action_id, tx_index, operation, to_address, data, value, chain_id, created_at, updated_at)
            VALUES ('retained', 0, 'kick', ?, '0x1234', '0x0', 1, '2026-09-01', '2026-09-01')""", (TARGET,))
        connection.commit()
        with closing(sqlite3.connect(target)) as destination:
            connection.backup(destination)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{target}")
    command.upgrade(cfg, "head")
    with closing(sqlite3.connect(outbox)) as connection:
        connection.execute("""CREATE TABLE action_report_outbox (
            id INTEGER PRIMARY KEY, base_url TEXT, action_id TEXT, tx_index INTEGER,
            report_type TEXT, payload_json TEXT, created_at TEXT, updated_at TEXT)""")
        connection.commit()
    settings = Settings(DB_PATH=target, MANAGED_SIGNERS={"scan": SIGNER, "kick": SIGNER})
    database = Database(settings.database_url)
    binding = activation_binding(inspect_database(target)["database_identity"], 1, settings.managed_signers)
    write_activation(tmp_path / "activation.json", binding)
    with database.session() as session:
        yield settings, session, original, outbox
    database.engine.dispose()


def test_retained_api_transaction_is_imported_even_without_an_outbox_report(retained):
    settings, session, original, outbox = retained
    preview = {"preparedOperations": [
        {"operation": "kick", "txIndex": 0, "auctionAddress": TARGET,
         "tokenAddress": "0x" + "3" * 40, "sellAmount": "99999999999999999999999999"},
        {"operation": "kick", "txIndex": 0, "auctionAddress": TARGET,
         "tokenAddress": "0x" + "4" * 40, "sellAmount": "7"},
    ]}
    with closing(sqlite3.connect(original)) as connection:
        connection.execute("UPDATE api_actions SET preview_json=?", (json.dumps(preview),))
        connection.execute("UPDATE api_action_transactions SET tx_hash=?, broadcast_at='original-submit-time'", (HASH,))
        connection.commit()
    outcome = run(retained)
    assert outcome["reports_read"] == 0 and outcome["transactions_imported"] == 1
    transaction = session.execute(select(models.transactions)).mappings().one()
    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert transaction["status"] == "PENDING"
    assert len(rows) == 2 and {row["transaction_id"] for row in rows} == {transaction["id"]}
    assert {row["created_at"] for row in rows} == {"original-submit-time"}
    assert rows[0]["requested_sell_amount"] == "99999999999999999999999999"
    # Missing historical source identity stays missing; today's mapping is not evidence.
    assert all(row["source_address"] is None for row in rows)
    run(retained)
    assert len(session.execute(select(models.kick_txs)).all()) == 2


def test_index_free_multi_transaction_preview_is_not_guessed(retained):
    settings, session, original, outbox = retained
    with closing(sqlite3.connect(original)) as connection:
        connection.execute("UPDATE api_actions SET preview_json=?", (json.dumps({
            "preparedOperations": [{"operation": "kick", "auctionAddress": TARGET, "tokenAddress": "0x" + "3" * 40}],
        }),))
        connection.execute("UPDATE api_action_transactions SET tx_hash=?", (HASH,))
        connection.execute("""INSERT INTO api_action_transactions
            (action_id, tx_index, operation, to_address, data, value, chain_id, created_at, updated_at)
            VALUES ('retained', 1, 'kick', ?, '0x5678', '0x0', 1, '2026-09-01', '2026-09-01')""", (TARGET,))
        connection.commit()
    run(retained)
    assert session.execute(select(models.kick_txs)).first() is None
    assert session.execute(select(models.transactions.c.status)).scalar_one() == "PENDING"

def report(outbox, kind="submission", **overrides):
    payload = {"txIndex": 0, "txHash": HASH, "sender": SIGNER, "chainId": 1, "nonce": 7,
               "broadcastAt": "2026-09-01T02:03:04+00:00", **overrides}
    with closing(sqlite3.connect(outbox)) as connection:
        connection.execute("""INSERT INTO action_report_outbox
            (base_url, action_id, tx_index, report_type, payload_json, created_at, updated_at)
            VALUES ('https://example.invalid', 'retained', 0, ?, ?, '2026-09-01', '2026-09-02')""", (kind, json.dumps(payload)))
        connection.commit()


def run(retained):
    settings, session, original, outbox = retained
    return import_legacy(settings=settings, session=session, source_database=original, outbox=outbox)


def test_delivered_submission_is_imported_without_modifying_sources_and_retries_are_idempotent(retained):
    settings, session, original, outbox = retained
    # Broadcast/receipt reports can be delivered and removed while submission
    # identity still survives locally. It must still enter the native ledger.
    report(outbox)
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (original, outbox)]
    assert run(retained)["transactions_imported"] == 1
    assert not (settings.resolved_home_path / "activation.json").exists()
    row = session.execute(select(models.transactions)).mappings().one()
    assert (row["nonce"], row["signer"], row["status"], row["value"]) == (7, SIGNER, "PENDING", "0")
    assert row["data"] == "0x1234"
    assert run(retained)["transactions_imported"] == 1
    assert session.execute(select(models.transactions)).mappings().one()["id"] == row["id"]
    assert before == [hashlib.sha256(path.read_bytes()).hexdigest() for path in (original, outbox)]


def test_reported_receipt_cannot_manufacture_finality(retained):
    _, session, _, outbox = retained
    report(outbox, "receipt", receiptStatus="CONFIRMED", blockNumber=123)
    run(retained)
    row = session.execute(select(models.transactions)).mappings().one()
    assert row["status"] == "PENDING"
    assert row["verified_at"] is None
    assert row["block_number"] is None


def test_conflicting_reports_rollback_entire_import_and_keep_activation_held(retained):
    settings, session, _, outbox = retained
    report(outbox)
    report(outbox, "receipt", nonce=8)
    with pytest.raises(LifecycleError, match="nonce"):
        run(retained)
    row = session.execute(select(models.transactions)).mappings().one()
    assert row["tx_hash"] is None
    assert row["nonce"] is None
    assert not (settings.resolved_home_path / "activation.json").exists()


def test_unknown_request_retains_identity_with_missing_intent_visible(retained):
    _, session, _, outbox = retained
    report(outbox)
    with closing(sqlite3.connect(outbox)) as connection:
        connection.execute("UPDATE action_report_outbox SET action_id='missing-preview'")
        connection.commit()
    run(retained)
    row = session.execute(select(models.transactions).where(models.transactions.c.tx_hash == HASH)).mappings().one()
    assert row["nonce"] == 7
    assert row["data"] is None
    assert row["status"] == "PENDING"


def test_reimport_never_downgrades_natively_verified_evidence(retained):
    _, session, _, outbox = retained
    report(outbox)
    run(retained)
    session.execute(update(models.transactions).values(
        status="CONFIRMED", verified_at="2026-09-02", block_hash="0x" + "cd" * 32,
    ))
    session.commit()
    run(retained)
    assert session.execute(select(models.transactions.c.status)).scalar_one() == "CONFIRMED"


def test_malformed_source_is_not_silently_discarded(retained):
    _, _, _, outbox = retained
    report(outbox, txHash=None)
    with pytest.raises(LifecycleError, match="no transaction hash"):
        run(retained)
