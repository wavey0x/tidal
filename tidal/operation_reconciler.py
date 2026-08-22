"""Shared receipt decoding and canonical kick-operation reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Collection, Mapping, Sequence, cast

import structlog
from eth_utils import to_checksum_address
from web3.logs import DISCARD

from tidal.auction_rounds import (
    RoundOutcome,
    classify_pair_operations,
    operation_closes_round,
)
from tidal.chain.contracts.abis import (
    AUCTION_ABI,
    AUCTION_KICKER_ABI,
    AUCTION_KICKER_KICKED_EVENT_ABIS,
)
from tidal.normalizers import normalize_address, to_decimal_string
from tidal.persistence.repositories import KickTxRepository, TokenRepository

logger = structlog.get_logger(__name__)
SETTLEMENT_LOG_BLOCK_SPAN = 50_000


def _event_signature(event_abi: Mapping[str, object]) -> str:
    inputs = cast(Sequence[Mapping[str, object]], event_abi["inputs"])
    parameter_types = ",".join(str(item["type"]) for item in inputs)
    return f"{event_abi['name']}({parameter_types})"


@dataclass(frozen=True, slots=True)
class DecodedKick:
    source_address: str
    auction_address: str
    token_address: str
    requested_amount: int
    placed_amount: int | None


@dataclass(frozen=True, slots=True)
class DecodedResolve:
    auction_address: str
    token_address: str
    path: int
    recovered_amount: int


@dataclass(frozen=True, slots=True)
class DecodedSweep:
    auction_address: str
    token_address: str
    recovered_amount: int


@dataclass(frozen=True, slots=True)
class DecodedSettlement:
    auction_address: str
    token_address: str


@dataclass(frozen=True, slots=True)
class DecodedReceipt:
    kicks: tuple[DecodedKick, ...] = ()
    resolves: tuple[DecodedResolve, ...] = ()
    sweeps: tuple[DecodedSweep, ...] = ()
    settlements: tuple[DecodedSettlement, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconciliationError:
    tx_hash: str
    error_code: str
    error_message: str


class OperationReconciler:
    """Idempotently finalize every operation row sharing a transaction receipt."""

    def __init__(
        self,
        *,
        session,
        web3_client,
        auction_kicker_address: str,
        decode_receipt_fn: Callable[[dict[str, object], Sequence[str]], DecodedReceipt]
        | None = None,
    ) -> None:
        self.session = session
        self.web3_client = web3_client
        self.auction_kicker_address = normalize_address(auction_kicker_address)
        self.kick_repo = KickTxRepository(session)
        self.token_repo = TokenRepository(session)
        self.decode_receipt_fn = decode_receipt_fn or self._decode_receipt

    async def reconcile_submitted(
        self,
        *,
        timeout_seconds: int = 2,
        tx_hashes: Collection[str] | None = None,
    ) -> list[ReconciliationError]:
        submitted_hashes = {
            str(row["tx_hash"])
            for row in self.kick_repo.list_submitted()
            if tx_hashes is None or str(row["tx_hash"]) in tx_hashes
        }
        return await self.reconcile_receipts(
            submitted_hashes,
            timeout_seconds=timeout_seconds,
        )

    async def reconcile_receipts(
        self,
        tx_hashes: Collection[str],
        *,
        timeout_seconds: int = 2,
    ) -> list[ReconciliationError]:
        """Fetch and finalize each retained transaction receipt exactly once."""

        errors: list[ReconciliationError] = []
        for tx_hash in sorted(set(tx_hashes)):
            if not self.kick_repo.list_by_tx_hash(tx_hash):
                continue
            try:
                receipt = await self.web3_client.get_transaction_receipt(
                    tx_hash,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                if exc.__class__.__name__ in {"TransactionNotFound", "TimeExhausted"}:
                    continue
                errors.append(
                    ReconciliationError(
                        tx_hash=tx_hash,
                        error_code="receipt_lookup_failed",
                        error_message="receipt lookup failed",
                    )
                )
                logger.warning(
                    "operation_receipt_lookup_failed",
                    tx_hash=tx_hash,
                    error_type=exc.__class__.__name__,
                )
                continue
            error_code = await self.finalize_receipt(tx_hash, receipt)
            if error_code is not None:
                errors.append(
                    ReconciliationError(
                        tx_hash=tx_hash,
                        error_code=error_code,
                        error_message="confirmed receipt could not be decoded",
                    )
                )
        return errors

    async def repair_pairs(
        self,
        pairs: Collection[tuple[str, str]],
        *,
        timeout_seconds: int = 2,
    ) -> list[ReconciliationError]:
        """Replay retained evidence and settlement logs for exact pairs."""

        normalized_pairs = {
            (normalize_address(auction), normalize_address(token))
            for auction, token in pairs
        }
        normalized_pairs = {
            pair
            for pair in normalized_pairs
            if (
                (
                    latest := classify_pair_operations(
                        self.kick_repo.list_pair_operations(*pair)
                    ).latest
                )
                is not None
                and latest.outcome in {RoundOutcome.UNKNOWN, RoundOutcome.INCOMPLETE}
            )
        }
        if not normalized_pairs:
            return []
        tx_hashes = {
            str(row["tx_hash"])
            for auction_address, token_address in normalized_pairs
            for row in self.kick_repo.list_pair_operations(
                auction_address,
                token_address,
            )
            if row.get("tx_hash")
            and row.get("status") in {"SUBMITTED", "CONFIRMED"}
            and row.get("operation_type")
            in {"kick", "resolve_auction", "sweep_auction"}
        }
        errors = await self.reconcile_receipts(
            tx_hashes,
            timeout_seconds=timeout_seconds,
        )
        self.rebuild_round_links(normalized_pairs)
        errors.extend(
            await self.discover_direct_settlements(
                timeout_seconds=timeout_seconds,
                pairs=normalized_pairs,
            )
        )
        self.rebuild_round_links(normalized_pairs)
        return errors

    async def finalize_receipt(
        self, tx_hash: str, receipt: dict[str, object]
    ) -> str | None:
        rows = self.kick_repo.list_by_tx_hash(tx_hash)
        if not rows:
            return None

        receipt_status = int(receipt.get("status") or 0)
        block_number = int(receipt["blockNumber"])
        transaction_index = int(receipt.get("transactionIndex") or 0)
        gas_used = (
            int(receipt["gasUsed"]) if receipt.get("gasUsed") is not None else None
        )
        effective_gas_price = receipt.get("effectiveGasPrice")
        gas_price_gwei = (
            str(round(int(effective_gas_price) / 1e9, 4))
            if effective_gas_price
            else None
        )
        block = await self.web3_client.get_block(block_number)
        mined_at = datetime.fromtimestamp(
            int(block["timestamp"]), tz=timezone.utc
        ).isoformat()
        common: dict[str, object] = {
            "status": "CONFIRMED" if receipt_status == 1 else "REVERTED",
            "block_number": block_number,
            "transaction_index": transaction_index,
            "mined_at": mined_at,
            "gas_used": gas_used,
            "gas_price_gwei": gas_price_gwei,
        }
        if receipt_status != 1:
            for row in rows:
                self.kick_repo.update_fields(int(row["id"]), **common)
            self.session.commit()
            return None

        auctions = sorted(
            {normalize_address(str(row["auction_address"])) for row in rows}
        )
        try:
            decoded = self.decode_receipt_fn(receipt, auctions)
        except Exception as exc:  # noqa: BLE001
            for row in rows:
                self.kick_repo.update_fields(
                    int(row["id"]),
                    **common,
                    error_message="confirmed receipt event decode failed",
                )
            self.session.commit()
            logger.warning(
                "operation_receipt_event_decode_failed",
                tx_hash=tx_hash,
                error_type=exc.__class__.__name__,
            )
            return "event_decode_failed"

        position = (block_number, transaction_index)
        reconciliation_error: str | None = None
        for row in rows:
            operation_type = str(row["operation_type"])
            auction = normalize_address(str(row["auction_address"]))
            token = normalize_address(str(row["token_address"]))
            values = dict(common)
            values["error_message"] = None
            if operation_type == "kick":
                event = next(
                    (
                        item
                        for item in decoded.kicks
                        if item.auction_address == auction
                        and item.token_address == token
                    ),
                    None,
                )
                if event is None:
                    values["sell_amount"] = None
                    values["error_message"] = "confirmed kick event evidence missing"
                else:
                    values["source_address"] = event.source_address
                    values["requested_sell_amount"] = str(event.requested_amount)
                    values["sell_amount"] = (
                        str(event.placed_amount)
                        if event.placed_amount is not None
                        else None
                    )
                    values["normalized_balance"] = self._normalized(
                        token, event.placed_amount
                    )
                    if event.placed_amount is None:
                        values["error_message"] = (
                            "underlying AuctionKicked event missing"
                        )
            elif operation_type == "resolve_auction":
                event = next(
                    (
                        item
                        for item in decoded.resolves
                        if item.auction_address == auction
                        and item.token_address == token
                    ),
                    None,
                )
                if event is None:
                    values["sell_amount"] = None
                    values["error_message"] = "confirmed AuctionResolved event missing"
                else:
                    values["resolution_path"] = event.path
                    values["sell_amount"] = str(event.recovered_amount)
                    values["normalized_balance"] = self._normalized(
                        token, event.recovered_amount
                    )
                    round_kick_id = (
                        None
                        if event.path == 0
                        else self._round_kick_id(row, auction, token, position)
                    )
                    values["round_kick_id"] = round_kick_id
                    if event.path != 0 and round_kick_id is None:
                        values["error_message"] = (
                            "confirmed resolve could not be linked to a kick"
                        )
                        reconciliation_error = "round_link_failed"
            elif operation_type == "sweep_auction":
                event = next(
                    (
                        item
                        for item in decoded.sweeps
                        if item.auction_address == auction
                        and item.token_address == token
                    ),
                    None,
                )
                if event is None:
                    values["sell_amount"] = None
                    values["error_message"] = "confirmed AuctionSwept event missing"
                else:
                    values["sell_amount"] = str(event.recovered_amount)
                    values["normalized_balance"] = self._normalized(
                        token, event.recovered_amount
                    )
                    values["round_kick_id"] = self._round_kick_id(
                        row, auction, token, position
                    )
            self.kick_repo.update_fields(int(row["id"]), **values)

        resolved_pairs = {
            (item.auction_address, item.token_address) for item in decoded.resolves
        }
        for event in decoded.settlements:
            pair = (event.auction_address, event.token_address)
            if pair in resolved_pairs:
                continue
            if (
                self.kick_repo.find_exact_operation(
                    operation_type="auction_settled",
                    tx_hash=tx_hash,
                    auction_address=event.auction_address,
                    token_address=event.token_address,
                )
                is not None
            ):
                continue
            round_kick = self.kick_repo.latest_confirmed_unclosed_kick(
                event.auction_address,
                event.token_address,
                before_position=position,
            )
            if round_kick is None:
                continue
            self.kick_repo.insert(
                {
                    "run_id": f"chain-observed:{tx_hash}",
                    "operation_type": "auction_settled",
                    "source_type": round_kick.get("source_type"),
                    "source_address": round_kick.get("source_address"),
                    "strategy_address": round_kick.get("strategy_address"),
                    "token_address": event.token_address,
                    "auction_address": event.auction_address,
                    "sell_amount": "0",
                    "normalized_balance": self._normalized(event.token_address, 0),
                    "status": "CONFIRMED",
                    "tx_hash": tx_hash,
                    "block_number": block_number,
                    "transaction_index": transaction_index,
                    "mined_at": mined_at,
                    "round_kick_id": int(round_kick["id"]),
                    "created_at": mined_at,
                }
            )
        self.session.commit()
        return reconciliation_error

    async def discover_direct_settlements(
        self,
        *,
        timeout_seconds: int = 2,
        pairs: Collection[tuple[str, str]] | None = None,
    ) -> list[ReconciliationError]:
        """Persist all missing AuctionSettled closes using one log scan per auction."""

        kicks = self.kick_repo.list_confirmed_kicks()
        pair_filter = (
            {
                (normalize_address(auction), normalize_address(token))
                for auction, token in pairs
            }
            if pairs is not None
            else None
        )
        by_auction: dict[str, list[dict[str, object]]] = {}
        for kick in kicks:
            pair = (
                normalize_address(str(kick["auction_address"])),
                normalize_address(str(kick["token_address"])),
            )
            if pair_filter is not None and pair not in pair_filter:
                continue
            if (
                kick.get("block_number") is None
                or kick.get("transaction_index") is None
            ):
                continue
            by_auction.setdefault(pair[0], []).append(kick)

        errors: list[ReconciliationError] = []
        receipt_cache: dict[str, dict[str, object]] = {}
        block_cache: dict[int, dict[str, object]] = {}
        latest_block = await self.web3_client.get_block_number() if by_auction else None
        for auction_address, auction_kicks in sorted(by_auction.items()):
            assert latest_block is not None
            earliest_block = min(int(kick["block_number"]) for kick in auction_kicks)
            try:
                contract = self.web3_client.contract(
                    to_checksum_address(auction_address), AUCTION_ABI
                )
                logs: list[Mapping[str, object]] = []
                chunk_start = earliest_block
                while chunk_start <= latest_block:
                    chunk_end = min(
                        chunk_start + SETTLEMENT_LOG_BLOCK_SPAN - 1,
                        latest_block,
                    )
                    logs.extend(
                        await contract.events.AuctionSettled().get_logs(
                            from_block=chunk_start,
                            to_block=chunk_end,
                        )
                    )
                    chunk_start = chunk_end + 1
            except Exception as exc:  # noqa: BLE001
                anchor = min(auction_kicks, key=lambda row: int(row["block_number"]))
                errors.append(
                    ReconciliationError(
                        tx_hash=str(anchor.get("tx_hash") or ""),
                        error_code="event_lookup_failed",
                        error_message="settlement event lookup failed",
                    )
                )
                logger.warning(
                    "operation_settlement_lookup_failed",
                    auction_address=auction_address,
                    error_type=exc.__class__.__name__,
                )
                continue

            settlements_by_tx: dict[str, tuple[int, int]] = {}
            for log in logs:
                tx_hash_value = log["transactionHash"]
                tx_hash = (
                    tx_hash_value.hex()
                    if hasattr(tx_hash_value, "hex")
                    else str(tx_hash_value)
                )
                if not tx_hash.startswith("0x"):
                    tx_hash = f"0x{tx_hash}"
                settlements_by_tx[tx_hash] = (
                    int(log["blockNumber"]),
                    int(log["transactionIndex"]),
                )

            for tx_hash, position in sorted(
                settlements_by_tx.items(), key=lambda item: item[1]
            ):
                try:
                    receipt = receipt_cache.get(tx_hash)
                    if receipt is None:
                        receipt = await self.web3_client.get_transaction_receipt(
                            tx_hash,
                            timeout_seconds=timeout_seconds,
                        )
                        receipt_cache[tx_hash] = receipt
                    decoded = self.decode_receipt_fn(receipt, (auction_address,))
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        ReconciliationError(
                            tx_hash=tx_hash,
                            error_code="event_decode_failed",
                            error_message="settlement receipt decode failed",
                        )
                    )
                    logger.warning(
                        "operation_settlement_decode_failed",
                        tx_hash=tx_hash,
                        error_type=exc.__class__.__name__,
                    )
                    continue

                resolved_pairs = {
                    (item.auction_address, item.token_address)
                    for item in decoded.resolves
                }
                for event in decoded.settlements:
                    pair = (event.auction_address, event.token_address)
                    if pair[0] != auction_address:
                        continue
                    if pair_filter is not None and pair not in pair_filter:
                        continue
                    if pair in resolved_pairs:
                        continue
                    if (
                        self.kick_repo.find_exact_operation(
                            operation_type="auction_settled",
                            tx_hash=tx_hash,
                            auction_address=event.auction_address,
                            token_address=event.token_address,
                        )
                        is not None
                    ):
                        continue
                    kick = self.kick_repo.latest_confirmed_unclosed_kick(
                        event.auction_address,
                        event.token_address,
                        before_position=position,
                    )
                    if kick is None:
                        continue
                    block_number = position[0]
                    block = block_cache.get(block_number)
                    if block is None:
                        block = await self.web3_client.get_block(block_number)
                        block_cache[block_number] = block
                    mined_at = datetime.fromtimestamp(
                        int(block["timestamp"]), tz=timezone.utc
                    ).isoformat()
                    self.kick_repo.insert(
                        {
                            "run_id": f"chain-observed:{tx_hash}",
                            "operation_type": "auction_settled",
                            "source_type": kick.get("source_type"),
                            "source_address": kick.get("source_address"),
                            "strategy_address": kick.get("strategy_address"),
                            "token_address": event.token_address,
                            "auction_address": event.auction_address,
                            "sell_amount": "0",
                            "normalized_balance": self._normalized(
                                event.token_address, 0
                            ),
                            "status": "CONFIRMED",
                            "tx_hash": tx_hash,
                            "block_number": block_number,
                            "transaction_index": position[1],
                            "mined_at": mined_at,
                            "round_kick_id": int(kick["id"]),
                            "created_at": mined_at,
                        }
                    )
        self.session.commit()
        return errors

    def rebuild_round_links(
        self,
        pairs: Collection[tuple[str, str]],
    ) -> None:
        """Rebuild confirmed recovery links from deterministic chain order."""

        changed = False
        for auction_address, token_address in sorted(pairs):
            rows = [
                row
                for row in self.kick_repo.list_pair_operations(
                    auction_address,
                    token_address,
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
            for row in rows:
                operation_type = row.get("operation_type")
                if operation_type == "kick":
                    open_kick_id = int(row["id"])
                    continue
                if operation_type not in {
                    "resolve_auction",
                    "sweep_auction",
                    "auction_settled",
                }:
                    continue
                target_kick_id = open_kick_id
                if operation_type == "resolve_auction":
                    try:
                        if int(row.get("resolution_path")) == 0:
                            target_kick_id = None
                    except (TypeError, ValueError):
                        pass
                existing = row.get("round_kick_id")
                existing_id = int(existing) if existing is not None else None
                if existing_id != target_kick_id:
                    self.kick_repo.update_fields(
                        int(row["id"]),
                        round_kick_id=target_kick_id,
                    )
                    changed = True
                if open_kick_id is not None and operation_closes_round(row):
                    open_kick_id = None
        if changed:
            self.session.commit()

    def _round_kick_id(
        self,
        row: dict[str, object],
        auction: str,
        token: str,
        position: tuple[int, int],
    ) -> int | None:
        if row.get("round_kick_id") is not None:
            return int(row["round_kick_id"])
        kick = self.kick_repo.latest_confirmed_unclosed_kick(
            auction,
            token,
            before_position=position,
        )
        return int(kick["id"]) if kick is not None else None

    def _normalized(self, token_address: str, amount: int | None) -> str | None:
        if amount is None:
            return None
        metadata = self.token_repo.get(token_address)
        if metadata is None:
            return None
        return to_decimal_string(amount, metadata.decimals)

    def _decode_receipt(
        self, receipt: dict[str, object], auctions: Sequence[str]
    ) -> DecodedReceipt:
        receipt_destination = receipt.get("to")
        kicker_address = (
            normalize_address(str(receipt_destination))
            if receipt_destination is not None
            else self.auction_kicker_address
        )
        kicker = self.web3_client.contract(
            to_checksum_address(kicker_address),
            [*AUCTION_KICKER_ABI, *AUCTION_KICKER_KICKED_EVENT_ABIS[1:]],
        )
        kicked_logs = [
            log
            for event_abi in AUCTION_KICKER_KICKED_EVENT_ABIS
            for log in kicker.get_event_by_signature(
                _event_signature(event_abi)
            )().process_receipt(receipt, errors=DISCARD)
        ]
        resolved_logs = kicker.events.AuctionResolved().process_receipt(
            receipt, errors=DISCARD
        )
        swept_logs = kicker.events.AuctionSwept().process_receipt(
            receipt, errors=DISCARD
        )

        placed_by_pair: dict[tuple[str, str], list[int]] = {}
        settlements: list[DecodedSettlement] = []
        for auction_address in auctions:
            auction = self.web3_client.contract(
                to_checksum_address(auction_address), AUCTION_ABI
            )
            for log in auction.events.AuctionKicked().process_receipt(
                receipt, errors=DISCARD
            ):
                token = normalize_address(str(log["args"]["from"]))
                placed_by_pair.setdefault(
                    (normalize_address(auction_address), token), []
                ).append(int(log["args"]["available"]))
            for log in auction.events.AuctionSettled().process_receipt(
                receipt, errors=DISCARD
            ):
                settlements.append(
                    DecodedSettlement(
                        auction_address=normalize_address(auction_address),
                        token_address=normalize_address(str(log["args"]["from"])),
                    )
                )

        kicks: list[DecodedKick] = []
        for log in kicked_logs:
            args = log["args"]
            auction_address = normalize_address(str(args["auction"]))
            token_address = normalize_address(str(args["sellToken"]))
            kicks.append(
                DecodedKick(
                    source_address=normalize_address(str(args["source"])),
                    auction_address=auction_address,
                    token_address=token_address,
                    requested_amount=int(args["sellAmount"]),
                    placed_amount=(
                        placed_by_pair[(auction_address, token_address)][0]
                        if len(placed_by_pair.get((auction_address, token_address), ()))
                        == 1
                        else None
                    ),
                )
            )
        resolves = tuple(
            DecodedResolve(
                auction_address=normalize_address(str(log["args"]["auction"])),
                token_address=normalize_address(str(log["args"]["sellToken"])),
                path=int(log["args"]["path"]),
                recovered_amount=int(log["args"]["recoveredBalance"]),
            )
            for log in resolved_logs
        )
        sweeps = tuple(
            DecodedSweep(
                auction_address=normalize_address(str(log["args"]["auction"])),
                token_address=normalize_address(str(log["args"]["sellToken"])),
                recovered_amount=int(log["args"]["recoveredBalance"]),
            )
            for log in swept_logs
        )
        return DecodedReceipt(
            kicks=tuple(kicks),
            resolves=resolves,
            sweeps=sweeps,
            settlements=tuple(settlements),
        )
