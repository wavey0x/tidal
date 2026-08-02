"""Shared scope checks for current auction automation."""

from __future__ import annotations

from typing import Mapping

from sqlalchemy import select

from tidal.normalizers import normalize_address
from tidal.persistence import models
from tidal.transaction_service.evaluator import build_shortlist

AutomationPairKey = tuple[str, str, str, str]


def current_automation_pairs(session, settings) -> frozenset[AutomationPairKey]:  # noqa: ANN001
    """Return source pairs that are eligible inputs to kick automation now."""

    shortlist = build_shortlist(
        session,
        usd_threshold=settings.txn_usd_threshold,
        max_data_age_seconds=settings.txn_data_freshness_limit_seconds,
    )
    ignore_policy = settings.kick_config.ignore_policy
    return frozenset(
        (
            candidate.source_type,
            normalize_address(candidate.source_address),
            normalize_address(candidate.auction_address),
            normalize_address(candidate.token_address),
        )
        for candidate in shortlist.eligible_candidates
        if ignore_policy.match(
            source_address=candidate.source_address,
            auction_address=candidate.auction_address,
            token_address=candidate.token_address,
        )
        is None
    )


def pair_in_automation_scope(
    session,  # noqa: ANN001
    current_pairs: frozenset[AutomationPairKey],
    operation: Mapping[str, object],
) -> bool:
    """Return whether an operation pair still belongs to active automation."""

    source_type = str(operation.get("source_type") or "")
    source_address = str(
        operation.get("source_address") or operation.get("strategy_address") or ""
    )
    if not source_address:
        return False
    pair_key = (
        source_type,
        normalize_address(source_address),
        normalize_address(str(operation["auction_address"])),
        normalize_address(str(operation["token_address"])),
    )
    if pair_key not in current_pairs:
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
