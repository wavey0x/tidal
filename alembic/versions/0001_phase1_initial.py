"""phase1 initial schema

Revision ID: 0001_phase1_initial
Revises:
Create Date: 2026-03-03 16:20:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_phase1_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategies",
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("chain_id", sa.Integer(), nullable=False),
        sa.Column("vault_address", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("adapter", sa.String(), nullable=False, server_default="yearn_curve_strategy"),
        sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.String(), nullable=False),
        sa.Column("last_seen_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("address"),
    )

    op.create_table(
        "tokens",
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("chain_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("symbol", sa.String(), nullable=True),
        sa.Column("decimals", sa.Integer(), nullable=False),
        sa.Column("is_core_reward", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.String(), nullable=False),
        sa.Column("last_seen_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("address"),
    )

    op.create_table(
        "strategy_tokens",
        sa.Column("strategy_address", sa.String(), nullable=False),
        sa.Column("token_address", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.String(), nullable=False),
        sa.Column("last_seen_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("strategy_address", "token_address"),
    )

    op.create_table(
        "strategy_token_balances_latest",
        sa.Column("strategy_address", sa.String(), nullable=False),
        sa.Column("token_address", sa.String(), nullable=False),
        sa.Column("raw_balance", sa.Text(), nullable=False),
        sa.Column("normalized_balance", sa.Text(), nullable=False),
        sa.Column("block_number", sa.Integer(), nullable=False),
        sa.Column("scanned_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("strategy_address", "token_address"),
    )

    op.create_table(
        "scan_runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("started_at", sa.String(), nullable=False),
        sa.Column("finished_at", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("vaults_seen", sa.Integer(), nullable=False),
        sa.Column("strategies_seen", sa.Integer(), nullable=False),
        sa.Column("pairs_seen", sa.Integer(), nullable=False),
        sa.Column("pairs_succeeded", sa.Integer(), nullable=False),
        sa.Column("pairs_failed", sa.Integer(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )

    op.create_table(
        "scan_item_errors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("strategy_address", sa.String(), nullable=True),
        sa.Column("token_address", sa.String(), nullable=True),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("error_code", sa.String(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_strategy_token_balances_strategy_scanned",
        "strategy_token_balances_latest",
        ["strategy_address", "scanned_at"],
        unique=False,
    )
    op.create_index("ix_scan_item_errors_run_id", "scan_item_errors", ["run_id"], unique=False)
    op.create_index(
        "ix_scan_item_errors_identity",
        "scan_item_errors",
        ["strategy_address", "token_address", "stage", "error_code"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_scan_item_errors_identity", table_name="scan_item_errors")
    op.drop_index("ix_scan_item_errors_run_id", table_name="scan_item_errors")
    op.drop_index("ix_strategy_token_balances_strategy_scanned", table_name="strategy_token_balances_latest")
    op.drop_table("scan_item_errors")
    op.drop_table("scan_runs")
    op.drop_table("strategy_token_balances_latest")
    op.drop_table("strategy_tokens")
    op.drop_table("tokens")
    op.drop_table("strategies")
