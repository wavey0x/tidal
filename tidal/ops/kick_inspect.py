"""Explainability helpers for kick candidate inspection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from tidal.persistence.repositories import KickTxRepository
from tidal.runtime import build_txn_service
from tidal.transaction_service.evaluator import build_shortlist
from tidal.transaction_service.types import AuctionInspection, KickCandidate, SourceType


def _candidate_key(candidate: KickCandidate) -> tuple[str, str]:
    return candidate.auction_address, candidate.token_address


@dataclass(slots=True)
class KickInspectEntry:
    state: str
    source_type: str
    source_address: str
    source_name: str | None
    auction_address: str
    token_address: str
    token_symbol: str | None
    want_symbol: str | None
    normalized_balance: str
    usd_value: float
    detail: str | None = None
    auction_active: bool | None = None
    active_token: str | None = None
    active_tokens: tuple[str, ...] = ()
    minimum_price_raw: int | None = None


@dataclass(slots=True)
class KickInspectResult:
    source_type: SourceType | None
    source_address: str | None
    auction_address: str | None
    limit: int | None
    eligible_count: int
    selected_count: int
    ready_count: int
    ignored_count: int
    cooldown_count: int
    deferred_same_auction_count: int
    limited_count: int
    ready: list[KickInspectEntry]
    ignored_skips: list[KickInspectEntry]
    cooldown_skips: list[KickInspectEntry]
    deferred_same_auction: list[KickInspectEntry]
    limited: list[KickInspectEntry]


def inspect_kick_candidates(
    session,
    settings,
    *,
    source_type: SourceType | None = None,
    source_address: str | None = None,
    auction_address: str | None = None,
    token_address: str | None = None,
    limit: int | None = None,
    include_live_inspection: bool = True,
) -> KickInspectResult:
    kick_config = settings.kick_config
    shortlist = build_shortlist(
        session,
        usd_threshold=settings.txn_usd_threshold,
        max_data_age_seconds=settings.txn_max_data_age_seconds,
        source_type=source_type,
        source_address=source_address,
        auction_address=auction_address,
        token_address=token_address,
        limit=limit,
        ignore_policy=kick_config.ignore_policy,
        cooldown_policy=kick_config.cooldown_policy,
        kick_tx_repository=KickTxRepository(session),
    )
    ready_candidates = shortlist.selected_candidates
    ready_inspections: dict[tuple[str, str], AuctionInspection] = {}
    if include_live_inspection and settings.rpc_url and ready_candidates:
        txn_service = build_txn_service(settings, session)
        ready_inspections = asyncio.run(txn_service.preparer.inspect_candidates(ready_candidates))

    def build_entry(
        candidate: KickCandidate,
        *,
        state: str,
        detail: str | None = None,
    ) -> KickInspectEntry:
        inspection = ready_inspections.get(_candidate_key(candidate))
        return KickInspectEntry(
            state=state,
            source_type=candidate.source_type,
            source_address=candidate.source_address,
            source_name=candidate.source_name,
            auction_address=candidate.auction_address,
            token_address=candidate.token_address,
            token_symbol=candidate.token_symbol,
            want_symbol=candidate.want_symbol,
            normalized_balance=candidate.normalized_balance,
            usd_value=candidate.usd_value,
            detail=detail,
            auction_active=inspection.is_active_auction if inspection is not None else None,
            active_token=inspection.active_token if inspection is not None else None,
            active_tokens=inspection.active_tokens if inspection is not None else (),
            minimum_price_raw=inspection.minimum_price_raw if inspection is not None else None,
        )

    ready_entries = [build_entry(candidate, state="ready") for candidate in ready_candidates]
    ignored_entries = [
        build_entry(decision.candidate, state="ignored", detail=decision.detail)
        for decision in shortlist.ignored_skips
    ]
    cooldown_entries = [
        build_entry(decision.candidate, state="cooldown", detail=decision.detail)
        for decision in shortlist.cooldown_skips
    ]
    deferred_entries = [
        build_entry(candidate, state="deferred_same_auction")
        for candidate in shortlist.deferred_same_auction_candidates
    ]
    limited_entries = [build_entry(candidate, state="limited") for candidate in shortlist.limited_candidates]
    return KickInspectResult(
        source_type=source_type,
        source_address=source_address,
        auction_address=auction_address,
        limit=limit,
        eligible_count=len(shortlist.eligible_candidates),
        selected_count=len(shortlist.selected_candidates) + len(shortlist.limited_candidates),
        ready_count=len(ready_entries),
        ignored_count=len(ignored_entries),
        cooldown_count=len(cooldown_entries),
        deferred_same_auction_count=len(deferred_entries),
        limited_count=len(limited_entries),
        ready=ready_entries,
        ignored_skips=ignored_entries,
        cooldown_skips=cooldown_entries,
        deferred_same_auction=deferred_entries,
        limited=limited_entries,
    )
