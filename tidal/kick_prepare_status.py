"""Latest operator-facing status from live kick preparation."""

from __future__ import annotations

import json
from collections.abc import Mapping

from sqlalchemy.orm import Session

from tidal.persistence.repositories import KickPrepareStatusRepository
from tidal.time import utcnow_iso
from tidal.transaction_service.types import KickSkipReason


def _candidate_key(item: Mapping[str, object]) -> tuple[str, str, str, str] | None:
    values = (
        item.get("sourceType"),
        item.get("sourceAddress"),
        item.get("auctionAddress"),
        item.get("tokenAddress"),
    )
    if not all(isinstance(value, str) and value for value in values):
        return None
    source_type, source_address, auction_address, token_address = values
    return (
        str(source_type),
        str(source_address),
        str(auction_address),
        str(token_address),
    )


def record_kick_prepare_status(
    session: Session,
    preview: Mapping[str, object] | None,
) -> None:
    """Replace pause rows for candidates evaluated by one prepare request."""

    if preview is None:
        return

    evaluated_keys: set[tuple[str, str, str, str]] = set()
    pause_rows: list[dict[str, object]] = []
    checked_at = utcnow_iso()

    prepared_operations = preview.get("preparedOperations")
    if isinstance(prepared_operations, list):
        for item in prepared_operations:
            if not isinstance(item, Mapping) or item.get("operation") != "kick":
                continue
            key = _candidate_key(item)
            if key is not None:
                evaluated_keys.add(key)

    skipped = preview.get("skippedDuringPrepare")
    if isinstance(skipped, list):
        for item in skipped:
            if not isinstance(item, Mapping):
                continue
            key = _candidate_key(item)
            if key is None:
                continue
            evaluated_keys.add(key)
            if item.get("reasonCode") != KickSkipReason.AUCTION_PRICE_GRANULARITY.value:
                continue
            reason_data = item.get("reasonData")
            if not isinstance(reason_data, Mapping):
                continue
            source_balance_raw = reason_data.get("sourceBalanceRaw")
            if not isinstance(source_balance_raw, str) or not source_balance_raw:
                continue
            source_type, source_address, auction_address, token_address = key
            pause_rows.append(
                {
                    "source_type": source_type,
                    "source_address": source_address,
                    "auction_address": auction_address,
                    "token_address": token_address,
                    "status": "PAUSED",
                    "reason": KickSkipReason.AUCTION_PRICE_GRANULARITY.value,
                    "source_balance_raw": source_balance_raw,
                    "detail_json": json.dumps(dict(reason_data), sort_keys=True),
                    "checked_at": checked_at,
                }
            )

    if evaluated_keys:
        KickPrepareStatusRepository(session).replace_for_candidates(
            evaluated_keys,
            pause_rows,
        )
