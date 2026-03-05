"""add vaults table for cached vault metadata

Revision ID: 0002_add_vaults_table
Revises: 0001_phase1_initial
Create Date: 2026-03-03 22:20:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_add_vaults_table"
down_revision = "0001_phase1_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vaults",
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("chain_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.String(), nullable=False),
        sa.Column("last_seen_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("address"),
    )


def downgrade() -> None:
    op.drop_table("vaults")
