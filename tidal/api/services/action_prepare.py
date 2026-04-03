"""Prepare services for operator actions."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any

from eth_utils import to_checksum_address
from sqlalchemy import text
from sqlalchemy.orm import Session

from tidal.api.errors import APIError
from tidal.api.services.action_audit import create_prepared_action
from tidal.auction_settlement import (
    build_auction_settlement_call,
    decide_auction_settlement,
    inspect_auction_settlement,
)
from tidal.cli_support import build_sync_web3
from tidal.config import Settings
from tidal.normalizers import normalize_address
from tidal.ops.auction_enable import AuctionTokenEnabler, format_probe_reason
from tidal.ops.deploy import (
    SINGLE_AUCTION_FACTORY_ABI,
    build_default_salt,
    default_factory_address,
    default_governance_address,
    preview_deployment,
    read_existing_matches,
    read_factory_auction_addresses,
)
from tidal.ops.kick_inspect import inspect_kick_candidates
from tidal.pricing.token_price_agg import TokenPriceAggProvider
from tidal.runtime import build_txn_service, build_web3_client
from tidal.transaction_service.kick_shared import _GAS_ESTIMATE_BUFFER, _format_execution_error

STRATEGY_DEPLOY_CONTEXT_SQL = """
SELECT
    s.address AS strategy_address,
    s.name AS strategy_name,
    s.auction_address AS auction_address,
    s.want_address AS want_address,
    wt.symbol AS want_symbol,
    s.active AS active,
    stbl.token_address AS token_address,
    stbl.raw_balance AS raw_balance,
    stbl.normalized_balance AS normalized_balance,
    t.symbol AS token_symbol,
    t.decimals AS token_decimals,
    t.price_usd AS token_price_usd
FROM strategies s
LEFT JOIN tokens wt ON wt.address = s.want_address
LEFT JOIN strategy_token_balances_latest stbl ON stbl.strategy_address = s.address
LEFT JOIN tokens t ON t.address = stbl.token_address
WHERE s.address = :strategy_address
ORDER BY t.symbol, stbl.token_address
"""


async def prepare_kick_action(
    session: Session,
    settings: Settings,
    *,
    operator_id: str,
    source_type: str | None,
    source_address: str | None,
    auction_address: str | None,
    token_address: str | None,
    limit: int | None,
    sender: str | None,
    require_curve_quote: bool | None = None,
) -> tuple[str, list[str], dict[str, object]]:
    txn_service = build_txn_service(settings, session, require_curve_quote=require_curve_quote)
    planner = txn_service.planner
    plan = await planner.plan(
        source_type=source_type,  # type: ignore[arg-type]
        source_address=source_address,
        auction_address=auction_address,
        token_address=token_address,
        limit=limit,
        sender=sender,
        run_id="api-prepare",
        batch=True,
    )
    preview = plan.to_preview_payload()
    transactions = plan.to_transaction_payloads()
    warnings = list(plan.warnings)

    if not transactions:
        return plan.status(), warnings, {"preview": preview, "transactions": []}

    action_id = create_prepared_action(
        session,
        operator_id=operator_id,
        action_type="kick",
        sender=sender,
        request_payload={
            "sourceType": source_type,
            "sourceAddress": source_address,
            "auctionAddress": auction_address,
            "tokenAddress": token_address,
            "limit": limit,
            "sender": sender,
            "requireCurveQuote": require_curve_quote,
        },
        preview_payload=preview,
        transactions=transactions,
        resource_address=auction_address or source_address,
        auction_address=auction_address,
        source_address=source_address,
        token_address=token_address,
    )
    return "ok", warnings, {
        "actionId": action_id,
        "actionType": "kick",
        "preview": preview,
        "transactions": transactions,
    }


def inspect_kicks(
    session: Session,
    settings: Settings,
    *,
    source_type: str | None,
    source_address: str | None,
    auction_address: str | None,
    token_address: str | None,
    limit: int | None,
    include_live_inspection: bool,
) -> dict[str, object]:
    result = inspect_kick_candidates(
        session,
        settings,
        source_type=source_type,  # type: ignore[arg-type]
        source_address=source_address,
        auction_address=auction_address,
        token_address=token_address,
        limit=limit,
        include_live_inspection=include_live_inspection,
    )
    return _serialize(result)


async def load_strategy_deploy_defaults(
    session: Session,
    settings: Settings,
    *,
    strategy_address: str,
) -> dict[str, object]:
    normalized_strategy = normalize_address(strategy_address).lower()
    rows = session.execute(
        text(STRATEGY_DEPLOY_CONTEXT_SQL),
        {"strategy_address": normalized_strategy},
    ).mappings().all()
    if not rows:
        raise APIError("Strategy not found", status_code=404)

    first = rows[0]
    context = {
        "strategyAddress": normalize_address(str(first["strategy_address"])),
        "strategyName": first["strategy_name"],
        "auctionAddress": _optional_normalize_address(first["auction_address"]),
        "wantAddress": _optional_normalize_address(first["want_address"]),
        "wantSymbol": first["want_symbol"],
        "active": bool(first["active"]) if first["active"] is not None else None,
        "balances": [],
    }
    for row in rows:
        if not row["token_address"]:
            continue
        context["balances"].append(
            {
                "tokenAddress": normalize_address(str(row["token_address"])),
                "rawBalance": row["raw_balance"],
                "normalizedBalance": row["normalized_balance"],
                "tokenSymbol": row["token_symbol"],
                "tokenDecimals": row["token_decimals"],
                "priceUsd": row["token_price_usd"],
            }
        )

    warnings: list[str] = []
    if context["auctionAddress"]:
        warnings.append("Strategy already has an auction mapped.")
    if not context["wantAddress"]:
        raise APIError("Strategy is missing want token metadata", status_code=409)

    balance = _select_deploy_balance(context)
    quote_provider = TokenPriceAggProvider(
        chain_id=settings.chain_id,
        base_url=settings.token_price_agg_base_url,
        api_key=settings.token_price_agg_key,
        timeout_seconds=settings.price_timeout_seconds,
        retry_attempts=settings.price_retry_attempts,
    )
    quote = await quote_provider.quote(
        token_in=str(balance["tokenAddress"]),
        token_out=str(context["wantAddress"]),
        amount_in=str(balance["rawBalance"]),
    )
    await quote_provider.close()
    starting_price = _compute_starting_price(
        quote.amount_out_raw,
        quote.token_out_decimals,
        buffer_bps=settings.txn_start_price_buffer_bps,
    )
    curve_quote_available = quote.curve_quote_available()
    curve_status = quote.provider_statuses.get("curve") or ("ok" if curve_quote_available else "not present")
    if not curve_quote_available:
        warnings.append(f"Curve quote unavailable for deploy inference (status: {curve_status})")

    w3 = build_sync_web3(settings)
    factory_address = default_factory_address(settings)
    governance_address = default_governance_address()
    existing_auctions = read_factory_auction_addresses(w3, factory_address)
    matches = read_existing_matches(
        w3,
        settings,
        factory_address=factory_address,
        auction_addresses=existing_auctions,
        want=str(context["wantAddress"]),
        receiver=str(context["strategyAddress"]),
        governance=governance_address,
    )
    salt = build_default_salt(str(context["wantAddress"]), str(context["strategyAddress"]), governance_address)
    preview = preview_deployment(
        w3,
        settings,
        factory_address=factory_address,
        want=str(context["wantAddress"]),
        receiver=str(context["strategyAddress"]),
        governance=governance_address,
        starting_price=starting_price,
        salt=salt,
        sender_address=None,
    )
    return {
        "strategyAddress": context["strategyAddress"],
        "strategyName": context["strategyName"],
        "auctionAddress": context["auctionAddress"],
        "receiverAddress": context["strategyAddress"],
        "wantAddress": context["wantAddress"],
        "wantSymbol": context["wantSymbol"],
        "factoryAddress": factory_address,
        "governanceAddress": governance_address,
        "startingPrice": starting_price,
        "salt": salt,
        "warnings": warnings,
        "inference": {
            "sellTokenAddress": balance["tokenAddress"],
            "sellTokenSymbol": balance["tokenSymbol"],
            "rawBalance": balance["rawBalance"],
            "normalizedBalance": balance["normalizedBalance"],
            "priceUsd": balance["priceUsd"],
            "usdValue": balance["usdValue"],
            "quoteAmountOutRaw": str(quote.amount_out_raw) if quote.amount_out_raw is not None else None,
            "quoteRequestUrl": quote.request_url,
            "curveQuoteAvailable": curve_quote_available,
            "curveQuoteStatus": curve_status,
            "providerStatuses": quote.provider_statuses,
        },
        "predictedAuctionAddress": preview.predicted_address,
        "predictedAuctionAddressExists": preview.predicted_address_exists,
        "matchingAuctions": [_serialize(match) for match in matches],
    }


async def prepare_deploy_action(
    settings: Settings,
    session: Session,
    *,
    operator_id: str,
    want: str,
    receiver: str,
    sender: str | None,
    factory: str | None,
    governance: str | None,
    starting_price: int,
    salt: str | None,
) -> tuple[str, list[str], dict[str, object]]:
    warnings, request_payload, preview_payload, tx = _build_deploy_prepare_payload(
        settings,
        want=want,
        receiver=receiver,
        sender=sender,
        factory=factory,
        governance=governance,
        starting_price=starting_price,
        salt=salt,
    )
    action_id = create_prepared_action(
        session,
        operator_id=operator_id,
        action_type="deploy",
        sender=sender,
        request_payload=request_payload,
        preview_payload=preview_payload,
        transactions=[tx],
        resource_address=str(request_payload["receiver"]),
        auction_address=_optional_normalize_address(preview_payload["predictedAuctionAddress"]),
        source_address=str(request_payload["receiver"]),
    )
    return "ok", warnings, {
        "actionId": action_id,
        "actionType": "deploy",
        "preview": preview_payload,
        "transactions": [tx],
    }


async def prepare_deploy_browser_action(
    settings: Settings,
    *,
    want: str,
    receiver: str,
    sender: str | None,
    factory: str | None,
    governance: str | None,
    starting_price: int,
    salt: str | None,
) -> tuple[str, list[str], dict[str, object]]:
    warnings, _, preview_payload, tx = _build_deploy_prepare_payload(
        settings,
        want=want,
        receiver=receiver,
        sender=sender,
        factory=factory,
        governance=governance,
        starting_price=starting_price,
        salt=salt,
    )
    return "ok", warnings, {
        "actionType": "deploy",
        "preview": preview_payload,
        "transactions": [tx],
    }


def _build_deploy_prepare_payload(
    settings: Settings,
    *,
    want: str,
    receiver: str,
    sender: str | None,
    factory: str | None,
    governance: str | None,
    starting_price: int,
    salt: str | None,
) -> tuple[list[str], dict[str, object], dict[str, object], dict[str, object]]:
    w3 = build_sync_web3(settings)
    normalized_want = normalize_address(want)
    normalized_receiver = normalize_address(receiver)
    normalized_factory = normalize_address(factory) if factory else default_factory_address(settings)
    normalized_governance = normalize_address(governance) if governance else default_governance_address()
    resolved_salt = salt or build_default_salt(normalized_want, normalized_receiver, normalized_governance)
    preview = preview_deployment(
        w3,
        settings,
        factory_address=normalized_factory,
        want=normalized_want,
        receiver=normalized_receiver,
        governance=normalized_governance,
        starting_price=starting_price,
        salt=resolved_salt,
        sender_address=sender,
    )
    factory_contract = w3.eth.contract(address=to_checksum_address(normalized_factory), abi=SINGLE_AUCTION_FACTORY_ABI)
    data = factory_contract.functions.createNewAuction(
        to_checksum_address(normalized_want),
        to_checksum_address(normalized_receiver),
        to_checksum_address(normalized_governance),
        starting_price,
        bytes.fromhex(resolved_salt.removeprefix("0x")),
    )._encode_transaction_data()
    warnings = [line for line in (
        f"Preview call failed: {preview.preview_error}" if preview.preview_error else None,
        f"Gas estimate failed: {preview.gas_error}" if preview.gas_error else None,
    ) if line]
    tx = {
        "operation": "deploy",
        "to": normalized_factory,
        "data": data,
        "value": "0x0",
        "chainId": settings.chain_id,
        "sender": sender,
        "gasEstimate": preview.gas_estimate,
        "gasLimit": min(int(preview.gas_estimate * _GAS_ESTIMATE_BUFFER), settings.txn_max_gas_limit) if preview.gas_estimate is not None else None,
    }
    request_payload = {
        "want": normalized_want,
        "receiver": normalized_receiver,
        "sender": sender,
        "factory": normalized_factory,
        "governance": normalized_governance,
        "startingPrice": starting_price,
        "salt": resolved_salt,
    }
    preview_payload = {
        "factoryAddress": normalized_factory,
        "want": normalized_want,
        "receiver": normalized_receiver,
        "governance": normalized_governance,
        "startingPrice": starting_price,
        "salt": resolved_salt,
        "predictedAuctionAddress": preview.predicted_address,
        "predictedAuctionAddressExists": preview.predicted_address_exists,
        "existingMatches": [_serialize(match) for match in preview.existing_matches],
    }
    return warnings, request_payload, preview_payload, tx


async def prepare_enable_tokens_action(
    settings: Settings,
    session: Session,
    *,
    operator_id: str,
    auction_address: str,
    sender: str | None,
    extra_tokens: list[str],
) -> tuple[str, list[str], dict[str, object]]:
    w3 = build_sync_web3(settings)
    enabler = AuctionTokenEnabler(w3, settings)
    normalized_auction = normalize_address(auction_address)
    inspection = enabler.inspect_auction(normalized_auction)
    source = enabler.resolve_source(inspection)
    discovery = enabler.discover_tokens(
        inspection=inspection,
        source=source,
        manual_tokens=[normalize_address(value) for value in extra_tokens],
    )
    probes = enabler.probe_tokens(
        inspection=inspection,
        source=source,
        discovery=discovery,
    )
    warnings = list(source.warnings) + list(discovery.notes)
    if not inspection.in_configured_factory:
        warnings.append("Auction is not in the configured factory.")
    eligible = [probe for probe in probes if probe.status == "eligible"]
    if not eligible:
        return "noop", warnings, {
            "preview": {
                "inspection": _serialize(inspection),
                "source": _serialize(source),
                "probes": [_serialize(probe) for probe in probes],
                "selectedTokens": [],
            },
            "transactions": [],
        }

    selected_tokens = [probe.token_address for probe in eligible]
    preview_payload = {
        "inspection": _serialize(inspection),
        "source": _serialize(source),
        "probes": [
            {
                **_serialize(probe),
                "reasonLabel": format_probe_reason(probe.reason),
            }
            for probe in probes
        ],
        "selectedTokens": selected_tokens,
    }

    try:
        execution_plan = enabler.build_execution_plan(
            inspection=inspection,
            tokens=selected_tokens,
            caller_address=sender,
        )
    except RuntimeError as exc:
        warnings.append(str(exc))
        return "error", warnings, {
            "preview": preview_payload,
            "transactions": [],
        }

    if execution_plan.error_message:
        warnings.append(execution_plan.error_message)

    preview_payload.update(
        {
            "executionTarget": execution_plan.to_address,
            "previewSender": sender,
            "previewSenderAuthorized": execution_plan.sender_authorized,
            "authorizationTarget": execution_plan.authorization_target,
            "executionPreview": {
                "call_succeeded": execution_plan.call_succeeded,
                "gas_estimate": execution_plan.gas_estimate,
                "error_message": execution_plan.error_message,
            },
        }
    )
    tx = {
        "operation": "enable-tokens",
        "to": normalize_address(execution_plan.to_address),
        "data": execution_plan.data,
        "value": "0x0",
        "chainId": settings.chain_id,
        "sender": sender,
        "gasEstimate": execution_plan.gas_estimate,
        "gasLimit": (
            min(int(execution_plan.gas_estimate * _GAS_ESTIMATE_BUFFER), settings.txn_max_gas_limit)
            if execution_plan.gas_estimate is not None
            else None
        ),
    }
    action_id = create_prepared_action(
        session,
        operator_id=operator_id,
        action_type="enable_tokens",
        sender=sender,
        request_payload={
            "auctionAddress": normalized_auction,
            "sender": sender,
            "extraTokens": extra_tokens,
        },
        preview_payload=preview_payload,
        transactions=[tx],
        resource_address=normalized_auction,
        auction_address=normalized_auction,
        source_address=source.source_address,
    )
    return "ok", warnings, {
        "actionId": action_id,
        "actionType": "enable_tokens",
        "preview": preview_payload,
        "transactions": [tx],
    }


async def prepare_settle_action(
    settings: Settings,
    session: Session,
    *,
    operator_id: str,
    auction_address: str,
    sender: str | None,
    token_address: str | None,
    sweep: bool,
) -> tuple[str, list[str], dict[str, object]]:
    normalized_auction = normalize_address(auction_address)
    normalized_token = normalize_address(token_address) if token_address else None
    web3_client = build_web3_client(settings)
    inspection = await inspect_auction_settlement(web3_client, settings, normalized_auction)
    decision = decide_auction_settlement(
        inspection,
        token_address=normalized_token,
        method="sweep_and_settle" if sweep else "auto",
        allow_above_floor=sweep,
    )
    preview_payload = {
        "inspection": _serialize(inspection),
        "decision": _serialize(decision),
        "requestedSweep": sweep,
    }
    if decision.status == "noop":
        return "noop", [], {"preview": preview_payload, "transactions": []}
    if decision.status == "error":
        raise APIError(decision.reason, status_code=409)

    settlement_call = build_auction_settlement_call(
        settings=settings,
        web3_client=web3_client,
        auction_address=normalized_auction,
        decision=decision,
    )
    gas_estimate, gas_limit, gas_warning = await _estimate_transaction(
        web3_client,
        settings,
        sender=sender,
        to_address=settlement_call.target_address,
        data=settlement_call.data,
        gas_cap=settings.txn_max_gas_limit,
    )
    warnings = [gas_warning] if gas_warning else []
    if (
        sweep
        and inspection.active_available_raw
        and inspection.active_price_public_raw is not None
        and inspection.minimum_price_public_raw is not None
        and inspection.active_price_public_raw > inspection.minimum_price_public_raw
    ):
        warnings.append(
            "Forced sweep requested while auction is still above floor; unsold tokens will be returned to the receiver."
        )
    tx = {
        "operation": settlement_call.operation_type.replace("_", "-"),
        "to": normalize_address(settlement_call.target_address),
        "data": settlement_call.data,
        "value": "0x0",
        "chainId": settings.chain_id,
        "sender": sender,
        "gasEstimate": gas_estimate,
        "gasLimit": gas_limit,
    }
    action_id = create_prepared_action(
        session,
        operator_id=operator_id,
        action_type="settle",
        sender=sender,
        request_payload={
            "auctionAddress": normalized_auction,
            "sender": sender,
            "tokenAddress": normalized_token,
            "sweep": sweep,
        },
        preview_payload=preview_payload,
        transactions=[tx],
        resource_address=normalized_auction,
        auction_address=normalized_auction,
        token_address=decision.token_address,
    )
    return "ok", warnings, {
        "actionId": action_id,
        "actionType": "settle",
        "preview": preview_payload,
        "transactions": [tx],
    }


async def _estimate_transaction(
    web3_client,
    settings: Settings,
    *,
    sender: str | None,
    to_address: str,
    data: str,
    gas_cap: int,
) -> tuple[int | None, int | None, str | None]:
    if sender is None:
        return None, None, "No sender provided for gas estimation."
    try:
        gas_estimate = await web3_client.estimate_gas(
            {
                "from": to_checksum_address(sender),
                "to": to_checksum_address(to_address),
                "data": data,
                "chainId": settings.chain_id,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return None, None, f"Gas estimate failed: {_format_execution_error(exc)}"
    gas_limit = min(int(gas_estimate * _GAS_ESTIMATE_BUFFER), gas_cap)
    return gas_estimate, gas_limit, None


def _serialize(value: object) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def _optional_normalize_address(value: object) -> str | None:
    if not value:
        return None
    return normalize_address(str(value))


def _select_deploy_balance(strategy_context: dict[str, object]) -> dict[str, object]:
    want_address = str(strategy_context["wantAddress"])
    candidates = []
    for balance in strategy_context["balances"]:  # type: ignore[index]
        token_address = str(balance["tokenAddress"])
        if token_address.lower() == want_address.lower():
            continue
        raw_balance = _parse_decimal(balance["rawBalance"])
        normalized_balance = _parse_decimal(balance["normalizedBalance"])
        price_usd = _parse_decimal(balance["priceUsd"])
        if raw_balance is None or normalized_balance is None or price_usd is None:
            continue
        if raw_balance <= 0 or normalized_balance <= 0 or price_usd <= 0:
            continue
        usd_value = normalized_balance * price_usd
        candidates.append({**balance, "usdValue": str(usd_value)})

    candidates.sort(key=lambda item: (-Decimal(str(item["usdValue"])), str(item["tokenAddress"]).lower()))
    if not candidates:
        raise APIError(
            "No eligible priced non-want token balance is available to infer deploy starting price",
            status_code=409,
        )
    return candidates[0]


def _compute_starting_price(amount_out_raw: int | None, token_out_decimals: int | None, *, buffer_bps: int) -> int:
    parsed_amount = _parse_decimal(amount_out_raw)
    if parsed_amount is None or parsed_amount <= 0:
        raise APIError("Quote amount is missing or zero", status_code=409)
    if token_out_decimals is None:
        raise APIError("Quote response is missing output token decimals", status_code=502)
    normalized = parsed_amount / (Decimal(10) ** int(token_out_decimals))
    buffer = Decimal(1) + Decimal(buffer_bps) / Decimal(10_000)
    starting_price = int((normalized * buffer).to_integral_value(rounding=ROUND_CEILING))
    if starting_price <= 0:
        raise APIError("Computed starting price is zero", status_code=409)
    return starting_price


def _parse_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
