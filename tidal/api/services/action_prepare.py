"""Prepare services for operator actions."""

from __future__ import annotations

from copy import copy
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any

from eth_utils import to_checksum_address
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from tidal.api.errors import APIError
from tidal.api.services.action_audit import create_prepared_action
from tidal.auction_settlement import (
    PATH_SWEEP_AND_RESET,
    build_auction_sweep_call,
    build_auction_settlement_calls,
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
from tidal.persistence import models
from tidal.persistence.repositories import TokenRepository
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


@dataclass(slots=True)
class _AuctionAddressResolution:
    auction_address: str
    warnings: list[str]


@dataclass(slots=True)
class _EnableTokenBatch:
    tokens: list[str]
    execution_plan: Any


def _settings_with_kick_overrides(
    settings: Settings,
    *,
    txn_max_gas_limit: int | None,
    min_usd_value: float | None,
) -> Settings:
    updates: dict[str, object] = {}
    if txn_max_gas_limit is not None:
        updates["txn_max_gas_limit"] = int(txn_max_gas_limit)
    if min_usd_value is not None:
        updates["txn_usd_threshold"] = float(min_usd_value)

    if not updates:
        return settings

    if hasattr(settings, "model_copy"):
        return settings.model_copy(update=updates)
    cloned = copy(settings)
    for key, value in updates.items():
        setattr(cloned, key, value)
    return cloned


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
    min_usd_value: float | None = None,
    require_curve_quote: bool | None = None,
    txn_max_gas_limit: int | None = None,
    allow_killed_gauge: bool = False,
) -> tuple[str, list[str], dict[str, object]]:
    effective_settings = _settings_with_kick_overrides(
        settings,
        txn_max_gas_limit=txn_max_gas_limit,
        min_usd_value=min_usd_value,
    )
    txn_service = build_txn_service(effective_settings, session, require_curve_quote=require_curve_quote)
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
        allow_killed_gauge=allow_killed_gauge,
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
            "minUsdValue": min_usd_value,
            "sender": sender,
            "requireCurveQuote": require_curve_quote,
            "txnMaxGasLimit": effective_settings.txn_max_gas_limit,
            "allowKilledGauge": allow_killed_gauge,
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
    min_usd_value: float | None,
    include_live_inspection: bool,
) -> dict[str, object]:
    effective_settings = _settings_with_kick_overrides(
        settings,
        txn_max_gas_limit=None,
        min_usd_value=min_usd_value,
    )
    result = inspect_kick_candidates(
        session,
        effective_settings,
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


def _load_fee_burner_alias_rows(session: Session, normalized_address: str) -> list[dict[str, object]]:
    try:
        result = session.execute(
            select(
                models.fee_burners.c.address,
                models.fee_burners.c.name,
                models.fee_burners.c.auction_address,
                models.fee_burners.c.want_address,
                models.fee_burners.c.auction_error_message,
            ).where(
                or_(
                    models.fee_burners.c.address == normalized_address,
                    models.fee_burners.c.want_address == normalized_address,
                )
            )
        )
        return [dict(row) for row in result.mappings().all()]
    except Exception:  # noqa: BLE001
        return []


def _resolve_enable_tokens_auction_alias(
    session: Session,
    settings: Settings,
    requested_address: str,
) -> _AuctionAddressResolution | None:
    normalized_address = normalize_address(requested_address)
    candidates: dict[str, dict[str, object]] = {}

    for fee_burner in getattr(settings, "monitored_fee_burners", []) or []:
        try:
            fee_burner_address = normalize_address(fee_burner.address)
            want_address = normalize_address(fee_burner.want_address)
        except Exception:  # noqa: BLE001
            continue
        if normalized_address not in {fee_burner_address, want_address}:
            continue
        candidates[fee_burner_address] = {
            "address": fee_burner_address,
            "want_address": want_address,
            "name": fee_burner.label or "fee burner",
            "role": "fee burner address" if normalized_address == fee_burner_address else "want token",
            "auction_address": None,
            "auction_error_message": None,
        }

    for row in _load_fee_burner_alias_rows(session, normalized_address):
        try:
            fee_burner_address = normalize_address(str(row["address"]))
        except Exception:  # noqa: BLE001
            continue
        try:
            want_address = normalize_address(str(row["want_address"])) if row.get("want_address") else None
        except Exception:  # noqa: BLE001
            want_address = None
        if normalized_address not in {fee_burner_address, want_address}:
            continue
        candidate = candidates.setdefault(
            fee_burner_address,
            {
                "address": fee_burner_address,
                "want_address": want_address,
                "name": row.get("name") or "fee burner",
                "role": "fee burner address" if normalized_address == fee_burner_address else "want token",
                "auction_address": None,
                "auction_error_message": None,
            },
        )
        candidate["name"] = row.get("name") or candidate.get("name") or "fee burner"
        candidate["want_address"] = want_address or candidate.get("want_address")
        candidate["auction_address"] = row.get("auction_address") or candidate.get("auction_address")
        candidate["auction_error_message"] = row.get("auction_error_message") or candidate.get("auction_error_message")

    if not candidates:
        return None

    if len(candidates) > 1:
        raise RuntimeError(
            f"{to_checksum_address(normalized_address)} matches multiple fee burners; pass the auction address"
        )

    candidate = next(iter(candidates.values()))
    source_name = str(candidate.get("name") or "fee burner").strip() or "fee burner"
    role = str(candidate.get("role") or "fee burner address")
    raw_auction_address = candidate.get("auction_address")
    if raw_auction_address:
        try:
            auction_address = normalize_address(str(raw_auction_address))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"cached auction mapping for {source_name} is invalid; run tidal-server scan run"
            ) from exc
        return _AuctionAddressResolution(
            auction_address=auction_address,
            warnings=[f"Resolved {source_name} {role} to auction {to_checksum_address(auction_address)}."],
        )

    message = (
        f"{to_checksum_address(normalized_address)} is {source_name}'s {role}, not an auction address; "
        "no cached auction mapping is available. Run tidal-server scan run, or pass the auction address."
    )
    mapping_error = str(candidate.get("auction_error_message") or "").strip()
    if mapping_error:
        message = f"{message} Last mapping error: {mapping_error}"
    raise RuntimeError(message)


def _gas_limit_from_estimate(gas_estimate: int | None, *, gas_cap: int) -> int | None:
    if gas_estimate is None:
        return None
    return min(int(gas_estimate * _GAS_ESTIMATE_BUFFER), gas_cap)


def _enable_tokens_transaction(
    settings: Settings,
    *,
    execution_plan: Any,
    sender: str | None,
    gas_cap: int,
) -> dict[str, object]:
    return {
        "operation": "enable-tokens",
        "to": normalize_address(execution_plan.to_address),
        "data": execution_plan.data,
        "value": "0x0",
        "chainId": settings.chain_id,
        "sender": sender,
        "gasEstimate": execution_plan.gas_estimate,
        "gasLimit": _gas_limit_from_estimate(execution_plan.gas_estimate, gas_cap=gas_cap),
    }


def _enable_token_prepared_operations(
    *,
    normalized_auction: str,
    source: Any,
    inspection: Any,
    probes_by_token: dict[str, Any],
    batches: list[_EnableTokenBatch],
) -> list[dict[str, object]]:
    prepared_operations: list[dict[str, object]] = []
    for tx_index, batch in enumerate(batches):
        for token_address in batch.tokens:
            probe = probes_by_token[token_address]
            prepared_operations.append(
                {
                    "operation": "enable-tokens",
                    "txIndex": tx_index,
                    "auctionAddress": normalized_auction,
                    "sourceType": source.source_type,
                    "sourceAddress": source.source_address,
                    "sourceName": source.source_name,
                    "tokenAddress": token_address,
                    "tokenSymbol": probe.symbol,
                    "wantAddress": inspection.want,
                    "balanceRaw": str(probe.raw_balance) if probe.raw_balance is not None else None,
                    "normalizedBalance": probe.normalized_balance,
                    "reason": probe.reason,
                }
            )
    return prepared_operations


def _build_enable_token_batches(
    enabler: AuctionTokenEnabler,
    *,
    inspection: Any,
    selected_tokens: list[str],
    caller_address: str | None,
    gas_cap: int,
) -> list[_EnableTokenBatch]:
    batches: list[_EnableTokenBatch] = []
    current_tokens: list[str] = []
    current_plan: Any | None = None

    for token_address in selected_tokens:
        tentative_tokens = [*current_tokens, token_address]
        tentative_plan = enabler.build_execution_plan(
            inspection=inspection,
            tokens=tentative_tokens,
            caller_address=caller_address,
        )
        if (
            tentative_plan.gas_estimate is not None
            and int(tentative_plan.gas_estimate) > gas_cap
            and current_tokens
        ):
            if current_plan is not None:
                batches.append(_EnableTokenBatch(tokens=current_tokens, execution_plan=current_plan))
            tentative_tokens = [token_address]
            tentative_plan = enabler.build_execution_plan(
                inspection=inspection,
                tokens=tentative_tokens,
                caller_address=caller_address,
            )

        if tentative_plan.gas_estimate is not None and int(tentative_plan.gas_estimate) > gas_cap:
            raise RuntimeError(
                "enable-tokens batch for "
                f"{to_checksum_address(token_address)} estimates {int(tentative_plan.gas_estimate):,} gas, "
                f"above txn_max_gas_limit {gas_cap:,}"
            )

        current_tokens = tentative_tokens
        current_plan = tentative_plan

    if current_plan is not None:
        batches.append(_EnableTokenBatch(tokens=current_tokens, execution_plan=current_plan))
    return batches


async def prepare_enable_tokens_action(
    settings: Settings,
    session: Session,
    *,
    operator_id: str,
    auction_address: str,
    sender: str | None,
    extra_tokens: list[str],
    txn_max_gas_limit: int | None = None,
) -> tuple[str, list[str], dict[str, object]]:
    w3 = build_sync_web3(settings)
    enabler = AuctionTokenEnabler(w3, settings)
    normalized_auction = normalize_address(auction_address)
    resolution_warnings: list[str] = []
    try:
        resolution = _resolve_enable_tokens_auction_alias(session, settings, normalized_auction)
    except RuntimeError as exc:
        return "error", [str(exc)], {
            "preview": {},
            "transactions": [],
        }
    if resolution is not None:
        normalized_auction = resolution.auction_address
        resolution_warnings.extend(resolution.warnings)

    try:
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
    except RuntimeError as exc:
        return "error", [*resolution_warnings, str(exc)], {
            "preview": {},
            "transactions": [],
        }

    warnings = resolution_warnings + list(source.warnings) + list(discovery.notes)
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
    gas_cap = int(txn_max_gas_limit or settings.txn_max_gas_limit)
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
        batches = _build_enable_token_batches(
            enabler,
            inspection=inspection,
            selected_tokens=selected_tokens,
            caller_address=sender,
            gas_cap=gas_cap,
        )
    except RuntimeError as exc:
        warnings.append(str(exc))
        return "error", warnings, {
            "preview": preview_payload,
            "transactions": [],
        }

    execution_plans = [batch.execution_plan for batch in batches]
    execution_plan = execution_plans[0]
    seen_execution_errors: set[str] = set()
    for plan in execution_plans:
        if plan.error_message and plan.error_message not in seen_execution_errors:
            warnings.append(plan.error_message)
            seen_execution_errors.add(plan.error_message)

    execution_gas_estimate = (
        sum(int(plan.gas_estimate) for plan in execution_plans)
        if all(plan.gas_estimate is not None for plan in execution_plans)
        else None
    )
    execution_error_message = next((plan.error_message for plan in execution_plans if plan.error_message), None)
    probes_by_token = {probe.token_address: probe for probe in eligible}
    prepared_operations = _enable_token_prepared_operations(
        normalized_auction=normalized_auction,
        source=source,
        inspection=inspection,
        probes_by_token=probes_by_token,
        batches=batches,
    )

    preview_payload.update(
        {
            "executionTarget": execution_plan.to_address,
            "previewSender": sender,
            "previewSenderAuthorized": execution_plan.sender_authorized,
            "authorizationTarget": execution_plan.authorization_target,
            "executionPreview": {
                "call_succeeded": all(plan.call_succeeded for plan in execution_plans),
                "gas_estimate": execution_gas_estimate,
                "error_message": execution_error_message,
            },
            "executionBatches": [
                {
                    "tokens": batch.tokens,
                    "call_succeeded": batch.execution_plan.call_succeeded,
                    "gas_estimate": batch.execution_plan.gas_estimate,
                    "error_message": batch.execution_plan.error_message,
                }
                for batch in batches
            ],
            "preparedOperations": prepared_operations,
            "txnMaxGasLimit": gas_cap,
        }
    )
    transactions = [
        _enable_tokens_transaction(settings, execution_plan=batch.execution_plan, sender=sender, gas_cap=gas_cap)
        for batch in batches
    ]
    action_id = create_prepared_action(
        session,
        operator_id=operator_id,
        action_type="enable_tokens",
        sender=sender,
        request_payload={
            "auctionAddress": normalized_auction,
            "sender": sender,
            "extraTokens": extra_tokens,
            "txnMaxGasLimit": gas_cap,
        },
        preview_payload=preview_payload,
        transactions=transactions,
        resource_address=normalized_auction,
        auction_address=normalized_auction,
        source_address=source.source_address,
    )
    return "ok", warnings, {
        "actionId": action_id,
        "actionType": "enable_tokens",
        "preview": preview_payload,
        "transactions": transactions,
    }


async def prepare_settle_action(
    settings: Settings,
    session: Session,
    *,
    operator_id: str,
    auction_address: str,
    sender: str | None,
    token_address: str | None,
    force: bool,
) -> tuple[str, list[str], dict[str, object]]:
    normalized_auction = normalize_address(auction_address)
    normalized_token = normalize_address(token_address) if token_address else None
    if force and normalized_token is None:
        raise APIError("force requires tokenAddress", status_code=422)
    web3_client = build_web3_client(settings)
    inspection = await inspect_auction_settlement(
        web3_client,
        settings,
        normalized_auction,
        token_address=normalized_token,
    )
    decision = decide_auction_settlement(
        inspection,
        token_address=normalized_token,
        force=force,
    )
    def _prepared_operation_payload(operation, tx_index: int | None = None) -> dict[str, object]:  # noqa: ANN001
        payload = {
            "operation": "resolve-auction",
            "auctionAddress": normalized_auction,
            "tokenAddress": operation.token_address,
            "reason": operation.reason,
            "path": operation.path,
            "requiresForce": operation.requires_force,
            "balanceRaw": str(operation.balance_raw),
            "receiver": operation.receiver,
        }
        if tx_index is not None:
            payload["txIndex"] = tx_index
        return payload

    preview_payload = {
        "inspection": _serialize(inspection),
        "decision": _serialize(decision),
        "requestedForce": force,
        "preparedOperations": [_prepared_operation_payload(operation) for operation in decision.operations],
    }
    if decision.status == "noop":
        return "noop", [], {"preview": preview_payload, "transactions": []}
    if decision.status == "error":
        raise APIError(decision.reason, status_code=409)

    settlement_calls = build_auction_settlement_calls(
        settings=settings,
        web3_client=web3_client,
        auction_address=normalized_auction,
        decision=decision,
    )
    warnings: list[str] = []
    transactions: list[dict[str, object]] = []
    prepared_operations: list[dict[str, object]] = []
    operation_by_token = {operation.token_address: operation for operation in decision.operations}
    manual_sweep_required = False
    token_repo = TokenRepository(session)
    for settlement_call in settlement_calls:
        matching_operation = operation_by_token.get(settlement_call.token_address)
        gas_estimate, gas_limit, gas_warning = await _estimate_transaction(
            web3_client,
            settings,
            sender=sender,
            to_address=settlement_call.target_address,
            data=settlement_call.data,
            gas_cap=settings.txn_max_gas_limit,
        )
        if gas_warning and gas_warning not in warnings:
            warnings.append(gas_warning)
        if (
            matching_operation is not None
            and matching_operation.path == PATH_SWEEP_AND_RESET
            and gas_warning
            and "Amount is zero." in gas_warning
        ):
            token_meta = token_repo.get(matching_operation.token_address)
            token_label = token_meta.symbol if token_meta is not None and token_meta.symbol else matching_operation.token_address
            manual_sweep_command = (
                f"tidal auction sweep {to_checksum_address(normalized_auction)} "
                f"--token {to_checksum_address(matching_operation.token_address)}"
            )
            hint = f"Resolve failed for {token_label}. This token may require a manual sweep."
            if hint not in warnings:
                warnings.append(hint)
            next_step = f"Next Step: {manual_sweep_command}"
            if next_step not in warnings:
                warnings.append(next_step)
            manual_sweep_required = True
            continue
        tx_index = len(transactions)
        transactions.append(
            {
                "operation": settlement_call.operation_type.replace("_", "-"),
                "to": normalize_address(settlement_call.target_address),
                "data": settlement_call.data,
                "value": "0x0",
                "chainId": settings.chain_id,
                "sender": sender,
                "gasEstimate": gas_estimate,
                "gasLimit": gas_limit,
            }
        )
        if matching_operation is not None:
            prepared_operations.append(_prepared_operation_payload(matching_operation, tx_index=tx_index))

    preview_decision = _serialize(decision)
    if manual_sweep_required and not transactions:
        preview_decision = {
            "status": "noop",
            "operations": [],
            "reason": "manual sweep required before settlement",
        }
    elif prepared_operations and len(prepared_operations) != len(decision.operations):
        preview_decision = {
            "status": "actionable",
            "operations": [],
            "reason": f"prepared {len(prepared_operations)} resolvable lot(s)",
        }
    preview_payload = {
        "inspection": _serialize(inspection),
        "decision": preview_decision,
        "requestedForce": force,
        "preparedOperations": prepared_operations,
    }

    if not transactions:
        return "noop", warnings, {"preview": preview_payload, "transactions": []}

    action_id = create_prepared_action(
        session,
        operator_id=operator_id,
        action_type="settle",
        sender=sender,
        request_payload={
            "auctionAddress": normalized_auction,
            "sender": sender,
            "tokenAddress": normalized_token,
            "force": force,
        },
        preview_payload=preview_payload,
        transactions=transactions,
        resource_address=normalized_auction,
        auction_address=normalized_auction,
        token_address=decision.operations[0].token_address if decision.operations else normalized_token,
    )
    return "ok", warnings, {
        "actionId": action_id,
        "actionType": "settle",
        "preview": preview_payload,
        "transactions": transactions,
    }


async def prepare_sweep_action(
    settings: Settings,
    session: Session,
    *,
    operator_id: str,
    auction_address: str,
    sender: str | None,
    token_address: str,
) -> tuple[str, list[str], dict[str, object]]:
    normalized_auction = normalize_address(auction_address)
    normalized_token = normalize_address(token_address)
    web3_client = build_web3_client(settings)
    inspection = await inspect_auction_settlement(
        web3_client,
        settings,
        normalized_auction,
        token_address=normalized_token,
    )
    preview = inspection.preview_for_token(normalized_token)
    if preview is None or not preview.read_ok:
        detail = preview.error_message if preview is not None else "resolve preview failed for the requested token"
        raise APIError(detail or "resolve preview failed for the requested token", status_code=409)
    balance_raw = int(preview.balance_raw or 0)
    if balance_raw == 0:
        preview_payload = {
            "inspection": _serialize(inspection),
            "decision": {
                "status": "noop",
                "reason": "requested token has no auction balance to sweep",
            },
            "preparedOperations": [],
        }
        return "noop", [], {"preview": preview_payload, "transactions": []}

    token_metadata = TokenRepository(session).get(normalized_token)
    prepared_operations = [
        {
            "operation": "sweep-auction",
            "txIndex": 0,
            "auctionAddress": normalized_auction,
            "tokenAddress": normalized_token,
            "tokenSymbol": token_metadata.symbol if token_metadata is not None else None,
            "reason": "manual auction sweep",
            "path": preview.path,
            "balanceRaw": str(balance_raw),
            "receiver": preview.receiver,
        }
    ]
    preview_payload = {
        "inspection": _serialize(inspection),
        "decision": {
            "status": "actionable",
            "reason": "manual sweep prepared",
        },
        "preparedOperations": prepared_operations,
    }

    sweep_call = build_auction_sweep_call(
        settings=settings,
        web3_client=web3_client,
        auction_address=normalized_auction,
        token_address=normalized_token,
    )
    gas_estimate, gas_limit, gas_warning = await _estimate_transaction(
        web3_client,
        settings,
        sender=sender,
        to_address=sweep_call.target_address,
        data=sweep_call.data,
        gas_cap=settings.txn_max_gas_limit,
    )
    warnings = [gas_warning] if gas_warning else []
    transactions = [
        {
            "operation": sweep_call.operation_type.replace("_", "-"),
            "to": normalize_address(sweep_call.target_address),
            "data": sweep_call.data,
            "value": "0x0",
            "chainId": settings.chain_id,
            "sender": sender,
            "gasEstimate": gas_estimate,
            "gasLimit": gas_limit,
        }
    ]

    action_id = create_prepared_action(
        session,
        operator_id=operator_id,
        action_type="sweep",
        sender=sender,
        request_payload={
            "auctionAddress": normalized_auction,
            "sender": sender,
            "tokenAddress": normalized_token,
        },
        preview_payload=preview_payload,
        transactions=transactions,
        resource_address=normalized_auction,
        auction_address=normalized_auction,
        token_address=normalized_token,
    )
    return "ok", warnings, {
        "actionId": action_id,
        "actionType": "sweep",
        "preview": preview_payload,
        "transactions": transactions,
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
