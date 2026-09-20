"""Shared receipt decoding and canonical kick-operation reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Collection, Mapping, Sequence, cast

import structlog
from eth_utils import to_checksum_address
from hexbytes import HexBytes
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
from tidal.constants import TRUSTED_HISTORICAL_AUCTION_KICKERS
from tidal.persistence.repositories import KickTxRepository, TokenRepository
from tidal.persistence import models
from sqlalchemy import select
from tidal.transaction_evidence import EvidenceError, hydrate_legacy_identity, verify_evidence, rpc_int
from tidal.time import utcnow_iso

logger = structlog.get_logger(__name__)
SETTLEMENT_LOG_BLOCK_SPAN = 50_000


def _event_signature(event_abi: Mapping[str, object]) -> str:
    inputs = cast(Sequence[Mapping[str, object]], event_abi["inputs"])
    parameter_types = ",".join(str(item["type"]) for item in inputs)
    return f"{event_abi['name']}({parameter_types})"


def _receipt_from_emitters(receipt: dict[str, object], addresses: Collection[str]) -> dict[str, object]:
    """Web3 event decoding matches topics, not the contract's bound address."""
    return {
        **receipt,
        "logs": [log for log in receipt.get("logs", []) if str(log.get("address", "")).lower() in addresses],
    }


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
    enabled: tuple[DecodedSettlement, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconciliationError:
    tx_hash: str
    error_code: str
    error_message: str
    auction_address: str | None = None
    token_address: str | None = None
    kick_id: int | None = None


class OperationReconciler:
    """Idempotently finalize every operation row sharing a transaction receipt."""

    def __init__(
        self,
        *,
        session,
        web3_client,
        auction_kicker_address: str,
        chain_id: int = 1,
        settings=None,
        decode_receipt_fn: Callable[[dict[str, object], Sequence[str]], DecodedReceipt]
        | None = None,
    ) -> None:
        self.session = session
        self.lifecycle_settings = settings
        self.chain_id = chain_id
        self.web3_client = web3_client
        self.auction_kicker_address = normalize_address(auction_kicker_address)
        self.trusted_kicker_addresses = {self.auction_kicker_address}
        if chain_id == 1:
            self.trusted_kicker_addresses.update(TRUSTED_HISTORICAL_AUCTION_KICKERS)
        self.kick_repo = KickTxRepository(session)
        self.token_repo = TokenRepository(session)
        self.decode_receipt_fn = decode_receipt_fn or self._decode_receipt

    async def reconcile_submitted(
        self,
        *,
        timeout_seconds: int = 2,
        tx_hashes: Collection[str] | None = None,
    ) -> list[ReconciliationError]:
        from tidal.transactions import LedgerReconciler
        del timeout_seconds
        if self.lifecycle_settings is None:
            return [ReconciliationError("", "native_reconciliation_settings_missing", "Native reconciliation requires application settings.")]
        native = LedgerReconciler(session=self.session, settings=self.lifecycle_settings,
            web3_client=self.web3_client, operation_reconciler=self)
        ids = None if tx_hashes is None else list(self.session.execute(select(models.transactions.c.id).where(
            models.transactions.c.tx_hash.in_(tx_hashes),
        )).scalars())
        pending = await native.reconcile(transaction_ids=ids)
        return [ReconciliationError(str(row.get("tx_hash") or ""), "transaction_review_required",
                    str(row.get("error_message") or "Retained evidence needs review."))
                for row in pending if row["status"] == "REVIEW_REQUIRED"]

    async def reconcile_receipts(
        self, tx_hashes: Collection[str], *, timeout_seconds: int = 2,
    ) -> list[ReconciliationError]:
        """Ask the ledger for incomplete evidence only, before applying the limit."""
        del timeout_seconds  # Native evidence uses the bounded two-second lookup.
        selected = []
        for tx_hash in set(value.lower() for value in tx_hashes):
            rows = self.kick_repo.list_by_tx_hash(tx_hash)
            retained = self.session.execute(select(models.transactions).where(
                models.transactions.c.tx_hash == tx_hash,
            )).mappings().first()
            if retained is not None and retained["status"] in {"REVERTED", "SUPERSEDED"}:
                continue
            if rows and all(self._receipt_evidence_complete(row) for row in rows):
                # Legacy imports retain canonical business evidence without a
                # hydrated native identity. Missing ledger metadata alone must
                # not turn completed history into a receipt replay job.
                if retained is None or (retained["status"] == "CONFIRMED" and (
                    retained["legacy"] or (retained.get("verified_at") and retained.get("receipt_status") == "CONFIRMED")
                )):
                    continue
            selected.append((max((int(row["id"]) for row in rows), default=0), tx_hash))
        selected.sort(reverse=True)
        errors = []
        if len(selected) > 100:
            errors.append(ReconciliationError("", "known_history_limit", "Incomplete receipt evidence exceeds the bounded lookup; select a smaller set of known rounds."))
        for _, tx_hash in selected[:100]:
            try:
                code = await self.finalize_receipt(tx_hash, {})
            except Exception as exc:
                self.session.rollback()
                code = "receipt_lookup_failed"
                logger.warning("operation_receipt_lookup_failed", tx_hash=tx_hash, error_type=type(exc).__name__)
            if code:
                errors.append(ReconciliationError(tx_hash=tx_hash, error_code=code,
                    error_message="Retained receipt evidence remains incomplete; inspect the transaction ledger."))
        return errors

    @staticmethod
    def _receipt_evidence_complete(row: Mapping[str, object]) -> bool:
        """A missing logical close is not missing evidence in the kick receipt."""
        if row.get("status") != "CONFIRMED" or row.get("error_message"):
            return False
        if any(row.get(field) is None for field in ("block_number", "transaction_index", "mined_at")):
            return False
        kind = row.get("operation_type")
        if kind == "kick":
            return all(row.get(field) is not None for field in ("requested_sell_amount", "sell_amount"))
        if kind == "resolve_auction":
            return row.get("sell_amount") is not None and row.get("resolution_path") is not None and (
                row.get("resolution_path") == 0 or row.get("round_kick_id") is not None
            )
        if kind in {"sweep_auction", "auction_settled"}:
            return row.get("sell_amount") is not None and row.get("round_kick_id") is not None
        return kind == "enable_tokens"

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
                any(round_.outcome in {RoundOutcome.UNKNOWN, RoundOutcome.INCOMPLETE}
                    for round_ in classify_pair_operations(self.kick_repo.list_pair_operations(*pair)).rounds)
            )
        }
        if not normalized_pairs:
            return []
        self.rebuild_round_links(normalized_pairs)
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

    async def finalize_receipt(self, tx_hash: str, receipt: dict[str, object]) -> str | None:
        """Only the native ledger can authorize receipt-derived business changes."""
        from tidal.transactions import LedgerReconciler
        del receipt  # Never trust caller-supplied or previously fetched evidence.
        if self.lifecycle_settings is None:
            return "native_reconciliation_settings_missing"
        retained = self.session.execute(select(models.transactions).where(
            models.transactions.c.tx_hash == tx_hash.lower(),
        )).mappings().first()
        if retained is None:
            return "transaction_identity_missing"
        native = LedgerReconciler(session=self.session, settings=self.lifecycle_settings,
            web3_client=self.web3_client, operation_reconciler=self)
        await native.reconcile(transaction_ids=[int(retained["id"])])
        row = native.repository.get(int(retained["id"]))
        return str(row.get("error_message") or "transaction_review_required") if row["status"] == "REVIEW_REQUIRED" else None

    def _finalize_operations(
        self, tx_hash: str, receipt: dict[str, object], rows: list[dict[str, object]], block: Mapping[str, object],
        *, legacy_sweeps: tuple[DecodedSweep, ...] = (),
    ) -> str | None:
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
                self.kick_repo.update_fields(int(row["id"]), **common, error_message=None)
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
            elif operation_type == "enable_tokens":
                if not any(item.auction_address == auction and item.token_address == token for item in decoded.enabled):
                    values["error_message"] = "confirmed AuctionEnabled event missing"
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
                        for item in (*decoded.sweeps, *legacy_sweeps)
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
                },
                commit=False,
            )
        return reconciliation_error

    def _round_error(
        self, kick: Mapping[str, object], code: str, message: str, *, tx_hash: str | None = None,
    ) -> ReconciliationError:
        auction, token, kick_id = str(kick["auction_address"]), str(kick["token_address"]), int(kick["id"])
        message = f"{message} Auction {auction}, token {token}, kick {kick_id}."
        logger.warning("operation_settlement_reconciliation_warning", auction_address=auction,
            token_address=token, kick_id=kick_id, tx_hash=tx_hash or kick.get("tx_hash"),
            error_code=code, error_message=message)
        return ReconciliationError(tx_hash or str(kick.get("tx_hash") or ""), code, message, auction, token, kick_id)

    @staticmethod
    def _chain_position(row: Mapping[str, object]) -> tuple[int, int]:
        return rpc_int(row["block_number"]), rpc_int(row["transaction_index"])

    async def _record_direct_settlement(
        self, *, kick: dict[str, object], next_position: tuple[int, int] | None,
        tx_hash: str, finalized: dict, receipt_cache: dict,
        timeout_seconds: int, expected_position: tuple[int, int] | None = None,
    ) -> bool:
        """Verify and persist a close for this exact round, never a fallback kick."""
        cached = receipt_cache.get(tx_hash)
        if cached is None:
            receipt = await self.web3_client.get_transaction_receipt(tx_hash, timeout_seconds=timeout_seconds)
            transaction = await self.web3_client.get_transaction(tx_hash)
            identity = hydrate_legacy_identity({"tx_hash": tx_hash}, transaction, chain_id=self.chain_id)
            block = await self.web3_client.get_block(rpc_int(receipt["blockNumber"]))
            proof = verify_evidence(identity, transaction=transaction, receipt=receipt,
                block=block, finalized_head=finalized, chain_id=self.chain_id)
            if not proof.finalized or rpc_int(receipt["status"]) != 1:
                raise EvidenceError("UNFINALIZED_SETTLEMENT", "A settlement needs a successful finalized receipt.")
            receipt_cache[tx_hash] = receipt, block
        else:
            receipt, block = cached
        position = rpc_int(receipt["blockNumber"]), rpc_int(receipt["transactionIndex"])
        if expected_position is not None and position != expected_position:
            raise EvidenceError("SETTLEMENT_POSITION_MISMATCH", "Settlement log and canonical receipt positions disagree.")
        if position <= self._chain_position(kick) or (next_position is not None and position >= next_position):
            raise EvidenceError("SETTLEMENT_OUTSIDE_ROUND", "Settlement must follow this kick and precede the next kick.")
        auction, token = str(kick["auction_address"]), str(kick["token_address"])
        pair = auction, token
        decoded = self.decode_receipt_fn(receipt, (auction,))
        if pair in {(item.auction_address, item.token_address) for item in decoded.resolves}:
            # The ledger must retain recovered amounts for managed resolutions;
            # recording a zero-recovery direct close would change no-fill policy.
            return False
        if pair not in {(item.auction_address, item.token_address) for item in decoded.settlements}:
            return False
        existing = self.kick_repo.find_exact_operation(operation_type="auction_settled", tx_hash=tx_hash,
            auction_address=auction, token_address=token)
        if existing is not None:
            if existing.get("round_kick_id") == kick["id"] and self._receipt_evidence_complete(existing):
                return True
            raise EvidenceError("SETTLEMENT_LINK_CONFLICT", "Retained settlement is not a complete close for this kick.")
        if any(row.get("round_kick_id") == kick["id"] and row.get("status") == "CONFIRMED"
               and operation_closes_round(row) for row in self.kick_repo.list_pair_operations(auction, token)):
            raise EvidenceError("ROUND_ALREADY_CLOSED", "This kick already has a different logical close.")
        mined_at = datetime.fromtimestamp(rpc_int(block["timestamp"]), tz=timezone.utc).isoformat()
        self.kick_repo.insert({
            "run_id": f"chain-observed:{tx_hash}", "operation_type": "auction_settled",
            "source_type": kick.get("source_type"), "source_address": kick.get("source_address"),
            "strategy_address": kick.get("strategy_address"), "token_address": token, "auction_address": auction,
            "sell_amount": "0", "normalized_balance": self._normalized(token, 0), "status": "CONFIRMED",
            "tx_hash": tx_hash, "block_number": position[0], "transaction_index": position[1],
            "mined_at": mined_at, "round_kick_id": int(kick["id"]), "created_at": mined_at,
        })
        return True

    async def discover_direct_settlements(
        self, *, timeout_seconds: int = 2, pairs: Collection[tuple[str, str]] | None = None,
        max_log_chunks: int = 20, kick_id: int | None = None, settlement_tx_hash: str | None = None,
    ) -> list[ReconciliationError]:
        """Search unresolved rounds newest first, stopping at each verified close."""
        if max_log_chunks < 1:
            raise ValueError("max_log_chunks must be positive")
        if (kick_id is None) != (settlement_tx_hash is None):
            raise ValueError("An exact settlement requires both kick_id and settlement_tx_hash")
        pair_filter = {(normalize_address(a), normalize_address(t)) for a, t in pairs} if pairs is not None else None
        by_pair: dict[tuple[str, str], list[dict[str, object]]] = {}
        for kick in self.kick_repo.list_confirmed_kicks():
            pair = normalize_address(str(kick["auction_address"])), normalize_address(str(kick["token_address"]))
            if pair_filter is None or pair in pair_filter:
                by_pair.setdefault(pair, []).append(kick)
        self.rebuild_round_links(set(by_pair))
        closed = {int(row["round_kick_id"]) for row in self.kick_repo.list_round_operations()
            if row.get("round_kick_id") is not None and row.get("status") == "CONFIRMED"
            and not row.get("error_message") and operation_closes_round(row)}
        errors: list[ReconciliationError] = []
        rounds = []
        found_target = False
        for pair_kicks in by_pair.values():
            positioned = [row for row in pair_kicks if row.get("block_number") is not None and row.get("transaction_index") is not None]
            positioned.sort(key=lambda row: (*self._chain_position(row), int(row["id"])))
            for index, kick in enumerate(positioned):
                if kick_id is not None:
                    if kick["id"] != kick_id:
                        continue
                    found_target = True
                elif int(kick["id"]) in closed or kick.get("historical_baseline"):
                    continue
                next_position = self._chain_position(positioned[index + 1]) if index + 1 < len(positioned) else None
                rounds.append((kick, next_position))
            for kick in pair_kicks:
                if kick in positioned or (kick_id is not None and kick["id"] != kick_id):
                    continue
                if int(kick["id"]) not in closed and not kick.get("historical_baseline"):
                    errors.append(self._round_error(kick, "round_position_missing", "Repair the kick's missing chain position first."))
        if kick_id is not None and not found_target:
            raise ValueError("Select a confirmed kick with a chain position in the requested auction/token pair")
        if not rounds:
            return errors
        rounds.sort(key=lambda item: (*self._chain_position(item[0]), int(item[0]["id"])), reverse=True)
        try:
            if await self.web3_client.get_chain_id() != self.chain_id:
                raise EvidenceError("WRONG_CHAIN", "Settlement lookup RPC is on another chain.")
            finalized = await self.web3_client.get_block("finalized")
            latest_block = rpc_int(finalized["number"])
        except Exception:
            return errors + [self._round_error(kick, "finality_unavailable", "Finalized settlement evidence is unavailable; retry later.") for kick, _ in rounds]
        receipt_cache: dict = {}
        for kick, next_position in rounds:
            if settlement_tx_hash is not None:
                tx_hash = "0x" + bytes(HexBytes(settlement_tx_hash)).hex()
                try:
                    recorded = await self._record_direct_settlement(kick=kick, next_position=next_position,
                        tx_hash=tx_hash, finalized=finalized, receipt_cache=receipt_cache, timeout_seconds=timeout_seconds)
                    if not recorded:
                        raise EvidenceError("SETTLEMENT_EVENT_MISSING", "No direct settlement for this pair; reconcile managed resolutions through the ledger.")
                except Exception as exc:
                    self.session.rollback()
                    message = str(exc) if isinstance(exc, EvidenceError) else f"Settlement verification failed ({type(exc).__name__}); retry this transaction."
                    errors.append(self._round_error(kick, "event_decode_failed", message, tx_hash=tx_hash))
                continue
            auction, token = str(kick["auction_address"]), str(kick["token_address"])
            chunk_start = self._chain_position(kick)[0]
            last_block = min(latest_block, next_position[0]) if next_position is not None else latest_block
            chunks = 0
            recorded = False
            while chunk_start <= last_block and chunks < max_log_chunks:
                chunk_end = min(chunk_start + SETTLEMENT_LOG_BLOCK_SPAN - 1, last_block)
                try:
                    contract = self.web3_client.contract(to_checksum_address(auction), AUCTION_ABI)
                    logs = await contract.events.AuctionSettled().get_logs(
                        from_block=chunk_start, to_block=chunk_end,
                        argument_filters={"from": to_checksum_address(token)},
                    )
                except Exception as exc:
                    errors.append(self._round_error(kick, "event_lookup_failed", f"Settlement event lookup failed ({type(exc).__name__}); retry this round."))
                    break
                chunks += 1
                settlements_by_tx = {}
                for log in logs:
                    try:
                        if log.get("removed") or str(log.get("args", {}).get("from", token)).lower() != token:
                            continue
                        position = rpc_int(log["blockNumber"]), rpc_int(log["transactionIndex"])
                        if not chunk_start <= position[0] <= chunk_end or position <= self._chain_position(kick):
                            continue
                        if next_position is not None and position >= next_position:
                            continue
                        tx_hash = "0x" + bytes(HexBytes(log["transactionHash"])).hex()
                        settlements_by_tx[tx_hash] = position
                    except (KeyError, TypeError, ValueError):
                        errors.append(self._round_error(kick, "event_decode_failed", "Settlement log is malformed; retry this round."))
                for tx_hash, position in sorted(settlements_by_tx.items(), key=lambda item: item[1]):
                    try:
                        recorded = await self._record_direct_settlement(kick=kick, next_position=next_position,
                            tx_hash=tx_hash, finalized=finalized, receipt_cache=receipt_cache,
                            timeout_seconds=timeout_seconds, expected_position=position)
                    except Exception as exc:
                        self.session.rollback()
                        errors.append(self._round_error(kick, "event_decode_failed", f"Settlement receipt verification failed ({type(exc).__name__}); inspect this transaction.", tx_hash=tx_hash))
                    if recorded:
                        break
                if recorded:
                    break
                chunk_start = chunk_end + 1
            else:
                if chunk_start <= last_block:
                    errors.append(self._round_error(kick, "known_history_limit",
                        f"Checked {chunks} settlement chunks; use this kick's exact settlement transaction for targeted repair."))
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
        kicker_receipt = _receipt_from_emitters(
            receipt, self.trusted_kicker_addresses,
        )
        kicker = self.web3_client.contract(
            to_checksum_address(self.auction_kicker_address),
            [*AUCTION_KICKER_ABI, *AUCTION_KICKER_KICKED_EVENT_ABIS[1:]],
        )
        kicked_logs = [
            log
            for event_abi in AUCTION_KICKER_KICKED_EVENT_ABIS
            for log in kicker.get_event_by_signature(
                _event_signature(event_abi)
            )().process_receipt(kicker_receipt, errors=DISCARD)
        ]
        resolved_logs = kicker.events.AuctionResolved().process_receipt(
            kicker_receipt, errors=DISCARD
        )
        swept_logs = kicker.events.AuctionSwept().process_receipt(
            kicker_receipt, errors=DISCARD
        )

        placed_by_pair: dict[tuple[str, str], list[int]] = {}
        settlements: list[DecodedSettlement] = []
        enabled: list[DecodedSettlement] = []
        for auction_address in auctions:
            auction_receipt = _receipt_from_emitters(receipt, {normalize_address(auction_address)})
            auction = self.web3_client.contract(
                to_checksum_address(auction_address), AUCTION_ABI
            )
            for log in auction.events.AuctionEnabled().process_receipt(auction_receipt, errors=DISCARD):
                enabled.append(DecodedSettlement(
                    auction_address=normalize_address(auction_address),
                    token_address=normalize_address(str(log["args"]["from"])),
                ))
            for log in auction.events.AuctionKicked().process_receipt(
                auction_receipt, errors=DISCARD
            ):
                token = normalize_address(str(log["args"]["from"]))
                placed_by_pair.setdefault(
                    (normalize_address(auction_address), token), []
                ).append(int(log["args"]["available"]))
            for log in auction.events.AuctionSettled().process_receipt(
                auction_receipt, errors=DISCARD
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
            enabled=tuple(enabled),
        )
