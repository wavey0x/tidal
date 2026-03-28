"""Shared helpers for interactive CLI workflows."""

from __future__ import annotations

import getpass
import json
from pathlib import Path
from typing import Any

from web3 import HTTPProvider, Web3

from tidal.normalizers import normalize_address
from tidal.transaction_service.signer import TransactionSigner


def build_sync_web3(settings: Any) -> Web3:
    if not settings.rpc_url:
        raise SystemExit("RPC_URL is required")
    return Web3(
        HTTPProvider(
            settings.rpc_url,
            request_kwargs={"timeout": settings.rpc_timeout_seconds},
        )
    )


def prompt_text(label: str, *, default: str | None = None, required: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default not in {None, ""} else ""
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        if not required:
            return ""
        print("Value is required.")


def prompt_address(label: str, *, default: str | None = None) -> str:
    while True:
        value = prompt_text(label, default=default)
        try:
            return normalize_address(value)
        except Exception as exc:  # noqa: BLE001
            print(f"Invalid address: {exc}")


def prompt_optional_address(label: str, *, default: str | None = None) -> str | None:
    while True:
        value = prompt_text(label, default=default, required=False)
        if not value:
            return None
        try:
            return normalize_address(value)
        except Exception as exc:  # noqa: BLE001
            print(f"Invalid address: {exc}")


def prompt_uint(label: str, *, default: int | None = None) -> int:
    while True:
        raw = prompt_text(label, default=str(default) if default is not None else None)
        try:
            value = int(raw, 10)
        except ValueError:
            print("Enter a base-10 integer.")
            continue
        if value < 0:
            print("Value must be non-negative.")
            continue
        return value


def prompt_bool(label: str, *, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Enter y or n.")


def discover_local_keystore_path(settings: Any) -> Path | None:
    if settings.txn_keystore_path:
        configured = Path(settings.txn_keystore_path).expanduser()
        if configured.is_file():
            return configured

    foundry_dir = Path.home() / ".foundry" / "keystores"
    if not foundry_dir.is_dir():
        return None

    for preferred_name in ("wavey3", "wavey2"):
        candidate = foundry_dir / preferred_name
        if candidate.is_file():
            return candidate

    keystores = sorted(path for path in foundry_dir.iterdir() if path.is_file())
    if len(keystores) == 1:
        return keystores[0]
    return None


def read_keystore_address(keystore_path: Path | None) -> str | None:
    if keystore_path is None or not keystore_path.is_file():
        return None
    try:
        payload = json.loads(keystore_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None

    address = payload.get("address")
    if not address:
        return None
    if not str(address).startswith("0x"):
        address = f"0x{address}"
    try:
        return normalize_address(address)
    except Exception:  # noqa: BLE001
        return None


def maybe_load_signer(
    settings: Any,
    *,
    required: bool,
    required_for: str = "live execution",
) -> TransactionSigner | None:
    discovered_keystore = discover_local_keystore_path(settings)
    keystore_path = str(discovered_keystore) if discovered_keystore is not None else settings.txn_keystore_path
    passphrase = settings.txn_keystore_passphrase

    if not keystore_path and required:
        keystore_path = prompt_text("Keystore path", required=True)
    elif keystore_path and required:
        print(f"Using keystore: {keystore_path}")
    if keystore_path and not passphrase:
        passphrase = getpass.getpass("Keystore passphrase: ")

    if keystore_path and passphrase:
        return TransactionSigner(keystore_path, passphrase)

    if required:
        raise SystemExit(f"Keystore path and passphrase are required for {required_for}.")

    return None
