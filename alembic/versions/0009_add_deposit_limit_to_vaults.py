"""add deposit_limit to vaults

Revision ID: 0009_add_deposit_limit_to_vaults
Revises: 0008_add_want_address_to_strategies
Create Date: 2026-03-16 12:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_add_deposit_limit_to_vaults"
down_revision = "0008_add_want_address_to_strategies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vaults", sa.Column("deposit_limit", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("vaults", "deposit_limit")
