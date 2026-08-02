"""Derived public operational Alerts read model and notification transitions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from tidal.alerts.base import AlertMessage
from tidal.automation_scope import current_automation_pairs, pair_in_automation_scope
from tidal.auction_rounds import NoFillGuard, NoFillReason, RoundOutcome
from tidal.normalizers import normalize_address
from tidal.persistence import models
from tidal.persistence.repositories import KickTxRepository


@dataclass(frozen=True, slots=True)
class AlertItem:
    id: str
    occurrence_id: str
    kind: str
    severity: str
    status: str
    title: str
    summary: str
    opened_at: str
    updated_at: str
    retry_at: str | None
    scope: dict[str, object]
    evidence: dict[str, object]
    links: dict[str, str]
    next_action: dict[str, str]


@dataclass(frozen=True, slots=True)
class AlertEvaluation:
    evaluated_at: str
    latest_successful_scan_at: str | None
    needs_action_count: int
    items: tuple[AlertItem, ...]
    transitions: tuple[AlertMessage, ...]

    def api_payload(self) -> dict[str, object]:
        return {
            "evaluatedAt": self.evaluated_at,
            "latestSuccessfulScanAt": self.latest_successful_scan_at,
            "needsActionCount": self.needs_action_count,
            "items": [_camel_item(item) for item in self.items],
        }


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def _camel_item(item: AlertItem) -> dict[str, object]:
    return {
        "id": item.id,
        "occurrenceId": item.occurrence_id,
        "kind": item.kind,
        "severity": item.severity,
        "status": item.status,
        "title": item.title,
        "summary": item.summary,
        "openedAt": item.opened_at,
        "updatedAt": item.updated_at,
        "retryAt": item.retry_at,
        "scope": item.scope,
        "evidence": item.evidence,
        "links": item.links,
        "nextAction": item.next_action,
    }


class AlertService:
    def __init__(self, *, session, settings) -> None:  # noqa: ANN001
        self.session = session
        self.settings = settings
        self.kick_repo = KickTxRepository(session)

    def evaluate(self, *, now: datetime | None = None) -> AlertEvaluation:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        items: list[AlertItem] = []
        transitions: list[AlertMessage] = []
        auction_items, auction_transitions = self._auction_retry_items(current)
        items.extend(auction_items)
        transitions.extend(auction_transitions)
        scan_item, scan_transition, latest_success = self._scan_unhealthy(current)
        if scan_item is not None:
            items.append(scan_item)
        if scan_transition is not None:
            transitions.append(scan_transition)
        repeated_items, repeated_transitions = self._repeated_scan_failures()
        items.extend(repeated_items)
        transitions.extend(repeated_transitions)
        severity_rank = {"critical": 0, "warning": 1, "info": 2}
        items.sort(
            key=lambda item: (
                0 if item.status == "needs_action" else 1,
                severity_rank.get(item.severity, 9),
                item.retry_at or item.opened_at,
                item.id,
            )
        )
        return AlertEvaluation(
            evaluated_at=current.isoformat(),
            latest_successful_scan_at=latest_success,
            needs_action_count=sum(item.status == "needs_action" for item in items),
            items=tuple(items),
            transitions=tuple(transitions),
        )

    def _auction_retry_items(
        self, now: datetime
    ) -> tuple[list[AlertItem], list[AlertMessage]]:
        kicks = self.kick_repo.list_confirmed_kicks()
        pairs = sorted(
            {
                (
                    normalize_address(str(row["auction_address"])),
                    normalize_address(str(row["token_address"])),
                )
                for row in kicks
            }
        )
        if not pairs:
            return [], []
        guard = NoFillGuard(
            self.kick_repo,
            self.settings.kick_config.no_fill_policy.retry_delays_minutes,
        )
        items: list[AlertItem] = []
        transitions: list[AlertMessage] = []
        current_pairs = current_automation_pairs(self.session, self.settings)
        for auction_address, token_address in pairs:
            rows = self.kick_repo.list_pair_operations(auction_address, token_address)
            latest_kick = next(
                (
                    row
                    for row in reversed(rows)
                    if row.get("operation_type") == "kick"
                    and row.get("status") in {"CONFIRMED", "SUBMITTED"}
                ),
                None,
            )
            if latest_kick is None or not pair_in_automation_scope(
                self.session,
                current_pairs,
                latest_kick,
            ):
                continue
            decision = guard.decide(
                auction_address=auction_address,
                token_address=token_address,
                now=now,
            )
            latest = decision.sequence.latest
            if latest is None or latest.outcome == RoundOutcome.PRODUCTIVE:
                continue
            if latest.outcome == RoundOutcome.INCOMPLETE:
                continue
            item_id = f"auction_retry:{auction_address}:{token_address}"
            if latest.outcome == RoundOutcome.UNKNOWN:
                anchor_id = latest.close_id or latest.kick_id
                occurrence_id = f"{item_id}:{anchor_id}"
                item = self._auction_item(
                    item_id=item_id,
                    occurrence_id=occurrence_id,
                    latest_kick=latest_kick,
                    decision=decision,
                    severity="warning",
                    status="needs_action",
                    title="Auction outcome needs review",
                    summary=f"Canonical round evidence is ambiguous ({latest.reason_code}). Automation is paused.",
                    retry_at=None,
                    next_command=None,
                )
                items.append(item)
                transitions.append(self._message(item, f"auction_unknown:{anchor_id}"))
                continue

            no_fill_rounds = decision.sequence.no_fill_rounds
            oldest = no_fill_rounds[-1]
            occurrence_id = f"{item_id}:{oldest.close_id or oldest.kick_id}"
            exhausted = decision.reason_code == NoFillReason.RETRY_EXHAUSTED
            if exhausted:
                item = self._auction_item(
                    item_id=item_id,
                    occurrence_id=occurrence_id,
                    latest_kick=latest_kick,
                    decision=decision,
                    severity="critical",
                    status="needs_action",
                    title="Auction retry budget exhausted",
                    summary=f"{decision.consecutive_no_fills} consecutive no-fill rounds paused automation.",
                    retry_at=None,
                    next_command=(
                        f"tidal kick run --auction {auction_address} --token {token_address} "
                        "--allow-no-fill-retry"
                    ),
                )
                items.append(item)
                transitions.append(
                    self._message(item, f"auction_retry_exhausted:{latest.kick_id}")
                )
                continue

            retry_at = (
                decision.retry_at.isoformat() if decision.retry_at is not None else None
            )
            item = self._auction_item(
                item_id=item_id,
                occurrence_id=occurrence_id,
                latest_kick=latest_kick,
                decision=decision,
                severity="info",
                status="watching",
                title=f"Auction retry {decision.retry_ordinal} of {decision.retry_total} scheduled",
                summary="A confirmed no-fill entered bounded retry backoff.",
                retry_at=retry_at,
                next_command=None,
            )
            items.append(item)
            transitions.append(self._message(item, f"retry_backoff:{latest.kick_id}"))
        return items, transitions

    def _auction_item(
        self,
        *,
        item_id: str,
        occurrence_id: str,
        latest_kick: dict[str, object],
        decision,  # noqa: ANN001
        severity: str,
        status: str,
        title: str,
        summary: str,
        retry_at: str | None,
        next_command: str | None,
    ) -> AlertItem:
        sequence = decision.sequence
        evidence_rows = {
            int(row["id"]): row
            for row in self.kick_repo.list_pair_operations(
                decision.auction_address,
                decision.token_address,
            )
        }
        rounds = []
        for round_ in reversed(sequence.rounds):
            kick_row = evidence_rows.get(round_.kick_id, {})
            close_row = evidence_rows.get(round_.close_id or -1, {})
            rounds.append(
                {
                    "kickId": round_.kick_id,
                    "closeId": round_.close_id,
                    "outcome": round_.outcome.value,
                    "reasonCode": round_.reason_code,
                    "requestedAmount": round_.requested_amount,
                    "placedAmount": round_.placed_amount,
                    "recoveredAmount": round_.recovered_amount,
                    "kickAt": round_.kick_at.isoformat() if round_.kick_at else None,
                    "closeAt": round_.close_at.isoformat() if round_.close_at else None,
                    "kickTxHash": kick_row.get("tx_hash"),
                    "closeTxHash": close_row.get("tx_hash"),
                    "minimumQuote": kick_row.get("minimum_quote"),
                    "quoteAmount": kick_row.get("quote_amount"),
                    "providers": _provider_evidence(
                        kick_row.get("quote_response_json")
                    ),
                }
            )
        no_fill_rounds = sequence.no_fill_rounds
        if no_fill_rounds:
            oldest_no_fill = no_fill_rounds[-1]
            newest_no_fill = no_fill_rounds[0]
            opened = (
                oldest_no_fill.close_at
                or oldest_no_fill.kick_at
                or datetime.now(timezone.utc)
            )
            updated = newest_no_fill.close_at or newest_no_fill.kick_at or opened
        else:
            latest_round = sequence.latest
            opened = (
                (latest_round.close_at or latest_round.kick_at)
                if latest_round is not None
                else None
            ) or datetime.now(timezone.utc)
            updated = opened
        kick_id = sequence.latest.kick_id if sequence.latest else int(latest_kick["id"])
        tx_hash = str(latest_kick.get("tx_hash") or "")
        links = {
            "logs": f"/logs?kick_id={kick_id}",
            "etherscan": f"https://etherscan.io/tx/{tx_hash}" if tx_hash else "",
            "auctionScan": (
                f"https://auctionscan.info/auction/{self.settings.chain_id}/{decision.auction_address}"
            ),
        }
        source_address = latest_kick.get("source_address") or latest_kick.get(
            "strategy_address"
        )
        return AlertItem(
            id=item_id,
            occurrence_id=occurrence_id,
            kind="auction_retry",
            severity=severity,
            status=status,
            title=title,
            summary=summary,
            opened_at=opened.isoformat(),
            updated_at=updated.isoformat(),
            retry_at=retry_at,
            scope={
                "sourceType": latest_kick.get("source_type"),
                "sourceAddress": source_address,
                "auctionAddress": decision.auction_address,
                "tokenAddress": decision.token_address,
                "kickId": kick_id,
            },
            evidence={
                "decision": decision.action.value,
                "reasonCode": decision.reason_code.value,
                "consecutiveNoFills": decision.consecutive_no_fills,
                "retryOrdinal": decision.retry_ordinal,
                "retryTotal": decision.retry_total,
                "rounds": rounds,
            },
            links=links,
            next_action={
                "instruction": (
                    "Review the round evidence before a deliberate scoped retry."
                    if next_command
                    else "Wait for the scheduled retry; no action is required."
                ),
                **({"command": next_command} if next_command else {}),
            },
        )

    def _scan_unhealthy(
        self,
        now: datetime,
    ) -> tuple[AlertItem | None, AlertMessage | None, str | None]:
        completed = [
            dict(row)
            for row in self.session.execute(
                select(models.scan_runs)
                .where(models.scan_runs.c.status != "RUNNING")
                .order_by(models.scan_runs.c.started_at.desc())
            ).mappings()
        ]
        successful = next(
            (row for row in completed if row["status"] == "SUCCESS"), None
        )
        latest_success_at = (
            str(successful["finished_at"])
            if successful and successful.get("finished_at")
            else None
        )
        latest = completed[0] if completed else None
        newly_failed = latest is not None and latest["status"] == "FAILED"
        success_time = _parse_time(latest_success_at)
        stale = success_time is None or now > success_time + timedelta(
            minutes=self.settings.scan_stale_after_minutes
        )
        if not newly_failed and not stale:
            return None, None, latest_success_at
        anchor = (
            str(latest["run_id"])
            if newly_failed
            else str(successful["run_id"] if successful else "missing")
        )
        item_id = "scan_unhealthy:scanner"
        occurrence_id = f"{item_id}:{anchor}"
        opened = (
            str(latest.get("finished_at") or latest["started_at"])
            if newly_failed and latest is not None
            else latest_success_at or now.isoformat()
        )
        title = "Scanner run failed" if newly_failed else "Scanner data is stale"
        summary = (
            "The latest scanner run failed; cached operational state may be incomplete."
            if newly_failed
            else f"No successful scan completed within {self.settings.scan_stale_after_minutes} minutes."
        )
        links = {"logs": f"/logs?run_id={anchor}" if anchor != "missing" else "/logs"}
        item = AlertItem(
            id=item_id,
            occurrence_id=occurrence_id,
            kind="scan_unhealthy",
            severity="critical",
            status="needs_action",
            title=title,
            summary=summary,
            opened_at=opened,
            updated_at=str(latest.get("finished_at") or latest["started_at"])
            if latest
            else now.isoformat(),
            retry_at=None,
            scope={"runId": latest.get("run_id") if latest else None},
            evidence={
                "latestStatus": latest.get("status") if latest else None,
                "latestSuccessfulScanAt": latest_success_at,
                "staleAfterMinutes": self.settings.scan_stale_after_minutes,
            },
            links=links,
            next_action={
                "instruction": "Inspect scanner logs and restore successful scans."
            },
        )
        transition = (
            self._message(item, f"scan_failed:{latest['run_id']}")
            if newly_failed
            else None
        )
        return item, transition, latest_success_at

    def _repeated_scan_failures(self) -> tuple[list[AlertItem], list[AlertMessage]]:
        runs = [
            dict(row)
            for row in self.session.execute(
                select(models.scan_runs)
                .where(models.scan_runs.c.status.in_(("SUCCESS", "PARTIAL_SUCCESS")))
                .order_by(models.scan_runs.c.started_at.desc())
            ).mappings()
        ]
        if len(runs) < 3:
            return [], []
        errors_by_run: list[dict[tuple[object, ...], dict[str, object]]] = []
        for run in runs:
            mapping: dict[tuple[object, ...], dict[str, object]] = {}
            for row in self.session.execute(
                select(models.scan_item_errors).where(
                    models.scan_item_errors.c.run_id == run["run_id"],
                    models.scan_item_errors.c.stage != "PRICE_READ",
                )
            ).mappings():
                identity = (
                    row["source_type"],
                    row["source_address"],
                    row["token_address"],
                    row["stage"],
                    row["error_code"],
                )
                mapping[identity] = dict(row)
            errors_by_run.append(mapping)
        repeated = set(errors_by_run[0]).intersection(
            errors_by_run[1], errors_by_run[2]
        )
        items: list[AlertItem] = []
        transitions: list[AlertMessage] = []
        for identity in sorted(
            repeated, key=lambda value: tuple(str(item or "") for item in value)
        ):
            newest = errors_by_run[0][identity]
            oldest = newest
            for run_errors in errors_by_run:
                matching = run_errors.get(identity)
                if matching is None:
                    break
                oldest = matching
            scope_key = ":".join(str(value or "-") for value in identity)
            item_id = f"scan_item_repeated_failure:{scope_key}"
            occurrence_id = f"{item_id}:{oldest['id']}"
            item = AlertItem(
                id=item_id,
                occurrence_id=occurrence_id,
                kind="scan_item_repeated_failure",
                severity="warning",
                status="needs_action",
                title="Scan item failed three runs in a row",
                summary=f"{newest['stage']} continues to fail with {newest['error_code']}.",
                opened_at=str(oldest["created_at"]),
                updated_at=str(newest["created_at"]),
                retry_at=None,
                scope={
                    "sourceType": newest["source_type"],
                    "sourceAddress": newest["source_address"],
                    "tokenAddress": newest["token_address"],
                    "runId": newest["run_id"],
                },
                evidence={
                    "stage": newest["stage"],
                    "errorCode": newest["error_code"],
                    "runIds": [run["run_id"] for run in reversed(runs[:3])],
                },
                links={"logs": f"/logs?run_id={newest['run_id']}"},
                next_action={
                    "instruction": "Inspect the repeated item failure and correct its source."
                },
            )
            items.append(item)
            transitions.append(
                self._message(item, f"scan_item_repeated:{occurrence_id}")
            )
        return items, transitions

    @staticmethod
    def _message(item: AlertItem, delivery_key: str) -> AlertMessage:
        return AlertMessage(
            delivery_key=delivery_key,
            occurrence_id=item.occurrence_id,
            severity=item.severity,
            title=item.title,
            summary=item.summary,
            retry_at=item.retry_at,
            links=tuple(value for value in item.links.values() if value),
        )


def _provider_evidence(raw: object) -> dict[str, object]:
    if not raw:
        return {"entries": [], "spreadPct": None}
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"entries": [], "spreadPct": None}
    providers = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(providers, dict):
        return {"entries": [], "spreadPct": None}
    entries = []
    amounts: list[Decimal] = []
    for name, entry in sorted(providers.items()):
        if not isinstance(entry, dict):
            continue
        amount = entry.get("amount_out")
        entries.append(
            {"name": name, "status": entry.get("status"), "amountOut": amount}
        )
        try:
            parsed = Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if parsed > 0:
            amounts.append(parsed)
    spread = None
    if len(amounts) >= 2 and min(amounts) > 0:
        spread = str(
            ((max(amounts) - min(amounts)) / min(amounts) * Decimal(100)).quantize(
                Decimal("0.01")
            )
        )
    return {"entries": entries, "spreadPct": spread}
