"""Consolidate retained transaction identity into one ledger.

Revision ID: 0029_transaction_ledger
Revises: 0028_application_identity

The separate outbox importer must supply missing retained submission fields
before activation. Migration never infers a historical signer or nonce from
present account state. Existing business rows and their timestamps stay intact.
"""
from alembic import op
import sqlalchemy as sa

revision = "0029_transaction_ledger"
down_revision = "0028_application_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    # Validate duplicate hashes before DDL or removal of any source row.
    existing = list(connection.execute(sa.text(
        "SELECT t.*, a.sender FROM api_action_transactions t LEFT JOIN api_actions a ON a.action_id=t.action_id ORDER BY t.id"
    )).mappings())
    identities = {}
    duplicates = []
    for row in existing:
        if row["tx_hash"] is None:
            continue
        key = (row["chain_id"], row["tx_hash"].lower())
        if key in identities:
            first = identities[key]
            fields = ("to_address", "data", "value", "operation", "sender")
            if any(row[field] != first[field] for field in fields):
                raise RuntimeError("Conflicting retained transaction intents; preserve originals and resolve before migration")
            if row["verified_at"] and first["verified_at"] and row["receipt_status"] != first["receipt_status"]:
                raise RuntimeError("Conflicting retained verified outcomes; preserve originals and resolve before migration")
            # Prefer verified chain evidence to an unverified duplicate report.
            if row["verified_at"] and not first["verified_at"]:
                duplicates.append(first["id"])
                identities[key] = row
            else:
                duplicates.append(row["id"])
        else:
            identities[key] = row

    op.rename_table("api_action_transactions", "transactions")
    for row_id in duplicates:
        connection.execute(sa.text("DELETE FROM transactions WHERE id=:id"), {"id": row_id})
    with op.batch_alter_table("transactions") as batch:
        for column in ("action_id", "tx_index", "to_address", "data", "value", "chain_id"):
            batch.alter_column(column, nullable=True)
        batch.add_column(sa.Column("signer", sa.String(), nullable=True))
        batch.add_column(sa.Column("nonce", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("profile", sa.String(), nullable=True))
        batch.add_column(sa.Column("status", sa.String(), nullable=False, server_default="RECORDED"))
        batch.add_column(sa.Column("legacy", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("block_hash", sa.String(), nullable=True))
        batch.add_column(sa.Column("transaction_index", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("resolved_by_hash", sa.String(), nullable=True))
        batch.add_column(sa.Column("operator_note", sa.Text(), nullable=True))
        batch.create_check_constraint(
            "ck_transactions_recorded_identity",
            "legacy = 1 OR (chain_id IS NOT NULL AND signer IS NOT NULL AND nonce IS NOT NULL "
            "AND tx_hash IS NOT NULL AND to_address IS NOT NULL AND data IS NOT NULL AND value IS NOT NULL)",
        )
    connection.execute(sa.text("""
        UPDATE transactions SET tx_hash=lower(tx_hash),
            signer=(SELECT lower(sender) FROM api_actions WHERE api_actions.action_id=transactions.action_id),
            status=CASE
                WHEN verified_at IS NOT NULL AND receipt_status IN ('CONFIRMED', 'REVERTED') THEN receipt_status
                WHEN tx_hash IS NOT NULL THEN 'PENDING'
                ELSE 'LEGACY_PREVIEW' END
    """))
    # SQLite can add a nullable reference without rebuilding the operation table
    # and its self-referential auction-round links.
    op.execute("ALTER TABLE kick_txs ADD COLUMN transaction_id INTEGER REFERENCES transactions(id)")
    known = {}
    for row in connection.execute(sa.text("SELECT id, tx_hash FROM transactions WHERE tx_hash IS NOT NULL")).mappings():
        if row["tx_hash"] in known:
            raise RuntimeError("Same retained transaction hash spans different chains; review before migration")
        known[row["tx_hash"]] = row["id"]
    operations = list(connection.execute(sa.text(
        "SELECT * FROM kick_txs WHERE tx_hash IS NOT NULL OR status='SUBMITTED' ORDER BY id"
    )).mappings())
    for row in operations:
        tx_hash = row["tx_hash"].lower() if row["tx_hash"] else None
        transaction_id = known.get(tx_hash) if tx_hash else None
        if transaction_id is None:
            status = row["status"] if row["status"] in ("CONFIRMED", "REVERTED") else "REVIEW_REQUIRED"
            inserted = connection.execute(sa.text("""
                INSERT INTO transactions
                    (operation, tx_hash, status, legacy, created_at, updated_at, block_number, gas_used, gas_price_gwei,
                     error_message)
                VALUES (:operation, :tx_hash, :status, 1, :created_at, :created_at, :block_number, :gas_used,
                        :gas_price_gwei, :error_message)
            """), {
                "operation": row["operation_type"], "tx_hash": tx_hash, "status": status,
                "created_at": row["created_at"], "block_number": row["block_number"],
                "gas_used": row["gas_used"], "gas_price_gwei": row["gas_price_gwei"],
                "error_message": "Legacy submission identity or intent is incomplete" if status == "REVIEW_REQUIRED" else None,
            })
            transaction_id = inserted.lastrowid
            if tx_hash:
                known[tx_hash] = transaction_id
        else:
            # A submitted business row is retained uncertainty, even if a
            # duplicate legacy report has a terminal receipt status.
            if row["status"] == "SUBMITTED":
                connection.execute(sa.text(
                    "UPDATE transactions SET status='PENDING' WHERE id=:id"
                ), {"id": transaction_id})
        connection.execute(sa.text(
            "UPDATE kick_txs SET transaction_id=:transaction_id WHERE id=:id"
        ), {"transaction_id": transaction_id, "id": row["id"]})
    op.create_index("ix_transactions_chain_hash", "transactions", ["chain_id", "tx_hash"], unique=True)
    op.create_index("ix_transactions_signer_nonce", "transactions", ["chain_id", "signer", "nonce"])
    op.create_index("ix_transactions_unresolved", "transactions", ["status", "updated_at"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM transactions WHERE legacy=0 LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade after new attempts exist; retain their identities and repair forward")
    op.drop_column("kick_txs", "transaction_id")
    op.drop_index("ix_transactions_chain_hash", table_name="transactions")
    op.drop_index("ix_transactions_signer_nonce", table_name="transactions")
    op.drop_index("ix_transactions_unresolved", table_name="transactions")
    connection.execute(sa.text("DELETE FROM transactions WHERE action_id IS NULL"))
    with op.batch_alter_table("transactions") as batch:
        batch.drop_constraint("ck_transactions_recorded_identity", type_="check")
        for column in ("signer", "nonce", "profile", "status", "legacy", "block_hash", "transaction_index", "resolved_by_hash", "operator_note"):
            batch.drop_column(column)
        for column in ("action_id", "tx_index", "to_address", "data", "value", "chain_id"):
            batch.alter_column(column, nullable=False)
    op.rename_table("transactions", "api_action_transactions")
