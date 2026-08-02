"""Shared scope checks for current auction automation."""

from __future__ import annotations

from typing import Mapping

from sqlalchemy import select

from tidal.normalizers import normalize_address
from tidal.persistence import models


def pair_in_automation_scope(
    session,  # noqa: ANN001
    ignore_policy,  # noqa: ANN001
    operation: Mapping[str, object],
) -> bool:
    """Return whether an operation pair still belongs to active automation."""

    source_type = str(operation.get("source_type") or "")
    source_address = str(
        operation.get("source_address") or operation.get("strategy_address") or ""
    )
    if not source_address:
        return False
    if (
        ignore_policy.match(
            source_address=source_address,
            auction_address=str(operation["auction_address"]),
            token_address=str(operation["token_address"]),
        )
        is not None
    ):
        return False

    table = models.strategies if source_type == "strategy" else models.fee_burners
    row = (
        session.execute(
            select(table.c.active, table.c.auction_address).where(
                table.c.address == source_address
            )
        )
        .mappings()
        .first()
    )
    return bool(
        row is not None
        and int(row["active"]) == 1
        and row["auction_address"]
        and normalize_address(str(row["auction_address"]))
        == normalize_address(str(operation["auction_address"]))
    )
