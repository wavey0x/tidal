"""Transaction service orchestration."""

from __future__ import annotations

import asyncio
import fcntl
import uuid
from collections import Counter
from pathlib import Path

import structlog

from factory_dashboard.normalizers import normalize_address
from factory_dashboard.persistence.repositories import KickTxRepository, TxnRunRepository
from factory_dashboard.time import utcnow_iso
from factory_dashboard.transaction_service.evaluator import check_pre_send, shortlist_candidates, sort_candidates
from factory_dashboard.transaction_service.kicker import AuctionKicker
from factory_dashboard.transaction_service.types import (
    KickAction,
    KickCandidate,
    KickResult,
    KickStatus,
    PreparedKick,
    PreparedSweepAndSettle,
    SourceType,
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
        kicker: AuctionKicker,
        txn_run_repository: TxnRunRepository,
        kick_tx_repository: KickTxRepository,
        usd_threshold: float,
        max_data_age_seconds: int,
        cooldown_seconds: int,
        lock_path: Path,
        max_batch_kick_size: int = 5,
        batch_kick_delay_seconds: float = 5,
    ):
        self.session = session
        self.kicker = kicker
        self.txn_run_repository = txn_run_repository
        self.kick_tx_repository = kick_tx_repository
        self.usd_threshold = usd_threshold
        self.max_data_age_seconds = max_data_age_seconds
        self.cooldown_seconds = cooldown_seconds
        self.lock_path = lock_path
        self.max_batch_kick_size = max_batch_kick_size
        self.batch_kick_delay_seconds = batch_kick_delay_seconds

    async def run_once(self, *, live: bool, batch: bool = True, source_type: SourceType | None = None) -> TxnRunResult:
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
            return await self._run(run_id=run_id, started_at=started_at, live=live, batch=batch, source_type=source_type)
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

        # 2. Shortlist candidates from SQLite.
        candidates = shortlist_candidates(
            self.session,
            usd_threshold=self.usd_threshold,
            max_data_age_seconds=self.max_data_age_seconds,
            source_type=source_type,
        )

        logger.info(
            "txn_run_started",
            run_id=run_id,
            live=live,
            source_type=source_type,
            candidates_shortlisted=len(candidates),
        )

        # 3. Pre-send checks (cooldown, circuit breaker).
        decisions = check_pre_send(
            candidates,
            kick_tx_repository=self.kick_tx_repository,
            cooldown_seconds=self.cooldown_seconds,
        )

        kicks_attempted = 0
        kicks_succeeded = 0
        kicks_failed = 0

        # Phase 1: Prepare all candidates.
        prepared: list[PreparedKick] = []
        prepared_sweep_and_settle: list[PreparedSweepAndSettle] = []
        failed_messages: list[str] = []
        candidates_to_prepare = sort_candidates(
            [decision.candidate for decision in decisions if decision.action == KickAction.KICK]
        )

        if candidates_to_prepare:
            logger.info(
                "txn_candidates_ranked",
                run_id=run_id,
                source_type=source_type,
                candidates=_candidate_order_log(candidates_to_prepare),
            )

        for decision in decisions:
            if decision.action == KickAction.SKIP:
                logger.debug(
                    "txn_candidate_skip",
                    run_id=run_id,
                    source=decision.candidate.source_address,
                    token=decision.candidate.token_address,
                    reason=decision.skip_reason,
                )
                continue

            if not live:
                # Dry-run: persist DRY_RUN row.
                row = {
                    "run_id": run_id,
                    "source_type": decision.candidate.source_type,
                    "source_address": decision.candidate.source_address,
                    "token_address": decision.candidate.token_address,
                    "auction_address": decision.candidate.auction_address,
                    "price_usd": decision.candidate.price_usd,
                    "usd_value": str(decision.candidate.usd_value),
                    "status": "DRY_RUN",
                    "created_at": utcnow_iso(),
                    "want_address": decision.candidate.want_address,
                    "want_symbol": decision.candidate.want_symbol,
                    "token_symbol": decision.candidate.token_symbol,
                }
                if decision.candidate.source_type == "strategy":
                    row["strategy_address"] = decision.candidate.source_address
                self.kick_tx_repository.insert(row)
                logger.info(
                    "txn_kick_dry_run",
                    run_id=run_id,
                    source=decision.candidate.source_address,
                    token=decision.candidate.token_address,
                    usd_value=decision.candidate.usd_value,
                )
                kicks_attempted += 1
        
        if live and candidates_to_prepare:
            kicks_attempted += len(candidates_to_prepare)

        if live and not batch and candidates_to_prepare:
            # Non-batch: prepare and execute one candidate at a time (highest USD first).
            for candidate in candidates_to_prepare:
                inspection = (await self.kicker.inspect_candidates([candidate])).get(_candidate_key(candidate))
                result = await self.kicker.prepare_kick(candidate, run_id, inspection=inspection)
                if isinstance(result, KickResult):
                    if result.status == KickStatus.SKIP:
                        kicks_attempted -= 1
                    elif result.status in (KickStatus.REVERTED, KickStatus.ERROR, KickStatus.ESTIMATE_FAILED):
                        kicks_failed += 1
                        if result.error_message:
                            failed_messages.append(result.error_message)
                    continue
                if isinstance(result, PreparedSweepAndSettle):
                    exec_result = await self.kicker.execute_sweep_and_settle(result, run_id)
                else:
                    exec_result = await self.kicker.execute_single(result, run_id)
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
            prepare_results: list[PreparedKick | PreparedSweepAndSettle | KickResult] = []
            for batch_start in range(0, len(candidates_to_prepare), self.max_batch_kick_size):
                if batch_start > 0:
                    await asyncio.sleep(self.batch_kick_delay_seconds)
                group = candidates_to_prepare[batch_start:batch_start + self.max_batch_kick_size]
                inspections = await self.kicker.inspect_candidates(group)
                batch_results = await asyncio.gather(
                    *(self.kicker.prepare_kick(c, run_id, inspection=inspections.get(_candidate_key(c))) for c in group)
                )
                prepare_results.extend(batch_results)
            for result in prepare_results:
                if isinstance(result, KickResult):
                    if result.status == KickStatus.SKIP:
                        kicks_attempted -= 1
                    elif result.status in (KickStatus.REVERTED, KickStatus.ERROR, KickStatus.ESTIMATE_FAILED):
                        kicks_failed += 1
                        if result.error_message:
                            failed_messages.append(result.error_message)
                elif isinstance(result, PreparedSweepAndSettle):
                    prepared_sweep_and_settle.append(result)
                else:
                    prepared.append(result)

            for prepared_operation in prepared_sweep_and_settle:
                exec_result = await self.kicker.execute_sweep_and_settle(prepared_operation, run_id)
                if exec_result.status == KickStatus.CONFIRMED:
                    kicks_succeeded += 1
                elif exec_result.status in (KickStatus.REVERTED, KickStatus.ERROR, KickStatus.ESTIMATE_FAILED):
                    kicks_failed += 1
                    if exec_result.error_message:
                        failed_messages.append(exec_result.error_message)
                elif exec_result.status == KickStatus.USER_SKIPPED:
                    kicks_attempted -= 1

            if prepared:
                exec_results = await self.kicker.execute_batch(prepared, run_id)
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
        candidates_found = len([d for d in decisions if d.action == KickAction.KICK])
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
