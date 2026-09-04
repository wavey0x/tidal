"""Bounded receipt reconciliation for API-only as well as scanner deployments."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import structlog

from tidal.operation_reconciler import OperationReconciler
from tidal.persistence.repositories import APIActionRepository
from tidal.runtime import build_web3_client
from tidal.time import utcnow_iso

logger = structlog.get_logger(__name__)


async def reconcile_pending_actions(database, settings, web3_client) -> None:
    threshold = datetime.now(timezone.utc) - timedelta(
        seconds=max(0, settings.tidal_api_receipt_reconcile_threshold_seconds)
    )
    with database.session() as session:
        repo = APIActionRepository(session)
        pending = repo.pending_receipt_transactions(older_than=threshold.isoformat(), limit=20)
        reconciler = OperationReconciler(
            session=session, web3_client=web3_client,
            auction_kicker_address=settings.auction_kicker_address,
        )
        for tx_hash in dict.fromkeys(str(row["tx_hash"]) for row in pending):
            # Rotate unresolved hashes through the bounded batch without holding
            # a database write transaction across RPC awaits.
            repo.mark_receipt_checked(tx_hash, checked_at=utcnow_iso())
            try:
                receipt = await web3_client.get_transaction_receipt(tx_hash, timeout_seconds=2)
                error = await reconciler.finalize_receipt(tx_hash, receipt)
                if error:
                    logger.warning("api_receipt_verification_incomplete", tx_hash=tx_hash, reason=error)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                logger.debug("api_receipt_pending", tx_hash=tx_hash, error_type=type(exc).__name__)


async def run_action_reconciler(database, settings) -> None:
    web3_client = build_web3_client(settings)
    try:
        while True:
            try:
                await reconcile_pending_actions(database, settings, web3_client)
            except Exception as exc:  # noqa: BLE001
                logger.warning("api_receipt_reconcile_failed", error_type=type(exc).__name__)
            await asyncio.sleep(max(1, settings.tidal_api_receipt_reconcile_interval_seconds))
    finally:
        await web3_client.close()
