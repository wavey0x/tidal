"""Shared helpers for interactive CLI workflows."""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
from typing import Any

from eth_utils import to_checksum_address
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


def foundry_keystore_dir() -> Path:
    return Path.home() / ".foundry" / "keystores"


def discover_local_keystore_path(settings: Any) -> Path | None:
    if settings.txn_keystore_path:
        configured = Path(settings.txn_keystore_path).expanduser()
        if configured.is_file():
            return configured

    foundry_dir = foundry_keystore_dir()
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


def resolve_keystore_path(
    settings: Any,
    *,
    account_name: str | None = None,
    keystore_path: str | Path | None = None,
    required: bool = False,
    required_for: str = "broadcast execution",
) -> Path | None:
    if account_name and keystore_path is not None:
        raise SystemExit(f"Specify only one of --account or --keystore for {required_for}.")

    if account_name:
        resolved = foundry_keystore_dir() / account_name.strip()
        if resolved.is_file():
            return resolved
        raise SystemExit(f"Account keystore not found for {required_for}: {resolved}")

    if keystore_path is not None:
        resolved = Path(keystore_path).expanduser()
        if resolved.is_file():
            return resolved
        raise SystemExit(f"Keystore file not found for {required_for}: {resolved}")

    configured = settings.txn_keystore_path
    if configured:
        resolved = Path(configured).expanduser()
        if resolved.is_file():
            return resolved
        raise SystemExit(f"Configured keystore file not found for {required_for}: {resolved}")

    if required:
        raise SystemExit(
            f"A wallet is required for {required_for}. "
            "Provide --account or --keystore, or configure TXN_KEYSTORE_PATH."
        )

    return None


def _read_password_file(password_file: str | Path, *, required_for: str) -> str:
    resolved = Path(password_file).expanduser()
    if not resolved.is_file():
        raise SystemExit(f"Password file not found for {required_for}: {resolved}")
    value = resolved.read_text(encoding="utf-8").strip()
    if value:
        return value
    raise SystemExit(f"Password file is empty for {required_for}: {resolved}")


def resolve_keystore_password(
    settings: Any,
    *,
    password_file: str | Path | None = None,
    passphrase: str | None = None,
    prompt_if_missing: bool = False,
    required_for: str = "broadcast execution",
) -> str | None:
    if passphrase is not None:
        return passphrase

    if password_file is not None:
        return _read_password_file(password_file, required_for=required_for)

    if settings.txn_keystore_passphrase:
        return settings.txn_keystore_passphrase

    env_value = os.getenv("ETH_PASSWORD")
    if env_value:
        return env_value

    if prompt_if_missing:
        return getpass.getpass("Keystore password: ")

    return None


def load_signer_from_options(
    settings: Any,
    *,
    required: bool,
    required_for: str = "broadcast execution",
    account_name: str | None = None,
    keystore_path: str | Path | None = None,
    password_file: str | Path | None = None,
    passphrase: str | None = None,
) -> TransactionSigner | None:
    resolved_keystore_path = resolve_keystore_path(
        settings,
        account_name=account_name,
        keystore_path=keystore_path,
        required=required,
        required_for=required_for,
    )
    if resolved_keystore_path is None:
        return None

    resolved_password = resolve_keystore_password(
        settings,
        password_file=password_file,
        passphrase=passphrase,
        prompt_if_missing=required,
        required_for=required_for,
    )
    if not resolved_password:
        if required:
            raise SystemExit(f"Keystore password is required for {required_for}.")
        return None

    return TransactionSigner(str(resolved_keystore_path), resolved_password)


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
    required_for: str = "broadcast execution",
    account_name: str | None = None,
    keystore_path: str | Path | None = None,
    passphrase: str | None = None,
) -> TransactionSigner | None:
    try:
        return load_signer_from_options(
            settings,
            required=required,
            required_for=required_for,
            account_name=account_name,
            keystore_path=keystore_path,
            passphrase=passphrase,
        )
    except SystemExit:
        if not required:
            return None
        raise


def resolve_sender_address(
    settings: Any,
    *,
    sender: str | None = None,
    account_name: str | None = None,
    keystore_path: str | Path | None = None,
    signer: TransactionSigner | None = None,
) -> str | None:
    if sender is not None:
        return normalize_address(sender)
    if signer is not None:
        return normalize_address(signer.address)
    resolved_keystore_path = resolve_keystore_path(
        settings,
        account_name=account_name,
        keystore_path=keystore_path,
        required=False,
    )
    return read_keystore_address(resolved_keystore_path)


def validate_sender_matches_signer(
    *,
    sender: str | None,
    signer: TransactionSigner | None,
    required_for: str = "broadcast execution",
) -> str | None:
    normalized_sender = normalize_address(sender) if sender is not None else None
    if signer is None:
        return normalized_sender
    signer_address = normalize_address(signer.address)
    if normalized_sender is not None and normalized_sender != signer_address:
        raise SystemExit(
            f"--sender {to_checksum_address(normalized_sender)} does not match signer address "
            f"{to_checksum_address(signer_address)} for {required_for}."
        )
    return normalized_sender or signer_address
