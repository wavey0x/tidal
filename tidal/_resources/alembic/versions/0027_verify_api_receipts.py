"""Require chain verification for API execution outcomes.

Revision ID: 0027_verify_api_receipts
Revises: 0026_add_kick_prepare_status_latest
"""
from alembic import op
import sqlalchemy as sa

revision = "0027_verify_api_receipts"
down_revision = "0026_add_kick_prepare_status_latest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_action_transactions", sa.Column("verified_at", sa.String(), nullable=True))
    op.create_index("ix_api_action_transactions_verification_pending", "api_action_transactions", ["verified_at", "updated_at"])
    # Legacy updated_at could be a client-provided broadcastAt/observedAt.
    op.execute("UPDATE api_action_transactions SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now') WHERE tx_hash IS NOT NULL")
    # Keep old reported fields for audit, but do not treat them as chain evidence.
    op.execute("""
        UPDATE api_actions SET status = CASE WHEN EXISTS (
            SELECT 1 FROM api_action_transactions t
            WHERE t.action_id = api_actions.action_id AND t.tx_hash IS NOT NULL
        ) THEN 'BROADCAST_REPORTED' ELSE 'PREPARED' END
    """)
    op.execute("""
        UPDATE kick_txs SET status = 'SUBMITTED'
        WHERE tx_hash IS NOT NULL AND run_id IN (
            SELECT 'api-action:' || action_id FROM api_actions
        )
    """)


def downgrade() -> None:
    op.drop_index("ix_api_action_transactions_verification_pending", table_name="api_action_transactions")
    op.drop_column("api_action_transactions", "verified_at")
