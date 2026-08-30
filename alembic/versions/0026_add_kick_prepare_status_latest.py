"""add latest kick prepare status

Revision ID: 0026_add_kick_prepare_status_latest
Revises: 0025_add_auction_history_baselines
Create Date: 2026-08-30 13:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0026_add_kick_prepare_status_latest"
down_revision = "0025_add_auction_history_baselines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kick_prepare_status_latest",
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("source_address", sa.String(), nullable=False),
        sa.Column("auction_address", sa.String(), nullable=False),
        sa.Column("token_address", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("source_balance_raw", sa.Text(), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint(
            "source_type",
            "source_address",
            "auction_address",
            "token_address",
        ),
    )
    op.create_index(
        "ix_kick_prepare_status_reason",
        "kick_prepare_status_latest",
        ["reason", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kick_prepare_status_reason",
        table_name="kick_prepare_status_latest",
    )
    op.drop_table("kick_prepare_status_latest")
