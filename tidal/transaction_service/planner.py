"""Kick planning helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from eth_utils import to_checksum_address

from tidal.auction_settlement import (
    PATH_SWEEP_AND_RESET,
    default_actionable_previews,
    inspect_auction_settlements,
    live_funded_previews,
    path_reason,
)
from tidal.config import Settings
from tidal.normalizers import to_decimal_string
from tidal.persistence.repositories import KickTxRepository, TokenRepository
from tidal.transaction_service.evaluator import build_shortlist, sort_candidates
from tidal.transaction_service.kick_shared import (
    _GAS_ESTIMATE_BUFFER,
    _candidate_key,
    _format_execution_error,
    _is_active_auction_error,
)
from tidal.transaction_service.types import (
    KickCandidate,
    KickPlan,
    KickResult,
    KickStatus,
    PreparedKick,
    PreparedResolveAuction,
    SkippedPreparedCandidate,
    SourceType,
    TxIntent,
)


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


def _prepared_estimate_failure(prepared: PreparedKick, reason: str) -> SkippedPreparedCandidate:
    return SkippedPreparedCandidate(
        candidate=prepared.candidate,
        reason=reason,
        result=KickResult(
            kick_tx_id=0,
            status=KickStatus.ESTIMATE_FAILED,
            error_message=reason,
            sell_amount=prepared.sell_amount_str,
            starting_price=prepared.starting_price_str,
            minimum_price=prepared.minimum_price_str,
            minimum_quote=prepared.minimum_quote_str,
            live_balance_raw=prepared.live_balance_raw,
            usd_value=prepared.usd_value_str,
            quote_response_json=prepared.quote_response_json,
        ),
    )


def _operator_settle_command(auction_address: str, token_address: str | None = None, *, force: bool = False) -> str:
    command = f"tidal auction settle {to_checksum_address(auction_address)}"
    if token_address is not None:
        command += f" --token {to_checksum_address(token_address)}"
    if force:
        command += " --force"
    return command


def _operator_sweep_command(auction_address: str, token_address: str) -> str:
    return (
        f"tidal auction sweep {to_checksum_address(auction_address)} "
        f"--token {to_checksum_address(token_address)}"
    )


def _lookup_token_symbol(token_repo: TokenRepository, token_address: str, *, fallback: str | None = None) -> str | None:
    try:
        metadata = token_repo.get(token_address)
    except Exception:  # noqa: BLE001
        metadata = None
    if metadata is not None and metadata.symbol:
        return metadata.symbol
    return fallback


def _needs_manual_sweep(preview, gas_warning: str | None) -> bool:  # noqa: ANN001
    return bool(preview.path == PATH_SWEEP_AND_RESET and gas_warning and "Amount is zero." in gas_warning)


class KickPlanner:
    """Build a single typed kick plan for API prepare or live execution."""

    def __init__(
        self,
        *,
        session,
        settings: Settings,
        preparer,
        tx_builder,
        kick_tx_repository: KickTxRepository,
        web3_client=None,
        shortlist_builder: Callable[..., object] | None = None,
        candidate_sorter: Callable[[list[KickCandidate]], list[KickCandidate]] | None = None,
        estimate_transaction_fn: Callable[..., Awaitable[tuple[int | None, int | None, str | None]]] | None = None,
    ) -> None:
        if preparer is None:
            raise ValueError("KickPlanner requires a preparer.")
        if tx_builder is None:
            raise ValueError("KickPlanner requires a tx builder.")
        self.session = session
        self.settings = settings
        self.preparer = preparer
        self.tx_builder = tx_builder
        self.web3_client = web3_client or getattr(preparer, "web3_client", None) or getattr(tx_builder, "web3_client", None)
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
        estimate_transactions: bool = True,
    ) -> KickPlan:
        kick_config = self.settings.kick_config
        shortlist = self.shortlist_builder(
            self.session,
            usd_threshold=self.settings.txn_usd_threshold,
            max_data_age_seconds=self.settings.txn_data_freshness_limit_seconds,
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

        auction_candidates: dict[str, list[KickCandidate]] = {}
        for candidate in candidates_to_prepare:
            auction_candidates.setdefault(_candidate_key(candidate)[0], []).append(candidate)

        resolve_inspections = await inspect_auction_settlements(
            self.web3_client,
            self.settings,
            sorted(auction_candidates),
        )
        clean_candidates: list[KickCandidate] = []
        resolved_tokens: set[tuple[str, str]] = set()
        token_repo = TokenRepository(self.session)
        for auction_key, auction_group in auction_candidates.items():
            inspection = resolve_inspections[auction_key]
            anchor_candidate = auction_group[0]
            if inspection.preview_failures:
                reason = "auction resolution preview failed"
                for candidate in auction_group:
                    plan.skipped_during_prepare.append(
                        SkippedPreparedCandidate(candidate=candidate, reason=reason)
                    )
                continue

            actionable_previews = default_actionable_previews(inspection)
            if actionable_previews:
                manual_sweep_preview = None
                for preview in actionable_previews:
                    token_key = (auction_key, preview.token_address)
                    if token_key in resolved_tokens:
                        continue
                    resolved_tokens.add(token_key)
                    normalized_balance = None
                    fallback_symbol = anchor_candidate.token_symbol if preview.token_address == anchor_candidate.token_address else None
                    token_symbol = _lookup_token_symbol(token_repo, preview.token_address, fallback=fallback_symbol)
                    if preview.balance_raw is not None and preview.token_address == anchor_candidate.token_address:
                        normalized_balance = to_decimal_string(preview.balance_raw, anchor_candidate.decimals)
                    prepared_operation = PreparedResolveAuction(
                        candidate=anchor_candidate,
                        sell_token=preview.token_address,
                        path=int(preview.path or 0),
                        reason=path_reason(int(preview.path or 0)),
                        balance_raw=int(preview.balance_raw or 0),
                        requires_force=bool(preview.requires_force),
                        receiver=preview.receiver,
                        token_symbol=token_symbol,
                        normalized_balance=normalized_balance,
                    )
                    intent = self.tx_builder.build_resolve_auction_intent(prepared_operation, sender=sender)
                    if estimate_transactions:
                        gas_warning = await self._estimate_intent(intent, gas_cap=self.settings.txn_max_gas_limit)
                        if gas_warning:
                            plan.warnings.append(gas_warning)
                            if _needs_manual_sweep(preview, gas_warning):
                                manual_sweep_preview = preview
                                manual_sweep_token = token_symbol or to_checksum_address(preview.token_address)
                                hint = f"Resolve failed for {manual_sweep_token}. This token may require a manual sweep."
                                if hint not in plan.warnings:
                                    plan.warnings.append(hint)
                            continue
                    plan.resolve_operations.append(prepared_operation)
                    plan.tx_intents.append(intent)
                blocker_preview = manual_sweep_preview or actionable_previews[0]
                blocker_fallback = (
                    anchor_candidate.token_symbol if blocker_preview.token_address == anchor_candidate.token_address else None
                )
                blocker_symbol = _lookup_token_symbol(
                    token_repo,
                    blocker_preview.token_address,
                    fallback=blocker_fallback,
                )
                blocker_reason = path_reason(int(blocker_preview.path or 0))
                if manual_sweep_preview is not None:
                    next_step = _operator_sweep_command(auction_key, blocker_preview.token_address)
                    skip_reason = "auction requires manual sweep before kick"
                elif len(actionable_previews) == 1:
                    next_step = _operator_settle_command(auction_key, blocker_preview.token_address)
                    skip_reason = "auction requires settlement before kick"
                else:
                    next_step = _operator_settle_command(auction_key)
                    skip_reason = "auction requires settlement before kick"
                for candidate in auction_group:
                    plan.skipped_during_prepare.append(
                        SkippedPreparedCandidate(
                            candidate=candidate,
                            reason=skip_reason,
                            blocked_token_address=blocker_preview.token_address,
                            blocked_token_symbol=blocker_symbol,
                            blocked_reason=blocker_reason,
                            next_step=next_step,
                        )
                    )
                continue

            live_previews = live_funded_previews(inspection)
            if live_previews:
                live_preview = live_previews[0]
                live_fallback = anchor_candidate.token_symbol if live_preview.token_address == anchor_candidate.token_address else None
                live_symbol = _lookup_token_symbol(token_repo, live_preview.token_address, fallback=live_fallback)
                live_reason = path_reason(int(live_preview.path or 0))
                next_step = (
                    _operator_settle_command(auction_key, live_preview.token_address, force=True)
                    if len(live_previews) == 1
                    else None
                )
                for candidate in auction_group:
                    plan.skipped_during_prepare.append(
                        SkippedPreparedCandidate(
                            candidate=candidate,
                            reason="auction still active with live sell balance",
                            blocked_token_address=live_preview.token_address,
                            blocked_token_symbol=live_symbol,
                            blocked_reason=live_reason,
                            next_step=next_step,
                        )
                    )
                continue

            clean_candidates.extend(auction_group)

        inspections = await self.preparer.inspect_candidates(clean_candidates)
        prepared_kicks: list[PreparedKick] = []

        for candidate in clean_candidates:
            result = await self.preparer.prepare_kick(
                candidate,
                run_id=run_id,
                inspection=inspections.get(_candidate_key(candidate)),
            )
            if isinstance(result, KickResult):
                reason = result.error_message or "candidate was skipped during prepare"
                plan.skipped_during_prepare.append(
                    SkippedPreparedCandidate(candidate=candidate, reason=reason, result=result)
                )
                continue
            prepared_kicks.append(result)

        if not prepared_kicks:
            plan.ready_count = len(plan.resolve_operations)
            return plan

        if not batch:
            for prepared_kick in prepared_kicks:
                recovered_kick, tx_intent, warning = await self._prepare_single_kick_intent(
                    prepared_kick,
                    sender=sender,
                    estimate_transactions=estimate_transactions,
                )
                if warning is not None:
                    plan.warnings.append(warning)
                    plan.skipped_during_prepare.append(_prepared_estimate_failure(prepared_kick, warning))
                    continue
                assert recovered_kick is not None and tx_intent is not None
                plan.kick_operations.append(recovered_kick)
                plan.tx_intents.append(tx_intent)
            plan.ready_count = len(plan.resolve_operations) + len(plan.kick_operations)
            return plan

        if len(prepared_kicks) == 1:
            prepared_kick, tx_intent, warning = await self._prepare_single_kick_intent(
                prepared_kicks[0],
                sender=sender,
                estimate_transactions=estimate_transactions,
            )
            if warning is not None:
                plan.warnings.append(warning)
                plan.skipped_during_prepare.append(_prepared_estimate_failure(prepared_kicks[0], warning))
                plan.ready_count = len(plan.resolve_operations)
                return plan
            assert prepared_kick is not None and tx_intent is not None
            plan.kick_operations.append(prepared_kick)
            plan.tx_intents.append(tx_intent)
            plan.ready_count = len(plan.resolve_operations) + len(plan.kick_operations)
            return plan

        if not estimate_transactions:
            batch_intent = self._build_batch_kick_intent(prepared_kicks, sender=sender)
            plan.kick_operations.extend(prepared_kicks)
            plan.tx_intents.append(batch_intent)
            plan.ready_count = len(plan.resolve_operations) + len(plan.kick_operations)
            return plan

        batch_intent = self._build_batch_kick_intent(prepared_kicks, sender=sender)
        batch_warning = await self._estimate_intent(
            batch_intent,
            gas_cap=self.settings.txn_max_gas_limit * max(len(prepared_kicks), 1),
        )
        if batch_warning is None:
            plan.kick_operations.extend(prepared_kicks)
            plan.tx_intents.append(batch_intent)
            plan.ready_count = len(plan.resolve_operations) + len(plan.kick_operations)
            return plan

        if not _is_active_auction_error(batch_warning):
            plan.warnings.append(batch_warning)
            plan.skipped_during_prepare.extend(
                _prepared_estimate_failure(prepared, batch_warning)
                for prepared in prepared_kicks
            )
            plan.ready_count = len(plan.resolve_operations)
            return plan

        individual_warnings: list[str] = []
        successful_prepared: list[PreparedKick] = []
        successful_intents: list[TxIntent] = []
        individual_skips: list[SkippedPreparedCandidate] = []
        for prepared in prepared_kicks:
            recovered_kick, tx_intent, warning = await self._prepare_single_kick_intent(
                prepared,
                sender=sender,
                estimate_transactions=estimate_transactions,
            )
            if warning is not None:
                individual_warnings.append(warning)
                individual_skips.append(_prepared_estimate_failure(prepared, warning))
                continue
            assert recovered_kick is not None and tx_intent is not None
            successful_prepared.append(recovered_kick)
            successful_intents.append(tx_intent)

        if not successful_intents:
            plan.warnings.append(batch_warning)
            plan.skipped_during_prepare.extend(
                _prepared_estimate_failure(prepared, batch_warning)
                for prepared in prepared_kicks
            )
            plan.ready_count = len(plan.resolve_operations)
            return plan

        plan.kick_operations.extend(successful_prepared)
        plan.tx_intents.extend(successful_intents)
        plan.warnings.extend(individual_warnings)
        plan.skipped_during_prepare.extend(individual_skips)
        plan.ready_count = len(plan.resolve_operations) + len(plan.kick_operations)
        return plan

    async def _prepare_single_kick_intent(
        self,
        prepared: PreparedKick,
        *,
        sender: str | None,
        estimate_transactions: bool = True,
    ) -> tuple[PreparedKick | None, TxIntent | None, str | None]:
        standard_intent = self._build_single_kick_intent(prepared, sender=sender)
        if not estimate_transactions:
            return prepared, standard_intent, None
        gas_warning = await self._estimate_intent(standard_intent, gas_cap=self.settings.txn_max_gas_limit)
        if gas_warning is None:
            return prepared, standard_intent, None
        return None, None, gas_warning

    def _build_single_kick_intent(self, prepared: PreparedKick, *, sender: str | None) -> TxIntent:
        return self.tx_builder.build_single_kick_intent(prepared, sender=sender)

    def _build_batch_kick_intent(self, prepared_kicks: list[PreparedKick], *, sender: str | None) -> TxIntent:
        return self.tx_builder.build_batch_kick_intent(prepared_kicks, sender=sender)

    async def _estimate_intent(self, intent: TxIntent, *, gas_cap: int) -> str | None:
        if self.estimate_transaction_fn is not None:
            gas_estimate, gas_limit, gas_warning = await self.estimate_transaction_fn(
                self.web3_client,
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
        if self.web3_client is None:
            return "No web3 client provided for gas estimation."
        try:
            gas_estimate = await self.web3_client.estimate_gas(
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
