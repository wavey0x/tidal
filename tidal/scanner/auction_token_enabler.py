"""Scanner-side auction token enablement."""

from __future__ import annotations

from dataclasses import dataclass

from eth_utils import to_checksum_address
from hexbytes import HexBytes
import structlog

from tidal.chain.contracts.abis import AUCTION_ABI, AUCTION_KICKER_ABI
from tidal.constants import YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS
from tidal.normalizers import normalize_address
from tidal.persistence.repositories import AuctionEnabledTokenRepository, KickTxRepository
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
class AuctionEnableSource:
    source_type: str
    source_address: str
    auction_address: str
    want_address: str | None
    factory_verified: bool


@dataclass(slots=True)
class AuctionEnableCandidate:
    source: AuctionEnableSource
    token_address: str
    decimals: int
    balance_raw: int
    normalized_balance: str
    token_symbol: str | None = None
    want_symbol: str | None = None


@dataclass(slots=True, frozen=True)
class _AuctionMetadata:
    governance: str
    want: str
    receiver: str


@dataclass(slots=True)
class AuctionTokenEnablementStats:
    auctions_seen: int = 0
    candidates_seen: int = 0
    skipped_unverified_sources: int = 0
    already_enabled_tokens: int = 0
    eligible_tokens: int = 0
    preview_failed_tokens: int = 0
    enable_transactions_attempted: int = 0
    enable_transactions_confirmed: int = 0
    enable_transactions_failed: int = 0
    enable_transactions_submitted: int = 0
    tokens_confirmed: int = 0
    skipped_high_base_fee: bool = False


@dataclass(slots=True)
class AuctionTokenEnablementPassResult:
    stats: AuctionTokenEnablementStats
    errors: list[ScanItemError]


class AuctionTokenEnablementService:
    """Auto-enable discovered sell tokens on verified Yearn auctions."""

    def __init__(
        self,
        *,
        web3_client,
        auction_state_reader,
        signer: TransactionSigner,
        kick_tx_repository: KickTxRepository,
        auction_enabled_token_repository: AuctionEnabledTokenRepository,
        base_fee_cap_gwei: float,
        max_priority_fee_gwei: int,
        max_gas_limit: int,
        chain_id: int,
        settings,
    ) -> None:
        self.web3_client = web3_client
        self.auction_state_reader = auction_state_reader
        self.signer = signer
        self.kick_tx_repository = kick_tx_repository
        self.auction_enabled_token_repository = auction_enabled_token_repository
        self.base_fee_cap_gwei = base_fee_cap_gwei
        self.max_priority_fee_gwei = max_priority_fee_gwei
        self.max_gas_limit = max_gas_limit
        self.chain_id = chain_id
        self.settings = settings
        self.required_trade_handler = normalize_address(YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS)

    async def enable_missing_tokens(
        self,
        *,
        run_id: str,
        candidates: list[AuctionEnableCandidate],
        enabled_tokens_by_auction: dict[str, set[str]] | None = None,
    ) -> AuctionTokenEnablementPassResult:
        stats = AuctionTokenEnablementStats(candidates_seen=len(candidates))
        errors: list[ScanItemError] = []
        if not candidates:
            return AuctionTokenEnablementPassResult(stats=stats, errors=errors)

        stats.auctions_seen = len({candidate.source.auction_address for candidate in candidates})
        normalized_enabled = {
            normalize_address(auction): {normalize_address(token) for token in tokens}
            for auction, tokens in (enabled_tokens_by_auction or {}).items()
        }

        verified_candidates: list[AuctionEnableCandidate] = []
        for candidate in candidates:
            if candidate.source.factory_verified:
                verified_candidates.append(candidate)
            else:
                stats.skipped_unverified_sources += 1
        if not verified_candidates:
            return AuctionTokenEnablementPassResult(stats=stats, errors=errors)

        candidates_by_auction: dict[str, list[AuctionEnableCandidate]] = {}
        for candidate in verified_candidates:
            candidates_by_auction.setdefault(candidate.source.auction_address, []).append(candidate)

        metadata_by_auction: dict[str, _AuctionMetadata] = {}
        for auction_address in sorted(candidates_by_auction):
            try:
                metadata_by_auction[auction_address] = await self._read_auction_metadata(auction_address)
            except Exception as exc:  # noqa: BLE001
                for candidate in candidates_by_auction[auction_address]:
                    errors.append(
                        self._candidate_error(
                            candidate,
                            code="auction_enable_metadata_failed",
                            message=f"auction metadata read failed: {exc}",
                        )
                    )

        state_pairs = [
            (candidate.source.auction_address, candidate.token_address)
            for candidate in verified_candidates
            if candidate.source.auction_address in metadata_by_auction
            and candidate.token_address not in normalized_enabled.get(candidate.source.auction_address, set())
        ]
        try:
            enabled_state = await self.auction_state_reader.read_auction_token_enabled_many(state_pairs)
            state_read_failed_pairs: set[tuple[str, str]] = set()
        except Exception as exc:  # noqa: BLE001
            enabled_state = {}
            state_read_failed_pairs = set(state_pairs)
            for candidate in verified_candidates:
                if (candidate.source.auction_address, candidate.token_address) in state_pairs:
                    errors.append(
                        self._candidate_error(
                            candidate,
                            code="auction_enable_state_read_failed",
                            message=f"auction token state read failed: {exc}",
                        )
                    )

        eligible: list[AuctionEnableCandidate] = []
        for candidate in verified_candidates:
            source = candidate.source
            metadata = metadata_by_auction.get(source.auction_address)
            if metadata is None:
                continue

            if metadata.governance != self.required_trade_handler:
                errors.append(
                    self._candidate_error(
                        candidate,
                        code="auction_enable_governance_mismatch",
                        message="auction governance does not match the required Yearn trade handler",
                    )
                )
                continue
            if source.want_address is None or metadata.want != source.want_address:
                errors.append(
                    self._candidate_error(
                        candidate,
                        code="auction_enable_want_mismatch",
                        message="auction want does not match the mapped source want",
                    )
                )
                continue
            if metadata.receiver != source.source_address:
                errors.append(
                    self._candidate_error(
                        candidate,
                        code="auction_enable_receiver_mismatch",
                        message="auction receiver does not match the mapped source",
                    )
                )
                continue
            if candidate.token_address == metadata.want or candidate.balance_raw <= 0:
                continue
            if candidate.token_address in normalized_enabled.get(source.auction_address, set()):
                stats.already_enabled_tokens += 1
                continue

            pair_key = (source.auction_address, candidate.token_address)
            if pair_key in state_read_failed_pairs:
                continue

            enabled = enabled_state.get(pair_key)
            if enabled is True:
                stats.already_enabled_tokens += 1
                continue
            if enabled is None:
                errors.append(
                    self._candidate_error(
                        candidate,
                        code="auction_enable_state_read_failed",
                        message="auction token state read failed",
                    )
                )
                continue

            preview_error = await self._preview_enable(candidate, metadata)
            if preview_error is not None:
                stats.preview_failed_tokens += 1
                errors.append(preview_error)
                continue

            eligible.append(candidate)

        stats.eligible_tokens = len(eligible)
        if not eligible:
            return AuctionTokenEnablementPassResult(stats=stats, errors=errors)

        try:
            base_fee_wei = await self.web3_client.get_base_fee()
            base_fee_gwei = base_fee_wei / 1e9
        except Exception as exc:  # noqa: BLE001
            errors.extend(
                [
                    self._candidate_error(
                        candidate,
                        code="auction_enable_base_fee_check_failed",
                        message=f"base fee check failed: {exc}",
                    )
                    for candidate in eligible
                ]
            )
            return AuctionTokenEnablementPassResult(stats=stats, errors=errors)

        if base_fee_gwei > self.base_fee_cap_gwei:
            stats.skipped_high_base_fee = True
            logger.info(
                "auction_token_enablement_skipped_high_base_fee",
                base_fee_gwei=f"{base_fee_gwei:.2f}",
                cap_gwei=self.base_fee_cap_gwei,
                eligible_tokens=stats.eligible_tokens,
            )
            return AuctionTokenEnablementPassResult(stats=stats, errors=errors)

        priority_fee_wei = await resolve_priority_fee_wei(self.web3_client, self.max_priority_fee_gwei)
        for auction_address in sorted({candidate.source.auction_address for candidate in eligible}):
            auction_candidates = sorted(
                [candidate for candidate in eligible if candidate.source.auction_address == auction_address],
                key=lambda candidate: candidate.token_address,
            )
            batch_errors = await self._enable_auction_batches(
                run_id=run_id,
                auction_candidates=auction_candidates,
                base_fee_gwei=base_fee_gwei,
                priority_fee_wei=priority_fee_wei,
                stats=stats,
            )
            errors.extend(batch_errors)

        return AuctionTokenEnablementPassResult(stats=stats, errors=errors)

    async def _read_auction_metadata(self, auction_address: str) -> _AuctionMetadata:
        contract = self.web3_client.contract(auction_address, AUCTION_ABI)
        governance = normalize_address(await self.web3_client.call(contract.functions.governance()))
        want = normalize_address(await self.web3_client.call(contract.functions.want()))
        receiver = normalize_address(await self.web3_client.call(contract.functions.receiver()))
        return _AuctionMetadata(governance=governance, want=want, receiver=receiver)

    async def _preview_enable(
        self,
        candidate: AuctionEnableCandidate,
        metadata: _AuctionMetadata,
    ) -> ScanItemError | None:
        contract = self.web3_client.contract(candidate.source.auction_address, AUCTION_ABI)
        fn = contract.functions.enable(to_checksum_address(candidate.token_address))
        try:
            await self.web3_client.eth_call_raw(
                candidate.source.auction_address,
                bytes(HexBytes(fn._encode_transaction_data())),
                from_address=metadata.governance,
            )
            return None
        except Exception as exc:  # noqa: BLE001
            return self._candidate_error(
                candidate,
                code="auction_enable_preview_failed",
                message=_format_execution_error(exc),
            )

    async def _enable_auction_batches(
        self,
        *,
        run_id: str,
        auction_candidates: list[AuctionEnableCandidate],
        base_fee_gwei: float,
        priority_fee_wei: int,
        stats: AuctionTokenEnablementStats,
    ) -> list[ScanItemError]:
        errors: list[ScanItemError] = []
        current: list[AuctionEnableCandidate] = []
        current_estimate: int | None = None

        for candidate in auction_candidates:
            tentative = [*current, candidate]
            estimate, estimate_error = await self._estimate_batch(tentative)
            if estimate_error is not None:
                if current and current_estimate is not None:
                    errors.extend(
                        await self._send_batch(
                            run_id=run_id,
                            batch=current,
                            gas_estimate=current_estimate,
                            base_fee_gwei=base_fee_gwei,
                            priority_fee_wei=priority_fee_wei,
                            stats=stats,
                        )
                    )
                    current = []
                    current_estimate = None
                    estimate, estimate_error = await self._estimate_batch([candidate])

                if estimate_error is not None:
                    errors.extend(
                        self._insert_failed_batch_rows(
                            run_id=run_id,
                            batch=[candidate],
                            status="ESTIMATE_FAILED",
                            error_message=estimate_error,
                            stats=stats,
                        )
                    )
                    continue

            if estimate is not None and estimate > self.max_gas_limit:
                if current and current_estimate is not None:
                    errors.extend(
                        await self._send_batch(
                            run_id=run_id,
                            batch=current,
                            gas_estimate=current_estimate,
                            base_fee_gwei=base_fee_gwei,
                            priority_fee_wei=priority_fee_wei,
                            stats=stats,
                        )
                    )
                    current = []
                    current_estimate = None
                    estimate, estimate_error = await self._estimate_batch([candidate])
                    if estimate_error is not None:
                        errors.extend(
                            self._insert_failed_batch_rows(
                                run_id=run_id,
                                batch=[candidate],
                                status="ESTIMATE_FAILED",
                                error_message=estimate_error,
                                stats=stats,
                            )
                        )
                        continue

                if estimate is not None and estimate > self.max_gas_limit:
                    message = (
                        f"enable-tokens batch estimates {estimate:,} gas, "
                        f"above txn_max_gas_limit {self.max_gas_limit:,}"
                    )
                    errors.extend(
                        self._insert_failed_batch_rows(
                            run_id=run_id,
                            batch=[candidate],
                            status="ESTIMATE_FAILED",
                            error_message=message,
                            stats=stats,
                        )
                    )
                    continue

            current = [*current, candidate]
            current_estimate = estimate

        if current and current_estimate is not None:
            errors.extend(
                await self._send_batch(
                    run_id=run_id,
                    batch=current,
                    gas_estimate=current_estimate,
                    base_fee_gwei=base_fee_gwei,
                    priority_fee_wei=priority_fee_wei,
                    stats=stats,
                )
            )

        return errors

    async def _estimate_batch(self, batch: list[AuctionEnableCandidate]) -> tuple[int | None, str | None]:
        if not batch:
            return None, "empty enable batch"
        tx_params = {
            "from": self.signer.checksum_address,
            "to": to_checksum_address(normalize_address(self.settings.auction_kicker_address)),
            "data": self._batch_tx_data(batch),
            "chainId": self.chain_id,
        }
        try:
            return await self.web3_client.estimate_gas(tx_params), None
        except Exception as exc:  # noqa: BLE001
            return None, _format_execution_error(exc)

    async def _send_batch(
        self,
        *,
        run_id: str,
        batch: list[AuctionEnableCandidate],
        gas_estimate: int,
        base_fee_gwei: float,
        priority_fee_wei: int,
        stats: AuctionTokenEnablementStats,
    ) -> list[ScanItemError]:
        now_iso = utcnow_iso()
        tx_data = self._batch_tx_data(batch)
        gas_limit = min(int(gas_estimate * _GAS_ESTIMATE_BUFFER), self.max_gas_limit)
        nonce = await self.web3_client.get_transaction_count(self.signer.address)
        max_fee_wei = int(self.base_fee_cap_gwei * 10**9) + int(priority_fee_wei)
        full_tx = {
            "to": to_checksum_address(normalize_address(self.settings.auction_kicker_address)),
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
            return self._insert_failed_batch_rows(
                run_id=run_id,
                batch=batch,
                status="ERROR",
                error_message=f"send failed: {exc}",
                stats=stats,
            )

        stats.enable_transactions_attempted += 1
        row_ids = [
            self._insert_enable_row(
                run_id=run_id,
                candidate=candidate,
                now_iso=now_iso,
                status="SUBMITTED",
                tx_hash=tx_hash,
            )
            for candidate in batch
        ]
        logger.info(
            "auction_token_enablement_submitted",
            auction=batch[0].source.auction_address,
            tokens=[candidate.token_address for candidate in batch],
            tx_hash=tx_hash,
        )

        try:
            receipt = await self.web3_client.get_transaction_receipt(tx_hash, timeout_seconds=120)
        except Exception as exc:  # noqa: BLE001
            stats.enable_transactions_submitted += 1
            logger.warning("auction_token_enablement_receipt_timeout", tx_hash=tx_hash, error=str(exc))
            return []

        receipt_status = receipt.get("status", 0)
        receipt_gas_used = receipt.get("gasUsed")
        effective_gas_price = receipt.get("effectiveGasPrice")
        receipt_block = receipt.get("blockNumber")
        effective_gwei = str(round(effective_gas_price / 1e9, 4)) if effective_gas_price else None
        final_status = "CONFIRMED" if receipt_status == 1 else "REVERTED"
        for row_id in row_ids:
            self.kick_tx_repository.update_status(
                row_id,
                status=final_status,
                gas_used=receipt_gas_used,
                gas_price_gwei=effective_gwei,
                block_number=receipt_block,
                error_message="enable-tokens transaction reverted" if final_status == "REVERTED" else None,
            )

        if final_status == "CONFIRMED":
            stats.enable_transactions_confirmed += 1
            stats.tokens_confirmed += len(batch)
            self.auction_enabled_token_repository.mark_tokens_enabled(
                batch[0].source.auction_address,
                [candidate.token_address for candidate in batch],
                utcnow_iso(),
            )
            logger.info(
                "auction_token_enablement_confirmed",
                tx_hash=tx_hash,
                block_number=receipt_block,
                gas_used=receipt_gas_used,
                auction=batch[0].source.auction_address,
                tokens=[candidate.token_address for candidate in batch],
            )
            return []

        stats.enable_transactions_failed += 1
        logger.warning(
            "auction_token_enablement_reverted",
            tx_hash=tx_hash,
            block_number=receipt_block,
            auction=batch[0].source.auction_address,
            tokens=[candidate.token_address for candidate in batch],
        )
        return [
            self._candidate_error(
                candidate,
                code="auction_enable_reverted",
                message="enable-tokens transaction reverted",
            )
            for candidate in batch
        ]

    def _batch_tx_data(self, batch: list[AuctionEnableCandidate]) -> str | bytes:
        auction_address = batch[0].source.auction_address
        kicker_contract = self.web3_client.contract(self.settings.auction_kicker_address, AUCTION_KICKER_ABI)
        return kicker_contract.functions.enableTokens(
            to_checksum_address(auction_address),
            [to_checksum_address(candidate.token_address) for candidate in batch],
        )._encode_transaction_data()

    def _insert_failed_batch_rows(
        self,
        *,
        run_id: str,
        batch: list[AuctionEnableCandidate],
        status: str,
        error_message: str,
        stats: AuctionTokenEnablementStats,
    ) -> list[ScanItemError]:
        now_iso = utcnow_iso()
        for candidate in batch:
            self._insert_enable_row(
                run_id=run_id,
                candidate=candidate,
                now_iso=now_iso,
                status=status,
                error_message=error_message,
            )
        stats.enable_transactions_failed += 1
        return [
            self._candidate_error(
                candidate,
                code=(
                    "auction_enable_estimate_failed"
                    if status == "ESTIMATE_FAILED"
                    else "auction_enable_send_failed"
                ),
                message=error_message,
            )
            for candidate in batch
        ]

    def _insert_enable_row(
        self,
        *,
        run_id: str,
        candidate: AuctionEnableCandidate,
        now_iso: str,
        status: str,
        error_message: str | None = None,
        tx_hash: str | None = None,
    ) -> int:
        row: dict[str, object] = {
            "run_id": run_id,
            "operation_type": "enable_tokens",
            "source_type": candidate.source.source_type,
            "source_address": candidate.source.source_address,
            "token_address": candidate.token_address,
            "auction_address": candidate.source.auction_address,
            "normalized_balance": candidate.normalized_balance,
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

    def _candidate_error(
        self,
        candidate: AuctionEnableCandidate,
        *,
        code: str,
        message: str,
    ) -> ScanItemError:
        return ScanItemError(
            stage="AUCTION_TOKEN_ENABLEMENT",
            error_code=code,
            error_message=message,
            source_type=candidate.source.source_type,
            source_address=candidate.source.source_address,
            token_address=candidate.token_address,
        )
