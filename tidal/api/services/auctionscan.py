"""API wrapper for AuctionScan lookup and persistence."""

from __future__ import annotations

from sqlalchemy.orm import Session

from tidal.api.errors import APIError
from tidal.auctionscan import AuctionScanLookupError, AuctionScanService as BaseAuctionScanService
from tidal.config import Settings


class AuctionScanService(BaseAuctionScanService):
    def __init__(self, session: Session, settings: Settings) -> None:
        super().__init__(session, settings, persist=False)

    async def resolve_kick_auctionscan(self, kick_id: int) -> dict[str, object]:
        try:
            return await super().resolve_kick_auctionscan(kick_id)
        except ValueError as exc:
            raise APIError(str(exc), status_code=404) from exc
        except AuctionScanLookupError as exc:
            raise APIError(str(exc), status_code=502) from exc
