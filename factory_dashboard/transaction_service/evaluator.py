"""Candidate shortlisting and pre-send checks."""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import literal, null, select
from sqlalchemy.orm import Session

from factory_dashboard.persistence import models
from factory_dashboard.persistence.repositories import KickTxRepository
from factory_dashboard.transaction_service.types import KickAction, KickCandidate, KickDecision, SkipReason, SourceType

logger = structlog.get_logger(__name__)


def shortlist_candidates(
    session: Session,
    *,
    usd_threshold: float,
    max_data_age_seconds: int,
    source_type: SourceType | None = None,
) -> list[KickCandidate]:
    """Query SQLite for source-token pairs above threshold with fresh data."""

    now = datetime.now(timezone.utc)
    min_timestamp = datetime.fromtimestamp(
        now.timestamp() - max_data_age_seconds, tz=timezone.utc
    ).isoformat()

    want_tokens = models.tokens.alias("want_tokens")

    strategy_stmt = (
        select(
            literal("strategy").label("source_type"),
            models.strategy_token_balances_latest.c.strategy_address.label("source_address"),
            models.strategy_token_balances_latest.c.token_address,
            models.strategies.c.auction_address,
            models.strategies.c.want_address,
            models.strategy_token_balances_latest.c.normalized_balance,
            models.tokens.c.price_usd,
            models.tokens.c.decimals,
            models.strategy_token_balances_latest.c.scanned_at,
            models.tokens.c.price_fetched_at,
            models.strategies.c.name.label("source_name"),
            literal("vault").label("context_type"),
            models.vaults.c.address.label("context_address"),
            models.vaults.c.name.label("context_name"),
            models.vaults.c.symbol.label("context_symbol"),
            models.tokens.c.symbol.label("token_symbol"),
            want_tokens.c.symbol.label("want_symbol"),
        )
        .select_from(
            models.strategy_token_balances_latest.join(
                models.strategies,
                models.strategy_token_balances_latest.c.strategy_address
                == models.strategies.c.address,
            ).outerjoin(
                models.vaults,
                models.strategies.c.vault_address == models.vaults.c.address,
            ).join(
                models.tokens,
                models.strategy_token_balances_latest.c.token_address
                == models.tokens.c.address,
            ).outerjoin(
                want_tokens,
                models.strategies.c.want_address == want_tokens.c.address,
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

    fee_burner_stmt = (
        select(
            literal("fee_burner").label("source_type"),
            models.fee_burner_token_balances_latest.c.fee_burner_address.label("source_address"),
            models.fee_burner_token_balances_latest.c.token_address,
            models.fee_burners.c.auction_address,
            models.fee_burners.c.want_address,
            models.fee_burner_token_balances_latest.c.normalized_balance,
            models.tokens.c.price_usd,
            models.tokens.c.decimals,
            models.fee_burner_token_balances_latest.c.scanned_at,
            models.tokens.c.price_fetched_at,
            models.fee_burners.c.name.label("source_name"),
            null().label("context_type"),
            null().label("context_address"),
            null().label("context_name"),
            null().label("context_symbol"),
            models.tokens.c.symbol.label("token_symbol"),
            want_tokens.c.symbol.label("want_symbol"),
        )
        .select_from(
            models.fee_burner_token_balances_latest.join(
                models.fee_burners,
                models.fee_burner_token_balances_latest.c.fee_burner_address
                == models.fee_burners.c.address,
            ).join(
                models.tokens,
                models.fee_burner_token_balances_latest.c.token_address
                == models.tokens.c.address,
            ).outerjoin(
                want_tokens,
                models.fee_burners.c.want_address == want_tokens.c.address,
            )
        )
        .where(
            models.fee_burners.c.auction_address.isnot(None),
            models.fee_burners.c.want_address.isnot(None),
            models.tokens.c.price_status == "SUCCESS",
            models.tokens.c.price_usd.isnot(None),
            models.fee_burner_token_balances_latest.c.scanned_at >= min_timestamp,
            models.tokens.c.price_fetched_at >= min_timestamp,
        )
    )

    candidates: list[KickCandidate] = []
    for row in list(session.execute(strategy_stmt).mappings()) + list(session.execute(fee_burner_stmt).mappings()):
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
                source_type=row["source_type"],
                source_address=row["source_address"],
                token_address=row["token_address"],
                auction_address=row["auction_address"],
                normalized_balance=row["normalized_balance"],
                price_usd=row["price_usd"],
                want_address=row["want_address"],
                usd_value=usd_value,
                decimals=row["decimals"],
                source_name=row["source_name"],
                context_type=row["context_type"],
                context_address=row["context_address"],
                context_name=row["context_name"],
                context_symbol=row["context_symbol"],
                token_symbol=row["token_symbol"],
                want_symbol=row["want_symbol"],
            )
        )

    if source_type is not None:
        candidates = [candidate for candidate in candidates if candidate.source_type == source_type]

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
        last_kick = kick_tx_repository.last_kick_for_pair(candidate.source_address, candidate.token_address)
        if last_kick is not None and last_kick["created_at"] >= min_cooldown_timestamp:
            decisions.append(
                KickDecision(candidate=candidate, action=KickAction.SKIP, skip_reason=SkipReason.COOLDOWN)
            )
            logger.debug(
                "txn_candidate_skip",
                source=candidate.source_address,
                token=candidate.token_address,
                reason="cooldown",
                last_kick_at=last_kick["created_at"],
            )
            continue

        decisions.append(KickDecision(candidate=candidate, action=KickAction.KICK))

    return decisions
