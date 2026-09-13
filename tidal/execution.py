"""One locally coordinated path from prepared action to retained submission."""
from __future__ import annotations

import time
from eth_utils import keccak, to_checksum_address
from hexbytes import HexBytes
from sqlalchemy import func, select, text

from tidal.lifecycle import SCHEMA_REVISION, LifecycleError, activation_binding, execution_lock, require_activation
from tidal.normalizers import normalize_address
from tidal.persistence import models
from tidal.transaction_evidence import rpc_int
from tidal.transactions import LedgerReconciler, TransactionRepository


class ManagedExecutor:
    def __init__(self, *, settings, session, web3_client, signer, profile: str, operation_reconciler):
        self.settings = settings
        self.session = session
        self.web3_client = web3_client
        self.signer = signer
        self.profile = profile
        self.repository = TransactionRepository(session)
        self.reconciler = LedgerReconciler(
            settings=settings, session=session, web3_client=web3_client,
            operation_reconciler=operation_reconciler,
        )

    def _activation(self) -> dict:
        version = self.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if version != SCHEMA_REVISION:
            raise LifecycleError("INCOMPATIBLE_SCHEMA", "Execution requires this release's exact database schema.")
        identity = self.session.execute(select(models.app_metadata.c.database_identity).where(models.app_metadata.c.id == 1)).scalar_one()
        binding = activation_binding(identity, self.settings.chain_id, self.settings.managed_signers)
        expected = binding["signers"].get(self.profile)
        if self.signer is None or expected != normalize_address(self.signer.address):
            raise LifecycleError("WRONG_SIGNER", "Unlocked signer does not match the declared action profile.")
        return require_activation(self.settings.resolved_home_path / "activation.json", binding)

    async def submit(self, *, transaction: dict, operations: list[dict], action: str) -> dict:
        """Caller prepares under the same lock; every managed send returns promptly."""
        with execution_lock(self.settings.resolved_home_path / "execution.lock"):
            activation = self._activation()
            self.session.commit()
            await self.reconciler.reconcile()
            self.repository.assert_unblocked(self.signer.address)
            previous_nonce = self.session.execute(select(func.max(models.transactions.c.nonce)).where(
                models.transactions.c.chain_id == self.settings.chain_id,
                models.transactions.c.signer == normalize_address(self.signer.address),
            )).scalar_one()
            self.session.commit()
            if not operations:
                raise LifecycleError("INVALID_INTENT", "Managed sends require linked business operations.")
            if rpc_int(transaction["chainId"]) != self.settings.chain_id or await self.web3_client.get_chain_id() != self.settings.chain_id:
                raise LifecycleError("WRONG_CHAIN", "Prepared transaction and RPC must match the configured chain.")
            head = await self.web3_client.get_block("latest")
            finalized = await self.web3_client.get_block("finalized")
            now = time.time()
            if (
                not -120 <= now - rpc_int(head["timestamp"]) <= self.settings.chain_read_max_age_seconds
                or not -120 <= now - rpc_int(finalized["timestamp"]) <= self.settings.finality_max_age_seconds
                or rpc_int(finalized["number"]) > rpc_int(head["number"])
            ):
                raise LifecycleError("STALE_CHAIN", "Current chain or finality evidence is stale; keep execution held.")
            latest = await self.web3_client.get_transaction_count(self.signer.address, "latest")
            pending = await self.web3_client.get_transaction_count(self.signer.address, "pending")
            baseline = activation.get("nonce_baseline", {}).get(normalize_address(self.signer.address), 0)
            expected = max(int(baseline), int(previous_nonce) + 1) if previous_nonce is not None else int(baseline)
            if latest != pending or (expected and latest != expected):
                raise LifecycleError("UNEXPECTED_NONCE", "Account activity differs from retained attempts; inspect and explicitly resume after review.")
            if "nonce" in transaction and rpc_int(transaction["nonce"]) != latest:
                raise LifecycleError("UNEXPECTED_NONCE", "Prepared nonce is stale; prepare again from current conditions.")
            unsigned = {**transaction, "nonce": latest, "value": rpc_int(transaction.get("value", 0))}
            unsigned["to"] = to_checksum_address(unsigned["to"])
            if "from" in unsigned and normalize_address(unsigned.pop("from")) != normalize_address(self.signer.address):
                raise LifecycleError("WRONG_SIGNER", "Prepared sender differs from the managed signer.")
            signed = self.signer.sign_transaction(unsigned)
            tx_hash = "0x" + keccak(signed).hex()
            transaction_id, operation_ids = self.repository.record(identity={
                "chain_id": self.settings.chain_id, "signer": normalize_address(self.signer.address),
                "nonce": latest, "tx_hash": tx_hash, "to_address": normalize_address(unsigned["to"]),
                "data": "0x" + bytes(HexBytes(unsigned["data"])).hex(), "value": str(unsigned["value"]),
                "operation": action, "profile": self.profile,
                "gas_limit": unsigned.get("gas"),
            }, operations=operations)
            try:
                returned = await self.web3_client.send_raw_transaction(signed)
            except Exception as exc:
                self.repository.update(transaction_id, error_message=f"Submission response unavailable ({type(exc).__name__}); reconcile this exact hash")
                self.session.commit()
            else:
                try:
                    matches = HexBytes(returned) == HexBytes(tx_hash)
                except (ValueError, TypeError):
                    matches = False
                if not matches:
                    self.repository.update(transaction_id, status="REVIEW_REQUIRED", error_message="RPC returned a different transaction hash")
                    self.session.commit()
                    return {**self.repository.get(transaction_id), "operation_ids": operation_ids}
                self.repository.update(transaction_id, status="PENDING", error_message=None)
                self.session.commit()
            await self.reconciler.reconcile(transaction_ids=[transaction_id])
            return {**self.repository.get(transaction_id), "operation_ids": operation_ids}
