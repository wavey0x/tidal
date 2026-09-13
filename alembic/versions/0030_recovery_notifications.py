"""Track notification suppression without claiming successful delivery."""
from alembic import op
import sqlalchemy as sa

revision = "0030_recovery_notifications"
down_revision = "0029_transaction_ledger"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("alert_deliveries", sa.Column("suppressed_at", sa.String(), nullable=True))


def downgrade():
    op.drop_column("alert_deliveries", "suppressed_at")
