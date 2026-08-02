"""add bounded retry evidence and alert deliveries

Revision ID: 0023_bounded_retry_alerts
Revises: 0022_add_kick_guard_status_latest
Create Date: 2026-08-02 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0023_bounded_retry_alerts"
down_revision = "0022_add_kick_guard_status_latest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("kick_txs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "round_kick_id",
                sa.Integer(),
                sa.ForeignKey("kick_txs.id", name="fk_kick_txs_round_kick_id_kick_txs"),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("resolution_path", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("mined_at", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("transaction_index", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("requested_sell_amount", sa.Text(), nullable=True))

    op.create_index("ix_kick_txs_round_kick_id", "kick_txs", ["round_kick_id"])
    op.create_index(
        "ix_kick_txs_auction_token_chain_position",
        "kick_txs",
        ["auction_address", "token_address", "block_number", "transaction_index"],
    )
    op.create_index("ix_kick_txs_created", "kick_txs", ["created_at"])
    op.create_index("ix_kick_txs_status_created", "kick_txs", ["status", "created_at"])
    op.create_index("ix_kick_txs_auction_created", "kick_txs", ["auction_address", "created_at"])
    op.create_index("ix_kick_txs_run_created", "kick_txs", ["run_id", "created_at"])

    op.create_table(
        "alert_deliveries",
        sa.Column("delivery_key", sa.String(), nullable=False),
        sa.Column("destination", sa.String(), nullable=False),
        sa.Column("occurrence_id", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.String(), nullable=True),
        sa.Column("sent_at", sa.String(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "destination IN ('admin_alerts', 'operations_alerts')",
            name="ck_alert_deliveries_destination",
        ),
        sa.PrimaryKeyConstraint("delivery_key", "destination"),
    )
    op.create_index(
        "ix_alert_deliveries_occurrence_id",
        "alert_deliveries",
        ["occurrence_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_alert_deliveries_occurrence_id", table_name="alert_deliveries")
    op.drop_table("alert_deliveries")
    op.drop_index("ix_kick_txs_run_created", table_name="kick_txs")
    op.drop_index("ix_kick_txs_auction_created", table_name="kick_txs")
    op.drop_index("ix_kick_txs_status_created", table_name="kick_txs")
    op.drop_index("ix_kick_txs_created", table_name="kick_txs")
    op.drop_index("ix_kick_txs_auction_token_chain_position", table_name="kick_txs")
    op.drop_index("ix_kick_txs_round_kick_id", table_name="kick_txs")
    with op.batch_alter_table("kick_txs") as batch_op:
        batch_op.drop_column("requested_sell_amount")
        batch_op.drop_column("transaction_index")
        batch_op.drop_column("mined_at")
        batch_op.drop_column("resolution_path")
        batch_op.drop_column("round_kick_id")
