"""SQLAlchemy table definitions."""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)

metadata = MetaData()

vaults = Table(
    "vaults",
    metadata,
    Column("address", String, primary_key=True),
    Column("chain_id", Integer, nullable=False),
    Column("name", String, nullable=True),
    Column("symbol", String, nullable=True),
    Column("active", Integer, nullable=False, server_default="1"),
    Column("first_seen_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
)

strategies = Table(
    "strategies",
    metadata,
    Column("address", String, primary_key=True),
    Column("chain_id", Integer, nullable=False),
    Column("vault_address", String, nullable=False),
    Column("name", String, nullable=True),
    Column("adapter", String, nullable=False, server_default="yearn_curve_strategy"),
    Column("active", Integer, nullable=False, server_default="1"),
    Column("auction_address", String, nullable=True),
    Column("auction_updated_at", String, nullable=True),
    Column("auction_error_message", Text, nullable=True),
    Column("first_seen_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
)

tokens = Table(
    "tokens",
    metadata,
    Column("address", String, primary_key=True),
    Column("chain_id", Integer, nullable=False),
    Column("name", String, nullable=True),
    Column("symbol", String, nullable=True),
    Column("decimals", Integer, nullable=False),
    Column("is_core_reward", Integer, nullable=False, server_default="0"),
    Column("price_usd", Text, nullable=True),
    Column("price_source", String, nullable=True),
    Column("price_status", String, nullable=True),
    Column("price_fetched_at", String, nullable=True),
    Column("price_run_id", String, nullable=True),
    Column("price_error_message", Text, nullable=True),
    Column("logo_url", Text, nullable=True),
    Column("logo_source", String, nullable=True),
    Column("logo_status", String, nullable=True),
    Column("logo_validated_at", String, nullable=True),
    Column("logo_error_message", Text, nullable=True),
    Column("first_seen_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
)

strategy_tokens = Table(
    "strategy_tokens",
    metadata,
    Column("strategy_address", String, nullable=False),
    Column("token_address", String, nullable=False),
    Column("source", String, nullable=False),
    Column("active", Integer, nullable=False, server_default="1"),
    Column("first_seen_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
    PrimaryKeyConstraint("strategy_address", "token_address"),
)

strategy_token_balances_latest = Table(
    "strategy_token_balances_latest",
    metadata,
    Column("strategy_address", String, nullable=False),
    Column("token_address", String, nullable=False),
    Column("raw_balance", Text, nullable=False),
    Column("normalized_balance", Text, nullable=False),
    Column("block_number", Integer, nullable=False),
    Column("scanned_at", String, nullable=False),
    PrimaryKeyConstraint("strategy_address", "token_address"),
)

scan_runs = Table(
    "scan_runs",
    metadata,
    Column("run_id", String, primary_key=True),
    Column("started_at", String, nullable=False),
    Column("finished_at", String, nullable=True),
    Column("status", String, nullable=False),
    Column("vaults_seen", Integer, nullable=False),
    Column("strategies_seen", Integer, nullable=False),
    Column("pairs_seen", Integer, nullable=False),
    Column("pairs_succeeded", Integer, nullable=False),
    Column("pairs_failed", Integer, nullable=False),
    Column("error_summary", Text, nullable=True),
)

scan_item_errors = Table(
    "scan_item_errors",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, nullable=False),
    Column("strategy_address", String, nullable=True),
    Column("token_address", String, nullable=True),
    Column("stage", String, nullable=False),
    Column("error_code", String, nullable=False),
    Column("error_message", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

Index("ix_strategy_token_balances_strategy_scanned", strategy_token_balances_latest.c.strategy_address, strategy_token_balances_latest.c.scanned_at)
Index("ix_scan_item_errors_run_id", scan_item_errors.c.run_id)
Index("ix_scan_item_errors_identity", scan_item_errors.c.strategy_address, scan_item_errors.c.token_address, scan_item_errors.c.stage, scan_item_errors.c.error_code)
