"""Kick planning helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from eth_utils import to_checksum_address

from tidal.chain.contracts.abis import AUCTION_KICKER_ABI
from tidal.config import Settings
from tidal.normalizers import normalize_address
from tidal.persistence.repositories import KickTxRepository
from tidal.transaction_service.evaluator import build_shortlist, sort_candidates
from tidal.transaction_service.kicker import AuctionKicker, _GAS_ESTIMATE_BUFFER, _format_execution_error, _is_active_auction_error
from tidal.transaction_service.types import (
    KickCandidate,
    KickPlan,
    KickResult,
    PreparedKick,
    PreparedSweepAndSettle,
    SkippedPreparedCandidate,
    SourceType,
    TxIntent,
)


def _candidate_key(candidate: KickCandidate) -> tuple[str, str]:
    return candidate.auction_address, candidate.token_address


def _ignore_skip_payloads(shortlist) -> list[dict[str, object]]:  # noqa: ANN001
    return [
        {
            "sourceAddress": decision.candidate.source_address,
            "auctionAddress": decision.candidate.auction_address,
            "tokenAddress": decision.candidate.token_address,
            "tokenSymbol": decision.candidate.token_symbol,
            "detail": decision.detail,
        }
        for decision in shortlist.ignored_skips
    ]


def _cooldown_skip_payloads(shortlist) -> list[dict[str, object]]:  # noqa: ANN001
    return [
        {
            "sourceAddress": decision.candidate.source_address,
            "auctionAddress": decision.candidate.auction_address,
            "tokenAddress": decision.candidate.token_address,
            "tokenSymbol": decision.candidate.token_symbol,
            "detail": decision.detail,
        }
        for decision in shortlist.cooldown_skips
    ]


class KickPlanner:
    """Build a single typed kick plan for API prepare or live execution."""

    def __init__(
        self,
        *,
        session,
        settings: Settings,
        kicker: AuctionKicker,
        kick_tx_repository: KickTxRepository,
        shortlist_builder: Callable[..., object] | None = None,
        candidate_sorter: Callable[[list[KickCandidate]], list[KickCandidate]] | None = None,
        estimate_transaction_fn: Callable[..., Awaitable[tuple[int | None, int | None, str | None]]] | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.kicker = kicker
        self.kick_tx_repository = kick_tx_repository
        self.shortlist_builder = shortlist_builder or build_shortlist
        self.candidate_sorter = candidate_sorter or sort_candidates
        self.estimate_transaction_fn = estimate_transaction_fn

    async def plan(
        self,
        *,
        source_type: SourceType | None = None,
        source_address: str | None = None,
        auction_address: str | None = None,
        token_address: str | None = None,
        limit: int | None = None,
        sender: str | None,
        run_id: str,
        batch: bool = True,
    ) -> KickPlan:
        kick_config = self.settings.kick_config
        shortlist = self.shortlist_builder(
            self.session,
            usd_threshold=self.settings.txn_usd_threshold,
            max_data_age_seconds=self.settings.txn_max_data_age_seconds,
            source_type=source_type,
            source_address=source_address,
            auction_address=auction_address,
            token_address=token_address,
            limit=limit,
            ignore_policy=kick_config.ignore_policy,
            cooldown_policy=kick_config.cooldown_policy,
            kick_tx_repository=self.kick_tx_repository,
        )
        candidates_to_prepare = self.candidate_sorter(shortlist.selected_candidates)

        plan = KickPlan(
            source_type=source_type,
            source_address=source_address,
            auction_address=auction_address,
            token_address=token_address,
            limit=limit,
            eligible_count=len(shortlist.eligible_candidates),
            selected_count=len(shortlist.selected_candidates) + len(shortlist.limited_candidates),
            ready_count=len(shortlist.selected_candidates),
            ignored_skips=_ignore_skip_payloads(shortlist),
            cooldown_skips=_cooldown_skip_payloads(shortlist),
            deferred_same_auction_count=shortlist.deferred_same_auction_count,
            limited_count=len(shortlist.limited_candidates),
            ranked_candidates=list(candidates_to_prepare),
        )
        if not candidates_to_prepare:
            return plan

        inspections = await self.kicker.inspect_candidates(candidates_to_prepare)
        prepared_kicks: list[PreparedKick] = []

        for candidate in candidates_to_prepare:
            result = await self.kicker.prepare_kick(
                candidate,
                run_id=run_id,
                inspection=inspections.get(_candidate_key(candidate)),
            )
            if isinstance(result, KickResult):
                reason = result.error_message or "candidate was skipped during prepare"
                plan.skipped_during_prepare.append(SkippedPreparedCandidate(candidate=candidate, reason=reason))
                continue
            if isinstance(result, PreparedSweepAndSettle):
                intent = self._build_sweep_and_settle_intent(result, sender=sender)
                gas_warning = await self._estimate_intent(intent, gas_cap=self.settings.txn_max_gas_limit)
                if gas_warning:
                    plan.warnings.append(gas_warning)
                plan.sweep_operations.append(result)
                plan.tx_intents.append(intent)
                continue
            prepared_kicks.append(result)

        if not prepared_kicks:
            return plan

        if not batch:
            for prepared_kick in prepared_kicks:
                recovered_kick, tx_intent, warning = await self._prepare_single_kick_intent(prepared_kick, sender=sender)
                if warning is not None:
                    plan.warnings.append(warning)
                    plan.skipped_during_prepare.append(
                        SkippedPreparedCandidate(candidate=prepared_kick.candidate, reason=warning)
                    )
                    continue
                assert recovered_kick is not None and tx_intent is not None
                plan.kick_operations.append(recovered_kick)
                plan.tx_intents.append(tx_intent)
            return plan

        if len(prepared_kicks) == 1:
            prepared_kick, tx_intent, warning = await self._prepare_single_kick_intent(prepared_kicks[0], sender=sender)
            if warning is not None:
                plan.warnings.append(warning)
                plan.skipped_during_prepare.append(
                    SkippedPreparedCandidate(candidate=prepared_kicks[0].candidate, reason=warning)
                )
                return plan
            assert prepared_kick is not None and tx_intent is not None
            plan.kick_operations.append(prepared_kick)
            plan.tx_intents.append(tx_intent)
            return plan

        batch_intent = self._build_batch_kick_intent(prepared_kicks, sender=sender)
        batch_warning = await self._estimate_intent(
            batch_intent,
            gas_cap=self.settings.txn_max_gas_limit * max(len(prepared_kicks), 1),
        )
        if batch_warning is None:
            plan.kick_operations.extend(prepared_kicks)
            plan.tx_intents.append(batch_intent)
            return plan

        if not _is_active_auction_error(batch_warning):
            plan.warnings.append(batch_warning)
            plan.skipped_during_prepare.extend(
                SkippedPreparedCandidate(candidate=prepared.candidate, reason=batch_warning)
                for prepared in prepared_kicks
            )
            return plan

        individual_warnings: list[str] = []
        successful_prepared: list[PreparedKick] = []
        successful_intents: list[TxIntent] = []
        individual_skips: list[SkippedPreparedCandidate] = []
        for prepared in prepared_kicks:
            recovered_kick, tx_intent, warning = await self._prepare_single_kick_intent(prepared, sender=sender)
            if warning is not None:
                individual_warnings.append(warning)
                individual_skips.append(SkippedPreparedCandidate(candidate=prepared.candidate, reason=warning))
                continue
            assert recovered_kick is not None and tx_intent is not None
            successful_prepared.append(recovered_kick)
            successful_intents.append(tx_intent)

        if not successful_intents:
            plan.warnings.append(batch_warning)
            plan.skipped_during_prepare.extend(
                SkippedPreparedCandidate(candidate=prepared.candidate, reason=batch_warning)
                for prepared in prepared_kicks
            )
            return plan

        plan.kick_operations.extend(successful_prepared)
        plan.tx_intents.extend(successful_intents)
        plan.warnings.extend(individual_warnings)
        plan.skipped_during_prepare.extend(individual_skips)
        return plan

    async def _prepare_single_kick_intent(
        self,
        prepared: PreparedKick,
        *,
        sender: str | None,
    ) -> tuple[PreparedKick | None, TxIntent | None, str | None]:
        standard_intent = self._build_single_kick_intent(prepared, sender=sender)
        gas_warning = await self._estimate_intent(standard_intent, gas_cap=self.settings.txn_max_gas_limit)
        if gas_warning is None:
            return prepared, standard_intent, None

        if not _is_active_auction_error(gas_warning):
            return None, None, gas_warning

        recovered = await self.kicker.plan_recovery(prepared)
        if recovered is None:
            return None, None, gas_warning

        extended_intent = self._build_single_kick_intent(recovered, sender=sender)
        extended_warning = await self._estimate_intent(extended_intent, gas_cap=self.settings.txn_max_gas_limit)
        if extended_warning is not None:
            return None, None, extended_warning
        return recovered, extended_intent, None

    def _kicker_contract(self):
        return self.kicker.web3_client.contract(
            address=to_checksum_address(self.settings.auction_kicker_address),
            abi=AUCTION_KICKER_ABI,
        )

    def _build_single_kick_intent(self, prepared: PreparedKick, *, sender: str | None) -> TxIntent:
        if getattr(type(self.kicker), "build_single_kick_intent", None) is not None:
            return self.kicker.build_single_kick_intent(prepared, sender=sender)

        kicker_contract = self._kicker_contract()
        if prepared.recovery_plan is None:
            tx_data = kicker_contract.functions.kick(*self.kicker._kick_args(prepared))._encode_transaction_data()
        else:
            tx_data = kicker_contract.functions.kickExtended(
                *self.kicker._kick_extended_args(prepared)
            )._encode_transaction_data()
        return TxIntent(
            operation="kick",
            to=normalize_address(self.settings.auction_kicker_address),
            data=tx_data,
            value="0x0",
            chain_id=self.settings.chain_id,
            sender=sender,
        )

    def _build_batch_kick_intent(self, prepared_kicks: list[PreparedKick], *, sender: str | None) -> TxIntent:
        if getattr(type(self.kicker), "build_batch_kick_intent", None) is not None:
            return self.kicker.build_batch_kick_intent(prepared_kicks, sender=sender)

        kicker_contract = self._kicker_contract()
        kick_tuples = [self.kicker._kick_args(prepared_kick) for prepared_kick in prepared_kicks]
        tx_data = kicker_contract.functions.batchKick(kick_tuples)._encode_transaction_data()
        return TxIntent(
            operation="kick",
            to=normalize_address(self.settings.auction_kicker_address),
            data=tx_data,
            value="0x0",
            chain_id=self.settings.chain_id,
            sender=sender,
        )

    def _build_sweep_and_settle_intent(
        self,
        prepared_operation: PreparedSweepAndSettle,
        *,
        sender: str | None,
    ) -> TxIntent:
        if getattr(type(self.kicker), "build_sweep_and_settle_intent", None) is not None:
            return self.kicker.build_sweep_and_settle_intent(prepared_operation, sender=sender)
        raise RuntimeError("kicker does not support sweep-and-settle planning")

    async def _estimate_intent(self, intent: TxIntent, *, gas_cap: int) -> str | None:
        if self.estimate_transaction_fn is not None:
            gas_estimate, gas_limit, gas_warning = await self.estimate_transaction_fn(
                self.kicker.web3_client,
                self.settings,
                sender=intent.sender,
                to_address=intent.to,
                data=intent.data,
                gas_cap=gas_cap,
            )
            intent.gas_estimate = gas_estimate
            intent.gas_limit = gas_limit
            return gas_warning

        if intent.sender is None:
            return "No sender provided for gas estimation."
        try:
            gas_estimate = await self.kicker.web3_client.estimate_gas(
                {
                    "from": to_checksum_address(intent.sender),
                    "to": to_checksum_address(intent.to),
                    "data": intent.data,
                    "chainId": intent.chain_id,
                }
            )
        except Exception as exc:  # noqa: BLE001
            return f"Gas estimate failed: {_format_execution_error(exc)}"
        intent.gas_estimate = gas_estimate
        intent.gas_limit = min(int(gas_estimate * _GAS_ESTIMATE_BUFFER), gas_cap)
        return None
