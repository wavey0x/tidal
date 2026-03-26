"""add kick operation detail columns

Revision ID: 0017_add_kick_operation_detail_columns
Revises: 0016_add_auction_enabled_token_cache
Create Date: 2026-03-26 16:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0017_add_kick_operation_detail_columns"
down_revision = "0016_add_auction_enabled_token_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kick_txs", sa.Column("step_decay_rate_bps", sa.Integer(), nullable=True))
    op.add_column("kick_txs", sa.Column("settle_token", sa.String(), nullable=True))
    op.add_column("kick_txs", sa.Column("stuck_abort_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("kick_txs", "stuck_abort_reason")
    op.drop_column("kick_txs", "settle_token")
    op.drop_column("kick_txs", "step_decay_rate_bps")
