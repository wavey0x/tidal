"""Transaction service orchestration."""

from __future__ import annotations

import fcntl
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import structlog

from tidal.persistence.repositories import KickTxRepository, TxnRunRepository
from tidal.time import utcnow_iso
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
    """Orchestrates planner → execute → persist."""

    def __init__(
        self,
        *,
        executor,
        planner,
        txn_run_repository: TxnRunRepository,
        kick_tx_repository: KickTxRepository,
        lock_path: Path,
        execution_report_fn: Callable[[TransactionExecutionReport], None] | None = None,
    ):
        if executor is None:
            raise ValueError("TxnService requires an executor.")
        if planner is None:
            raise ValueError("TxnService requires a planner.")
        self.executor = executor
        self.planner = planner
        self.txn_run_repository = txn_run_repository
        self.kick_tx_repository = kick_tx_repository
        self.lock_path = lock_path
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

    def _tally_exec_result(
        self, exec_result: KickResult, failed_messages: list[str]
    ) -> tuple[int, int, int]:
        """Returns (succeeded_delta, failed_delta, attempted_delta)."""
        self._emit_execution_report(exec_result)
        if exec_result.status == KickStatus.CONFIRMED:
            return 1, 0, 0
        if exec_result.status in (KickStatus.REVERTED, KickStatus.ERROR, KickStatus.ESTIMATE_FAILED):
            if exec_result.error_message:
                failed_messages.append(exec_result.error_message)
            return 0, 1, 0
        if exec_result.status == KickStatus.USER_SKIPPED:
            return 0, 0, -1
        return 0, 0, 0

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
                s, f, a = self._tally_exec_result(exec_result, failed_messages)
                kicks_succeeded += s
                kicks_failed += f
                kicks_attempted += a

            kick_intent_count = sum(1 for intent in plan.tx_intents if intent.operation == "kick")
            if plan.kick_operations:
                if not batch or len(plan.kick_operations) == 1 or kick_intent_count != 1:
                    for prepared_kick in plan.kick_operations:
                        exec_result = await executor.execute_single(prepared_kick, run_id)
                        s, f, a = self._tally_exec_result(exec_result, failed_messages)
                        kicks_succeeded += s
                        kicks_failed += f
                        kicks_attempted += a
                else:
                    exec_results = await executor.execute_batch(plan.kick_operations, run_id)
                    for exec_result in exec_results:
                        s, f, a = self._tally_exec_result(exec_result, failed_messages)
                        kicks_succeeded += s
                        kicks_failed += f
                        kicks_attempted += a
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
