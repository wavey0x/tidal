"""AuctionScan lookup and scan-time enrichment helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from tidal.config import Settings
from tidal.read.kick_logs import KickLogReadService
from tidal.time import utcnow_iso


class AuctionScanLookupError(RuntimeError):
    """Raised when AuctionScan cannot be queried successfully."""


@dataclass(slots=True)
class AuctionScanEnrichmentResult:
    candidates_seen: int = 0
    kicks_checked: int = 0
    kicks_resolved: int = 0
    kicks_unresolved: int = 0
    kicks_failed: int = 0
    error_messages: list[str] = field(default_factory=list)


class AuctionScanService:
    def __init__(self, session: Session, settings: Settings, *, persist: bool = True) -> None:
        self.session = session
        self.settings = settings
        self.persist = persist
        self.kick_logs = KickLogReadService(
            session,
            chain_id=settings.chain_id,
            auctionscan_base_url=settings.auctionscan_base_url,
        )

    async def resolve_kick_auctionscan(self, kick_id: int) -> dict[str, object]:
        kick = self.kick_logs.load_kick_auctionscan_context(kick_id)

        if kick["auctionscan_round_id"] is not None:
            return self.kick_logs.build_auctionscan_response(kick, resolved=True, cached=True)
        if not kick["eligible"]:
            return self.kick_logs.build_auctionscan_response(kick, resolved=False, cached=False)
        if self._should_skip_recheck(kick["auctionscan_last_checked_at"]):
            return self.kick_logs.build_auctionscan_response(kick, resolved=False, cached=False)

        round_payload = await self._lookup_round(
            auction_address=str(kick["auction_address"]),
            from_token=str(kick["token_address"]),
            transaction_hash=str(kick["tx_hash"]),
        )
        checked_at = utcnow_iso()
        if round_payload and round_payload.get("round_id") is not None:
            round_id = int(round_payload["round_id"])
            if self.persist:
                self.kick_logs.persist_auctionscan_match(
                    kick_id, round_id=round_id, checked_at=checked_at, matched_at=checked_at,
                )
            kick["auctionscan_round_id"] = round_id
            kick["auctionscan_last_checked_at"] = checked_at
            kick["auctionscan_matched_at"] = checked_at
            return self.kick_logs.build_auctionscan_response(kick, resolved=True, cached=False)

        if self.persist:
            self.kick_logs.persist_auctionscan_check(kick_id, checked_at=checked_at)
        kick["auctionscan_last_checked_at"] = checked_at
        return self.kick_logs.build_auctionscan_response(kick, resolved=False, cached=False)

    async def enrich_pending_kicks(self, *, limit: int) -> AuctionScanEnrichmentResult:
        result = AuctionScanEnrichmentResult()
        if limit <= 0:
            return result

        candidate_ids = self.kick_logs.list_pending_auctionscan_kick_ids(
            limit=limit,
            checked_before=self._build_checked_before_cutoff(),
        )
        result.candidates_seen = len(candidate_ids)

        for kick_id in candidate_ids:
            try:
                payload = await self.resolve_kick_auctionscan(kick_id)
            except Exception as exc:  # noqa: BLE001
                result.kicks_failed += 1
                result.error_messages.append(f"kick_id={kick_id} {exc}")
                continue

            result.kicks_checked += 1
            if payload.get("resolved"):
                result.kicks_resolved += 1
            else:
                result.kicks_unresolved += 1

        return result

    async def _lookup_round(self, *, auction_address: str, from_token: str, transaction_hash: str) -> dict[str, object] | None:
        params = {
            "chain_id": self.settings.chain_id,
            "from_token": from_token,
            "transaction_hash": transaction_hash,
        }
        request_url = (
            f"{self.settings.auctionscan_api_base_url.rstrip('/')}/auctions/"
            f"{auction_address}/rounds?{urlencode(params)}"
        )
        try:
            async with httpx.AsyncClient(timeout=self.settings.price_timeout_seconds) as client:
                response = await client.get(request_url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise AuctionScanLookupError(f"AuctionScan request failed: {exc}") from exc

        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise AuctionScanLookupError(f"AuctionScan request failed with HTTP {response.status_code}")

        payload = response.json()
        if not isinstance(payload, dict):
            return None
        rounds = payload.get("rounds")
        if not isinstance(rounds, list) or not rounds:
            return None
        first = rounds[0]
        return first if isinstance(first, dict) else None

    def _build_checked_before_cutoff(self) -> str | None:
        if self.settings.auctionscan_recheck_seconds <= 0:
            return None
        checked_before = datetime.now(timezone.utc) - timedelta(seconds=self.settings.auctionscan_recheck_seconds)
        return checked_before.isoformat()

    def _should_skip_recheck(self, last_checked_at: object) -> bool:
        if not last_checked_at or self.settings.auctionscan_recheck_seconds <= 0:
            return False
        try:
            checked_at = datetime.fromisoformat(str(last_checked_at).replace("Z", "+00:00"))
        except ValueError:
            return False
        delta = datetime.now(timezone.utc) - checked_at
        return delta.total_seconds() < self.settings.auctionscan_recheck_seconds
