"""One-time strict-schema repair and audit for retained auction rounds."""

from __future__ import annotations

from dataclasses import dataclass

from eth_utils import to_checksum_address

from tidal.auction_rounds import (
    RoundOutcome,
    classify_all_pair_operations,
    classify_pair_operations,
)
from tidal.chain.contracts.abis import AUCTION_ABI
from tidal.normalizers import normalize_address
from tidal.lifecycle import LifecycleError, execution_lock
from tidal.transactions import TransactionRepository
from tidal.operation_reconciler import OperationReconciler, ReconciliationError
from tidal.persistence.repositories import KickTxRepository
from tidal.time import utcnow_iso


@dataclass(frozen=True, slots=True)
class RepairPairAudit:
    auction_address: str
    token_address: str
    outcome: str
    reason_code: str | None
    live_on_chain: bool | None
    baseline_kick_ids: tuple[int, ...]
    proposed_baseline_kick_ids: tuple[int, ...]
    passed: bool


@dataclass(frozen=True, slots=True)
class RepairReport:
    pairs: tuple[RepairPairAudit, ...]
    reconciliation_errors: tuple[ReconciliationError, ...]
    mutations: int

    @property
    def passed(self) -> bool:
        return not self.reconciliation_errors and all(
            pair.passed for pair in self.pairs
        )


class AuctionRoundRepair:
    def __init__(self, *, session, settings, web3_client) -> None:  # noqa: ANN001
        self.session = session
        self.settings = settings
        self.web3_client = web3_client
        self.repo = KickTxRepository(session)
        self.reconciler = OperationReconciler(
            session=session,
            web3_client=web3_client,
            auction_kicker_address=settings.auction_kicker_address,
            chain_id=settings.chain_id,
            settings=settings,
        )

    async def run(self, *, auction: str, token: str, apply: bool) -> RepairReport:
        """Inspect or repair exactly one selected pair, never the entire DB."""
        pairs = {(normalize_address(auction), normalize_address(token))}
        with execution_lock(self.settings.resolved_home_path / "execution.lock"):
            if apply:
                assert_history_repair_unblocked(self.session)
                from tidal.recovery import chain_readiness
                await chain_readiness(self.settings, self.web3_client)
            return await self._run(pairs=pairs, apply=apply)

    async def _run(self, *, pairs: set[tuple[str, str]], apply: bool) -> RepairReport:
        before = self._snapshot()
        errors: list[ReconciliationError] = []
        if apply:
            tx_hashes = {
                str(row["tx_hash"])
                for auction, token in pairs
                for row in self.repo.list_pair_operations(auction, token)
                if row.get("tx_hash")
                and not row.get("historical_baseline")
                and row.get("operation_type")
                in {"kick", "resolve_auction", "sweep_auction"}
            }
            errors.extend(
                await self.reconciler.reconcile_receipts(
                    tx_hashes,
                    timeout_seconds=2,
                )
            )
            self.reconciler.rebuild_round_links(pairs)
            errors.extend(
                await self.reconciler.discover_direct_settlements(
                    timeout_seconds=2,
                    pairs=pairs,
                )
            )
            self.reconciler.rebuild_round_links(pairs)
            assert_history_repair_unblocked(self.session)
            if not errors:
                await self._baseline_unprovable_rounds(pairs)
        audits = await self._audit_pairs(pairs)
        after = self._snapshot()
        mutations = sum(
            before.get(row_id) != after.get(row_id)
            for row_id in before.keys() | after.keys()
        )
        return RepairReport(
            pairs=tuple(audits),
            reconciliation_errors=tuple(errors),
            mutations=mutations,
        )

    async def _baseline_unprovable_rounds(
        self,
        pairs: set[tuple[str, str]],
    ) -> None:
        reviewed_at = utcnow_iso()
        changed = False
        for auction_address, token_address in sorted(pairs):
            rows = self.repo.list_pair_operations(auction_address, token_address)
            rounds = classify_all_pair_operations(rows)
            baselined_ids = {
                int(row["id"])
                for row in rows
                if int(row.get("historical_baseline") or 0) == 1
            }
            latest_active: bool | None = None
            latest_active_checked = False
            for index, round_ in enumerate(rounds):
                if round_.kick_id in baselined_ids or round_.outcome not in {
                    RoundOutcome.UNKNOWN,
                    RoundOutcome.INCOMPLETE,
                }:
                    continue
                if index == 0:
                    if not latest_active_checked:
                        latest_active = await self._pair_active(
                            auction_address,
                            token_address,
                        )
                        latest_active_checked = True
                    if latest_active is not False:
                        continue
                self.repo.update_fields(
                    round_.kick_id,
                    historical_baseline=1,
                    historical_baseline_reason=round_.reason_code,
                    historical_baselined_at=reviewed_at,
                )
                changed = True
        if changed:
            self.session.commit()

    async def _pair_active(
        self,
        auction_address: str,
        token_address: str,
    ) -> bool | None:
        try:
            contract = self.web3_client.contract(
                to_checksum_address(auction_address),
                AUCTION_ABI,
            )
            return bool(
                await self.web3_client.call(
                    contract.functions.isActive(to_checksum_address(token_address))
                )
            )
        except Exception:  # noqa: BLE001
            return None

    def _snapshot(self) -> dict[int, dict[str, object]]:
        return {int(row["id"]): row for row in self.repo.list_round_operations()}

    async def _audit_pairs(self, pairs) -> list[RepairPairAudit]:
        output: list[RepairPairAudit] = []
        for auction_address, token_address in pairs:
            rows = self.repo.list_pair_operations(auction_address, token_address)
            all_rounds = classify_all_pair_operations(rows)
            sequence = classify_pair_operations(rows)
            latest = sequence.latest
            baseline_kick_ids = tuple(
                int(row["id"])
                for row in rows
                if int(row.get("historical_baseline") or 0) == 1
            )
            baseline_set = set(baseline_kick_ids)
            unreviewed = [
                round_
                for round_ in all_rounds
                if round_.kick_id not in baseline_set
                and round_.outcome in {RoundOutcome.UNKNOWN, RoundOutcome.INCOMPLETE}
            ]
            live = None
            if (
                unreviewed
                and all_rounds
                and unreviewed[0].kick_id == all_rounds[0].kick_id
            ):
                live = await self._pair_active(auction_address, token_address)
            if latest is None:
                outcome = "HISTORICAL_BASELINE" if baseline_kick_ids else "NO_HISTORY"
                reason = None
            else:
                outcome = latest.outcome.value
                reason = latest.reason_code
            passed = not unreviewed or (
                len(unreviewed) == 1
                and unreviewed[0].outcome == RoundOutcome.INCOMPLETE
                and all_rounds
                and unreviewed[0].kick_id == all_rounds[0].kick_id
                and live is True
            )
            output.append(
                RepairPairAudit(
                    auction_address=auction_address,
                    token_address=token_address,
                    outcome=outcome,
                    reason_code=reason,
                    live_on_chain=live,
                    baseline_kick_ids=baseline_kick_ids,
                    proposed_baseline_kick_ids=tuple(round_.kick_id for round_ in unreviewed
                        if round_.kick_id != all_rounds[0].kick_id or live is False),
                    passed=passed,
                )
            )
        return output


def assert_history_repair_unblocked(session) -> None:
    """A baseline cannot make an unresolved send disappear from policy history."""
    if TransactionRepository(session).unresolved() or any(
        row.get("status") == "SUBMITTED" for row in KickTxRepository(session).list_round_operations()
    ):
        raise LifecycleError("UNRESOLVED_ATTEMPTS", "Reconcile retained attempts before applying any history baseline.")
