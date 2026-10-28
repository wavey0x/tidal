"""Persist confirmed, substantial pricing blocks without caching execution quotes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from tidal.auction_price_units import AuctionFeasibility, auction_feasibility
from tidal.auction_versions import StartingPriceEncoding
from tidal.persistence import models
from tidal.persistence.repositories import KickPrepareStatusRepository
from tidal.transaction_service.types import KickCandidate


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc)


def _key(candidate: KickCandidate) -> tuple[str, str, str, str]:
    return (
        candidate.source_type,
        candidate.source_address,
        candidate.auction_address,
        candidate.token_address,
    )


def _detail(row: dict | None) -> dict:
    try:
        value = json.loads(row["detail_json"]) if row else {}
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


class KickFeasibilityHolds:
    """One daily hold per candidate, evaluated by the existing kick timer."""

    def __init__(self, session, *, chain_id: int, price_max_age_seconds: int) -> None:
        self.session = session
        self.chain_id = chain_id
        self.price_max_age_seconds = price_max_age_seconds
        self.repository = KickPrepareStatusRepository(session)

    def _row(self, candidate: KickCandidate) -> dict | None:
        table = models.kick_prepare_status_latest
        row = (
            self.session.execute(
                select(table).where(
                    *(
                        table.c[name] == value
                        for name, value in zip(
                            (
                                "source_type",
                                "source_address",
                                "auction_address",
                                "token_address",
                            ),
                            _key(candidate),
                        )
                    )
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def _prices(self, candidate: KickCandidate, now: datetime) -> Decimal | None:
        """An observation can wake a candidate, never authorize a transaction."""
        prices = {}
        for row in self.session.execute(
            select(models.tokens).where(
                models.tokens.c.address.in_(
                    [candidate.token_address, candidate.want_address]
                ),
                models.tokens.c.chain_id == self.chain_id,
            )
        ).mappings():
            try:
                price = Decimal(str(row["price_usd"]))
                age = (now - _time(row["price_fetched_at"])).total_seconds()
                if (
                    row["price_status"] == "SUCCESS"
                    and price.is_finite()
                    and price > 0
                    and 0 <= age <= self.price_max_age_seconds
                ):
                    prices[row["address"]] = price
            except (ValueError, TypeError, InvalidOperation):
                continue
        if len(prices) != 2:
            return None
        return prices[candidate.token_address] / prices[candidate.want_address]

    def deferred(
        self,
        candidate: KickCandidate,
        *,
        context: dict,
        sell_amount: int,
        live_balance: int | None = None,
        now: datetime | None = None,
    ) -> dict | None:
        now = now or datetime.now(timezone.utc)
        row = self._row(candidate)
        if row is None or row["status"] != "PAUSED":
            return None
        try:
            detail = _detail(row)
            if detail["context"] != context or context["chainId"] != self.chain_id:
                return None
            if now >= _time(detail["nextFullCheckAt"]):
                return None
            scale = Decimal(sell_amount) / Decimal(detail["sellAmountRaw"])
            market = self._prices(candidate, now)
            previous_market = detail.get("marketRatio")
            if market is not None:
                if previous_market is None:
                    # Newly available observations deserve one fresh confirmation.
                    return None
                scale *= market / Decimal(previous_market)
            high = max(1, int(Decimal(detail["highQuoteAmountRaw"]) * scale))
            floor = max(1, int(Decimal(detail["floorQuoteAmountRaw"]) * scale))
            assessment = self.assess(
                context, sell_amount=sell_amount, high=high, floor=floor
            )
            if not assessment.clearly_blocked:
                return None
            if (
                live_balance is not None
                and str(live_balance) != row["source_balance_raw"]
            ):
                detail = detail | {"sourceBalanceRaw": str(live_balance)}
                self._save(candidate, "PAUSED", detail, now)
            return detail
        except (ValueError, TypeError, KeyError, InvalidOperation, ZeroDivisionError):
            # Old, incomplete or corrupt rows do not suppress a fresh evaluation.
            return None

    @staticmethod
    def assess(
        context: dict, *, sell_amount: int, high: int, floor: int
    ) -> AuctionFeasibility:
        return auction_feasibility(
            amount_out_raw=high,
            floor_quote_raw=floor,
            sell_amount_raw=sell_amount,
            encoding=StartingPriceEncoding(context["encoding"]),
            sell_decimals=context["sellDecimals"],
            want_decimals=context["wantDecimals"],
            start_buffer_bps=context["startBufferBps"],
            min_buffer_bps=context["minBufferBps"],
            step_decay_rate_bps=context["decayBps"],
            step_duration_seconds=context["stepSeconds"],
            auction_length_seconds=context["lengthSeconds"],
        )

    def blocked(
        self,
        candidate: KickCandidate,
        *,
        context: dict,
        assessment: AuctionFeasibility,
        high: int,
        floor: int,
        sell_amount: int,
        live_balance: int,
        now: datetime | None = None,
    ) -> dict | None:
        if not assessment.clearly_blocked:
            self.recovered(candidate)
            return None
        now = now or datetime.now(timezone.utc)
        old = self._row(candidate)
        previous = _detail(old)
        opened = (
            previous.get("holdOpenedAt") if old and old["status"] == "PAUSED" else None
        )
        market = self._prices(candidate, now)
        detail = {
            "context": context,
            "sourceBalanceRaw": str(live_balance),
            "sellAmountRaw": str(sell_amount),
            "highQuoteAmountRaw": str(high),
            "floorQuoteAmountRaw": str(floor),
            "minimumFullLotQuoteRaw": str(assessment.minimum_full_lot_raw),
            "terminalAskRaw": str(assessment.terminal_ask_raw),
            "requiredQuoteRaw": str(assessment.required_quote_raw),
            "wantDecimals": context["wantDecimals"],
            "marketRatio": str(market) if market is not None else None,
            "holdOpenedAt": opened or now.isoformat(),
            "lastFullCheckAt": now.isoformat(),
            "nextFullCheckAt": (now + timedelta(days=1)).isoformat(),
        }
        self._save(candidate, "PAUSED", detail, now)
        return detail

    def recovered(
        self, candidate: KickCandidate, *, now: datetime | None = None
    ) -> None:
        row = self._row(candidate)
        if row and row["status"] == "PAUSED":
            detail = _detail(row)
            if "holdOpenedAt" in detail:
                self._save(
                    candidate, "RECOVERED", detail, now or datetime.now(timezone.utc)
                )

    def _save(
        self, candidate: KickCandidate, status: str, detail: dict, now: datetime
    ) -> None:
        names = ("source_type", "source_address", "auction_address", "token_address")
        row = dict(zip(names, _key(candidate))) | {
            "status": status,
            "reason": "AUCTION_PRICE_GRANULARITY",
            "source_balance_raw": detail["sourceBalanceRaw"],
            "detail_json": json.dumps(detail, sort_keys=True),
            "checked_at": now.isoformat(),
        }
        self.repository.replace_for_candidates([_key(candidate)], [row])
