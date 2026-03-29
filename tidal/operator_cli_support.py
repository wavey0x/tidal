"""Helpers shared by API-backed operator CLI commands."""

from __future__ import annotations

import asyncio
from typing import Any

import typer
from eth_utils import to_checksum_address

from tidal.cli_renderers import BroadcastRecord, render_broadcast_records
from tidal.control_plane.client import ControlPlaneClient
from tidal.runtime import build_web3_client
from tidal.time import utcnow_iso


def render_action_preview(data: dict[str, Any], *, heading: str) -> None:
    typer.echo(f"{heading}:")
    action_id = data.get("actionId")
    action_type = data.get("actionType")
    if action_id:
        typer.echo(f"  Action ID:    {action_id}")
    if action_type:
        typer.echo(f"  Action Type:  {action_type}")
    preview = data.get("preview") or {}
    transactions = data.get("transactions") or []
    if isinstance(preview, dict):
        prepared = preview.get("preparedOperations")
        if isinstance(prepared, list) and prepared:
            typer.echo(f"  Operations:   {len(prepared)}")
    typer.echo(f"  Transactions: {len(transactions)}")
    for index, tx in enumerate(transactions, 1):
        typer.echo(f"  Tx {index}:       {tx.get('operation')} -> {tx.get('to')}")
        typer.echo(f"    Gas est:    {tx.get('gasEstimate') or 'unavailable'}")
        typer.echo(f"    Gas limit:  {tx.get('gasLimit') or 'unavailable'}")


def render_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        typer.echo(f"Warning: {warning}")
    if warnings:
        typer.echo()


async def broadcast_prepared_action(
    *,
    settings,
    client: ControlPlaneClient,
    action_id: str,
    sender: str,
    signer,
    transactions: list[dict[str, Any]],
    receipt_timeout_seconds: int = 120,
) -> list[dict[str, Any]]:  # noqa: ANN001
    web3_client = build_web3_client(settings)
    try:
        nonce = await web3_client.get_transaction_count(sender)
        checksum_sender = to_checksum_address(sender)

        try:
            base_fee_wei = await web3_client.get_base_fee()
            base_fee_gwei = base_fee_wei / 1e9
        except Exception:
            base_fee_gwei = 0.0
        try:
            priority_fee_wei = await web3_client.get_max_priority_fee()
        except Exception:
            priority_fee_wei = int(settings.txn_max_priority_fee_gwei * 10**9)
        priority_fee_wei = min(priority_fee_wei, int(settings.txn_max_priority_fee_gwei * 10**9))
        max_fee_wei = int(
            (max(settings.txn_max_base_fee_gwei, base_fee_gwei) + settings.txn_max_priority_fee_gwei) * 10**9
        )

        results: list[dict[str, Any]] = []
        for tx_index, tx in enumerate(transactions):
            tx_sender = str(tx.get("sender") or sender)
            if tx_sender.lower() != sender.lower():
                raise RuntimeError(f"prepared sender {tx_sender} does not match local sender {sender}")

            checksum_to = to_checksum_address(str(tx["to"]))
            value = (
                int(str(tx.get("value") or "0x0"), 16)
                if str(tx.get("value") or "0").startswith("0x")
                else int(tx.get("value") or 0)
            )

            gas_limit = tx.get("gasLimit")
            if gas_limit is None:
                gas_limit = await web3_client.estimate_gas(
                    {
                        "from": checksum_sender,
                        "to": checksum_to,
                        "data": tx["data"],
                        "value": value,
                        "chainId": settings.chain_id,
                    }
                )

            full_tx = {
                "to": checksum_to,
                "data": tx["data"],
                "value": value,
                "chainId": settings.chain_id,
                "gas": int(gas_limit),
                "maxFeePerGas": max_fee_wei,
                "maxPriorityFeePerGas": priority_fee_wei,
                "nonce": nonce,
                "type": 2,
            }
            try:
                signed_tx = signer.sign_transaction(full_tx)
                tx_hash = await web3_client.send_raw_transaction(signed_tx)
            except Exception as exc:  # noqa: BLE001
                observed_at = utcnow_iso()
                client.report_receipt(
                    action_id,
                    {
                        "txIndex": tx_index,
                        "receiptStatus": "FAILED",
                        "observedAt": observed_at,
                        "errorMessage": str(exc),
                    },
                )
                raise RuntimeError(f"transaction {tx_index + 1} failed: {exc}") from exc

            broadcast_at = utcnow_iso()
            client.report_broadcast(
                action_id,
                {
                    "txIndex": tx_index,
                    "sender": sender,
                    "txHash": tx_hash,
                    "broadcastAt": broadcast_at,
                },
            )
            record: dict[str, Any] = {
                "operation": tx.get("operation"),
                "sender": sender,
                "txHash": tx_hash,
                "broadcastAt": broadcast_at,
                "gasEstimate": tx.get("gasEstimate"),
            }
            try:
                receipt = await web3_client.get_transaction_receipt(tx_hash, timeout_seconds=receipt_timeout_seconds)
            except Exception:
                results.append(record)
                nonce += 1
                continue

            effective_gas_price = receipt.get("effectiveGasPrice")
            gas_price_gwei = str(round(effective_gas_price / 1e9, 4)) if effective_gas_price else None
            receipt_status = "CONFIRMED" if receipt.get("status") == 1 else "REVERTED"
            observed_at = utcnow_iso()
            client.report_receipt(
                action_id,
                {
                    "txIndex": tx_index,
                    "receiptStatus": receipt_status,
                    "blockNumber": receipt.get("blockNumber"),
                    "gasUsed": receipt.get("gasUsed"),
                    "gasPriceGwei": gas_price_gwei,
                    "observedAt": observed_at,
                },
            )
            record.update(
                {
                    "receiptStatus": receipt_status,
                    "blockNumber": receipt.get("blockNumber"),
                    "gasUsed": receipt.get("gasUsed"),
                }
            )
            results.append(record)
            nonce += 1
            if receipt_status != "CONFIRMED":
                break
        return results
    finally:
        await web3_client.close()


def execute_prepared_action_sync(
    *,
    settings,
    client: ControlPlaneClient,
    action_id: str,
    sender: str,
    signer,
    transactions: list[dict[str, Any]],
) -> list[dict[str, Any]]:  # noqa: ANN001
    return asyncio.run(
        broadcast_prepared_action(
            settings=settings,
            client=client,
            action_id=action_id,
            sender=sender,
            signer=signer,
            transactions=transactions,
        )
    )


def render_broadcast_result(records: list[dict[str, Any]]) -> None:
    render_broadcast_records(
        [
            BroadcastRecord(
                operation=str(record.get("operation")) if record.get("operation") is not None else None,
                sender=str(record.get("sender")) if record.get("sender") is not None else None,
                tx_hash=str(record["txHash"]),
                broadcast_at=str(record.get("broadcastAt")) if record.get("broadcastAt") is not None else None,
                receipt_status=str(record.get("receiptStatus")) if record.get("receiptStatus") is not None else None,
                block_number=int(record["blockNumber"]) if record.get("blockNumber") is not None else None,
                gas_used=int(record["gasUsed"]) if record.get("gasUsed") is not None else None,
                gas_estimate=int(record["gasEstimate"]) if record.get("gasEstimate") is not None else None,
            )
            for record in records
            if record.get("txHash")
        ]
    )
