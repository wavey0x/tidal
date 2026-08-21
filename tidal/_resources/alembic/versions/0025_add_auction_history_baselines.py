"""add reviewed auction history baselines

Revision ID: 0025_add_auction_history_baselines
Revises: 0024_drop_token_logo_state
Create Date: 2026-08-21 12:00:00.000000

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0025_add_auction_history_baselines"
down_revision = "0024_drop_token_logo_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kick_txs",
        sa.Column(
            "historical_baseline",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "kick_txs",
        sa.Column("historical_baseline_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "kick_txs",
        sa.Column("historical_baselined_at", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("kick_txs", "historical_baselined_at")
    op.drop_column("kick_txs", "historical_baseline_reason")
    op.drop_column("kick_txs", "historical_baseline")
