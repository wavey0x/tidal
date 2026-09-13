"""Native auction operations: fresh preparation and the common managed sender."""
from __future__ import annotations

import time
import uuid
from contextlib import AsyncExitStack

from tidal.api.errors import APIError
from tidal.api.services.action_prepare import prepare_enable_tokens_action, prepare_settle_action, prepare_sweep_action
from tidal.async_resources import close_client
from tidal.execution import ManagedExecutor
from tidal.lifecycle import LifecycleError, execution_lock
from tidal.normalizers import normalize_address
from tidal.operation_reconciler import OperationReconciler
from tidal.runtime import build_web3_client
from tidal.time import utcnow_iso
from tidal.transaction_evidence import rpc_int
from tidal.transaction_service.kick_shared import resolve_priority_fee_wei


def operation_rows(preview: dict, *, tx_index: int, operation: str, run_id: str, created_at: str) -> list[dict]:
    """Known native preparation supplies explicit business links, never a job."""
    operation = operation.replace("-", "_")
    if operation == "settle":
        operation = "resolve_auction"
    if operation in {"sweep", "sweep_and_settle"}:
        operation = "sweep_auction"
    if operation not in {"resolve_auction", "sweep_auction", "enable_tokens"}:
        raise LifecycleError("INVALID_INTENT", "This command only prepares supported auction operations.")
    rows = []
    for item in preview.get("preparedOperations", []):
        if item.get("txIndex") != tx_index:
            continue
        item_operation = str(item.get("operation", "")).replace("-", "_")
        if item_operation != operation:
            raise LifecycleError("INVALID_INTENT", "Prepared transaction and operation types differ.")
        source = normalize_address(item["sourceAddress"]) if item.get("sourceAddress") else None
        rows.append({
            "run_id": run_id, "operation_type": operation,
            "source_type": item.get("sourceType"), "source_address": source,
            "strategy_address": source if item.get("sourceType") == "strategy" else None,
            "auction_address": normalize_address(item["auctionAddress"]),
            "token_address": normalize_address(item["tokenAddress"]),
            "token_symbol": item.get("tokenSymbol"), "want_symbol": item.get("wantSymbol"),
            "want_address": normalize_address(item["wantAddress"]) if item.get("wantAddress") else None,
            "stuck_abort_reason": item.get("reason"), "created_at": created_at,
            "status": "SUBMITTED", "sell_amount": None,
        })
    if not rows:
        raise LifecycleError("INVALID_INTENT", "Every native transaction requires explicit business-operation links.")
    return rows


async def run_auction_action(*, settings, session, signer, action: str, auction: str,
                             token: str | None = None, extra_tokens: list[str] | None = None,
                             force: bool = False, confirm=None) -> dict:
    """Prepare under the host lock and preserve partial progress without replay."""
    with execution_lock(settings.resolved_home_path / "execution.lock"):
        async with AsyncExitStack() as clients:
            web3 = build_web3_client(settings)
            clients.push_async_callback(close_client, web3)
            managed = ManagedExecutor(
                settings=settings, session=session, web3_client=web3, signer=signer, profile="kick",
                operation_reconciler=OperationReconciler(
                    session=session, web3_client=web3, auction_kicker_address=settings.auction_kicker_address,
                    chain_id=settings.chain_id, settings=settings,
                ),
            )
            managed._activation()
            session.commit()
            await managed.reconciler.reconcile()
            managed.repository.assert_unblocked(signer.address)
            session.commit()
            prepared_at = time.monotonic()
            kwargs = dict(settings=settings, session=session, operator_id="local",
                          auction_address=auction, sender=signer.address)
            try:
                if action == "enable_tokens":
                    status, warnings, prepared = await prepare_enable_tokens_action(**kwargs, extra_tokens=extra_tokens or [])
                elif action == "settle":
                    status, warnings, prepared = await prepare_settle_action(**kwargs, token_address=token, force=force)
                elif action == "sweep":
                    status, warnings, prepared = await prepare_sweep_action(**kwargs, token_address=token)
                else:
                    raise LifecycleError("INVALID_INTENT", "Unknown local auction action.")
            except APIError as exc:
                raise LifecycleError("PREPARATION_ERROR", str(exc)) from exc
            result = {"preparation_status": status, "preview": prepared.get("preview"), "warnings": warnings,
                      "transactions": [], "unsubmitted": len(prepared.get("transactions", []))}
            if status != "ok" or not prepared.get("transactions"):
                return result
            if confirm is not None and not confirm(prepared):
                return {**result, "preparation_status": "skipped"}
            run_id = f"local-auction:{uuid.uuid4()}"
            for index, intent in enumerate(prepared["transactions"]):
                try:
                    if intent.get("sender") and normalize_address(intent["sender"]) != normalize_address(signer.address):
                        raise LifecycleError("WRONG_SIGNER", "Prepared sender differs from this command's managed signer.")
                    estimate, limit = intent.get("gasEstimate"), intent.get("gasLimit")
                    if estimate is None or limit is None or not 0 < int(estimate) <= int(limit) <= settings.txn_max_gas_limit:
                        raise LifecycleError("INVALID_INTENT", "Prepared gas estimate/limit is missing or outside the configured cap.")
                    rows = operation_rows(prepared["preview"], tx_index=index, operation=intent["operation"],
                                          run_id=run_id, created_at=utcnow_iso())
                    # Preserve the existing manual auction command's current-base
                    # fee policy and bounded priority fee. Scheduled kick caps are
                    # explicit profiles applied by their own native caller.
                    base_fee = await web3.get_base_fee()
                    priority = await resolve_priority_fee_wei(web3, settings.txn_max_priority_fee_gwei)
                    attempt = await managed.submit(transaction={
                        "to": intent["to"], "data": intent["data"], "chainId": intent["chainId"],
                        "value": rpc_int(intent.get("value", 0)), "gas": int(limit), "type": 2,
                        "maxFeePerGas": base_fee + priority, "maxPriorityFeePerGas": priority,
                    }, operations=rows, action=rows[0]["operation_type"], prepared_at_monotonic=prepared_at)
                    result["transactions"].append(attempt)
                    result["unsubmitted"] -= 1
                    if attempt["status"] not in {"CONFIRMED", "REVERTED"}:
                        break
                except LifecycleError as exc:
                    result["blockers"] = [{"code": exc.code, "message": str(exc)}]
                    break
            return result
