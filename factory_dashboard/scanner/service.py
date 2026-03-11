"""Main scanner orchestration service."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import structlog

from factory_dashboard.alerts.base import AlertSink
from factory_dashboard.constants import ADDITIONAL_DISCOVERY_VAULTS, CORE_REWARD_TOKENS
from factory_dashboard.normalizers import normalize_address, to_decimal_string
from factory_dashboard.pricing.service import PriceToken
from factory_dashboard.time import utcnow, utcnow_iso
from factory_dashboard.types import BalancePair, BalanceResult, ScanItemError, ScanRunResult

# (step_number, total_steps, stage_label, detail_string)
ProgressCallback = Callable[[int, int, str, str], None]

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class _Pair:
    strategy_address: str
    token_address: str
    decimals: int


def determine_scan_status(*, pairs_seen: int, pairs_failed: int) -> str:
    if pairs_seen == 0 and pairs_failed > 0:
        return "FAILED"
    if pairs_failed > 0:
        return "PARTIAL_SUCCESS"
    return "SUCCESS"


class ScannerService:
    """Coordinates discovery, metadata hydration, and balance caching."""

    def __init__(
        self,
        *,
        session,
        chain_id: int,
        concurrency: int,
        multicall_enabled: bool,
        web3_client,
        strategy_auction_mapper,
        strategy_discovery_service,
        reward_token_resolver,
        token_metadata_service,
        token_price_refresh_service,
        balance_reader,
        name_reader,
        vault_repository,
        strategy_repository,
        strategy_token_repository,
        balance_repository,
        scan_run_repository,
        scan_item_error_repository,
        alert_sink: AlertSink,
    ):
        del concurrency
        self.session = session
        self.chain_id = chain_id
        self.multicall_enabled = multicall_enabled
        self.web3_client = web3_client
        self.strategy_auction_mapper = strategy_auction_mapper
        self.strategy_discovery_service = strategy_discovery_service
        self.reward_token_resolver = reward_token_resolver
        self.token_metadata_service = token_metadata_service
        self.token_price_refresh_service = token_price_refresh_service
        self.balance_reader = balance_reader
        self.name_reader = name_reader
        self.vault_repository = vault_repository
        self.strategy_repository = strategy_repository
        self.strategy_token_repository = strategy_token_repository
        self.balance_repository = balance_repository
        self.scan_run_repository = scan_run_repository
        self.scan_item_error_repository = scan_item_error_repository
        self.alert_sink = alert_sink

    async def scan_once(self, on_progress: ProgressCallback | None = None) -> ScanRunResult:
        _TOTAL_STEPS = 7

        def _progress(step: int, label: str, detail: str = "") -> None:
            if on_progress is not None:
                on_progress(step, _TOTAL_STEPS, label, detail)

        run_id = str(uuid.uuid4())
        started_at = utcnow_iso()
        self.scan_run_repository.create(
            {
                "run_id": run_id,
                "started_at": started_at,
                "finished_at": None,
                "status": "RUNNING",
                "vaults_seen": 0,
                "strategies_seen": 0,
                "pairs_seen": 0,
                "pairs_succeeded": 0,
                "pairs_failed": 0,
                "error_summary": None,
            }
        )

        errors: list[ScanItemError] = []
        vaults_seen = 0
        strategies_seen = 0
        pairs_seen = 0
        pairs_succeeded = 0
        pairs_failed = 0

        stage_a_stats = {
            "batch_count": 0,
            "subcalls_total": 0,
            "subcalls_failed": 0,
            "fallback_direct_calls_total": 0,
            "overflow_vaults_count": 0,
        }
        stage_b_stats = {
            "batch_count": 0,
            "subcalls_total": 0,
            "subcalls_failed": 0,
            "fallback_direct_calls_total": 0,
        }
        stage_c_stats = {
            "batch_count": 0,
            "subcalls_total": 0,
            "subcalls_failed": 0,
            "fallback_direct_calls_total": 0,
        }
        stage_d_stats = {
            "tokens_seen": 0,
            "tokens_succeeded": 0,
            "tokens_not_found": 0,
            "tokens_failed": 0,
        }
        stage_e_stats = {
            "auction_count": 0,
            "governance_allowed_auction_count": 0,
            "strategies_mapped": 0,
            "strategies_unmapped": 0,
            "source": "none",
        }

        _progress(1, "Discovering strategies")
        try:
            discovered, vaults_seen, stage_a_stats = await self.strategy_discovery_service.discover()
        except Exception as exc:  # noqa: BLE001
            status = "FAILED"
            summary = f"discovery_failed: {exc}"
            finished_at = utcnow_iso()
            self.scan_run_repository.finalize(
                run_id,
                finished_at=finished_at,
                status=status,
                vaults_seen=0,
                strategies_seen=0,
                pairs_seen=0,
                pairs_succeeded=0,
                pairs_failed=0,
                error_summary=summary,
            )
            self.session.commit()
            await self.alert_sink.send_critical("scan failed", summary)
            raise

        now_iso = utcnow_iso()
        vault_addresses = sorted(
            {normalize_address(item.vault_address) for item in discovered}.union(
                ADDITIONAL_DISCOVERY_VAULTS
            )
        )
        vault_rows = [
            {
                "address": address,
                "chain_id": self.chain_id,
                "name": None,
                "symbol": None,
                "active": 1,
                "first_seen_at": now_iso,
                "last_seen_at": now_iso,
            }
            for address in vault_addresses
        ]
        self.vault_repository.upsert_many(vault_rows)

        strategy_rows = [
            {
                "address": normalize_address(item.strategy_address),
                "chain_id": self.chain_id,
                "vault_address": normalize_address(item.vault_address),
                "name": None,
                "adapter": "yearn_curve_strategy",
                "active": 1,
                "first_seen_at": now_iso,
                "last_seen_at": now_iso,
            }
            for item in discovered
        ]
        self.strategy_repository.upsert_many(strategy_rows)
        strategies_seen = len(discovered)
        self.vault_repository.delete_strategy_address_rows_without_children()
        _progress(1, "Discovering strategies", f"{strategies_seen} strategies, {vaults_seen} vaults")

        strategy_addresses = [normalize_address(item.strategy_address) for item in discovered]
        auction_updated_at = utcnow_iso()
        _progress(2, "Mapping auctions")
        try:
            mapping_result = await self.strategy_auction_mapper.refresh_for_strategies(strategy_addresses)
            self.strategy_repository.set_auction_mappings(
                mapping_result.strategy_to_auction,
                updated_at=auction_updated_at,
                strategy_to_want=mapping_result.strategy_to_want,
            )
            stage_e_stats["auction_count"] = mapping_result.auction_count
            stage_e_stats["governance_allowed_auction_count"] = mapping_result.governance_allowed_auction_count
            stage_e_stats["strategies_mapped"] = mapping_result.mapped_count
            stage_e_stats["strategies_unmapped"] = mapping_result.unmapped_count
            stage_e_stats["source"] = mapping_result.source
        except Exception as exc:  # noqa: BLE001
            errors.append(
                ScanItemError(
                    stage="AUCTION_MAPPING",
                    error_code="strategy_auction_mapping_failed",
                    error_message=str(exc),
                )
            )
            self.strategy_repository.mark_auction_refresh_failed(
                strategy_addresses,
                updated_at=auction_updated_at,
                error_message=str(exc),
            )
            cached_mapping = self.strategy_repository.auction_mapping_for_addresses(strategy_addresses)
            mapped_count = sum(
                1
                for strategy_address in set(strategy_addresses)
                if cached_mapping.get(strategy_address) is not None
            )
            stage_e_stats["strategies_mapped"] = mapped_count
            stage_e_stats["strategies_unmapped"] = max(0, len(set(strategy_addresses)) - mapped_count)
            stage_e_stats["source"] = "cache"
        _progress(2, "Mapping auctions", f"{stage_e_stats['strategies_mapped']} mapped, {stage_e_stats['strategies_unmapped']} unmapped")

        _progress(3, "Hydrating names")
        await self._hydrate_cached_names(vault_addresses=vault_addresses, strategy_addresses=strategy_addresses, errors=errors)
        _progress(3, "Hydrating names", "done")

        _progress(4, "Resolving reward tokens")
        try:
            resolved_tokens_by_strategy, stage_b_stats = await self.reward_token_resolver.resolve_many(strategy_addresses)
        except Exception as exc:  # noqa: BLE001
            stage_b_stats["subcalls_failed"] = len(strategy_addresses)
            resolved_tokens_by_strategy = {address: set(CORE_REWARD_TOKENS) for address in strategy_addresses}
            errors.extend(
                [
                    ScanItemError(
                        stage="TOKEN_RESOLUTION",
                        error_code="rewards_tokens_read_failed",
                        error_message=str(exc),
                        strategy_address=address,
                    )
                    for address in strategy_addresses
                ]
            )

        _progress(4, "Resolving reward tokens", "done")

        _progress(5, "Fetching token metadata")
        pairs: list[_Pair] = []

        for item in discovered:
            strategy_address = normalize_address(item.strategy_address)
            token_set = {
                normalize_address(token)
                for token in resolved_tokens_by_strategy.get(strategy_address, set(CORE_REWARD_TOKENS))
            }

            for token_address in token_set:
                source = "CORE" if token_address in CORE_REWARD_TOKENS else "REWARDS_TOKENS"
                self.strategy_token_repository.upsert(strategy_address, token_address, source, now_iso)

                try:
                    metadata = await self.token_metadata_service.get_or_fetch(
                        token_address,
                        is_core_reward=(token_address in CORE_REWARD_TOKENS),
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        ScanItemError(
                            stage="METADATA",
                            error_code="token_metadata_failed",
                            error_message=str(exc),
                            strategy_address=strategy_address,
                            token_address=token_address,
                        )
                    )
                    pairs_failed += 1
                    continue

                pairs.append(
                    _Pair(
                        strategy_address=strategy_address,
                        token_address=token_address,
                        decimals=metadata.decimals,
                    )
                )

        pairs_seen = len(pairs)
        _progress(5, "Fetching token metadata", f"{pairs_seen} pairs")
        block_number = await self.web3_client.get_block_number()
        scanned_at = utcnow()

        _progress(6, "Reading balances")
        balance_pairs = [
            BalancePair(
                strategy_address=pair.strategy_address,
                token_address=pair.token_address,
            )
            for pair in pairs
        ]
        balance_values, stage_c_stats = await self.balance_reader.read_many(balance_pairs)

        tokens_with_balance: set[str] = set()
        for pair in pairs:
            key = BalancePair(strategy_address=pair.strategy_address, token_address=pair.token_address)
            raw_balance = balance_values.get(key)
            if raw_balance is None:
                errors.append(
                    ScanItemError(
                        stage="BALANCE_READ",
                        error_code="balance_read_failed",
                        error_message="multicall/direct balance read failed",
                        strategy_address=pair.strategy_address,
                        token_address=pair.token_address,
                    )
                )
                pairs_failed += 1
                continue

            if raw_balance > 0:
                tokens_with_balance.add(pair.token_address)

            normalized = to_decimal_string(raw_balance, pair.decimals)
            self.balance_repository.upsert(
                BalanceResult(
                    strategy_address=pair.strategy_address,
                    token_address=pair.token_address,
                    raw_balance=raw_balance,
                    normalized_balance=normalized,
                    block_number=block_number,
                    scanned_at=scanned_at,
                )
            )
            pairs_succeeded += 1

        _progress(6, "Reading balances", f"{pairs_succeeded} succeeded, {pairs_failed} failed")

        price_token_map = {
            pair.token_address: pair.decimals
            for pair in pairs
            if pair.token_address in tokens_with_balance
        }
        all_token_count = len({pair.token_address for pair in pairs})
        price_tokens_skipped = all_token_count - len(price_token_map)
        price_tokens = [
            PriceToken(address=token_address, decimals=decimals)
            for token_address, decimals in price_token_map.items()
        ]
        _progress(7, "Refreshing prices")
        stage_d_stats, price_errors = await self.token_price_refresh_service.refresh_many(
            run_id=run_id,
            tokens=price_tokens,
        )
        errors.extend(price_errors)
        _progress(7, "Refreshing prices", f"{stage_d_stats['tokens_succeeded']}/{stage_d_stats['tokens_seen']} tokens, {price_tokens_skipped} skipped")

        status = determine_scan_status(pairs_seen=pairs_seen, pairs_failed=pairs_failed)

        finished_at = utcnow_iso()
        error_summary = f"{len(errors)} errors" if errors else None

        self.scan_item_error_repository.add_many(run_id, errors, finished_at)
        self.scan_run_repository.finalize(
            run_id,
            finished_at=finished_at,
            status=status,
            vaults_seen=vaults_seen,
            strategies_seen=strategies_seen,
            pairs_seen=pairs_seen,
            pairs_succeeded=pairs_succeeded,
            pairs_failed=pairs_failed,
            error_summary=error_summary,
        )
        self.session.commit()

        if status == "FAILED":
            await self.alert_sink.send_critical(
                "scan failed",
                f"run_id={run_id} pairs_seen={pairs_seen} pairs_failed={pairs_failed}",
            )

        await self._alert_repeated_errors(run_id, errors)

        multicall_subcalls_total = (
            stage_a_stats["subcalls_total"]
            + stage_b_stats["subcalls_total"]
            + stage_c_stats["subcalls_total"]
        )
        multicall_subcalls_failed = (
            stage_a_stats["subcalls_failed"]
            + stage_b_stats["subcalls_failed"]
            + stage_c_stats["subcalls_failed"]
        )
        fallback_direct_calls_total = (
            stage_a_stats["fallback_direct_calls_total"]
            + stage_b_stats["fallback_direct_calls_total"]
            + stage_c_stats["fallback_direct_calls_total"]
        )

        logger.info(
            "scan_completed",
            run_id=run_id,
            status=status,
            vaults_seen=vaults_seen,
            strategies_seen=strategies_seen,
            pairs_seen=pairs_seen,
            pairs_succeeded=pairs_succeeded,
            pairs_failed=pairs_failed,
            multicall_enabled=self.multicall_enabled,
            stage_a_batches=stage_a_stats["batch_count"],
            stage_b_batches=stage_b_stats["batch_count"],
            stage_c_batches=stage_c_stats["batch_count"],
            multicall_subcalls_total=multicall_subcalls_total,
            multicall_subcalls_failed=multicall_subcalls_failed,
            overflow_vaults_count=stage_a_stats["overflow_vaults_count"],
            fallback_direct_calls_total=fallback_direct_calls_total,
            price_tokens_seen=stage_d_stats["tokens_seen"],
            price_tokens_succeeded=stage_d_stats["tokens_succeeded"],
            price_tokens_not_found=stage_d_stats["tokens_not_found"],
            price_tokens_failed=stage_d_stats["tokens_failed"],
            price_tokens_skipped=price_tokens_skipped,
            auction_count=stage_e_stats["auction_count"],
            governance_allowed_auctions=stage_e_stats["governance_allowed_auction_count"],
            strategies_with_auction=stage_e_stats["strategies_mapped"],
            strategies_without_auction=stage_e_stats["strategies_unmapped"],
            auction_mapping_source=stage_e_stats["source"],
        )

        return ScanRunResult(
            run_id=run_id,
            status=status,
            vaults_seen=vaults_seen,
            strategies_seen=strategies_seen,
            pairs_seen=pairs_seen,
            pairs_succeeded=pairs_succeeded,
            pairs_failed=pairs_failed,
        )

    async def _alert_repeated_errors(self, run_id: str, errors: list[ScanItemError]) -> None:
        if not errors:
            return

        latest_runs = self.scan_run_repository.latest_run_ids(3)
        if len(latest_runs) < 3 or latest_runs[0] != run_id:
            return

        unique_keys = {
            (err.strategy_address, err.token_address, err.stage, err.error_code)
            for err in errors
        }

        for strategy_address, token_address, stage, error_code in unique_keys:
            if all(
                self.scan_item_error_repository.has_error_for_run(
                    candidate_run_id,
                    strategy_address=strategy_address,
                    token_address=token_address,
                    stage=stage,
                    error_code=error_code,
                )
                for candidate_run_id in latest_runs
            ):
                await self.alert_sink.send_critical(
                    "repeated scan item failure",
                    (
                        f"strategy={strategy_address} token={token_address} "
                        f"stage={stage} code={error_code} repeated across 3 runs"
                    ),
                )

    async def _hydrate_cached_names(
        self,
        *,
        vault_addresses: list[str],
        strategy_addresses: list[str],
        errors: list[ScanItemError],
    ) -> None:
        missing_vault_names = self.vault_repository.addresses_missing_name(vault_addresses)
        for vault_address in missing_vault_names:
            try:
                vault_name = await self.name_reader.read_vault_name(vault_address)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    ScanItemError(
                        stage="METADATA",
                        error_code="vault_name_lookup_failed",
                        error_message=str(exc),
                    )
                )
                continue
            if vault_name:
                self.vault_repository.set_name(vault_address, vault_name)

        missing_vault_symbols = self.vault_repository.addresses_missing_symbol(vault_addresses)
        for vault_address in missing_vault_symbols:
            try:
                vault_symbol = await self.name_reader.read_vault_symbol(vault_address)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    ScanItemError(
                        stage="METADATA",
                        error_code="vault_symbol_lookup_failed",
                        error_message=str(exc),
                    )
                )
                continue
            if vault_symbol:
                self.vault_repository.set_symbol(vault_address, vault_symbol)

        missing_strategy_names = self.strategy_repository.addresses_missing_name(strategy_addresses)
        for strategy_address in missing_strategy_names:
            try:
                strategy_name = await self.name_reader.read_strategy_name(strategy_address)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    ScanItemError(
                        stage="METADATA",
                        error_code="strategy_name_lookup_failed",
                        error_message=str(exc),
                        strategy_address=strategy_address,
                    )
                )
                continue
            if strategy_name:
                self.strategy_repository.set_name(strategy_address, strategy_name)
