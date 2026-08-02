"""add auction/token created index to kick_txs

Revision ID: 0021_add_auction_token_kick_index
Revises: 0020_add_minimum_quote_to_kick_txs
Create Date: 2026-03-30 18:30:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "0021_add_auction_token_kick_index"
down_revision = "0020_add_minimum_quote_to_kick_txs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_kick_txs_auction_token_created",
        "kick_txs",
        ["auction_address", "token_address", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_kick_txs_auction_token_created", table_name="kick_txs")
