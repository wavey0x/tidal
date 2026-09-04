from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config

_LOGO_COLUMNS = {
    "logo_url",
    "logo_source",
    "logo_status",
    "logo_validated_at",
    "logo_error_message",
}


def _alembic_config(db_path: Path) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _token_columns(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {str(row[1]) for row in connection.execute("PRAGMA table_info(tokens)")}


def _kick_columns(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {
            str(row[1]) for row in connection.execute("PRAGMA table_info(kick_txs)")
        }


def test_drop_token_logo_state_migration_preserves_token_and_price_facts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "tidal.db"
    config = _alembic_config(db_path)
    command.upgrade(config, "0023_bounded_retry_alerts")

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO tokens (
                address,
                chain_id,
                name,
                symbol,
                decimals,
                is_core_reward,
                price_usd,
                price_source,
                price_status,
                price_fetched_at,
                price_run_id,
                price_error_message,
                logo_url,
                logo_source,
                logo_status,
                logo_validated_at,
                logo_error_message,
                first_seen_at,
                last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                1,
                "USD Coin",
                "USDC",
                6,
                0,
                "1",
                "token_price_agg_usd_price",
                "SUCCESS",
                "2026-08-17T00:00:00+00:00",
                "run-1",
                None,
                "https://assets.example/legacy.png",
                "legacy",
                "SUCCESS",
                "2026-03-01T00:00:00+00:00",
                None,
                "2026-03-01T00:00:00+00:00",
                "2026-08-17T00:00:00+00:00",
            ),
        )

    assert _LOGO_COLUMNS <= _token_columns(db_path)

    command.upgrade(config, "head")

    assert _LOGO_COLUMNS.isdisjoint(_token_columns(db_path))
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT address, price_usd, price_status, price_run_id FROM tokens"
        ).fetchone()
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()

    assert row == (
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "1",
        "SUCCESS",
        "run-1",
    )
    assert revision == ("0027_verify_api_receipts",)

    command.downgrade(config, "0023_bounded_retry_alerts")

    assert _LOGO_COLUMNS <= _token_columns(db_path)
    with sqlite3.connect(db_path) as connection:
        restored = connection.execute(
            "SELECT address, price_usd, logo_url FROM tokens"
        ).fetchone()
    assert restored == (
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "1",
        None,
    )


def test_source_and_packaged_logo_migrations_are_identical() -> None:
    root = Path(__file__).resolve().parents[2]
    source = root / "alembic/versions/0024_drop_token_logo_state.py"
    packaged = root / "tidal/_resources/alembic/versions/0024_drop_token_logo_state.py"

    assert source.read_bytes() == packaged.read_bytes()


def test_receipt_verification_migration_requeues_retained_api_hashes_only(tmp_path: Path) -> None:
    db_path = tmp_path / "tidal.db"
    config = _alembic_config(db_path)
    command.upgrade(config, "0026_add_kick_prepare_status_latest")
    with sqlite3.connect(db_path) as connection:
        for action_id, tx_hash in [("retained", "0x1234"), ("unsent", None)]:
            connection.execute(
                """INSERT INTO api_actions
                (action_id, action_type, status, operator_id, request_json, preview_json, created_at, updated_at)
                VALUES (?, 'kick', 'CONFIRMED', 'operator', '{}', '{}', '2026', '2026')""", (action_id,),
            )
            connection.execute(
                """INSERT INTO api_action_transactions
                (action_id, tx_index, operation, to_address, data, value, chain_id, tx_hash,
                 receipt_status, block_number, created_at, updated_at)
                VALUES (?, 0, 'kick', '0x1234', '0x', '0', 1, ?, 'CONFIRMED', 123, '2026', '9999-invalid')""",
                (action_id, tx_hash),
            )
        for run_id in ["api-action:retained", "native-scanner"]:
            connection.execute(
                """INSERT INTO kick_txs (run_id, operation_type, token_address, auction_address, status, tx_hash, created_at)
                VALUES (?, 'kick', '0x1234', '0x5678', 'CONFIRMED', '0x1234', '2026')""", (run_id,),
            )
    command.upgrade(config, "0027_verify_api_receipts")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM api_action_transactions WHERE tx_hash IS NOT NULL AND updated_at <= strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now', '+1 second')"
        ).fetchone() == (1,)
        assert connection.execute("SELECT action_id, status FROM api_actions ORDER BY action_id").fetchall() == [
            ("retained", "BROADCAST_REPORTED"), ("unsent", "PREPARED"),
        ]
        assert connection.execute("SELECT receipt_status, block_number, verified_at FROM api_action_transactions").fetchall() == [
            ("CONFIRMED", 123, None), ("CONFIRMED", 123, None),
        ]
        assert connection.execute("SELECT run_id, status FROM kick_txs ORDER BY run_id").fetchall() == [
            ("api-action:retained", "SUBMITTED"), ("native-scanner", "CONFIRMED"),
        ]
    command.downgrade(config, "0026_add_kick_prepare_status_latest")
    with sqlite3.connect(db_path) as connection:
        assert "verified_at" not in {row[1] for row in connection.execute("PRAGMA table_info(api_action_transactions)")}
    root = Path(__file__).resolve().parents[2]
    assert (root / "alembic/versions/0027_verify_api_receipts.py").read_bytes() == (
        root / "tidal/_resources/alembic/versions/0027_verify_api_receipts.py"
    ).read_bytes()


def test_auction_history_baseline_migration_defaults_existing_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "tidal.db"
    config = _alembic_config(db_path)
    command.upgrade(config, "0024_drop_token_logo_state")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO kick_txs (
                run_id, operation_type, token_address, auction_address, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "run-1",
                "kick",
                "0x00000000000000000000000000000000000000b1",
                "0x00000000000000000000000000000000000000a1",
                "CONFIRMED",
                "2026-08-21T00:00:00+00:00",
            ),
        )

    command.upgrade(config, "head")

    assert {
        "historical_baseline",
        "historical_baseline_reason",
        "historical_baselined_at",
    } <= _kick_columns(db_path)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT historical_baseline, historical_baseline_reason,
                   historical_baselined_at
            FROM kick_txs
            """
        ).fetchone()
    assert row == (0, None, None)


def test_source_and_packaged_baseline_migrations_are_identical() -> None:
    root = Path(__file__).resolve().parents[2]
    source = root / "alembic/versions/0025_add_auction_history_baselines.py"
    packaged = (
        root / "tidal/_resources/alembic/versions/0025_add_auction_history_baselines.py"
    )

    assert source.read_bytes() == packaged.read_bytes()


def test_kick_prepare_status_migration_and_packaged_copy(tmp_path: Path) -> None:
    db_path = tmp_path / "tidal.db"
    config = _alembic_config(db_path)
    command.upgrade(config, "head")

    with sqlite3.connect(db_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(kick_prepare_status_latest)"
            )
        }
    assert {
        "source_type",
        "source_address",
        "auction_address",
        "token_address",
        "status",
        "reason",
        "source_balance_raw",
        "detail_json",
        "checked_at",
    } <= columns

    root = Path(__file__).resolve().parents[2]
    source = root / "alembic/versions/0026_add_kick_prepare_status_latest.py"
    packaged = (
        root
        / "tidal/_resources/alembic/versions/0026_add_kick_prepare_status_latest.py"
    )
    assert source.read_bytes() == packaged.read_bytes()
