"""Candidate shortlisting and pre-send checks."""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from factory_dashboard.persistence import models
from factory_dashboard.persistence.repositories import KickTxRepository
from factory_dashboard.transaction_service.types import KickAction, KickCandidate, KickDecision, SkipReason

logger = structlog.get_logger(__name__)


def shortlist_candidates(
    session: Session,
    *,
    usd_threshold: float,
    max_data_age_seconds: int,
) -> list[KickCandidate]:
    """Query SQLite for (strategy, token) pairs above threshold with fresh data."""

    now = datetime.now(timezone.utc)
    min_timestamp = datetime.fromtimestamp(
        now.timestamp() - max_data_age_seconds, tz=timezone.utc
    ).isoformat()

    stmt = (
        select(
            models.strategy_token_balances_latest.c.strategy_address,
            models.strategy_token_balances_latest.c.token_address,
            models.strategies.c.auction_address,
            models.strategies.c.want_address,
            models.strategy_token_balances_latest.c.normalized_balance,
            models.tokens.c.price_usd,
            models.tokens.c.decimals,
            models.strategy_token_balances_latest.c.scanned_at,
            models.tokens.c.price_fetched_at,
        )
        .select_from(
            models.strategy_token_balances_latest.join(
                models.strategies,
                models.strategy_token_balances_latest.c.strategy_address
                == models.strategies.c.address,
            ).join(
                models.tokens,
                models.strategy_token_balances_latest.c.token_address
                == models.tokens.c.address,
            )
        )
        .where(
            models.strategies.c.auction_address.isnot(None),
            models.strategies.c.want_address.isnot(None),
            models.tokens.c.price_status == "SUCCESS",
            models.tokens.c.price_usd.isnot(None),
            models.strategy_token_balances_latest.c.scanned_at >= min_timestamp,
            models.tokens.c.price_fetched_at >= min_timestamp,
        )
    )

    candidates: list[KickCandidate] = []
    for row in session.execute(stmt).mappings():
        try:
            balance = float(row["normalized_balance"])
            price = float(row["price_usd"])
        except (ValueError, TypeError):
            continue

        usd_value = balance * price
        if usd_value < usd_threshold:
            continue

        candidates.append(
            KickCandidate(
                strategy_address=row["strategy_address"],
                token_address=row["token_address"],
                auction_address=row["auction_address"],
                normalized_balance=row["normalized_balance"],
                price_usd=row["price_usd"],
                want_address=row["want_address"],
                usd_value=usd_value,
                decimals=row["decimals"],
            )
        )

    return candidates


def check_pre_send(
    candidates: list[KickCandidate],
    *,
    kick_tx_repository: KickTxRepository,
    cooldown_seconds: int,
) -> list[KickDecision]:
    """Apply cooldown checks to shortlisted candidates."""

    now = datetime.now(timezone.utc)
    min_cooldown_timestamp = datetime.fromtimestamp(
        now.timestamp() - cooldown_seconds, tz=timezone.utc
    ).isoformat()

    decisions: list[KickDecision] = []

    for candidate in candidates:
        last_kick = kick_tx_repository.last_kick_for_pair(
            candidate.strategy_address, candidate.token_address
        )
        if last_kick is not None and last_kick["created_at"] >= min_cooldown_timestamp:
            decisions.append(
                KickDecision(candidate=candidate, action=KickAction.SKIP, skip_reason=SkipReason.COOLDOWN)
            )
            logger.debug(
                "txn_candidate_skip",
                strategy=candidate.strategy_address,
                token=candidate.token_address,
                reason="cooldown",
                last_kick_at=last_kick["created_at"],
            )
            continue

        decisions.append(KickDecision(candidate=candidate, action=KickAction.KICK))

    return decisions
