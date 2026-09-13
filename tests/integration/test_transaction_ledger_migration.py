from pathlib import Path
from contextlib import closing
import sqlite3

import pytest
from alembic import command
from alembic.config import Config

HASH = "0x" + "a" * 64
SENDER = "0x" + "1" * 40
TARGET = "0x" + "2" * 40
AUCTION = "0x" + "3" * 40
TOKEN = "0x" + "4" * 40


def legacy_db(tmp_path):
    path = tmp_path / "legacy.db"
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "0028_application_identity")
    return path, config


def add_action(connection, identifier, *, calldata="0x1234", sender=SENDER):
    connection.execute("""
        INSERT INTO api_actions (action_id, action_type, status, operator_id, sender,
                                 request_json, preview_json, created_at, updated_at)
        VALUES (?, 'kick', 'BROADCAST_REPORTED', 'fixture', ?, '{}', '{}', '2026-09-01', '2026-09-01')
    """, (identifier, sender))
    connection.execute("""
        INSERT INTO api_action_transactions
            (action_id, tx_index, operation, to_address, data, value, chain_id, tx_hash, created_at, updated_at)
        VALUES (?, 0, 'kick', ?, ?, '0', 1, ?, '2026-09-01', '2026-09-01')
    """, (identifier, TARGET, calldata, HASH))


def add_operation(connection, *, operation="kick", tx_hash=HASH, status="SUBMITTED", round_id=None):
    return connection.execute("""
        INSERT INTO kick_txs (run_id, operation_type, token_address, auction_address, status, tx_hash,
                             requested_sell_amount, sell_amount, created_at, round_kick_id,
                             historical_baseline, historical_baseline_reason, historical_baselined_at)
        VALUES ('retained', ?, ?, ?, ?, ?, '123456789012345678901234', '123456789012345678901234',
                '2026-08-31T01:02:03+00:00', ?, 1, 'reviewed', '2026-09-01T00:00:00+00:00')
    """, (operation, TOKEN, AUCTION, status, tx_hash, round_id)).lastrowid


def test_one_transaction_row_keeps_all_business_rows_round_links_and_policy_facts(tmp_path):
    path, config = legacy_db(tmp_path)
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        add_action(connection, "first")
        add_action(connection, "duplicate")
        kick_id = add_operation(connection)
        add_operation(connection, operation="resolve_auction", round_id=kick_id)
        connection.commit()
        before = [dict(row) for row in connection.execute("SELECT * FROM kick_txs ORDER BY id")]
    command.upgrade(config, "head")
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        transactions = [dict(row) for row in connection.execute("SELECT * FROM transactions")]
        assert len(transactions) == 1
        assert transactions[0]["signer"] == SENDER
        assert transactions[0]["nonce"] is None
        assert transactions[0]["status"] == "PENDING"
        after = [dict(row) for row in connection.execute("SELECT * FROM kick_txs ORDER BY id")]
        for old, new in zip(before, after, strict=True):
            assert new.pop("transaction_id") == transactions[0]["id"]
            assert new == old
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='api_action_transactions'").fetchone() is None


@pytest.mark.parametrize("changed", ["intent", "signer"])
def test_conflicting_duplicate_identity_stops_before_replacing_source_tables(tmp_path, changed):
    path, config = legacy_db(tmp_path)
    with closing(sqlite3.connect(path)) as connection:
        add_action(connection, "first")
        add_action(connection, "conflict", calldata="0x5678" if changed == "intent" else "0x1234",
                   sender=TARGET if changed == "signer" else SENDER)
        connection.commit()
    with pytest.raises(RuntimeError, match="Conflicting retained transaction intents"):
        command.upgrade(config, "head")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT count(*) FROM api_action_transactions").fetchone() == (2,)
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='transactions'").fetchone() is None


def test_unknown_legacy_submission_is_explicit_and_never_gets_an_invented_identity(tmp_path):
    path, config = legacy_db(tmp_path)
    with closing(sqlite3.connect(path)) as connection:
        add_operation(connection, tx_hash=None)
        connection.commit()
    command.upgrade(config, "head")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT status, legacy, tx_hash, signer, nonce, chain_id, to_address, data, value FROM transactions"
        ).fetchone() == ("REVIEW_REQUIRED", 1, None, None, None, None, None, None, None)
        assert connection.execute("SELECT transaction_id FROM kick_txs").fetchone()[0] is not None


def test_retained_scanner_transactions_are_linked_without_rewriting_history(tmp_path):
    path, config = legacy_db(tmp_path)
    with closing(sqlite3.connect(path)) as connection:
        first = add_operation(connection, status="CONFIRMED")
        second = add_operation(connection, status="CONFIRMED")
        connection.commit()
    command.upgrade(config, "head")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT count(*) FROM transactions").fetchone() == (1,)
        assert connection.execute("SELECT status, legacy, signer, nonce FROM transactions").fetchone() == ("CONFIRMED", 1, None, None)
        rows = connection.execute("SELECT id, transaction_id FROM kick_txs ORDER BY id").fetchall()
        assert [row[0] for row in rows] == [first, second]
        assert rows[0][1] == rows[1][1]
