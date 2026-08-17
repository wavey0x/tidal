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


def test_drop_token_logo_state_migration_preserves_token_and_price_facts(tmp_path: Path) -> None:
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
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert row == (
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "1",
        "SUCCESS",
        "run-1",
    )
    assert revision == ("0024_drop_token_logo_state",)

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
