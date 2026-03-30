"""Helpers shared by API-backed operator CLI commands."""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import typer
from eth_utils import to_checksum_address
from rich.console import Console

from tidal.cli_renderers import (
    BroadcastRecord,
    render_broadcast_records,
    render_prepared_action_summary,
    render_status_panel,
    render_warning_panel,
)
from tidal.control_plane.client import ControlPlaneClient
from tidal.control_plane.outbox import ActionReportOutbox
from tidal.runtime import build_web3_client
from tidal.time import utcnow_iso


def render_action_preview(data: dict[str, Any], *, heading: str) -> None:
    render_prepared_action_summary(data, heading=heading)


def render_warnings(warnings: list[str]) -> None:
    render_warning_panel(warnings)
    if warnings:
        typer.echo()


@contextmanager
def progress_status(
    message: str,
    *,
    border_style: str = "cyan",
    spinner: str = "dots",
    fallback_render: bool = False,
    fallback_title: str = "Working",
) -> Iterator[Callable[[str], None]]:
    if not sys.stdout.isatty():
        if fallback_render:
            render_status_panel(fallback_title, message, border_style=border_style)
        yield lambda _message: None
        return

    console = Console(file=sys.stdout, highlight=False, soft_wrap=True)
    with console.status(
        f"[bold {border_style}]{message}[/bold {border_style}]",
        spinner=spinner,
        spinner_style=f"bold {border_style}",
    ) as status:
        yield lambda next_message: status.update(
            f"[bold {border_style}]{next_message}[/bold {border_style}]",
            spinner=spinner,
            spinner_style=f"bold {border_style}",
        )


@contextmanager
def submission_progress(message: str) -> Iterator[Callable[[str], None]]:
    with progress_status(
        message,
        border_style="cyan",
        spinner="dots",
        fallback_render=True,
        fallback_title="Submitting",
    ) as update:
        yield update


def _send_action_report(
    *,
    outbox: ActionReportOutbox,
    client: ControlPlaneClient,
    action_id: str,
    report_type: str,
    payload: dict[str, Any],
    warning_label: str,
) -> None:
    tx_index = int(payload["txIndex"])
    queued = False
    queue_error: Exception | None = None
    try:
        if report_type == "broadcast":
            outbox.queue_broadcast(base_url=client.base_url, action_id=action_id, payload=payload)
        else:
            outbox.queue_receipt(base_url=client.base_url, action_id=action_id, payload=payload)
        queued = True
    except Exception as exc:  # noqa: BLE001
        queue_error = exc

    try:
        if report_type == "broadcast":
            client.report_broadcast(action_id, payload)
        else:
            client.report_receipt(action_id, payload)
    except Exception as exc:  # noqa: BLE001
        if queued:
            typer.echo(f"Warning: {warning_label} queued for retry ({exc})", err=True)
        else:
            typer.echo(
                f"Warning: {warning_label} failed and could not be queued ({exc}; queue error: {queue_error})",
                err=True,
            )
        return

    if queued:
        try:
            outbox.mark_delivered(
                base_url=client.base_url,
                action_id=action_id,
                tx_index=tx_index,
                report_type=report_type,
            )
        except Exception:  # noqa: BLE001
            pass


async def broadcast_prepared_action(
    *,
    settings,
    client: ControlPlaneClient,
    action_id: str,
    sender: str,
    signer,
    transactions: list[dict[str, Any]],
    receipt_timeout_seconds: int = 120,
    outbox: ActionReportOutbox | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:  # noqa: ANN001
    report_outbox = outbox or ActionReportOutbox()
    try:
        report_outbox.flush_pending(client)
    except Exception:  # noqa: BLE001
        pass
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
                _send_action_report(
                    outbox=report_outbox,
                    client=client,
                    action_id=action_id,
                    report_type="receipt",
                    payload={
                        "txIndex": tx_index,
                        "receiptStatus": "FAILED",
                        "observedAt": observed_at,
                        "errorMessage": str(exc),
                    },
                    warning_label=f"control-plane failure report for transaction {tx_index + 1}",
                )
                raise RuntimeError(f"transaction {tx_index + 1} failed: {exc}") from exc

            broadcast_at = utcnow_iso()
            if progress_callback is not None:
                progress_callback(f"Awaiting confirmation {tx_hash[:10]}...{tx_hash[-6:]}")
            _send_action_report(
                outbox=report_outbox,
                client=client,
                action_id=action_id,
                report_type="broadcast",
                payload={
                    "txIndex": tx_index,
                    "sender": sender,
                    "txHash": tx_hash,
                    "broadcastAt": broadcast_at,
                },
                warning_label=f"control-plane broadcast report for transaction {tx_index + 1}",
            )
            record: dict[str, Any] = {
                "operation": tx.get("operation"),
                "sender": sender,
                "txHash": tx_hash,
                "broadcastAt": broadcast_at,
                "chainId": settings.chain_id,
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
            _send_action_report(
                outbox=report_outbox,
                client=client,
                action_id=action_id,
                report_type="receipt",
                payload={
                    "txIndex": tx_index,
                    "receiptStatus": receipt_status,
                    "blockNumber": receipt.get("blockNumber"),
                    "gasUsed": receipt.get("gasUsed"),
                    "gasPriceGwei": gas_price_gwei,
                    "observedAt": observed_at,
                },
                warning_label=f"control-plane receipt report for transaction {tx_index + 1}",
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
    outbox: ActionReportOutbox | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:  # noqa: ANN001
    return asyncio.run(
        broadcast_prepared_action(
            settings=settings,
            client=client,
            action_id=action_id,
            sender=sender,
            signer=signer,
            transactions=transactions,
            outbox=outbox,
            progress_callback=progress_callback,
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
                chain_id=int(record["chainId"]) if record.get("chainId") is not None else None,
                receipt_status=str(record.get("receiptStatus")) if record.get("receiptStatus") is not None else None,
                block_number=int(record["blockNumber"]) if record.get("blockNumber") is not None else None,
                gas_used=int(record["gasUsed"]) if record.get("gasUsed") is not None else None,
                gas_estimate=int(record["gasEstimate"]) if record.get("gasEstimate") is not None else None,
            )
            for record in records
            if record.get("txHash")
        ]
    )
