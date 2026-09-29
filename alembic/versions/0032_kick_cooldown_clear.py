"""Record an explicit clear of a kick's cooldown without changing its history."""
from alembic import op
import sqlalchemy as sa

revision = "0032_kick_cooldown_clear"
down_revision = "0031_retire_action_protocol"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("kick_txs", sa.Column("cooldown_cleared_at", sa.String(), nullable=True))


def downgrade():
    op.drop_column("kick_txs", "cooldown_cleared_at")
