"""One-time strict-schema repair and audit for retained auction rounds."""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from eth_utils import to_checksum_address

from tidal.automation_scope import current_automation_pairs, pair_in_automation_scope
from tidal.auction_rounds import (
    RoundOutcome,
    classify_pair_operations,
    operation_closes_round,
)
from tidal.chain.contracts.abis import AUCTION_ABI
from tidal.normalizers import normalize_address
from tidal.operation_reconciler import OperationReconciler, ReconciliationError
from tidal.persistence.repositories import KickTxRepository

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RepairPairAudit:
    auction_address: str
    token_address: str
    outcome: str
    reason_code: str | None
    live_on_chain: bool | None
    in_scope: bool
    passed: bool


@dataclass(frozen=True, slots=True)
class RepairReport:
    pairs: tuple[RepairPairAudit, ...]
    reconciliation_errors: tuple[ReconciliationError, ...]

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
        )

    async def run(self, *, apply: bool) -> RepairReport:
        errors: list[ReconciliationError] = []
        if apply:
            scoped_pairs = self._in_scope_pairs()
            self._repair_links(scoped_pairs)
            scoped_tx_hashes = {
                str(row["tx_hash"])
                for auction_address, token_address in scoped_pairs
                for row in self.repo.list_pair_operations(
                    auction_address, token_address
                )
                if row.get("tx_hash")
            }
            errors.extend(
                await self.reconciler.reconcile_submitted(
                    timeout_seconds=2,
                    tx_hashes=scoped_tx_hashes,
                )
            )
            tx_hashes = sorted(
                {
                    str(row["tx_hash"])
                    for auction_address, token_address in scoped_pairs
                    for row in self.repo.list_pair_operations(
                        auction_address,
                        token_address,
                    )
                    if row.get("status") == "CONFIRMED"
                    and row.get("operation_type")
                    in {"kick", "resolve_auction", "sweep_auction"}
                    and row.get("tx_hash")
                }
            )
            for tx_hash in tx_hashes:
                try:
                    receipt = await self.web3_client.get_transaction_receipt(
                        tx_hash, timeout_seconds=2
                    )
                    error_code = await self.reconciler.finalize_receipt(
                        tx_hash, receipt
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "auction_round_repair_receipt_failed",
                        tx_hash=tx_hash,
                        error_type=exc.__class__.__name__,
                    )
                    errors.append(
                        ReconciliationError(
                            tx_hash, "receipt_lookup_failed", "receipt lookup failed"
                        )
                    )
                    continue
                if error_code:
                    errors.append(
                        ReconciliationError(
                            tx_hash,
                            error_code,
                            "confirmed receipt reconciliation failed",
                        )
                    )
            self._repair_links(scoped_pairs)
            errors.extend(
                await self.reconciler.discover_direct_settlements(
                    timeout_seconds=2,
                    pairs=scoped_pairs,
                )
            )
            self._repair_links(scoped_pairs)
        pairs = await self._audit_pairs()
        return RepairReport(pairs=tuple(pairs), reconciliation_errors=tuple(errors))

    def _in_scope_pairs(self) -> set[tuple[str, str]]:
        output: set[tuple[str, str]] = set()
        current_pairs = current_automation_pairs(self.session, self.settings)
        for auction_address, token_address in self._pair_keys():
            rows = self.repo.list_pair_operations(auction_address, token_address)
            latest_kick = next(
                (
                    row
                    for row in reversed(rows)
                    if row.get("operation_type") == "kick"
                    and row.get("status") in {"CONFIRMED", "SUBMITTED"}
                ),
                None,
            )
            if latest_kick is not None and pair_in_automation_scope(
                self.session,
                current_pairs,
                latest_kick,
            ):
                output.add((auction_address, token_address))
        return output

    def _pair_keys(self) -> set[tuple[str, str]]:
        kicks = [
            *self.repo.list_confirmed_kicks(),
            *(
                row
                for row in self.repo.list_submitted()
                if row.get("operation_type") == "kick"
            ),
        ]
        return {
            (
                normalize_address(str(kick["auction_address"])),
                normalize_address(str(kick["token_address"])),
            )
            for kick in kicks
        }

    def _repair_links(self, pairs: set[tuple[str, str]]) -> None:
        for auction_address, token_address in sorted(pairs):
            rows = [
                row
                for row in self.repo.list_pair_operations(
                    auction_address, token_address
                )
                if row.get("status") == "CONFIRMED"
                and row.get("block_number") is not None
                and row.get("transaction_index") is not None
            ]
            rows.sort(
                key=lambda row: (
                    int(row["block_number"]),
                    int(row["transaction_index"]),
                    int(row["id"]),
                )
            )
            open_kick_id: int | None = None
            changed = False
            for row in rows:
                if row.get("operation_type") == "kick":
                    open_kick_id = int(row["id"])
                    continue
                if row.get("operation_type") not in {
                    "resolve_auction",
                    "sweep_auction",
                    "auction_settled",
                }:
                    continue
                existing = row.get("round_kick_id")
                existing_id = int(existing) if existing is not None else None
                if existing_id != open_kick_id:
                    self.repo.update_fields(int(row["id"]), round_kick_id=open_kick_id)
                    changed = True
                if open_kick_id is not None and operation_closes_round(row):
                    open_kick_id = None
            if changed:
                self.session.commit()

    async def _audit_pairs(self) -> list[RepairPairAudit]:
        pairs = sorted(self._pair_keys())
        current_pairs = current_automation_pairs(self.session, self.settings)
        output: list[RepairPairAudit] = []
        for auction_address, token_address in pairs:
            rows = self.repo.list_pair_operations(auction_address, token_address)
            sequence = classify_pair_operations(rows)
            latest = sequence.latest
            latest_kick = next(
                (
                    row
                    for row in reversed(rows)
                    if row.get("operation_type") == "kick"
                    and row.get("status") in {"CONFIRMED", "SUBMITTED"}
                ),
                None,
            )
            in_scope = latest_kick is not None and pair_in_automation_scope(
                self.session,
                current_pairs,
                latest_kick,
            )
            live = None
            if (
                in_scope
                and latest is not None
                and latest.outcome == RoundOutcome.INCOMPLETE
            ):
                try:
                    contract = self.web3_client.contract(
                        to_checksum_address(auction_address),
                        AUCTION_ABI,
                    )
                    live = bool(
                        await self.web3_client.call(
                            contract.functions.isAnActiveAuction()
                        )
                    )
                except Exception:  # noqa: BLE001
                    live = None
            if latest is None:
                outcome = "NO_HISTORY"
                reason = None
                passed = not in_scope
            else:
                outcome = latest.outcome.value
                reason = latest.reason_code
                passed = not in_scope or (
                    latest.outcome != RoundOutcome.UNKNOWN
                    and (latest.outcome != RoundOutcome.INCOMPLETE or live is True)
                )
            output.append(
                RepairPairAudit(
                    auction_address=auction_address,
                    token_address=token_address,
                    outcome=outcome,
                    reason_code=reason,
                    live_on_chain=live,
                    in_scope=in_scope,
                    passed=passed,
                )
            )
        return output
