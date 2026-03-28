"""add api keys table

Revision ID: 0019_add_api_keys
Revises: 0018_add_api_action_audit_tables
Create Date: 2026-03-28 14:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0019_add_api_keys"
down_revision = "0018_add_api_action_audit_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("key_prefix", sa.String(8), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("label"),
    )


def downgrade() -> None:
    op.drop_table("api_keys")
