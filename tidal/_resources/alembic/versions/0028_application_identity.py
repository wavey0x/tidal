"""Add database identity and explicit recovery metadata.

Revision ID: 0028_application_identity
Revises: 0027_verify_api_receipts
"""
import uuid

from alembic import op
import sqlalchemy as sa

revision = "0028_application_identity"
down_revision = "0027_verify_api_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "app_metadata",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("database_identity", sa.String(), nullable=False),
        sa.Column("notification_baseline_pending", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recovery_refreshed_at", sa.String(), nullable=True),
        sa.Column("recovery_block_number", sa.Integer(), nullable=True),
        sa.Column("recovery_block_hash", sa.String(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_app_metadata_singleton"),
    )
    op.bulk_insert(table, [{"id": 1, "database_identity": str(uuid.uuid4())}])


def downgrade() -> None:
    op.drop_table("app_metadata")
