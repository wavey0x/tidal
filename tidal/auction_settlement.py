"""Shared auction settlement inspection and decision helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from eth_utils import to_checksum_address

from tidal.chain.contracts.abis import AUCTION_ABI, AUCTION_KICKER_ABI
from tidal.chain.contracts.multicall import MulticallClient
from tidal.normalizers import normalize_address
from tidal.scanner.auction_state import AuctionStateReader
from tidal.transaction_service.types import AuctionInspection

SettlementMethod = Literal["auto", "settle", "sweep_and_settle"]
SettlementDecisionStatus = Literal["actionable", "noop", "error"]
SettlementOperationType = Literal["settle", "sweep_and_settle"]


@dataclass(slots=True)
class AuctionSettlementDecision:
    status: SettlementDecisionStatus
    operation_type: SettlementOperationType | None
    token_address: str | None
    reason: str


@dataclass(slots=True)
class AuctionSettlementCall:
    operation_type: SettlementOperationType
    token_address: str
    target_address: str
    data: str


def normalize_settlement_method(value: str) -> SettlementMethod:
    normalized = value.strip().lower().replace("-", "_")
    if normalized in {"auto", "settle", "sweep_and_settle"}:
        return normalized  # type: ignore[return-value]
    raise ValueError("expected 'auto', 'settle', or 'sweep-and-settle'")


def format_operation_type(operation_type: SettlementOperationType | None) -> str:
    if operation_type is None:
        return "-"
    return operation_type.replace("_", "-")


async def inspect_auction_settlement(web3_client, settings, auction_address: str) -> AuctionInspection:  # noqa: ANN001
    normalized_auction = normalize_address(auction_address)
    multicall_client = MulticallClient(
        web3_client,
        settings.multicall_address,
        enabled=settings.multicall_enabled,
    )
    reader = AuctionStateReader(
        web3_client=web3_client,
        multicall_client=multicall_client,
        multicall_enabled=settings.multicall_enabled,
        multicall_auction_batch_calls=settings.multicall_auction_batch_calls,
    )

    active_flags = await reader.read_bool_noargs_many([normalized_auction], "isAnActiveAuction")
    is_active_auction = active_flags.get(normalized_auction)
    if is_active_auction is not True:
        return AuctionInspection(
            auction_address=normalized_auction,
            is_active_auction=is_active_auction,
            active_tokens=(),
        )

    enabled_tokens_result = await reader.read_address_array_noargs_many([normalized_auction], "getAllEnabledAuctions")
    enabled_tokens = enabled_tokens_result.get(normalized_auction) or []
    probe_pairs = [(normalized_auction, token_address) for token_address in enabled_tokens]

    token_active = {}
    if probe_pairs:
        token_active = await reader.read_bool_arg_many(probe_pairs, "isActive")

    active_tokens = tuple(
        sorted(
            token_address
            for token_address in enabled_tokens
            if token_active.get((normalized_auction, token_address)) is True
        )
    )
    active_token = active_tokens[0] if len(active_tokens) == 1 else None

    active_available_raw = None
    active_price_raw = None
    minimum_price_raw = None

    if active_tokens:
        minimum_price_by_auction = await reader.read_uint_noargs_many([normalized_auction], "minimumPrice")
        minimum_price_raw = minimum_price_by_auction.get(normalized_auction)

    if active_token is not None:
        available_by_pair, price_by_pair = await asyncio.gather(
            reader.read_uint_arg_many([(normalized_auction, active_token)], "available"),
            reader.read_uint_arg_many([(normalized_auction, active_token)], "price"),
        )
        active_available_raw = available_by_pair.get((normalized_auction, active_token))
        active_price_raw = price_by_pair.get((normalized_auction, active_token))

    return AuctionInspection(
        auction_address=normalized_auction,
        is_active_auction=is_active_auction,
        active_tokens=active_tokens,
        active_token=active_token,
        active_available_raw=active_available_raw,
        active_price_raw=active_price_raw,
        minimum_price_raw=minimum_price_raw,
    )


def decide_auction_settlement(
    inspection: AuctionInspection,
    *,
    token_address: str | None = None,
    method: SettlementMethod = "auto",
) -> AuctionSettlementDecision:
    normalized_token = normalize_address(token_address) if token_address else None
    forced = method in {"settle", "sweep_and_settle"}

    if inspection.is_active_auction is None:
        return AuctionSettlementDecision(
            status="error",
            operation_type=None,
            token_address=None,
            reason="auction isAnActiveAuction() read failed",
        )

    if inspection.is_active_auction is not True:
        return AuctionSettlementDecision(
            status="error" if forced else "noop",
            operation_type=None,
            token_address=None,
            reason=(
                "requested settlement method is not applicable: auction has no active lot"
                if forced
                else "auction has no active lot"
            ),
        )

    if len(inspection.active_tokens) > 1:
        return AuctionSettlementDecision(
            status="error",
            operation_type=None,
            token_address=None,
            reason="multiple active tokens detected for auction",
        )

    if inspection.active_token is None:
        return AuctionSettlementDecision(
            status="error",
            operation_type=None,
            token_address=None,
            reason="active auction token inspection failed",
        )

    active_token = normalize_address(inspection.active_token)
    if normalized_token is not None and normalized_token != active_token:
        return AuctionSettlementDecision(
            status="error",
            operation_type=None,
            token_address=active_token,
            reason=(
                f"requested token {to_checksum_address(normalized_token)} does not match "
                f"active token {to_checksum_address(active_token)}"
            ),
        )

    if inspection.active_available_raw is None:
        return AuctionSettlementDecision(
            status="error",
            operation_type=None,
            token_address=active_token,
            reason="active auction available() read failed",
        )

    if inspection.minimum_price_raw is None:
        return AuctionSettlementDecision(
            status="error",
            operation_type=None,
            token_address=active_token,
            reason="auction minimumPrice() read failed",
        )

    if inspection.active_available_raw == 0:
        if method == "sweep_and_settle":
            return AuctionSettlementDecision(
                status="error",
                operation_type=None,
                token_address=active_token,
                reason="sweep-and-settle is not applicable: active lot is already sold out",
            )
        return AuctionSettlementDecision(
            status="actionable",
            operation_type="settle",
            token_address=active_token,
            reason="active lot is sold out",
        )

    if inspection.active_price_raw is None:
        return AuctionSettlementDecision(
            status="error",
            operation_type=None,
            token_address=active_token,
            reason="active auction price() read failed",
        )

    if inspection.active_price_raw <= inspection.minimum_price_raw:
        if method == "settle":
            return AuctionSettlementDecision(
                status="error",
                operation_type=None,
                token_address=active_token,
                reason="settle is not applicable: active lot still has available balance",
            )
        return AuctionSettlementDecision(
            status="actionable",
            operation_type="sweep_and_settle",
            token_address=active_token,
            reason="active auction price is at or below minimumPrice",
        )

    return AuctionSettlementDecision(
        status="error" if forced else "noop",
        operation_type=None,
        token_address=active_token,
        reason=(
            "requested settlement method is not applicable: auction still active above minimumPrice"
            if forced
            else "auction still active above minimumPrice"
        ),
    )


def build_auction_settlement_call(
    *,
    settings,
    web3_client,
    auction_address: str,
    decision: AuctionSettlementDecision,
) -> AuctionSettlementCall:  # noqa: ANN001
    if decision.status != "actionable" or decision.operation_type is None or decision.token_address is None:
        raise ValueError("settlement call requires an actionable decision")

    normalized_auction = normalize_address(auction_address)
    normalized_token = normalize_address(decision.token_address)

    if decision.operation_type == "settle":
        contract = web3_client.contract(normalized_auction, AUCTION_ABI)
        tx_data = contract.functions.settle(to_checksum_address(normalized_token))._encode_transaction_data()
        return AuctionSettlementCall(
            operation_type="settle",
            token_address=normalized_token,
            target_address=normalized_auction,
            data=tx_data,
        )

    kicker_address = normalize_address(settings.auction_kicker_address)
    contract = web3_client.contract(kicker_address, AUCTION_KICKER_ABI)
    tx_data = contract.functions.sweepAndSettle(
        to_checksum_address(normalized_auction),
        to_checksum_address(normalized_token),
    )._encode_transaction_data()
    return AuctionSettlementCall(
        operation_type="sweep_and_settle",
        token_address=normalized_token,
        target_address=kicker_address,
        data=tx_data,
    )
