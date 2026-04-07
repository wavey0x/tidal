"""Explainability helpers for kick candidate inspection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from itertools import chain

from tidal.auction_settlement import (
    PATH_NOOP,
    AuctionSettlementInspection,
    default_actionable_previews,
    inspect_auction_settlements,
    live_funded_previews,
    path_reason,
)
from tidal.normalizers import normalize_address
from tidal.persistence.repositories import KickTxRepository
from tidal.runtime import build_web3_client
from tidal.transaction_service.evaluator import build_shortlist
from tidal.transaction_service.types import KickCandidate, SourceType


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


@dataclass(slots=True)
class KickInspectResult:
    source_type: SourceType | None
    source_address: str | None
    auction_address: str | None
    limit: int | None
    eligible_count: int
    selected_count: int
    ready_count: int
    resolve_first_count: int
    blocked_live_count: int
    preview_failed_count: int
    ignored_count: int
    cooldown_count: int
    deferred_same_auction_count: int
    limited_count: int
    ready: list[KickInspectEntry]
    resolve_first: list[KickInspectEntry]
    blocked_live: list[KickInspectEntry]
    preview_failed: list[KickInspectEntry]
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
    inspections_by_auction: dict[str, AuctionSettlementInspection] = {}
    if include_live_inspection and settings.rpc_url and ready_candidates:
        auctions_to_inspect = sorted(
            {
                candidate.auction_address
                for candidate in chain(
                    shortlist.selected_candidates,
                    shortlist.deferred_same_auction_candidates,
                    shortlist.limited_candidates,
                )
            }
        )
        if auctions_to_inspect:
            web3_client = build_web3_client(settings)
            inspections_by_auction = asyncio.run(
                inspect_auction_settlements(
                    web3_client,
                    settings,
                    auctions_to_inspect,
                )
            )

    # Avoid repeated lot_previews passes per candidate.
    auction_classification: dict[str, tuple[str, str | None]] = {}
    auction_active_info: dict[str, tuple[bool | None, tuple[str, ...], str | None]] = {}
    for auction_addr, inspection in inspections_by_auction.items():
        preview_failures = inspection.preview_failures
        if preview_failures:
            messages = [preview.error_message or "resolve preview failed" for preview in preview_failures]
            auction_classification[auction_addr] = ("preview_failed", "; ".join(dict.fromkeys(messages)))
        else:
            actionable = default_actionable_previews(inspection)
            if actionable:
                reasons = [path_reason(int(preview.path or PATH_NOOP)) for preview in actionable]
                unique_reasons = list(dict.fromkeys(reasons))
                if len(unique_reasons) == 1:
                    detail = unique_reasons[0]
                else:
                    detail = ", ".join(unique_reasons[:3])
                    if len(unique_reasons) > 3:
                        detail += f", +{len(unique_reasons) - 3} more"
                auction_classification[auction_addr] = ("resolve_first", detail)
            else:
                live = live_funded_previews(inspection)
                if live:
                    if len(live) == 1:
                        auction_classification[auction_addr] = ("blocked_live", path_reason(int(live[0].path or PATH_NOOP)))
                    else:
                        auction_classification[auction_addr] = ("blocked_live", f"{len(live)} live funded lots")
                else:
                    auction_classification[auction_addr] = ("ready", None)

        active_tokens = tuple(
            preview.token_address
            for preview in inspection.lot_previews
            if preview.read_ok and preview.active is True
        )
        active_token = active_tokens[0] if len(active_tokens) == 1 else None
        auction_active_info[auction_addr] = (inspection.is_active_auction, active_tokens, active_token)

    def classify_selected_candidate(candidate: KickCandidate) -> tuple[str, str | None]:
        return auction_classification.get(normalize_address(candidate.auction_address), ("ready", None))

    def build_entry(
        candidate: KickCandidate,
        *,
        state: str,
        detail: str | None = None,
    ) -> KickInspectEntry:
        auction_key = normalize_address(candidate.auction_address)
        auction_active, active_tokens, active_token = auction_active_info.get(auction_key, (None, (), None))
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
            auction_active=auction_active,
            active_token=active_token,
            active_tokens=active_tokens,
        )

    ready_entries: list[KickInspectEntry] = []
    resolve_first_entries: list[KickInspectEntry] = []
    blocked_live_entries: list[KickInspectEntry] = []
    preview_failed_entries: list[KickInspectEntry] = []
    for candidate in ready_candidates:
        state, detail = classify_selected_candidate(candidate)
        entry = build_entry(candidate, state=state, detail=detail)
        if state == "resolve_first":
            resolve_first_entries.append(entry)
        elif state == "blocked_live":
            blocked_live_entries.append(entry)
        elif state == "preview_failed":
            preview_failed_entries.append(entry)
        else:
            ready_entries.append(entry)
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
        resolve_first_count=len(resolve_first_entries),
        blocked_live_count=len(blocked_live_entries),
        preview_failed_count=len(preview_failed_entries),
        ignored_count=len(ignored_entries),
        cooldown_count=len(cooldown_entries),
        deferred_same_auction_count=len(deferred_entries),
        limited_count=len(limited_entries),
        ready=ready_entries,
        resolve_first=resolve_first_entries,
        blocked_live=blocked_live_entries,
        preview_failed=preview_failed_entries,
        ignored_skips=ignored_entries,
        cooldown_skips=cooldown_entries,
        deferred_same_auction=deferred_entries,
        limited=limited_entries,
    )
