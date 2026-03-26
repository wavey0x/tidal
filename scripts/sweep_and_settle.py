#!/usr/bin/env python3
"""Human-operated helper for AuctionKicker.sweepAndSettle()."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from eth_utils import to_checksum_address


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from factory_dashboard.chain.contracts.abis import AUCTION_KICKER_ABI  # noqa: E402
from factory_dashboard.config import load_settings  # noqa: E402
from factory_dashboard.runtime import build_web3_client  # noqa: E402
from factory_dashboard.transaction_service.kicker import _DEFAULT_PRIORITY_FEE_GWEI, _format_execution_error  # noqa: E402
from factory_dashboard.transaction_service.signer import TransactionSigner  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Call AuctionKicker.sweepAndSettle()")
    parser.add_argument("--auction", required=True, help="Auction contract address")
    parser.add_argument("--token", required=True, help="Sell token to sweep and settle")
    parser.add_argument("--config", help="Optional path to config.yaml")
    parser.add_argument(
        "--broadcast",
        action="store_true",
        help="Sign and send the transaction. Without this flag the script only prints the prepared transaction.",
    )
    parser.add_argument(
        "--receipt-timeout",
        type=int,
        default=120,
        help="Seconds to wait for a receipt after broadcasting",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.config) if args.config else None)
    if not settings.txn_keystore_path or not settings.txn_keystore_passphrase:
        raise SystemExit("TXN_KEYSTORE_PATH and TXN_KEYSTORE_PASSPHRASE must be configured")

    signer = TransactionSigner(settings.txn_keystore_path, settings.txn_keystore_passphrase)
    web3_client = build_web3_client(settings)

    auction = to_checksum_address(args.auction)
    token = to_checksum_address(args.token)
    kicker_address = to_checksum_address(settings.auction_kicker_address)
    contract = web3_client.contract(kicker_address, AUCTION_KICKER_ABI)
    tx_data = contract.functions.sweepAndSettle(auction, token)._encode_transaction_data()

    try:
        gas_estimate = await web3_client.estimate_gas(
            {
                "from": signer.checksum_address,
                "to": kicker_address,
                "data": tx_data,
                "chainId": settings.chain_id,
            }
        )
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Gas estimate failed: {_format_execution_error(exc)}") from exc

    base_fee_wei = await web3_client.get_base_fee()
    base_fee_gwei = base_fee_wei / 1e9
    try:
        suggested_priority_fee_wei = await web3_client.get_max_priority_fee()
    except Exception:  # noqa: BLE001
        suggested_priority_fee_wei = int(_DEFAULT_PRIORITY_FEE_GWEI * 1e9)
    priority_fee_wei = min(suggested_priority_fee_wei, settings.txn_max_priority_fee_gwei * 10**9)

    gas_limit = min(int(gas_estimate * 1.2), settings.txn_max_gas_limit)
    tx = {
        "to": kicker_address,
        "data": tx_data,
        "chainId": settings.chain_id,
        "gas": gas_limit,
        "maxFeePerGas": int((max(settings.txn_max_base_fee_gwei, base_fee_gwei) + settings.txn_max_priority_fee_gwei) * 10**9),
        "maxPriorityFeePerGas": priority_fee_wei,
        "nonce": await web3_client.get_transaction_count(signer.address),
        "type": 2,
    }

    print(f"AuctionKicker: {kicker_address}")
    print(f"Auction:       {auction}")
    print(f"Sell token:    {token}")
    print(f"From:          {signer.checksum_address}")
    print(f"Gas estimate:  {gas_estimate}")
    print(f"Gas limit:     {gas_limit}")
    print(f"Base fee:      {base_fee_gwei:.4f} gwei")
    print(f"Priority fee:  {priority_fee_wei / 1e9:.4f} gwei")
    print(f"Data:          {tx_data}")

    if not args.broadcast:
        print("Dry run only. Re-run with --broadcast to send.")
        return 0

    signed_tx = signer.sign_transaction(tx)
    tx_hash = await web3_client.send_raw_transaction(signed_tx)
    print(f"Submitted:     {tx_hash}")

    receipt = await web3_client.get_transaction_receipt(tx_hash, timeout_seconds=args.receipt_timeout)
    status = "CONFIRMED" if receipt.get("status") == 1 else "REVERTED"
    print(f"Receipt:       {status}")
    print(f"Block:         {receipt.get('blockNumber')}")
    print(f"Gas used:      {receipt.get('gasUsed')}")
    return 0 if status == "CONFIRMED" else 1


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
