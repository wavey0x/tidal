"""The application's single managed transaction ledger and reconciler."""
from __future__ import annotations

from decimal import Decimal
from hexbytes import HexBytes

from sqlalchemy import insert, or_, select, update
from sqlalchemy.orm import Session

from tidal.lifecycle import LifecycleError, execution_lock
from tidal.persistence import models
from tidal.persistence.repositories import AuctionEnabledTokenRepository, KickTxRepository
from tidal.time import utcnow_iso
from tidal.transaction_evidence import EvidenceError, hydrate_legacy_identity, observe_transaction, rpc_int

TERMINAL_STATUSES = frozenset({"CONFIRMED", "REVERTED", "SUPERSEDED"})
UNRESOLVED_STATUSES = frozenset({"RECORDED", "PENDING", "INCLUDED", "REVIEW_REQUIRED"})


class TransactionRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, transaction_id: int) -> dict:
        return dict(self.session.execute(
            select(models.transactions).where(models.transactions.c.id == transaction_id)
        ).mappings().one())

    def unresolved(self, *, signer: str | None = None) -> list[dict]:
        query = select(models.transactions).where(models.transactions.c.status.in_(UNRESOLVED_STATUSES))
        if signer is not None:
            query = query.where(or_(models.transactions.c.signer == signer.lower(), models.transactions.c.signer.is_(None)))
        return [dict(row) for row in self.session.execute(
            query.order_by(models.transactions.c.updated_at, models.transactions.c.id)
        ).mappings()]

    def assert_unblocked(self, signer: str) -> None:
        pending = self.unresolved(signer=signer)
        if pending:
            raise LifecycleError(
                "UNRESOLVED_ATTEMPTS", f"{len(pending)} retained attempt(s) require reconciliation before this signer can send.",
            )

    def record(self, *, identity: dict, operations: list[dict]) -> tuple[int, list[int]]:
        """Commit exact identity and every business-operation link before RPC send."""
        self.assert_unblocked(str(identity["signer"]))
        values = {**identity, "legacy": 0, "status": "RECORDED"}
        values.setdefault("created_at", utcnow_iso())
        values["updated_at"] = values["created_at"]
        try:
            transaction_id = int(self.session.execute(insert(models.transactions).values(**values)).lastrowid)
            operation_ids = []
            repo = KickTxRepository(self.session)
            for operation in operations:
                operation_ids.append(repo.insert({
                    **operation, "transaction_id": transaction_id, "tx_hash": values["tx_hash"],
                    "status": "SUBMITTED", "created_at": operation.get("created_at") or values["created_at"],
                }, commit=False))
            self.session.commit()
            return transaction_id, operation_ids
        except BaseException:
            self.session.rollback()
            raise

    def update(self, transaction_id: int, **values: object) -> None:
        self.session.execute(update(models.transactions).where(models.transactions.c.id == transaction_id).values(
            **values, updated_at=utcnow_iso(),
        ))


class LedgerReconciler:
    """Observe retained attempts only. Never create, sign, resend or deliver."""
    def __init__(self, *, session: Session, settings, web3_client, operation_reconciler):
        self.session = session
        self.settings = settings
        self.web3_client = web3_client
        self.operations = operation_reconciler
        self.repository = TransactionRepository(session)

    async def reconcile(self, *, transaction_ids: list[int] | None = None, limit: int = 100) -> list[dict]:
        with execution_lock(self.settings.resolved_home_path / "execution.lock"):
            rows = self.repository.unresolved() if transaction_ids is None else [
                dict(row) for row in self.session.execute(select(models.transactions).where(
                    models.transactions.c.id.in_(transaction_ids),
                )).mappings()
            ]
            self.session.commit()
            for row in rows[:limit]:
                await self._reconcile_one(row)
            return self.repository.unresolved()

    async def _reconcile_one(self, row: dict) -> None:
        transaction_id = int(row["id"])
        try:
            if row["legacy"] and row.get("tx_hash") and any(row.get(field) is None for field in (
                "chain_id", "signer", "nonce", "to_address", "data", "value",
            )):
                identity = hydrate_legacy_identity(
                    row, await self.web3_client.get_transaction(str(row["tx_hash"])),
                    chain_id=self.settings.chain_id,
                )
                self.repository.update(transaction_id, **identity)
                self.session.commit()
                row = {**row, **identity}
            evidence = await observe_transaction(self.web3_client, row)
        except EvidenceError as exc:
            self.repository.update(transaction_id, status="REVIEW_REQUIRED", error_message=f"{exc.code}: {exc}")
            self.session.commit()
            return
        except Exception as exc:
            disappeared = row["status"] == "INCLUDED" and type(exc).__name__ == "TransactionNotFound"
            self.repository.update(
                transaction_id, status="REVIEW_REQUIRED" if disappeared else row["status"],
                error_message="Previously included receipt disappeared; review the chain evidence" if disappeared
                else f"Chain evidence unavailable ({type(exc).__name__}); retained attempt remains unresolved",
            )
            self.session.commit()
            return
        receipt, block = evidence.receipt, evidence.block
        block_hash = "0x" + bytes(HexBytes(block["hash"])).hex()
        if row.get("block_hash") and str(row["block_hash"]).lower() != block_hash:
            self.repository.update(transaction_id, status="REVIEW_REQUIRED", error_message="Previously observed inclusion changed block; review before resolving")
            self.session.commit()
            return
        common = {
            "block_number": rpc_int(block["number"]), "block_hash": block_hash,
            "transaction_index": rpc_int(receipt["transactionIndex"]),
            "gas_used": rpc_int(receipt["gasUsed"]) if receipt.get("gasUsed") is not None else None,
            "gas_price_gwei": str(Decimal(rpc_int(receipt["effectiveGasPrice"])) / Decimal(10**9)) if receipt.get("effectiveGasPrice") is not None else None,
        }
        if not evidence.finalized:
            self.repository.update(transaction_id, status="INCLUDED", error_message=None, **common)
            self.session.commit()
            return
        operation_rows = [dict(item) for item in self.session.execute(
            select(models.kick_txs).where(models.kick_txs.c.transaction_id == transaction_id)
        ).mappings()]
        # Legacy deployment is the sole retained transaction type without
        # auction-operation rows. Unknown/aliased intent never grants success.
        business_action = str(row["operation"]).replace("-", "_") != "deploy"
        if not operation_rows and (not row["legacy"] or business_action):
            self.repository.update(transaction_id, status="REVIEW_REQUIRED", error_message="Managed transaction has no linked business operations", **common)
            self.session.commit()
            return
        try:
            failure = None
            with self.session.begin_nested() as effects:
                error = self.operations._finalize_operations(str(row["tx_hash"]), receipt, operation_rows, block)
                errors = self.session.execute(select(models.kick_txs.c.error_message).where(
                    models.kick_txs.c.transaction_id == transaction_id,
                    models.kick_txs.c.error_message.is_not(None),
                )).scalars().all()
                if error or errors:
                    failure = error or str(errors[0])
                    effects.rollback()
            if failure:
                self.repository.update(transaction_id, status="REVIEW_REQUIRED", error_message=failure, **common)
            else:
                # Include any incidental settlement operations decoded from
                # this same receipt in the single transaction's identity.
                self.session.execute(update(models.kick_txs).where(
                    models.kick_txs.c.tx_hash == row["tx_hash"], models.kick_txs.c.transaction_id.is_(None),
                ).values(transaction_id=transaction_id))
                outcome = "CONFIRMED" if rpc_int(receipt["status"]) == 1 else "REVERTED"
                if outcome == "CONFIRMED" and str(row["operation"]).replace("-", "_") == "enable_tokens":
                    for auction in {item["auction_address"] for item in operation_rows}:
                        AuctionEnabledTokenRepository(self.session).mark_tokens_enabled(
                            str(auction), [str(item["token_address"]) for item in operation_rows if item["auction_address"] == auction],
                            utcnow_iso(),
                        )
                self.repository.update(
                    transaction_id, status=outcome, receipt_status=outcome,
                    verified_at=utcnow_iso(), error_message=None, **common,
                )
            self.session.commit()
        except BaseException:
            self.session.rollback()
            raise

    async def resolve_with_replacement(self, *, transaction_id: int, replacement_hash: str, note: str) -> dict:
        """Explicitly verify external nonce resolution; never send or retry."""
        if not note.strip():
            raise LifecycleError("REVIEW_NOTE_REQUIRED", "Describe the reviewed replacement or cancellation.")
        with execution_lock(self.settings.resolved_home_path / "execution.lock"):
            original = self.repository.get(transaction_id)
            if original["status"] in TERMINAL_STATUSES:
                return original
            self.session.commit()
            await self._reconcile_one(original)
            original = self.repository.get(transaction_id)
            if original["status"] in TERMINAL_STATUSES:
                return original
            if any(original.get(field) is None for field in ("chain_id", "signer", "nonce", "tx_hash")):
                raise LifecycleError("INCOMPLETE_IDENTITY", "Original signer, chain, nonce and hash must be known before nonce resolution.")
            if HexBytes(replacement_hash) == HexBytes(original["tx_hash"]):
                raise LifecycleError("INVALID_REPLACEMENT", "The replacement hash must differ from the original attempt.")
            self.session.commit()
            identity = hydrate_legacy_identity({
                "tx_hash": replacement_hash, "chain_id": original["chain_id"],
                "signer": original["signer"], "nonce": original["nonce"],
            }, await self.web3_client.get_transaction(replacement_hash), chain_id=self.settings.chain_id)
            evidence = await observe_transaction(self.web3_client, identity)
            if not evidence.finalized:
                raise LifecycleError("UNRESOLVED_ATTEMPTS", "Replacement is not finalized; original attempt remains unresolved.")
            # Consuming the exact nonce resolves the original attempt. A
            # replacement's own successful receipt does not prove that the
            # original kick/settlement happened, even with similar calldata.
            self.repository.update(
                transaction_id, status="SUPERSEDED", resolved_by_hash=identity["tx_hash"],
                operator_note=note.strip(), verified_at=utcnow_iso(), error_message=None,
            )
            self.session.execute(update(models.kick_txs).where(
                models.kick_txs.c.transaction_id == transaction_id,
            ).values(status="SUPERSEDED", error_message="Original attempt superseded by verified finalized nonce consumption"))
            self.session.commit()
            return self.repository.get(transaction_id)
