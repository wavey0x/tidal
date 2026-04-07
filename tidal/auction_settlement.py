"""Shared auction resolution discovery, classification, and calldata helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from eth_abi import decode as abi_decode
from eth_utils import to_checksum_address
from hexbytes import HexBytes

from tidal.chain.contracts.abis import AUCTION_KICKER_ABI
from tidal.chain.contracts.multicall import MulticallClient, MulticallRequest
from tidal.normalizers import normalize_address
from tidal.scanner.auction_state import AuctionStateReader

SettlementDecisionStatus = Literal["actionable", "noop", "error"]
SettlementOperationType = Literal["resolve_auction"]

PATH_NOOP = 0
PATH_SETTLE_ONLY = 1
PATH_SWEEP_ONLY = 2
PATH_SWEEP_AND_SETTLE = 3
PATH_RESET_ONLY = 4
PATH_SWEEP_AND_RESET = 5


@dataclass(slots=True)
class AuctionLotPreview:
    token_address: str
    path: int | None
    active: bool | None
    kicked_at: int | None
    balance_raw: int | None
    requires_force: bool | None
    receiver: str | None
    read_ok: bool
    error_message: str | None = None


@dataclass(slots=True)
class AuctionSettlementInspection:
    auction_address: str
    is_active_auction: bool | None
    enabled_tokens: tuple[str, ...]
    requested_token: str | None
    lot_previews: tuple[AuctionLotPreview, ...]

    def preview_for_token(self, token_address: str) -> AuctionLotPreview | None:
        normalized = normalize_address(token_address)
        for preview in self.lot_previews:
            if preview.token_address == normalized:
                return preview
        return None

    @property
    def preview_failures(self) -> tuple[AuctionLotPreview, ...]:
        return tuple(preview for preview in self.lot_previews if not preview.read_ok)


@dataclass(slots=True)
class AuctionSettlementOperation:
    operation_type: SettlementOperationType
    token_address: str
    path: int
    reason: str
    balance_raw: int
    requires_force: bool
    receiver: str | None


@dataclass(slots=True)
class AuctionSettlementDecision:
    status: SettlementDecisionStatus
    operations: tuple[AuctionSettlementOperation, ...]
    reason: str


@dataclass(slots=True)
class AuctionSettlementCall:
    operation_type: SettlementOperationType
    token_address: str
    target_address: str
    data: str
    force_live: bool


async def inspect_auction_settlements(  # noqa: ANN001
    web3_client,
    settings,
    auction_addresses: list[str],
    *,
    requested_tokens: dict[str, str | None] | None = None,
) -> dict[str, AuctionSettlementInspection]:
    normalized_auctions = [normalize_address(address) for address in auction_addresses]
    normalized_requested = {
        normalize_address(auction): normalize_address(token) if token else None
        for auction, token in (requested_tokens or {}).items()
    }
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

    active_flags, enabled_tokens_result = await asyncio.gather(
        reader.read_bool_noargs_many(normalized_auctions, "isAnActiveAuction"),
        reader.read_address_array_noargs_many(normalized_auctions, "getAllEnabledAuctions"),
    )

    preview_pairs: list[tuple[str, str]] = []
    ordered_tokens_by_auction: dict[str, list[str]] = {}
    for auction_address in normalized_auctions:
        tokens = set(enabled_tokens_result.get(auction_address) or [])
        requested_token = normalized_requested.get(auction_address)
        if requested_token is not None:
            tokens.add(requested_token)
        ordered_tokens = sorted(tokens)
        ordered_tokens_by_auction[auction_address] = ordered_tokens
        preview_pairs.extend((auction_address, token_address) for token_address in ordered_tokens)

    preview_results = await _read_preview_many(
        web3_client=web3_client,
        settings=settings,
        multicall_client=multicall_client,
        pairs=preview_pairs,
    )

    inspections: dict[str, AuctionSettlementInspection] = {}
    for auction_address in normalized_auctions:
        lot_previews = tuple(
            preview_results[(auction_address, token_address)]
            for token_address in ordered_tokens_by_auction.get(auction_address, [])
        )
        inspections[auction_address] = AuctionSettlementInspection(
            auction_address=auction_address,
            is_active_auction=active_flags.get(auction_address),
            enabled_tokens=tuple(sorted(enabled_tokens_result.get(auction_address) or [])),
            requested_token=normalized_requested.get(auction_address),
            lot_previews=lot_previews,
        )

    return inspections


async def inspect_auction_settlement(  # noqa: ANN001
    web3_client,
    settings,
    auction_address: str,
    token_address: str | None = None,
) -> AuctionSettlementInspection:
    normalized_auction = normalize_address(auction_address)
    normalized_token = normalize_address(token_address) if token_address else None
    results = await inspect_auction_settlements(
        web3_client,
        settings,
        [normalized_auction],
        requested_tokens={normalized_auction: normalized_token},
    )
    return results[normalized_auction]


def default_actionable_previews(inspection: AuctionSettlementInspection) -> tuple[AuctionLotPreview, ...]:
    return tuple(
        preview
        for preview in inspection.lot_previews
        if preview.read_ok and preview.path not in {None, PATH_NOOP} and preview.requires_force is False
    )


def live_funded_previews(inspection: AuctionSettlementInspection) -> tuple[AuctionLotPreview, ...]:
    return tuple(
        preview
        for preview in inspection.lot_previews
        if preview.read_ok and preview.requires_force is True
    )


def decide_auction_settlement(
    inspection: AuctionSettlementInspection,
    *,
    token_address: str | None = None,
    force: bool = False,
) -> AuctionSettlementDecision:
    requested_token = normalize_address(token_address) if token_address is not None else inspection.requested_token
    if force and requested_token is None:
        raise ValueError("force requires an explicit token")

    if requested_token is not None:
        preview = inspection.preview_for_token(requested_token)
        if preview is None or not preview.read_ok:
            return AuctionSettlementDecision(
                status="error",
                operations=(),
                reason="resolve preview failed for the requested token",
            )

        if preview.path in {None, PATH_NOOP}:
            other_resolvable = tuple(
                candidate
                for candidate in inspection.lot_previews
                if candidate.read_ok and candidate.token_address != requested_token and candidate.path not in {None, PATH_NOOP}
            )
            if other_resolvable:
                if len(other_resolvable) == 1:
                    return AuctionSettlementDecision(
                        status="error",
                        operations=(),
                        reason=(
                            f"requested token {to_checksum_address(requested_token)} does not match "
                            f"resolved token {to_checksum_address(other_resolvable[0].token_address)}"
                        ),
                    )
                return AuctionSettlementDecision(
                    status="error",
                    operations=(),
                    reason="requested token does not match any resolvable lot on this auction",
                )
            return AuctionSettlementDecision(
                status="noop",
                operations=(),
                reason="requested token has nothing to resolve",
            )

        if preview.requires_force and not force:
            return AuctionSettlementDecision(
                status="noop",
                operations=(),
                reason="requested token is live and in progress; pass --force to close it",
            )

        operation = _operation_from_preview(preview)
        return AuctionSettlementDecision(
            status="actionable",
            operations=(operation,),
            reason=operation.reason,
        )

    if inspection.preview_failures:
        return AuctionSettlementDecision(
            status="error",
            operations=(),
            reason="one or more enabled lot previews failed; retry or pass --token",
        )

    operations = tuple(_operation_from_preview(preview) for preview in default_actionable_previews(inspection))
    if operations:
        return AuctionSettlementDecision(
            status="actionable",
            operations=operations,
            reason=f"prepared {len(operations)} resolvable lot(s)",
        )

    if live_funded_previews(inspection):
        return AuctionSettlementDecision(
            status="noop",
            operations=(),
            reason="auction is progressing normally",
        )

    return AuctionSettlementDecision(
        status="noop",
        operations=(),
        reason="auction has nothing to resolve",
    )


def build_auction_settlement_calls(
    *,
    settings,
    web3_client,
    auction_address: str,
    decision: AuctionSettlementDecision,
) -> list[AuctionSettlementCall]:  # noqa: ANN001
    if decision.status != "actionable" or not decision.operations:
        raise ValueError("settlement calls require an actionable decision")

    normalized_auction = normalize_address(auction_address)
    kicker_address = normalize_address(settings.auction_kicker_address)
    contract = web3_client.contract(kicker_address, AUCTION_KICKER_ABI)

    calls: list[AuctionSettlementCall] = []
    for operation in decision.operations:
        tx_data = contract.functions.resolveAuction(
            to_checksum_address(normalized_auction),
            to_checksum_address(operation.token_address),
            operation.requires_force,
        )._encode_transaction_data()
        calls.append(
            AuctionSettlementCall(
                operation_type="resolve_auction",
                token_address=operation.token_address,
                target_address=kicker_address,
                data=tx_data,
                force_live=operation.requires_force,
            )
        )
    return calls


def build_auction_settlement_call(
    *,
    settings,
    web3_client,
    auction_address: str,
    decision: AuctionSettlementDecision,
) -> AuctionSettlementCall:  # noqa: ANN001
    calls = build_auction_settlement_calls(
        settings=settings,
        web3_client=web3_client,
        auction_address=auction_address,
        decision=decision,
    )
    if len(calls) != 1:
        raise ValueError("expected exactly one settlement call")
    return calls[0]


async def _read_preview_many(  # noqa: ANN001
    *,
    web3_client,
    settings,
    multicall_client: MulticallClient,
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], AuctionLotPreview]:
    output: dict[tuple[str, str], AuctionLotPreview] = {}
    if not pairs:
        return output

    kicker_address = normalize_address(settings.auction_kicker_address)
    kicker_contract = web3_client.contract(kicker_address, AUCTION_KICKER_ABI)

    if not settings.multicall_enabled:
        for auction_address, token_address in pairs:
            output[(auction_address, token_address)] = await _read_preview_direct(
                web3_client=web3_client,
                kicker_contract=kicker_contract,
                auction_address=auction_address,
                token_address=token_address,
            )
        return output

    requests: list[MulticallRequest] = []
    for auction_address, token_address in pairs:
        fn = kicker_contract.functions.previewResolveAuction(
            to_checksum_address(auction_address),
            to_checksum_address(token_address),
        )
        requests.append(
            MulticallRequest(
                target=kicker_address,
                call_data=bytes(HexBytes(fn._encode_transaction_data())),
                logical_key=(auction_address, token_address),
            )
        )

    results = await multicall_client.execute(
        requests,
        batch_size=settings.multicall_auction_batch_calls,
        allow_failure=True,
    )
    for result in results:
        auction_address, token_address = result.logical_key
        key = (auction_address, token_address)
        if not result.success:
            output[key] = AuctionLotPreview(
                token_address=token_address,
                path=None,
                active=None,
                kicked_at=None,
                balance_raw=None,
                requires_force=None,
                receiver=None,
                read_ok=False,
                error_message=result.error_message or "multicall preview failed",
            )
            continue
        output[key] = _decode_preview_result(token_address, result.return_data)
    return output


async def _read_preview_direct(  # noqa: ANN001
    *,
    web3_client,
    kicker_contract,
    auction_address: str,
    token_address: str,
) -> AuctionLotPreview:
    try:
        raw = await web3_client.call(
            kicker_contract.functions.previewResolveAuction(
                to_checksum_address(auction_address),
                to_checksum_address(token_address),
            )
        )
    except Exception as exc:  # noqa: BLE001
        return AuctionLotPreview(
            token_address=token_address,
            path=None,
            active=None,
            kicked_at=None,
            balance_raw=None,
            requires_force=None,
            receiver=None,
            read_ok=False,
            error_message=str(exc),
        )

    return AuctionLotPreview(
        token_address=token_address,
        path=int(raw[0]),
        active=bool(raw[1]),
        kicked_at=int(raw[2]),
        balance_raw=int(raw[3]),
        requires_force=bool(raw[4]),
        receiver=normalize_address(raw[5]),
        read_ok=True,
    )


def _decode_preview_result(token_address: str, return_data: bytes) -> AuctionLotPreview:
    decoded = abi_decode(["uint8", "bool", "uint256", "uint256", "bool", "address"], return_data)
    return AuctionLotPreview(
        token_address=token_address,
        path=int(decoded[0]),
        active=bool(decoded[1]),
        kicked_at=int(decoded[2]),
        balance_raw=int(decoded[3]),
        requires_force=bool(decoded[4]),
        receiver=normalize_address(decoded[5]),
        read_ok=True,
    )


def _operation_from_preview(preview: AuctionLotPreview) -> AuctionSettlementOperation:
    if preview.path is None:
        raise ValueError("cannot build an operation from a failed preview")

    return AuctionSettlementOperation(
        operation_type="resolve_auction",
        token_address=preview.token_address,
        path=preview.path,
        reason=path_reason(preview.path),
        balance_raw=int(preview.balance_raw or 0),
        requires_force=bool(preview.requires_force),
        receiver=preview.receiver,
    )


def path_reason(path: int) -> str:
    if path == PATH_SETTLE_ONLY:
        return "active sold-out lot"
    if path == PATH_SWEEP_ONLY:
        return "inactive lot with residual inventory"
    if path == PATH_SWEEP_AND_SETTLE:
        return "live funded lot"
    if path == PATH_RESET_ONLY:
        return "inactive kicked empty lot"
    if path == PATH_SWEEP_AND_RESET:
        return "inactive kicked lot with stranded inventory"
    if path == PATH_NOOP:
        return "clean lot"
    return f"unknown resolver path {path}"
