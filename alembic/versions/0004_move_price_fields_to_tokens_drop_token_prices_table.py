"""move latest price fields onto tokens and drop token_prices_latest

Revision ID: 0004_move_price_fields_to_tokens_drop_token_prices_table
Revises: 0003_add_token_prices_latest
Create Date: 2026-03-03 22:50:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_move_price_fields_to_tokens_drop_token_prices_table"
down_revision = "0003_add_token_prices_latest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tokens", sa.Column("price_usd", sa.Text(), nullable=True))
    op.add_column("tokens", sa.Column("price_source", sa.String(), nullable=True))
    op.add_column("tokens", sa.Column("price_status", sa.String(), nullable=True))
    op.add_column("tokens", sa.Column("price_fetched_at", sa.String(), nullable=True))
    op.add_column("tokens", sa.Column("price_run_id", sa.String(), nullable=True))
    op.add_column("tokens", sa.Column("price_error_message", sa.Text(), nullable=True))

    op.execute(
        """
        UPDATE tokens
        SET
            price_usd = (
                SELECT tp.price_usd
                FROM token_prices_latest tp
                WHERE tp.token_address = tokens.address
            ),
            price_source = (
                SELECT tp.source
                FROM token_prices_latest tp
                WHERE tp.token_address = tokens.address
            ),
            price_status = (
                SELECT tp.status
                FROM token_prices_latest tp
                WHERE tp.token_address = tokens.address
            ),
            price_fetched_at = (
                SELECT tp.fetched_at
                FROM token_prices_latest tp
                WHERE tp.token_address = tokens.address
            ),
            price_run_id = (
                SELECT tp.run_id
                FROM token_prices_latest tp
                WHERE tp.token_address = tokens.address
            ),
            price_error_message = (
                SELECT tp.error_message
                FROM token_prices_latest tp
                WHERE tp.token_address = tokens.address
            )
        WHERE EXISTS (
            SELECT 1
            FROM token_prices_latest tp
            WHERE tp.token_address = tokens.address
        )
        """
    )

    op.drop_index("ix_token_prices_latest_run_id", table_name="token_prices_latest")
    op.drop_table("token_prices_latest")


def downgrade() -> None:
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

    op.execute(
        """
        INSERT INTO token_prices_latest (
            token_address,
            chain_id,
            source,
            price_usd,
            quote_token_address,
            quote_amount_in_raw,
            fetched_at,
            run_id,
            status,
            error_message
        )
        SELECT
            address AS token_address,
            chain_id,
            COALESCE(price_source, 'curve_usd_price') AS source,
            price_usd,
            'usd' AS quote_token_address,
            '1' AS quote_amount_in_raw,
            COALESCE(price_fetched_at, first_seen_at) AS fetched_at,
            COALESCE(price_run_id, '') AS run_id,
            COALESCE(price_status, 'NOT_FOUND') AS status,
            price_error_message AS error_message
        FROM tokens
        WHERE price_source IS NOT NULL OR price_status IS NOT NULL OR price_usd IS NOT NULL
        """
    )

    op.drop_column("tokens", "price_error_message")
    op.drop_column("tokens", "price_run_id")
    op.drop_column("tokens", "price_fetched_at")
    op.drop_column("tokens", "price_status")
    op.drop_column("tokens", "price_source")
    op.drop_column("tokens", "price_usd")
