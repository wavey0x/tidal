"""Scanner-side stale auction settlement."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from eth_utils import to_checksum_address
import structlog

from tidal.chain.contracts.abis import AUCTION_ABI
from tidal.chain.contracts.erc20 import ERC20Reader
from tidal.chain.contracts.multicall import MulticallClient
from tidal.chain.web3_client import Web3Client
from tidal.persistence.repositories import KickTxRepository
from tidal.scanner.auction_state import AuctionStateReader
from tidal.time import utcnow_iso
from tidal.transaction_service.kick_shared import _format_execution_error
from tidal.transaction_service.signer import TransactionSigner
from tidal.types import ScanItemError

logger = structlog.get_logger(__name__)

_GAS_ESTIMATE_BUFFER = 1.2
_DEFAULT_PRIORITY_FEE_GWEI = 0.1


@dataclass(slots=True, frozen=True)
class AuctionSource:
    source_type: str
    source_address: str
    auction_address: str
    want_address: str | None


@dataclass(slots=True)
class SettleCandidate:
    source: AuctionSource
    token_address: str
    kicked_at: int
    auction_length: int
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
    """Clears stale active auctions by calling settle(token) for zero-balance lots."""

    def __init__(
        self,
        *,
        web3_client: Web3Client,
        multicall_client: MulticallClient | None,
        multicall_enabled: bool,
        multicall_auction_batch_calls: int,
        erc20_reader: ERC20Reader,
        signer: TransactionSigner,
        kick_tx_repository: KickTxRepository,
        token_metadata_service,
        max_base_fee_gwei: float,
        max_priority_fee_gwei: int,
        max_gas_limit: int,
        chain_id: int,
    ) -> None:
        self.web3_client = web3_client
        self.multicall_client = multicall_client
        self.multicall_enabled = multicall_enabled
        self.multicall_auction_batch_calls = multicall_auction_batch_calls
        self.erc20_reader = erc20_reader
        self.signer = signer
        self.kick_tx_repository = kick_tx_repository
        self.token_metadata_service = token_metadata_service
        self.max_base_fee_gwei = max_base_fee_gwei
        self.max_priority_fee_gwei = max_priority_fee_gwei
        self.max_gas_limit = max_gas_limit
        self.chain_id = chain_id
        self.auction_state_reader = AuctionStateReader(
            web3_client=web3_client,
            multicall_client=multicall_client,
            multicall_enabled=multicall_enabled,
            multicall_auction_batch_calls=multicall_auction_batch_calls,
        )

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

        active_flags = await self.auction_state_reader.read_bool_noargs_many(auction_addresses, "isAnActiveAuction")
        active_auctions = [auction for auction in auction_addresses if active_flags.get(auction) is True]
        stats.active_auctions = len(active_auctions)
        if not active_auctions:
            return AuctionSettlementPassResult(stats=stats, errors=errors)

        enabled_tokens = await self.auction_state_reader.read_address_array_noargs_many(active_auctions, "getAllEnabledAuctions")
        auction_lengths = await self.auction_state_reader.read_uint_noargs_many(active_auctions, "auctionLength")

        auction_token_pairs: list[tuple[str, str]] = []
        for auction_address in active_auctions:
            for token_address in enabled_tokens.get(auction_address, []):
                auction_token_pairs.append((auction_address, token_address))

        if not auction_token_pairs:
            return AuctionSettlementPassResult(stats=stats, errors=errors)

        token_active = await self.auction_state_reader.read_bool_arg_many(auction_token_pairs, "isActive")
        kicked_at = await self.auction_state_reader.read_uint_arg_many(auction_token_pairs, "kicked")
        balances, _ = await self.erc20_reader.read_balances_many(
            [(auction_address, token_address) for auction_address, token_address in auction_token_pairs]
        )

        candidates_by_auction: dict[str, list[SettleCandidate]] = defaultdict(list)
        for auction_address, token_address in auction_token_pairs:
            source = source_by_auction[auction_address]
            active = token_active.get((auction_address, token_address))
            if active is not True:
                continue

            balance = balances.get((auction_address, token_address))
            if balance is None:
                errors.append(
                    ScanItemError(
                        stage="AUCTION_SETTLEMENT",
                        error_code="settlement_balance_read_failed",
                        error_message="balanceOf(auction) read failed",
                        source_type=source.source_type,
                        source_address=source.source_address,
                        token_address=token_address,
                    )
                )
                continue

            if balance > 0:
                stats.blocking_tokens += 1
                logger.info(
                    "auction_settlement_blocked_by_balance",
                    source_type=source.source_type,
                    source_address=source.source_address,
                    auction=auction_address,
                    token=token_address,
                    raw_balance=str(balance),
                    active_until=_active_until_iso(kicked_at.get((auction_address, token_address)), auction_lengths.get(auction_address)),
                )
                continue

            kicked_at_value = kicked_at.get((auction_address, token_address))
            auction_length = auction_lengths.get(auction_address)
            if kicked_at_value is None or auction_length is None:
                errors.append(
                    ScanItemError(
                        stage="AUCTION_SETTLEMENT",
                        error_code="settlement_metadata_read_failed",
                        error_message="kicked timestamp or auction length missing",
                        source_type=source.source_type,
                        source_address=source.source_address,
                        token_address=token_address,
                    )
                )
                continue

            candidates_by_auction[auction_address].append(
                SettleCandidate(
                    source=source,
                    token_address=token_address,
                    kicked_at=kicked_at_value,
                    auction_length=auction_length,
                )
            )
            stats.eligible_tokens += 1

        if not any(candidates_by_auction.values()):
            return AuctionSettlementPassResult(stats=stats, errors=errors)

        try:
            base_fee_wei = await self.web3_client.get_base_fee()
            base_fee_gwei = base_fee_wei / 1e9
        except Exception as exc:  # noqa: BLE001
            errors.extend(
                self._global_errors(
                    candidates_by_auction,
                    "settlement_base_fee_check_failed",
                    f"base fee check failed: {exc}",
                )
            )
            return AuctionSettlementPassResult(stats=stats, errors=errors)

        if base_fee_gwei > self.max_base_fee_gwei:
            stats.skipped_high_base_fee = True
            logger.info(
                "auction_settlement_skipped_high_base_fee",
                base_fee_gwei=f"{base_fee_gwei:.2f}",
                limit_gwei=self.max_base_fee_gwei,
                eligible_tokens=stats.eligible_tokens,
            )
            return AuctionSettlementPassResult(stats=stats, errors=errors)

        priority_fee_wei = await self._resolve_priority_fee_wei()
        for auction_address, auction_candidates in candidates_by_auction.items():
            auction_candidates.sort(key=lambda item: (item.kicked_at, item.token_address))
            for candidate in auction_candidates:
                if not await self._is_auction_active(auction_address):
                    break
                await self._hydrate_symbols(candidate)
                result_error = await self._settle_candidate(
                    run_id=run_id,
                    candidate=candidate,
                    base_fee_gwei=base_fee_gwei,
                    priority_fee_wei=priority_fee_wei,
                    stats=stats,
                )
                if result_error is not None:
                    errors.append(result_error)

        return AuctionSettlementPassResult(stats=stats, errors=errors)

    async def _resolve_priority_fee_wei(self) -> int:
        cap_wei = self.max_priority_fee_gwei * 10**9
        try:
            suggested_wei = await self.web3_client.get_max_priority_fee()
        except Exception:  # noqa: BLE001
            fallback_wei = int(_DEFAULT_PRIORITY_FEE_GWEI * 10**9)
            return min(fallback_wei, cap_wei)
        return min(suggested_wei, cap_wei)

    async def _hydrate_symbols(self, candidate: SettleCandidate) -> None:
        try:
            token_metadata = await self.token_metadata_service.get_or_fetch(candidate.token_address)
            candidate.token_symbol = token_metadata.symbol
        except Exception:  # noqa: BLE001
            candidate.token_symbol = None

        if candidate.source.want_address:
            try:
                want_metadata = await self.token_metadata_service.get_or_fetch(candidate.source.want_address)
                candidate.want_symbol = want_metadata.symbol
            except Exception:  # noqa: BLE001
                candidate.want_symbol = None

    async def _settle_candidate(
        self,
        *,
        run_id: str,
        candidate: SettleCandidate,
        base_fee_gwei: float,
        priority_fee_wei: int,
        stats: AuctionSettlementStats,
    ) -> ScanItemError | None:
        now_iso = utcnow_iso()
        auction_address = candidate.source.auction_address
        contract = self.web3_client.contract(auction_address, AUCTION_ABI)
        tx_data = contract.functions.settle(to_checksum_address(candidate.token_address))._encode_transaction_data()
        tx_params = {
            "from": self.signer.checksum_address,
            "to": to_checksum_address(auction_address),
            "data": tx_data,
            "chainId": self.chain_id,
        }

        try:
            gas_estimate = await self.web3_client.estimate_gas(tx_params)
        except Exception as exc:  # noqa: BLE001
            friendly_error = _format_execution_error(exc)
            if "not active" in friendly_error.lower():
                logger.info(
                    "auction_settlement_no_longer_needed",
                    source_type=candidate.source.source_type,
                    source_address=candidate.source.source_address,
                    auction=auction_address,
                    token=candidate.token_address,
                )
                return None
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
        max_fee_wei = int((max(self.max_base_fee_gwei, base_fee_gwei) + self.max_priority_fee_gwei) * 10**9)
        full_tx = {
            "to": to_checksum_address(auction_address),
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
            auction=auction_address,
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
            error_message="settlement transaction reverted" if final_status == "REVERTED" else None,
        )

        if final_status == "CONFIRMED":
            stats.settlements_confirmed += 1
            logger.info(
                "auction_settlement_confirmed",
                tx_hash=tx_hash,
                block_number=receipt_block,
                gas_used=receipt_gas_used,
                auction=auction_address,
                token=candidate.token_address,
            )
            return None

        stats.settlements_failed += 1
        logger.warning(
            "auction_settlement_reverted",
            tx_hash=tx_hash,
            block_number=receipt_block,
            auction=auction_address,
            token=candidate.token_address,
        )
        return ScanItemError(
            stage="AUCTION_SETTLEMENT",
            error_code="auction_settlement_reverted",
            error_message="settlement transaction reverted",
            source_type=candidate.source.source_type,
            source_address=candidate.source.source_address,
            token_address=candidate.token_address,
        )

    def _insert_settlement_row(
        self,
        *,
        run_id: str,
        candidate: SettleCandidate,
        now_iso: str,
        status: str,
        error_message: str | None = None,
        tx_hash: str | None = None,
    ) -> int:
        row: dict[str, object] = {
            "run_id": run_id,
            "operation_type": "settle",
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
        }
        if candidate.source.source_type == "strategy":
            row["strategy_address"] = candidate.source.source_address
        if error_message is not None:
            row["error_message"] = error_message
        if tx_hash is not None:
            row["tx_hash"] = tx_hash
        return self.kick_tx_repository.insert(row)

    async def _is_auction_active(self, auction_address: str) -> bool:
        contract = self.web3_client.contract(auction_address, AUCTION_ABI)
        try:
            return bool(await self.web3_client.call(contract.functions.isAnActiveAuction()))
        except Exception:  # noqa: BLE001
            return False

    def _global_errors(
        self,
        candidates_by_auction: dict[str, list[SettleCandidate]],
        error_code: str,
        error_message: str,
    ) -> list[ScanItemError]:
        errors: list[ScanItemError] = []
        for candidates in candidates_by_auction.values():
            for candidate in candidates:
                errors.append(
                    ScanItemError(
                        stage="AUCTION_SETTLEMENT",
                        error_code=error_code,
                        error_message=error_message,
                        source_type=candidate.source.source_type,
                        source_address=candidate.source.source_address,
                        token_address=candidate.token_address,
                    )
                )
        return errors

def _active_until_iso(kicked_at: int | None, auction_length: int | None) -> str | None:
    if kicked_at is None or auction_length is None or kicked_at <= 0 or auction_length <= 0:
        return None
    return datetime.fromtimestamp(kicked_at + auction_length, tz=timezone.utc).isoformat()
