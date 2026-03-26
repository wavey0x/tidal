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
    Column("deposit_limit", Text, nullable=True),
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
    Column("want_address", String, nullable=True),
    Column("auction_version", String, nullable=True),
    Column("auction_updated_at", String, nullable=True),
    Column("auction_error_message", Text, nullable=True),
    Column("first_seen_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
)

fee_burners = Table(
    "fee_burners",
    metadata,
    Column("address", String, primary_key=True),
    Column("chain_id", Integer, nullable=False),
    Column("name", String, nullable=True),
    Column("active", Integer, nullable=False, server_default="1"),
    Column("auction_address", String, nullable=True),
    Column("want_address", String, nullable=True),
    Column("auction_version", String, nullable=True),
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

fee_burner_tokens = Table(
    "fee_burner_tokens",
    metadata,
    Column("fee_burner_address", String, nullable=False),
    Column("token_address", String, nullable=False),
    Column("source", String, nullable=False),
    Column("active", Integer, nullable=False, server_default="1"),
    Column("first_seen_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
    PrimaryKeyConstraint("fee_burner_address", "token_address"),
)

fee_burner_token_balances_latest = Table(
    "fee_burner_token_balances_latest",
    metadata,
    Column("fee_burner_address", String, nullable=False),
    Column("token_address", String, nullable=False),
    Column("raw_balance", Text, nullable=False),
    Column("normalized_balance", Text, nullable=False),
    Column("block_number", Integer, nullable=False),
    Column("scanned_at", String, nullable=False),
    PrimaryKeyConstraint("fee_burner_address", "token_address"),
)

auction_enabled_tokens_latest = Table(
    "auction_enabled_tokens_latest",
    metadata,
    Column("auction_address", String, nullable=False),
    Column("token_address", String, nullable=False),
    Column("active", Integer, nullable=False, server_default="1"),
    Column("first_seen_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
    PrimaryKeyConstraint("auction_address", "token_address"),
)

auction_enabled_token_scans = Table(
    "auction_enabled_token_scans",
    metadata,
    Column("auction_address", String, primary_key=True),
    Column("scanned_at", String, nullable=False),
    Column("block_number", Integer, nullable=True),
    Column("status", String, nullable=False),
    Column("error_message", Text, nullable=True),
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
    Column("source_type", String, nullable=True),
    Column("source_address", String, nullable=True),
    Column("strategy_address", String, nullable=True),
    Column("token_address", String, nullable=True),
    Column("stage", String, nullable=False),
    Column("error_code", String, nullable=False),
    Column("error_message", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

txn_runs = Table(
    "txn_runs",
    metadata,
    Column("run_id", String, primary_key=True),
    Column("started_at", String, nullable=False),
    Column("finished_at", String, nullable=True),
    Column("status", String, nullable=False),
    Column("candidates_found", Integer, nullable=False, server_default="0"),
    Column("kicks_attempted", Integer, nullable=False, server_default="0"),
    Column("kicks_succeeded", Integer, nullable=False, server_default="0"),
    Column("kicks_failed", Integer, nullable=False, server_default="0"),
    Column("live", Integer, nullable=False, server_default="0"),
    Column("error_summary", Text, nullable=True),
)

kick_txs = Table(
    "kick_txs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, nullable=False),
    Column("operation_type", String, nullable=False, server_default="kick"),
    Column("source_type", String, nullable=True),
    Column("source_address", String, nullable=True),
    Column("strategy_address", String, nullable=True),
    Column("token_address", String, nullable=False),
    Column("auction_address", String, nullable=False),
    Column("sell_amount", Text, nullable=True),
    Column("starting_price", Text, nullable=True),
    Column("minimum_price", Text, nullable=True),
    Column("price_usd", Text, nullable=True),
    Column("usd_value", Text, nullable=True),
    Column("status", String, nullable=False),
    Column("tx_hash", String, nullable=True),
    Column("gas_used", Integer, nullable=True),
    Column("gas_price_gwei", Text, nullable=True),
    Column("block_number", Integer, nullable=True),
    Column("error_message", Text, nullable=True),
    Column("quote_amount", Text, nullable=True),
    Column("quote_response_json", Text, nullable=True),
    Column("start_price_buffer_bps", Integer, nullable=True),
    Column("min_price_buffer_bps", Integer, nullable=True),
    Column("step_decay_rate_bps", Integer, nullable=True),
    Column("settle_token", String, nullable=True),
    Column("stuck_abort_reason", Text, nullable=True),
    Column("token_symbol", String, nullable=True),
    Column("want_address", String, nullable=True),
    Column("want_symbol", String, nullable=True),
    Column("normalized_balance", Text, nullable=True),
    Column("auctionscan_round_id", Integer, nullable=True),
    Column("auctionscan_last_checked_at", String, nullable=True),
    Column("auctionscan_matched_at", String, nullable=True),
    Column("created_at", String, nullable=False),
)

Index("ix_strategy_token_balances_strategy_scanned", strategy_token_balances_latest.c.strategy_address, strategy_token_balances_latest.c.scanned_at)
Index("ix_fee_burner_token_balances_scanned", fee_burner_token_balances_latest.c.fee_burner_address, fee_burner_token_balances_latest.c.scanned_at)
Index("ix_auction_enabled_tokens_latest_active", auction_enabled_tokens_latest.c.auction_address, auction_enabled_tokens_latest.c.active)
Index("ix_scan_item_errors_run_id", scan_item_errors.c.run_id)
Index("ix_scan_item_errors_source_identity", scan_item_errors.c.source_address, scan_item_errors.c.token_address, scan_item_errors.c.stage, scan_item_errors.c.error_code)
Index("ix_scan_item_errors_identity", scan_item_errors.c.strategy_address, scan_item_errors.c.token_address, scan_item_errors.c.stage, scan_item_errors.c.error_code)
Index("ix_kick_txs_source_token_created", kick_txs.c.source_address, kick_txs.c.token_address, kick_txs.c.created_at.desc())
Index("ix_kick_txs_strategy_token_created", kick_txs.c.strategy_address, kick_txs.c.token_address, kick_txs.c.created_at.desc())
