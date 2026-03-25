"""add fee burner tables and generic source fields

Revision ID: 0013_add_fee_burners_and_source_fields
Revises: 0012_add_kick_price_logging_columns
Create Date: 2026-03-25 12:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_add_fee_burners_and_source_fields"
down_revision = "0012_add_kick_price_logging_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fee_burners",
        sa.Column("address", sa.String(), primary_key=True),
        sa.Column("chain_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("auction_address", sa.String(), nullable=True),
        sa.Column("want_address", sa.String(), nullable=True),
        sa.Column("auction_version", sa.String(), nullable=True),
        sa.Column("auction_updated_at", sa.String(), nullable=True),
        sa.Column("auction_error_message", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.String(), nullable=False),
        sa.Column("last_seen_at", sa.String(), nullable=False),
    )

    op.create_table(
        "fee_burner_tokens",
        sa.Column("fee_burner_address", sa.String(), nullable=False),
        sa.Column("token_address", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.String(), nullable=False),
        sa.Column("last_seen_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("fee_burner_address", "token_address"),
    )

    op.create_table(
        "fee_burner_token_balances_latest",
        sa.Column("fee_burner_address", sa.String(), nullable=False),
        sa.Column("token_address", sa.String(), nullable=False),
        sa.Column("raw_balance", sa.Text(), nullable=False),
        sa.Column("normalized_balance", sa.Text(), nullable=False),
        sa.Column("block_number", sa.Integer(), nullable=False),
        sa.Column("scanned_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("fee_burner_address", "token_address"),
    )
    op.create_index(
        "ix_fee_burner_token_balances_scanned",
        "fee_burner_token_balances_latest",
        ["fee_burner_address", "scanned_at"],
    )

    op.add_column("scan_item_errors", sa.Column("source_type", sa.String(), nullable=True))
    op.add_column("scan_item_errors", sa.Column("source_address", sa.String(), nullable=True))
    op.execute(
        "UPDATE scan_item_errors "
        "SET source_type = 'strategy', source_address = strategy_address "
        "WHERE strategy_address IS NOT NULL"
    )
    op.create_index(
        "ix_scan_item_errors_source_identity",
        "scan_item_errors",
        ["source_address", "token_address", "stage", "error_code"],
    )

    op.add_column("kick_txs", sa.Column("source_type", sa.String(), nullable=True))
    op.add_column("kick_txs", sa.Column("source_address", sa.String(), nullable=True))
    op.execute(
        "UPDATE kick_txs "
        "SET source_type = 'strategy', source_address = strategy_address "
        "WHERE strategy_address IS NOT NULL"
    )
    op.create_index(
        "ix_kick_txs_source_token_created",
        "kick_txs",
        ["source_address", "token_address", sa.text("created_at DESC")],
    )
    with op.batch_alter_table("kick_txs") as batch_op:
        batch_op.alter_column("strategy_address", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    op.execute(
        "UPDATE kick_txs "
        "SET strategy_address = source_address "
        "WHERE strategy_address IS NULL AND source_address IS NOT NULL"
    )
    with op.batch_alter_table("kick_txs") as batch_op:
        batch_op.alter_column("strategy_address", existing_type=sa.String(), nullable=False)
    op.drop_index("ix_kick_txs_source_token_created", table_name="kick_txs")
    op.drop_column("kick_txs", "source_address")
    op.drop_column("kick_txs", "source_type")

    op.drop_index("ix_scan_item_errors_source_identity", table_name="scan_item_errors")
    op.drop_column("scan_item_errors", "source_address")
    op.drop_column("scan_item_errors", "source_type")

    op.drop_index("ix_fee_burner_token_balances_scanned", table_name="fee_burner_token_balances_latest")
    op.drop_table("fee_burner_token_balances_latest")
    op.drop_table("fee_burner_tokens")
    op.drop_table("fee_burners")
