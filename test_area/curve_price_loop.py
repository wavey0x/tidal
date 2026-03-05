#!/usr/bin/env python3
import json
import os
import sys
from urllib import error, request

from dotenv import load_dotenv
from web3 import Web3

BASE_URL = "https://prices.curve.finance/v1/usd_price/ethereum/{token_address}"

VAULT_ADDRESSES = [
    "0xc952f3028E322da48e239A077b810A24556f36f1",
    "0xe0287cA62fE23f4FFAB827d5448d68aFe6DD9Fd7",
    "0x4282B8f159ee677559Dc6A20cd478DD0BDe75fF2",
    "0x75A291F0232ADD37d72Dd1Dcff55B715755ECDEe",
    "0xf165a634296800812B8B0607a75DeDdcD4D3cC88",
    "0x6E9455D109202b426169F0d8f01A3332DAE160f3",
    "0xa540744DEDBDA9eF64cf753F0E851EfE4a419EA9",
    "0xDb26d8815EdA864Dfa43306766f2F8CA50C03F9E",
]

VAULT_ABI = [
    {
        "inputs": [],
        "name": "token",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]

TOKEN_SYMBOL_ABI_STRING = [
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    }
]

TOKEN_SYMBOL_ABI_BYTES32 = [
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "bytes32", "name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def get_rpc_url() -> str:
    load_dotenv()
    rpc_url = os.getenv("RPC_URL")
    if not rpc_url:
        raise RuntimeError("RPC_URL is not set. Add it to your .env or environment.")
    return rpc_url


def resolve_vault_token(w3: Web3, vault_address: str) -> str:
    vault = w3.eth.contract(address=Web3.to_checksum_address(vault_address), abi=VAULT_ABI)
    token_address = vault.functions.token().call()
    return Web3.to_checksum_address(token_address)


def resolve_token_symbol(w3: Web3, token_address: str) -> str:
    token_addr = Web3.to_checksum_address(token_address)
    try:
        token = w3.eth.contract(address=token_addr, abi=TOKEN_SYMBOL_ABI_STRING)
        return token.functions.symbol().call()
    except Exception:
        token = w3.eth.contract(address=token_addr, abi=TOKEN_SYMBOL_ABI_BYTES32)
        raw = token.functions.symbol().call()
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace")


def fetch_price(token_address: str, timeout: int = 15) -> dict:
    url = BASE_URL.format(token_address=token_address)
    req = request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "curve-price-loop/1.0",
        },
    )
    with request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode(response.headers.get_content_charset() or "utf-8")
    return json.loads(body)


def main() -> None:
    w3 = Web3(Web3.HTTPProvider(get_rpc_url()))
    if not w3.is_connected():
        raise RuntimeError("Failed to connect to RPC_URL")

    for vault_address in VAULT_ADDRESSES:
        try:
            token_address = resolve_vault_token(w3, vault_address)
            token_symbol = resolve_token_symbol(w3, token_address)
        except Exception as exc:
            print(f"{vault_address}: ERROR resolving token/symbol - {exc}", file=sys.stderr)
            continue

        try:
            result = fetch_price(token_address)
            usd_price = result["data"]["usd_price"]
            print(f"{token_symbol} - {usd_price}")
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            print(
                f"{vault_address} {token_address}: HTTP {exc.code} - {details}",
                file=sys.stderr,
            )
        except Exception as exc:
            print(f"{vault_address} {token_address}: ERROR - {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
