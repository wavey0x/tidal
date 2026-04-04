"""Shared auction settlement inspection and decision helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Literal

from eth_utils import to_checksum_address

from tidal.auction_price_units import scaled_price_to_public_raw
from tidal.chain.contracts.erc20 import ERC20Reader
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


async def inspect_auction_settlement(  # noqa: ANN001
    web3_client,
    settings,
    auction_address: str,
    token_address: str | None = None,
) -> AuctionInspection:
    normalized_auction = normalize_address(auction_address)
    normalized_token = normalize_address(token_address) if token_address else None
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
    erc20_reader = ERC20Reader(
        web3_client,
        multicall_client=multicall_client,
        multicall_enabled=settings.multicall_enabled,
        multicall_balance_batch_calls=settings.multicall_balance_batch_calls,
    )

    active_flags = await reader.read_bool_noargs_many([normalized_auction], "isAnActiveAuction")
    is_active_auction = active_flags.get(normalized_auction)

    enabled_tokens_result = await reader.read_address_array_noargs_many([normalized_auction], "getAllEnabledAuctions")
    enabled_tokens: list[str] = enabled_tokens_result.get(normalized_auction) or []

    probe_tokens: set[str] = set(enabled_tokens)
    if normalized_token is not None:
        probe_tokens.add(normalized_token)
    probe_pairs = [(normalized_auction, token_address) for token_address in sorted(probe_tokens)]

    token_active = {}
    if probe_pairs:
        token_active = await reader.read_bool_arg_many(probe_pairs, "isActive")

    active_tokens = tuple(
        sorted(
            token_address
            for token_address in sorted(probe_tokens)
            if token_active.get((normalized_auction, token_address)) is True
        )
    )
    active_token = active_tokens[0] if len(active_tokens) == 1 else None

    active_available_raw = None
    active_price_public_raw = None
    minimum_price_scaled_1e18 = None
    minimum_price_public_raw = None
    want_address = None
    want_decimals = None
    inactive_tokens_with_balance: tuple[str, ...] = ()
    inactive_token = None
    inactive_token_balance_raw = None
    inactive_token_kickable_raw = None
    inactive_token_kicked_at = None
    auction_length_seconds = None

    if active_tokens or normalized_token is not None:
        minimum_price_by_auction, want_by_auction = await asyncio.gather(
            reader.read_uint_noargs_many([normalized_auction], "minimumPrice"),
            reader.read_address_noargs_many([normalized_auction], "want"),
        )
        minimum_price_scaled_1e18 = minimum_price_by_auction.get(normalized_auction)
        want_address = want_by_auction.get(normalized_auction)
        if want_address is not None:
            try:
                want_decimals = await erc20_reader.read_decimals(want_address)
            except Exception:  # noqa: BLE001
                want_decimals = None
        minimum_price_public_raw = scaled_price_to_public_raw(minimum_price_scaled_1e18, want_decimals)

    if active_token is not None:
        available_by_pair, price_by_pair = await asyncio.gather(
            reader.read_uint_arg_many([(normalized_auction, active_token)], "available"),
            reader.read_uint_arg_many([(normalized_auction, active_token)], "price"),
        )
        active_available_raw = available_by_pair.get((normalized_auction, active_token))
        active_price_public_raw = price_by_pair.get((normalized_auction, active_token))
    elif probe_pairs:
        balance_by_pair, _ = await erc20_reader.read_balances_many(probe_pairs)
        inactive_tokens_with_balance = tuple(
            sorted(
                token_address
                for _, token_address in probe_pairs
                if (balance_by_pair.get((normalized_auction, token_address)) or 0) > 0
            )
        )
        if normalized_token is not None and (balance_by_pair.get((normalized_auction, normalized_token)) or 0) > 0:
            inactive_token = normalized_token
        elif len(inactive_tokens_with_balance) == 1:
            inactive_token = inactive_tokens_with_balance[0]

        if inactive_token is not None:
            kickable_by_pair, kicked_by_pair, auction_length_by_auction = await asyncio.gather(
                reader.read_uint_arg_many([(normalized_auction, inactive_token)], "kickable"),
                reader.read_uint_arg_many([(normalized_auction, inactive_token)], "kicked"),
                reader.read_uint_noargs_many([normalized_auction], "auctionLength"),
            )
            inactive_token_balance_raw = balance_by_pair.get((normalized_auction, inactive_token))
            inactive_token_kickable_raw = kickable_by_pair.get((normalized_auction, inactive_token))
            inactive_token_kicked_at = kicked_by_pair.get((normalized_auction, inactive_token))
            auction_length_seconds = auction_length_by_auction.get(normalized_auction)

    return AuctionInspection(
        auction_address=normalized_auction,
        is_active_auction=is_active_auction,
        active_tokens=active_tokens,
        active_token=active_token,
        active_available_raw=active_available_raw,
        active_price_public_raw=active_price_public_raw,
        minimum_price_scaled_1e18=minimum_price_scaled_1e18,
        minimum_price_public_raw=minimum_price_public_raw,
        want_address=want_address,
        want_decimals=want_decimals,
        enabled_tokens=tuple(sorted(enabled_tokens)),
        inactive_tokens_with_balance=inactive_tokens_with_balance,
        inactive_token=inactive_token,
        inactive_token_balance_raw=inactive_token_balance_raw,
        inactive_token_kickable_raw=inactive_token_kickable_raw,
        inactive_token_kicked_at=inactive_token_kicked_at,
        auction_length_seconds=auction_length_seconds,
    )


def _describe_inactive_balance_reason(
    inspection: AuctionInspection,
    *,
    requested_token: str | None,
) -> str | None:
    if inspection.inactive_token is None or (inspection.inactive_token_balance_raw or 0) <= 0:
        return None

    inactive_token = normalize_address(inspection.inactive_token)
    token_label = to_checksum_address(inactive_token)
    if requested_token is not None and normalize_address(requested_token) == inactive_token:
        subject = f"requested token {token_label} has stranded balance"
    else:
        subject = f"auction has stranded balance for token {token_label}"

    if (
        inspection.inactive_token_kicked_at is not None
        and inspection.auction_length_seconds is not None
        and inspection.inactive_token_kicked_at > 0
    ):
        inactive_until = inspection.inactive_token_kicked_at + inspection.auction_length_seconds
        state = (
            "the lot is already inactive below minimumPrice"
            if inactive_until > int(time.time())
            else "the lot has already expired"
        )
    else:
        state = "the lot is inactive"

    unwind_options = "Use governance sweep()+disable() to unwind."
    if (inspection.inactive_token_kickable_raw or 0) > 0:
        unwind_options = "Use governance forceKick() to relist or governance sweep()+disable() to unwind."

    return (
        f"{subject}, but {state}; current sweep-and-settle only works while the lot is active. "
        f"{unwind_options}"
    )


def decide_auction_settlement(
    inspection: AuctionInspection,
    *,
    token_address: str | None = None,
    method: SettlementMethod = "auto",
    allow_above_floor: bool = False,
) -> AuctionSettlementDecision:
    if allow_above_floor and method != "sweep_and_settle":
        raise ValueError("allow_above_floor requires method='sweep_and_settle'")

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
        inactive_reason = _describe_inactive_balance_reason(inspection, requested_token=normalized_token)
        if inactive_reason is not None:
            return AuctionSettlementDecision(
                status="error" if forced else "noop",
                operation_type=None,
                token_address=inspection.inactive_token,
                reason=inactive_reason,
            )
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

    if inspection.minimum_price_scaled_1e18 is None:
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

    if inspection.active_price_public_raw is None:
        return AuctionSettlementDecision(
            status="error",
            operation_type=None,
            token_address=active_token,
            reason="active auction price() read failed",
        )

    if inspection.minimum_price_public_raw is None:
        return AuctionSettlementDecision(
            status="error",
            operation_type=None,
            token_address=active_token,
            reason="auction want token metadata read failed",
        )

    if inspection.active_price_public_raw <= inspection.minimum_price_public_raw:
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

    if method == "sweep_and_settle" and allow_above_floor:
        return AuctionSettlementDecision(
            status="actionable",
            operation_type="sweep_and_settle",
            token_address=active_token,
            reason="forced sweep requested while auction is still active above minimumPrice",
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
