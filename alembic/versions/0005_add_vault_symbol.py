"""add vault symbol cache column

Revision ID: 0005_add_vault_symbol
Revises: 0004_move_price_fields_to_tokens_drop_token_prices_table
Create Date: 2026-03-03 23:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_add_vault_symbol"
down_revision = "0004_move_price_fields_to_tokens_drop_token_prices_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vaults", sa.Column("symbol", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("vaults", "symbol")
