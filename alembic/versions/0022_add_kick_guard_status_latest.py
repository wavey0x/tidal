"""add latest kick guard status table

Revision ID: 0022_add_kick_guard_status_latest
Revises: 0021_add_auction_token_kick_index
Create Date: 2026-05-12 10:45:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0022_add_kick_guard_status_latest"
down_revision = "0021_add_auction_token_kick_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kick_guard_status_latest",
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("source_address", sa.String(), nullable=False),
        sa.Column("auction_address", sa.String(), nullable=True),
        sa.Column("disabled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.String(), nullable=False),
        sa.Column("block_number", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("source_type", "source_address"),
    )
    op.create_index(
        "ix_kick_guard_status_disabled",
        "kick_guard_status_latest",
        ["disabled", "source_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_kick_guard_status_disabled", table_name="kick_guard_status_latest")
    op.drop_table("kick_guard_status_latest")
