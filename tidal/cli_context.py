"""Shared CLI context helpers."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from tidal.cli_support import (
    build_sync_web3,
    load_signer_from_options,
    resolve_sender_address,
    validate_sender_matches_signer,
)
from tidal.config import Settings, load_settings
from tidal.errors import AddressNormalizationError, ConfigurationError
from tidal.normalizers import normalize_address
from tidal.persistence.db import Database
from tidal.runtime import build_web3_client

if TYPE_CHECKING:
    from collections.abc import Iterator

    from web3 import Web3

    from tidal.chain.web3_client import Web3Client
    from tidal.transaction_service.signer import TransactionSigner


def normalize_cli_address(value: str | None, *, param_hint: str = "") -> str | None:
    """Normalize an address CLI argument, raising typer.BadParameter on failure."""
    if value is None:
        return None
    try:
        return normalize_address(value.strip())
    except AddressNormalizationError as exc:
        raise typer.BadParameter(str(exc), param_hint=param_hint) from exc


@dataclass(slots=True)
class ExecutionContext:
    """Resolved signer and sender for a broadcast-capable CLI command."""

    signer: TransactionSigner | None
    sender: str | None


@dataclass(slots=True)
class CLIContext:
    config_path: Path | None = None
    settings: Settings = field(init=False)

    def __post_init__(self) -> None:
        self.settings = load_settings(self.config_path)

    def require_rpc(self) -> None:
        if not self.settings.rpc_url:
            raise ConfigurationError("RPC_URL is required for this command")

    @contextmanager
    def session(self) -> "Iterator[object]":
        db = Database(self.settings.database_url)
        with db.session() as session:
            yield session

    def sync_web3(self) -> "Web3":
        self.require_rpc()
        return build_sync_web3(self.settings)

    def web3_client(self) -> "Web3Client":
        self.require_rpc()
        return build_web3_client(self.settings)

    def resolve_execution(
        self,
        *,
        broadcast: bool,
        required_for: str,
        sender: str | None = None,
        account_name: str | None = None,
        keystore_path: str | Path | None = None,
        password_file: str | Path | None = None,
    ) -> ExecutionContext:
        """Resolve signer, validate sender, and resolve the effective sender address."""
        signer = load_signer_from_options(
            self.settings,
            required=broadcast,
            required_for=required_for,
            account_name=account_name,
            keystore_path=keystore_path,
            password_file=password_file,
        )
        validated_sender = validate_sender_matches_signer(
            sender=sender,
            signer=signer,
            required_for=required_for,
        )
        resolved_sender = resolve_sender_address(
            self.settings,
            sender=validated_sender,
            account_name=account_name,
            keystore_path=keystore_path,
            signer=signer,
        )
        return ExecutionContext(signer=signer, sender=resolved_sender)
