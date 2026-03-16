"""add auction_version to strategies

Revision ID: 0009_add_auction_version_to_strategies
Revises: 0008_add_want_address_to_strategies
Create Date: 2026-03-16 12:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_add_auction_version_to_strategies"
down_revision = "0008_add_want_address_to_strategies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("strategies", sa.Column("auction_version", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("strategies", "auction_version")
