"""Scanner-side stuck auction resolution."""

from __future__ import annotations

from dataclasses import dataclass

from eth_utils import to_checksum_address
import structlog

from tidal.auction_settlement import (
    default_actionable_previews,
    inspect_auction_settlements,
    live_funded_previews,
    path_reason,
)
from tidal.chain.contracts.abis import AUCTION_KICKER_ABI
from tidal.chain.web3_client import Web3Client
from tidal.constants import CORE_REWARD_TOKENS
from tidal.persistence.repositories import KickTxRepository
from tidal.time import utcnow_iso
from tidal.transaction_service.kick_shared import (
    _GAS_ESTIMATE_BUFFER,
    _format_execution_error,
    resolve_priority_fee_wei,
)
from tidal.transaction_service.signer import TransactionSigner
from tidal.types import ScanItemError

logger = structlog.get_logger(__name__)


@dataclass(slots=True, frozen=True)
class AuctionSource:
    source_type: str
    source_address: str
    auction_address: str
    want_address: str | None


@dataclass(slots=True)
class ResolveCandidate:
    source: AuctionSource
    token_address: str
    path: int
    balance_raw: int
    receiver: str | None
    token_symbol: str | None = None
    want_symbol: str | None = None


@dataclass(slots=True)
class AuctionSettlementStats:
    auctions_seen: int = 0
    active_auctions: int = 0
    eligible_tokens: int = 0
    blocking_tokens: int = 0
    settlements_attempted: int = 0
    settlements_confirmed: int = 0
    settlements_failed: int = 0
    settlements_submitted: int = 0
    skipped_high_base_fee: bool = False


@dataclass(slots=True)
class AuctionSettlementPassResult:
    stats: AuctionSettlementStats
    errors: list[ScanItemError]


class AuctionSettlementService:
    """Auto-close default-actionable stuck lots via AuctionKicker.resolveAuction(..., false)."""

    def __init__(
        self,
        *,
        web3_client: Web3Client,
        signer: TransactionSigner,
        kick_tx_repository: KickTxRepository,
        token_metadata_service,
        base_fee_cap_gwei: float,
        max_priority_fee_gwei: int,
        max_gas_limit: int,
        chain_id: int,
        settings,
    ) -> None:
        self.web3_client = web3_client
        self.signer = signer
        self.kick_tx_repository = kick_tx_repository
        self.token_metadata_service = token_metadata_service
        self.base_fee_cap_gwei = base_fee_cap_gwei
        self.max_priority_fee_gwei = max_priority_fee_gwei
        self.max_gas_limit = max_gas_limit
        self.chain_id = chain_id
        self.settings = settings

    async def settle_stale_auctions(
        self,
        *,
        run_id: str,
        sources: list[AuctionSource],
    ) -> AuctionSettlementPassResult:
        stats = AuctionSettlementStats()
        errors: list[ScanItemError] = []

        source_by_auction: dict[str, AuctionSource] = {}
        for source in sources:
            if source.auction_address and source.auction_address not in source_by_auction:
                source_by_auction[source.auction_address] = source

        auction_addresses = sorted(source_by_auction)
        stats.auctions_seen = len(auction_addresses)
        if not auction_addresses:
            return AuctionSettlementPassResult(stats=stats, errors=errors)

        inspections = await inspect_auction_settlements(
            self.web3_client,
            self.settings,
            auction_addresses,
        )
        stats.active_auctions = sum(1 for inspection in inspections.values() if inspection.is_active_auction is True)

        candidates: list[ResolveCandidate] = []
        for auction_address in auction_addresses:
            inspection = inspections[auction_address]
            source = source_by_auction[auction_address]

            if inspection.preview_failures:
                for preview in inspection.preview_failures:
                    errors.append(
                        ScanItemError(
                            stage="AUCTION_SETTLEMENT",
                            error_code="resolve_preview_failed",
                            error_message=preview.error_message or "resolve preview failed",
                            source_type=source.source_type,
                            source_address=source.source_address,
                            token_address=preview.token_address,
                        )
                    )
                continue

            live_previews = live_funded_previews(inspection)
            stats.blocking_tokens += len(live_previews)
            for preview in live_previews:
                logger.info(
                    "auction_settlement_in_progress",
                    source_type=source.source_type,
                    source_address=source.source_address,
                    auction=auction_address,
                    token=preview.token_address,
                    raw_balance=str(preview.balance_raw or 0),
                )

            actionable = default_actionable_previews(inspection)
            stats.eligible_tokens += len(actionable)
            for preview in actionable:
                candidates.append(
                    ResolveCandidate(
                        source=source,
                        token_address=preview.token_address,
                        path=int(preview.path or 0),
                        balance_raw=int(preview.balance_raw or 0),
                        receiver=preview.receiver,
                    )
                )

        if not candidates:
            return AuctionSettlementPassResult(stats=stats, errors=errors)

        try:
            base_fee_wei = await self.web3_client.get_base_fee()
            base_fee_gwei = base_fee_wei / 1e9
        except Exception as exc:  # noqa: BLE001
            errors.extend(
                [
                    ScanItemError(
                        stage="AUCTION_SETTLEMENT",
                        error_code="settlement_base_fee_check_failed",
                        error_message=f"base fee check failed: {exc}",
                        source_type=candidate.source.source_type,
                        source_address=candidate.source.source_address,
                        token_address=candidate.token_address,
                    )
                    for candidate in candidates
                ]
            )
            return AuctionSettlementPassResult(stats=stats, errors=errors)

        if base_fee_gwei > self.base_fee_cap_gwei:
            stats.skipped_high_base_fee = True
            logger.info(
                "auction_settlement_skipped_high_base_fee",
                base_fee_gwei=f"{base_fee_gwei:.2f}",
                cap_gwei=self.base_fee_cap_gwei,
                eligible_tokens=stats.eligible_tokens,
            )
            return AuctionSettlementPassResult(stats=stats, errors=errors)

        priority_fee_wei = await resolve_priority_fee_wei(self.web3_client, self.max_priority_fee_gwei)
        for candidate in candidates:
            await self._hydrate_symbols(candidate)
            result_error = await self._resolve_candidate(
                run_id=run_id,
                candidate=candidate,
                base_fee_gwei=base_fee_gwei,
                priority_fee_wei=priority_fee_wei,
                stats=stats,
            )
            if result_error is not None:
                errors.append(result_error)

        return AuctionSettlementPassResult(stats=stats, errors=errors)

    async def _hydrate_symbols(self, candidate: ResolveCandidate) -> None:
        try:
            token_metadata = await self.token_metadata_service.get_or_fetch(
                candidate.token_address,
                is_core_reward=(candidate.token_address in CORE_REWARD_TOKENS),
            )
            candidate.token_symbol = token_metadata.symbol
        except Exception:  # noqa: BLE001
            candidate.token_symbol = None

        if candidate.source.want_address:
            try:
                want_metadata = await self.token_metadata_service.get_or_fetch(
                    candidate.source.want_address,
                    is_core_reward=(candidate.source.want_address in CORE_REWARD_TOKENS),
                )
                candidate.want_symbol = want_metadata.symbol
            except Exception:  # noqa: BLE001
                candidate.want_symbol = None

    async def _resolve_candidate(
        self,
        *,
        run_id: str,
        candidate: ResolveCandidate,
        base_fee_gwei: float,
        priority_fee_wei: int,
        stats: AuctionSettlementStats,
    ) -> ScanItemError | None:
        now_iso = utcnow_iso()
        kicker_contract = self.web3_client.contract(self.settings.auction_kicker_address, AUCTION_KICKER_ABI)
        tx_data = kicker_contract.functions.resolveAuction(
            to_checksum_address(candidate.source.auction_address),
            to_checksum_address(candidate.token_address),
            False,
        )._encode_transaction_data()
        tx_params = {
            "from": self.signer.checksum_address,
            "to": to_checksum_address(self.settings.auction_kicker_address),
            "data": tx_data,
            "chainId": self.chain_id,
        }

        try:
            gas_estimate = await self.web3_client.estimate_gas(tx_params)
        except Exception as exc:  # noqa: BLE001
            friendly_error = _format_execution_error(exc)
            self._insert_settlement_row(
                run_id=run_id,
                candidate=candidate,
                now_iso=now_iso,
                status="ESTIMATE_FAILED",
                error_message=friendly_error,
            )
            stats.settlements_failed += 1
            return ScanItemError(
                stage="AUCTION_SETTLEMENT",
                error_code="auction_settlement_estimate_failed",
                error_message=friendly_error,
                source_type=candidate.source.source_type,
                source_address=candidate.source.source_address,
                token_address=candidate.token_address,
            )

        gas_limit = min(int(gas_estimate * _GAS_ESTIMATE_BUFFER), self.max_gas_limit)
        nonce = await self.web3_client.get_transaction_count(self.signer.address)
        max_fee_wei = int(self.base_fee_cap_gwei * 10**9) + int(priority_fee_wei)
        full_tx = {
            "to": to_checksum_address(self.settings.auction_kicker_address),
            "data": tx_data,
            "chainId": self.chain_id,
            "gas": gas_limit,
            "maxFeePerGas": max_fee_wei,
            "maxPriorityFeePerGas": priority_fee_wei,
            "nonce": nonce,
            "type": 2,
        }

        try:
            signed_tx = self.signer.sign_transaction(full_tx)
            tx_hash = await self.web3_client.send_raw_transaction(signed_tx)
        except Exception as exc:  # noqa: BLE001
            error_message = f"send failed: {exc}"
            self._insert_settlement_row(
                run_id=run_id,
                candidate=candidate,
                now_iso=now_iso,
                status="ERROR",
                error_message=error_message,
            )
            stats.settlements_failed += 1
            return ScanItemError(
                stage="AUCTION_SETTLEMENT",
                error_code="auction_settlement_send_failed",
                error_message=error_message,
                source_type=candidate.source.source_type,
                source_address=candidate.source.source_address,
                token_address=candidate.token_address,
            )

        stats.settlements_attempted += 1
        kick_tx_id = self._insert_settlement_row(
            run_id=run_id,
            candidate=candidate,
            now_iso=now_iso,
            status="SUBMITTED",
            tx_hash=tx_hash,
        )
        logger.info(
            "auction_settlement_submitted",
            source_type=candidate.source.source_type,
            source_address=candidate.source.source_address,
            auction=candidate.source.auction_address,
            token=candidate.token_address,
            tx_hash=tx_hash,
        )

        try:
            receipt = await self.web3_client.get_transaction_receipt(tx_hash, timeout_seconds=120)
        except Exception as exc:  # noqa: BLE001
            stats.settlements_submitted += 1
            logger.warning("auction_settlement_receipt_timeout", tx_hash=tx_hash, error=str(exc))
            return None

        receipt_status = receipt.get("status", 0)
        receipt_gas_used = receipt.get("gasUsed")
        effective_gas_price = receipt.get("effectiveGasPrice")
        receipt_block = receipt.get("blockNumber")
        effective_gwei = str(round(effective_gas_price / 1e9, 4)) if effective_gas_price else None
        final_status = "CONFIRMED" if receipt_status == 1 else "REVERTED"
        self.kick_tx_repository.update_status(
            kick_tx_id,
            status=final_status,
            gas_used=receipt_gas_used,
            gas_price_gwei=effective_gwei,
            block_number=receipt_block,
            error_message="resolve transaction reverted" if final_status == "REVERTED" else None,
        )

        if final_status == "CONFIRMED":
            stats.settlements_confirmed += 1
            logger.info(
                "auction_settlement_confirmed",
                tx_hash=tx_hash,
                block_number=receipt_block,
                gas_used=receipt_gas_used,
                auction=candidate.source.auction_address,
                token=candidate.token_address,
            )
            return None

        stats.settlements_failed += 1
        logger.warning(
            "auction_settlement_reverted",
            tx_hash=tx_hash,
            block_number=receipt_block,
            auction=candidate.source.auction_address,
            token=candidate.token_address,
        )
        return ScanItemError(
            stage="AUCTION_SETTLEMENT",
            error_code="auction_settlement_reverted",
            error_message="resolve transaction reverted",
            source_type=candidate.source.source_type,
            source_address=candidate.source.source_address,
            token_address=candidate.token_address,
        )

    def _insert_settlement_row(
        self,
        *,
        run_id: str,
        candidate: ResolveCandidate,
        now_iso: str,
        status: str,
        error_message: str | None = None,
        tx_hash: str | None = None,
    ) -> int:
        row: dict[str, object] = {
            "run_id": run_id,
            "operation_type": "resolve_auction",
            "source_type": candidate.source.source_type,
            "source_address": candidate.source.source_address,
            "token_address": candidate.token_address,
            "auction_address": candidate.source.auction_address,
            "normalized_balance": "0",
            "status": status,
            "created_at": now_iso,
            "token_symbol": candidate.token_symbol,
            "want_address": candidate.source.want_address,
            "want_symbol": candidate.want_symbol,
            "stuck_abort_reason": path_reason(candidate.path),
        }
        if candidate.source.source_type == "strategy":
            row["strategy_address"] = candidate.source.source_address
        if candidate.balance_raw:
            row["sell_amount"] = str(candidate.balance_raw)
        if error_message is not None:
            row["error_message"] = error_message
        if tx_hash is not None:
            row["tx_hash"] = tx_hash
        return self.kick_tx_repository.insert(row)
