"""add token_prices_latest cache table

Revision ID: 0003_add_token_prices_latest
Revises: 0002_add_vaults_table
Create Date: 2026-03-03 22:45:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_add_token_prices_latest"
down_revision = "0002_add_vaults_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "token_prices_latest",
        sa.Column("token_address", sa.String(), nullable=False),
        sa.Column("chain_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("price_usd", sa.Text(), nullable=True),
        sa.Column("quote_token_address", sa.String(), nullable=False),
        sa.Column("quote_amount_in_raw", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("token_address"),
    )
    op.create_index(
        "ix_token_prices_latest_run_id",
        "token_prices_latest",
        ["run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_token_prices_latest_run_id", table_name="token_prices_latest")
    op.drop_table("token_prices_latest")
