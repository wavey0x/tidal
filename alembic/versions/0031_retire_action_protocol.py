"""Retire action jobs after explicitly importing the protected original pair."""
from alembic import op
import sqlalchemy as sa

revision = "0031_retire_action_protocol"
down_revision = "0030_recovery_notifications"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    retained = connection.execute(sa.text(
        "SELECT EXISTS(SELECT 1 FROM api_actions) OR EXISTS(SELECT 1 FROM transactions)"
    )).scalar()
    if retained and not op.get_context().config.attributes.get("allow_action_retirement"):
        raise RuntimeError("Use native db migrate with the protected --source-database and --outbox before retiring action history.")
    # Unsent previews carry no durable attempt. Unknown submissions linked to
    # business rows remain REVIEW_REQUIRED even when their hash is missing.
    op.execute("""DELETE FROM transactions WHERE legacy=1 AND tx_hash IS NULL
        AND NOT EXISTS(SELECT 1 FROM kick_txs WHERE transaction_id=transactions.id)""")
    for index in (
        "ix_api_action_transactions_action_tx_index",
        "ix_api_action_transactions_receipt_pending",
        "ix_api_action_transactions_verification_pending",
    ):
        op.drop_index(index, table_name="transactions")
    op.drop_table("api_actions")


def downgrade():
    raise RuntimeError("Restore a protected pre-cutover snapshot only before any new attempt; after activation use forward repair.")
