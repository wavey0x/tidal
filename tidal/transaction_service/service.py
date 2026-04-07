"""Transaction service orchestration."""

from __future__ import annotations

import asyncio
import fcntl
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import structlog

from tidal.normalizers import normalize_address
from tidal.persistence.repositories import KickTxRepository, TxnRunRepository
from tidal.time import utcnow_iso
from tidal.transaction_service.evaluator import build_shortlist, sort_candidates
from tidal.transaction_service.kick_policy import CooldownPolicy, IgnorePolicy
from tidal.transaction_service.types import (
    KickCandidate,
    KickResult,
    KickStatus,
    PreparedKick,
    PreparedResolveAuction,
    SourceType,
    TransactionExecutionReport,
    TxnRunResult,
)

logger = structlog.get_logger(__name__)


def _candidate_key(candidate: KickCandidate) -> tuple[str, str]:
    try:
        return normalize_address(candidate.auction_address), normalize_address(candidate.token_address)
    except Exception:  # noqa: BLE001
        return candidate.auction_address, candidate.token_address


def _candidate_order_log(candidates: list[KickCandidate]) -> list[dict[str, object]]:
    return [
        {
            "rank": index + 1,
            "source": candidate.source_address,
            "source_type": candidate.source_type,
            "auction": candidate.auction_address,
            "token": candidate.token_address,
            "usd_value": candidate.usd_value,
        }
        for index, candidate in enumerate(candidates)
    ]


class TxnService:
    """Orchestrates evaluate → kick → persist."""

    def __init__(
        self,
        *,
        session,
        preparer,
        executor,
        planner=None,
        txn_run_repository: TxnRunRepository,
        kick_tx_repository: KickTxRepository,
        usd_threshold: float,
        max_data_age_seconds: int,
        cooldown_policy: CooldownPolicy,
        ignore_policy: IgnorePolicy,
        lock_path: Path,
        max_batch_kick_size: int = 5,
        batch_kick_delay_seconds: float = 5,
        execution_report_fn: Callable[[TransactionExecutionReport], None] | None = None,
    ):
        if preparer is None:
            raise ValueError("TxnService requires a preparer.")
        if executor is None:
            raise ValueError("TxnService requires an executor.")
        self.session = session
        self.preparer = preparer
        self.executor = executor
        self.planner = planner
        self.txn_run_repository = txn_run_repository
        self.kick_tx_repository = kick_tx_repository
        self.usd_threshold = usd_threshold
        self.max_data_age_seconds = max_data_age_seconds
        self.cooldown_policy = cooldown_policy
        self.ignore_policy = ignore_policy
        self.lock_path = lock_path
        self.max_batch_kick_size = max_batch_kick_size
        self.batch_kick_delay_seconds = batch_kick_delay_seconds
        self.execution_report_fn = execution_report_fn

    def _persist_prepare_failure(self, run_id: str, candidate: KickCandidate, result: KickResult) -> KickResult:
        if result.status == KickStatus.SKIP:
            return result
        return self.executor.record_prepare_failure(
            run_id=run_id,
            candidate=candidate,
            result=result,
        )

    def _apply_prepare_result(
        self,
        *,
        run_id: str,
        candidate: KickCandidate,
        result: KickResult,
        failed_messages: list[str],
    ) -> tuple[int, int]:
        result = self._persist_prepare_failure(run_id, candidate, result)
        if result.status == KickStatus.SKIP:
            return -1, 0
        if result.status in (KickStatus.REVERTED, KickStatus.ERROR, KickStatus.ESTIMATE_FAILED):
            if result.error_message:
                failed_messages.append(result.error_message)
            return 0, 1
        return 0, 0

    def _emit_execution_report(self, result: KickResult) -> None:
        report = result.execution_report
        if report is None or self.execution_report_fn is None:
            return
        try:
            self.execution_report_fn(report)
        except Exception as exc:  # noqa: BLE001
            logger.warning("txn_execution_report_failed", error=str(exc), tx_hash=report.tx_hash)

    def _planner_sender(self) -> str | None:
        signer = getattr(self.executor, "signer", None)
        if signer is None:
            return None
        checksum_address = getattr(signer, "checksum_address", None)
        if isinstance(checksum_address, str):
            return checksum_address
        address = getattr(signer, "address", None)
        if isinstance(address, str):
            return address
        return None

    def _insert_dry_run_kick_row(self, run_id: str, prepared_kick: PreparedKick, *, now_iso: str) -> None:
        candidate = prepared_kick.candidate
        row: dict[str, object] = {
            "run_id": run_id,
            "operation_type": "kick",
            "source_type": candidate.source_type,
            "source_address": candidate.source_address,
            "token_address": candidate.token_address,
            "auction_address": candidate.auction_address,
            "price_usd": candidate.price_usd,
            "usd_value": prepared_kick.usd_value_str,
            "status": "DRY_RUN",
            "created_at": now_iso,
            "want_address": candidate.want_address,
            "want_symbol": candidate.want_symbol,
            "token_symbol": candidate.token_symbol,
            "sell_amount": prepared_kick.sell_amount_str,
            "starting_price": prepared_kick.starting_price_str,
            "minimum_price": prepared_kick.minimum_price_str,
            "minimum_quote": prepared_kick.minimum_quote_str,
            "quote_amount": prepared_kick.quote_amount_str,
            "quote_response_json": prepared_kick.quote_response_json,
            "start_price_buffer_bps": prepared_kick.start_price_buffer_bps,
            "min_price_buffer_bps": prepared_kick.min_price_buffer_bps,
            "step_decay_rate_bps": prepared_kick.step_decay_rate_bps,
            "normalized_balance": prepared_kick.normalized_balance,
        }
        if candidate.source_type == "strategy":
            row["strategy_address"] = candidate.source_address
        self.kick_tx_repository.insert(row)

    def _insert_dry_run_resolve_row(self, run_id: str, prepared_operation: PreparedResolveAuction, *, now_iso: str) -> None:
        candidate = prepared_operation.candidate
        row: dict[str, object] = {
            "run_id": run_id,
            "operation_type": "resolve_auction",
            "source_type": candidate.source_type,
            "source_address": candidate.source_address,
            "token_address": prepared_operation.sell_token,
            "auction_address": candidate.auction_address,
            "status": "DRY_RUN",
            "created_at": now_iso,
            "want_address": candidate.want_address,
            "want_symbol": candidate.want_symbol,
            "token_symbol": prepared_operation.token_symbol if prepared_operation.token_symbol is not None else candidate.token_symbol,
            "stuck_abort_reason": prepared_operation.reason,
            "normalized_balance": prepared_operation.normalized_balance,
        }
        if prepared_operation.balance_raw:
            row["sell_amount"] = str(prepared_operation.balance_raw)
        if candidate.source_type == "strategy":
            row["strategy_address"] = candidate.source_address
        self.kick_tx_repository.insert(row)

    async def run_once(
        self,
        *,
        live: bool,
        batch: bool = True,
        source_type: SourceType | None = None,
        source_address: str | None = None,
        auction_address: str | None = None,
        limit: int | None = None,
    ) -> TxnRunResult:
        run_id = str(uuid.uuid4())
        started_at = utcnow_iso()

        lock_file = None
        if live:
            lock_file = self._acquire_lock()
            if lock_file is None:
                logger.warning("txn_lock_held", run_id=run_id)
                return TxnRunResult(
                    run_id=run_id,
                    status="FAILED",
                    candidates_found=0,
                    kicks_attempted=0,
                    kicks_succeeded=0,
                    kicks_failed=0,
                )

        try:
            return await self._run(
                run_id=run_id,
                started_at=started_at,
                live=live,
                batch=batch,
                source_type=source_type,
                source_address=source_address,
                auction_address=auction_address,
                limit=limit,
            )
        finally:
            if lock_file is not None:
                self._release_lock(lock_file)

    async def _run(
        self,
        *,
        run_id: str,
        started_at: str,
        live: bool,
        batch: bool = True,
        source_type: SourceType | None = None,
        source_address: str | None = None,
        auction_address: str | None = None,
        limit: int | None = None,
    ) -> TxnRunResult:
        # 1. INSERT txn_runs with status=RUNNING.
        self.txn_run_repository.create({
            "run_id": run_id,
            "started_at": started_at,
            "status": "RUNNING",
            "candidates_found": 0,
            "kicks_attempted": 0,
            "kicks_succeeded": 0,
            "kicks_failed": 0,
            "live": 1 if live else 0,
        })

        if self.planner is not None:
            executor = self.executor
            plan = await self.planner.plan(
                source_type=source_type,
                source_address=source_address,
                auction_address=auction_address,
                token_address=None,
                limit=limit,
                sender=self._planner_sender() if live else None,
                run_id=run_id,
                batch=batch,
                estimate_transactions=live,
            )

            logger.info(
                "txn_run_started",
                run_id=run_id,
                live=live,
                source_type=source_type,
                source_address=source_address,
                auction_address=auction_address,
                candidates_shortlisted=len(plan.ranked_candidates),
                candidates_eligible=plan.eligible_count,
                ignored_count=len(plan.ignored_skips),
                cooldown_count=len(plan.cooldown_skips),
                deferred_same_auction_count=plan.deferred_same_auction_count,
                limited_candidates_count=plan.limited_count,
            )

            if plan.ranked_candidates:
                logger.info(
                    "txn_candidates_ranked",
                    run_id=run_id,
                    source_type=source_type,
                    source_address=source_address,
                    auction_address=auction_address,
                    candidates=_candidate_order_log(plan.ranked_candidates),
                )

            kicks_attempted = len(plan.resolve_operations) + len(plan.kick_operations)
            kicks_succeeded = 0
            kicks_failed = 0
            failed_messages: list[str] = []
            for skipped in plan.skipped_during_prepare:
                if skipped.result is None:
                    continue
                if skipped.result.status == KickStatus.SKIP:
                    continue
                attempt_delta, failure_delta = self._apply_prepare_result(
                    run_id=run_id,
                    candidate=skipped.candidate,
                    result=skipped.result,
                    failed_messages=failed_messages,
                )
                kicks_attempted += attempt_delta
                kicks_failed += failure_delta

            if live:
                for prepared_operation in plan.resolve_operations:
                    exec_result = await executor.execute_resolve_auction(prepared_operation, run_id)
                    self._emit_execution_report(exec_result)
                    if exec_result.status == KickStatus.CONFIRMED:
                        kicks_succeeded += 1
                    elif exec_result.status in (KickStatus.REVERTED, KickStatus.ERROR, KickStatus.ESTIMATE_FAILED):
                        kicks_failed += 1
                        if exec_result.error_message:
                            failed_messages.append(exec_result.error_message)
                    elif exec_result.status == KickStatus.USER_SKIPPED:
                        kicks_attempted -= 1

                kick_intents = [intent for intent in plan.tx_intents if intent.operation == "kick"]
                if plan.kick_operations:
                    if not batch or len(plan.kick_operations) == 1 or len(kick_intents) != 1:
                        for prepared_kick in plan.kick_operations:
                            exec_result = await executor.execute_single(prepared_kick, run_id)
                            self._emit_execution_report(exec_result)
                            if exec_result.status == KickStatus.CONFIRMED:
                                kicks_succeeded += 1
                            elif exec_result.status in (KickStatus.REVERTED, KickStatus.ERROR, KickStatus.ESTIMATE_FAILED):
                                kicks_failed += 1
                                if exec_result.error_message:
                                    failed_messages.append(exec_result.error_message)
                            elif exec_result.status == KickStatus.USER_SKIPPED:
                                kicks_attempted -= 1
                    else:
                        exec_results = await executor.execute_batch(plan.kick_operations, run_id)
                        for exec_result in exec_results:
                            self._emit_execution_report(exec_result)
                            if exec_result.status == KickStatus.CONFIRMED:
                                kicks_succeeded += 1
                            elif exec_result.status in (KickStatus.REVERTED, KickStatus.ERROR, KickStatus.ESTIMATE_FAILED):
                                kicks_failed += 1
                                if exec_result.error_message:
                                    failed_messages.append(exec_result.error_message)
                            elif exec_result.status == KickStatus.USER_SKIPPED:
                                kicks_attempted -= 1
            else:
                now_iso = utcnow_iso()
                for prepared_operation in plan.resolve_operations:
                    self._insert_dry_run_resolve_row(run_id, prepared_operation, now_iso=now_iso)
                for prepared_kick in plan.kick_operations:
                    self._insert_dry_run_kick_row(run_id, prepared_kick, now_iso=now_iso)

            candidates_found = len(plan.ranked_candidates)
            if not live:
                status = "DRY_RUN"
            elif kicks_failed > 0 and kicks_succeeded == 0:
                status = "FAILED"
            elif kicks_failed > 0:
                status = "PARTIAL_SUCCESS"
            else:
                status = "SUCCESS"

            finished_at = utcnow_iso()
            self.txn_run_repository.finalize(
                run_id,
                finished_at=finished_at,
                status=status,
                candidates_found=candidates_found,
                kicks_attempted=kicks_attempted,
                kicks_succeeded=kicks_succeeded,
                kicks_failed=kicks_failed,
                error_summary=f"{kicks_failed} failures" if kicks_failed else None,
            )

            logger.info(
                "txn_run_completed",
                run_id=run_id,
                status=status,
                candidates_found=candidates_found,
                attempted=kicks_attempted,
                succeeded=kicks_succeeded,
                failed=kicks_failed,
            )

            failure_summary = None
            if failed_messages:
                failure_summary = dict(Counter(failed_messages))

            return TxnRunResult(
                run_id=run_id,
                status=status,
                candidates_found=candidates_found,
                kicks_attempted=kicks_attempted,
                kicks_succeeded=kicks_succeeded,
                kicks_failed=kicks_failed,
                eligible_candidates_found=plan.eligible_count,
                deferred_same_auction_count=plan.deferred_same_auction_count,
                limited_candidate_count=plan.limited_count,
                failure_summary=failure_summary,
            )

        # 2. Shortlist candidates from SQLite.
        shortlist = build_shortlist(
            self.session,
            usd_threshold=self.usd_threshold,
            max_data_age_seconds=self.max_data_age_seconds,
            source_type=source_type,
            source_address=source_address,
            auction_address=auction_address,
            limit=limit,
            ignore_policy=self.ignore_policy,
            cooldown_policy=self.cooldown_policy,
            kick_tx_repository=self.kick_tx_repository,
        )
        candidates_to_prepare = sort_candidates(shortlist.selected_candidates)

        logger.info(
            "txn_run_started",
            run_id=run_id,
            live=live,
            source_type=source_type,
            source_address=source_address,
            auction_address=auction_address,
            candidates_shortlisted=len(candidates_to_prepare),
            candidates_eligible=len(shortlist.eligible_candidates),
            ignored_count=len(shortlist.ignored_skips),
            cooldown_count=len(shortlist.cooldown_skips),
            deferred_same_auction_count=shortlist.deferred_same_auction_count,
            limited_candidates_count=len(shortlist.limited_candidates),
        )

        kicks_attempted = 0
        kicks_succeeded = 0
        kicks_failed = 0

        # Phase 1: Prepare all candidates.
        preparer = self.preparer
        executor = self.executor
        prepared: list[PreparedKick] = []
        failed_messages: list[str] = []

        if candidates_to_prepare:
            logger.info(
                "txn_candidates_ranked",
                run_id=run_id,
                source_type=source_type,
                source_address=source_address,
                auction_address=auction_address,
                candidates=_candidate_order_log(candidates_to_prepare),
            )

        for candidate in candidates_to_prepare:
            if not live:
                # Dry-run: persist DRY_RUN row.
                row = {
                    "run_id": run_id,
                    "source_type": candidate.source_type,
                    "source_address": candidate.source_address,
                    "token_address": candidate.token_address,
                    "auction_address": candidate.auction_address,
                    "price_usd": candidate.price_usd,
                    "usd_value": str(candidate.usd_value),
                    "status": "DRY_RUN",
                    "created_at": utcnow_iso(),
                    "want_address": candidate.want_address,
                    "want_symbol": candidate.want_symbol,
                    "token_symbol": candidate.token_symbol,
                }
                if candidate.source_type == "strategy":
                    row["strategy_address"] = candidate.source_address
                self.kick_tx_repository.insert(row)
                logger.info(
                    "txn_kick_dry_run",
                    run_id=run_id,
                    source=candidate.source_address,
                    token=candidate.token_address,
                    usd_value=candidate.usd_value,
                )
                kicks_attempted += 1
        
        if live and candidates_to_prepare:
            kicks_attempted += len(candidates_to_prepare)

        if live and not batch and candidates_to_prepare:
            # Non-batch: prepare and execute one candidate at a time (highest USD first).
            for candidate in candidates_to_prepare:
                inspection = (await preparer.inspect_candidates([candidate])).get(_candidate_key(candidate))
                result = await preparer.prepare_kick(candidate, run_id, inspection=inspection)
                if isinstance(result, KickResult):
                    attempt_delta, failure_delta = self._apply_prepare_result(
                        run_id=run_id,
                        candidate=candidate,
                        result=result,
                        failed_messages=failed_messages,
                    )
                    kicks_attempted += attempt_delta
                    kicks_failed += failure_delta
                    continue
                exec_result = await executor.execute_single(result, run_id)
                self._emit_execution_report(exec_result)
                if exec_result.status == KickStatus.CONFIRMED:
                    kicks_succeeded += 1
                elif exec_result.status in (KickStatus.REVERTED, KickStatus.ERROR, KickStatus.ESTIMATE_FAILED):
                    kicks_failed += 1
                    if exec_result.error_message:
                        failed_messages.append(exec_result.error_message)
                elif exec_result.status == KickStatus.USER_SKIPPED:
                    kicks_attempted -= 1
        elif live and candidates_to_prepare:
            # Batch: prepare all in groups, then execute as one batch transaction.
            prepare_results: list[tuple[KickCandidate, PreparedKick | KickResult]] = []
            for batch_start in range(0, len(candidates_to_prepare), self.max_batch_kick_size):
                if batch_start > 0:
                    await asyncio.sleep(self.batch_kick_delay_seconds)
                group = candidates_to_prepare[batch_start:batch_start + self.max_batch_kick_size]
                inspections = await preparer.inspect_candidates(group)
                batch_results = await asyncio.gather(
                    *(preparer.prepare_kick(c, run_id, inspection=inspections.get(_candidate_key(c))) for c in group)
                )
                prepare_results.extend(zip(group, batch_results, strict=True))
            for candidate, result in prepare_results:
                if isinstance(result, KickResult):
                    attempt_delta, failure_delta = self._apply_prepare_result(
                        run_id=run_id,
                        candidate=candidate,
                        result=result,
                        failed_messages=failed_messages,
                    )
                    kicks_attempted += attempt_delta
                    kicks_failed += failure_delta
                else:
                    prepared.append(result)

            if prepared:
                exec_results = await executor.execute_batch(prepared, run_id)
                for result in exec_results:
                    if result.status == KickStatus.CONFIRMED:
                        kicks_succeeded += 1
                    elif result.status in (KickStatus.REVERTED, KickStatus.ERROR, KickStatus.ESTIMATE_FAILED):
                        kicks_failed += 1
                        if result.error_message:
                            failed_messages.append(result.error_message)
                    elif result.status == KickStatus.USER_SKIPPED:
                        kicks_attempted -= 1

        # 4. Finalize txn_runs.
        candidates_found = len(candidates_to_prepare)
        if not live:
            status = "DRY_RUN"
        elif kicks_failed > 0 and kicks_succeeded == 0:
            status = "FAILED"
        elif kicks_failed > 0:
            status = "PARTIAL_SUCCESS"
        else:
            status = "SUCCESS"

        finished_at = utcnow_iso()
        self.txn_run_repository.finalize(
            run_id,
            finished_at=finished_at,
            status=status,
            candidates_found=candidates_found,
            kicks_attempted=kicks_attempted,
            kicks_succeeded=kicks_succeeded,
            kicks_failed=kicks_failed,
            error_summary=f"{kicks_failed} failures" if kicks_failed else None,
        )

        logger.info(
            "txn_run_completed",
            run_id=run_id,
            status=status,
            candidates_found=candidates_found,
            attempted=kicks_attempted,
            succeeded=kicks_succeeded,
            failed=kicks_failed,
        )

        failure_summary = None
        if failed_messages:
            failure_summary = dict(Counter(failed_messages))

        return TxnRunResult(
            run_id=run_id,
            status=status,
            candidates_found=candidates_found,
            kicks_attempted=kicks_attempted,
            kicks_succeeded=kicks_succeeded,
            kicks_failed=kicks_failed,
            eligible_candidates_found=len(shortlist.eligible_candidates),
            deferred_same_auction_count=shortlist.deferred_same_auction_count,
            limited_candidate_count=len(shortlist.limited_candidates),
            failure_summary=failure_summary,
        )

    def _acquire_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(self.lock_path, "w")  # noqa: SIM115
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_file
        except OSError:
            lock_file.close()
            return None

    def _release_lock(self, lock_file):
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()
