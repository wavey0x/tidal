"""add strategy auction and token logo fields

Revision ID: 0006_add_strategy_auction_and_token_logo_fields
Revises: 0005_add_vault_symbol
Create Date: 2026-03-10 18:45:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_add_strategy_auction_and_token_logo_fields"
down_revision = "0005_add_vault_symbol"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("strategies", sa.Column("auction_address", sa.String(), nullable=True))
    op.add_column("strategies", sa.Column("auction_updated_at", sa.String(), nullable=True))
    op.add_column("strategies", sa.Column("auction_error_message", sa.Text(), nullable=True))

    op.add_column("tokens", sa.Column("logo_url", sa.Text(), nullable=True))
    op.add_column("tokens", sa.Column("logo_source", sa.String(), nullable=True))
    op.add_column("tokens", sa.Column("logo_status", sa.String(), nullable=True))
    op.add_column("tokens", sa.Column("logo_validated_at", sa.String(), nullable=True))
    op.add_column("tokens", sa.Column("logo_error_message", sa.Text(), nullable=True))

    op.execute("PRAGMA journal_mode=WAL")


def downgrade() -> None:
    op.drop_column("tokens", "logo_error_message")
    op.drop_column("tokens", "logo_validated_at")
    op.drop_column("tokens", "logo_status")
    op.drop_column("tokens", "logo_source")
    op.drop_column("tokens", "logo_url")

    op.drop_column("strategies", "auction_error_message")
    op.drop_column("strategies", "auction_updated_at")
    op.drop_column("strategies", "auction_address")
