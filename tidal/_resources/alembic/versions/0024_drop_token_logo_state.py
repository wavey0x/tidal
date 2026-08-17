"""drop legacy token logo projection state

Revision ID: 0024_drop_token_logo_state
Revises: 0023_bounded_retry_alerts
Create Date: 2026-08-17 13:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0024_drop_token_logo_state"
down_revision = "0023_bounded_retry_alerts"
branch_labels = None
depends_on = None

_LOGO_COLUMNS = (
    "logo_error_message",
    "logo_validated_at",
    "logo_status",
    "logo_source",
    "logo_url",
)


def upgrade() -> None:
    for column_name in _LOGO_COLUMNS:
        op.drop_column("tokens", column_name)


def downgrade() -> None:
    op.add_column("tokens", sa.Column("logo_url", sa.Text(), nullable=True))
    op.add_column("tokens", sa.Column("logo_source", sa.String(), nullable=True))
    op.add_column("tokens", sa.Column("logo_status", sa.String(), nullable=True))
    op.add_column("tokens", sa.Column("logo_validated_at", sa.String(), nullable=True))
    op.add_column("tokens", sa.Column("logo_error_message", sa.Text(), nullable=True))
