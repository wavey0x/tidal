from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from tidal.alerts.base import AlertMessage
from tidal.alerts.dispatcher import AlertDispatcher
from tidal.alerts.service import AlertService
from tidal.alerts.telegram import TelegramAlertSink
from tidal.api.app import create_app
from tidal.config import Settings
from tidal.persistence import models
from tidal.persistence.db import Database
from tidal.runtime import build_alert_sink
from tidal.transaction_service.kick_policy import IgnorePolicy


AUCTION = "0x00000000000000000000000000000000000000a1"
TOKEN = "0x00000000000000000000000000000000000000b1"
SOURCE = "0x00000000000000000000000000000000000000c1"
NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'alerts.db'}")
    models.metadata.create_all(database.engine)
    session = database.session()
    session.execute(
        insert(models.strategies).values(
            address=SOURCE,
            chain_id=1,
            vault_address="0xvault",
            active=1,
            auction_address=AUCTION,
            want_address="0x00000000000000000000000000000000000000e1",
            first_seen_at=NOW.isoformat(),
            last_seen_at=NOW.isoformat(),
        )
    )
    session.execute(
        insert(models.tokens).values(
            address=TOKEN,
            chain_id=1,
            decimals=18,
            price_usd="1",
            price_status="SUCCESS",
            price_fetched_at=NOW.isoformat(),
            first_seen_at=NOW.isoformat(),
            last_seen_at=NOW.isoformat(),
        )
    )
    session.execute(
        insert(models.strategy_token_balances_latest).values(
            strategy_address=SOURCE,
            token_address=TOKEN,
            raw_balance="100",
            normalized_balance="100",
            block_number=100,
            scanned_at=NOW.isoformat(),
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        database.engine.dispose()


def _settings(*, stale_minutes: int = 90):
    return SimpleNamespace(
        chain_id=1,
        scan_stale_after_minutes=stale_minutes,
        txn_usd_threshold=1,
        txn_data_freshness_limit_seconds=86_400,
        kick_config=SimpleNamespace(
            no_fill_policy=SimpleNamespace(retry_delays_minutes=(720, 1440)),
            ignore_policy=IgnorePolicy(frozenset(), frozenset(), frozenset()),
        ),
    )


def _operation(
    row_id: int,
    operation_type: str,
    *,
    hour: int,
    round_kick_id: int | None = None,
    recovered: str = "100",
):
    mined_at = NOW + timedelta(hours=hour)
    values = {
        "id": row_id,
        "run_id": f"run-{row_id}",
        "operation_type": operation_type,
        "source_type": "strategy",
        "source_address": SOURCE,
        "strategy_address": SOURCE,
        "token_address": TOKEN,
        "auction_address": AUCTION,
        "status": "CONFIRMED",
        "tx_hash": f"0x{row_id:064x}",
        "block_number": 100 + hour,
        "transaction_index": 0,
        "mined_at": mined_at.isoformat(),
        "created_at": mined_at.isoformat(),
        "sell_amount": recovered,
        "round_kick_id": round_kick_id,
    }
    if operation_type == "kick":
        values["requested_sell_amount"] = recovered
    elif operation_type == "resolve_auction":
        values["resolution_path"] = 1
    return values


def _add_no_fill(session, kick_id: int, hour: int) -> None:
    session.execute(
        insert(models.kick_txs).values(**_operation(kick_id, "kick", hour=hour))
    )
    session.execute(
        insert(models.kick_txs).values(
            **_operation(
                kick_id + 1, "resolve_auction", hour=hour + 1, round_kick_id=kick_id
            )
        )
    )
    session.commit()


def _add_successful_scan(
    session, run_id: str = "scan-ok", *, at: datetime = NOW
) -> None:
    session.execute(
        insert(models.scan_runs).values(
            run_id=run_id,
            started_at=(at - timedelta(minutes=1)).isoformat(),
            finished_at=at.isoformat(),
            status="SUCCESS",
            vaults_seen=1,
            strategies_seen=1,
            pairs_seen=1,
            pairs_succeeded=1,
            pairs_failed=0,
        )
    )
    session.commit()


def test_no_fill_backoff_and_exhaustion_share_occurrence(session) -> None:
    _add_successful_scan(session)
    _add_no_fill(session, 1, 0)
    first = AlertService(
        session=session, settings=_settings(stale_minutes=180)
    ).evaluate(now=NOW + timedelta(hours=2))
    watching = next(item for item in first.items if item.kind == "auction_retry")
    assert watching.status == "watching"
    assert watching.retry_at == (NOW + timedelta(hours=13)).isoformat()
    assert first.needs_action_count == 0
    assert [transition.delivery_key for transition in first.transitions] == [
        "retry_backoff:1"
    ]

    _add_no_fill(session, 3, 14)
    _add_no_fill(session, 5, 40)
    exhausted = AlertService(session=session, settings=_settings()).evaluate(
        now=NOW + timedelta(days=3)
    )
    terminal = next(item for item in exhausted.items if item.kind == "auction_retry")
    assert terminal.status == "needs_action"
    assert terminal.severity == "critical"
    assert terminal.occurrence_id == watching.occurrence_id
    assert terminal.next_action["command"].endswith("--allow-no-fill-retry")
    assert any(
        message.delivery_key == "auction_retry_exhausted:5"
        for message in exhausted.transitions
    )


def test_fresh_scan_resolves_stale_alert_and_failed_scan_notifies(session) -> None:
    stale = AlertService(session=session, settings=_settings()).evaluate(now=NOW)
    assert any(item.kind == "scan_unhealthy" for item in stale.items)
    assert not stale.transitions

    _add_successful_scan(session)
    healthy = AlertService(session=session, settings=_settings()).evaluate(
        now=NOW + timedelta(minutes=1)
    )
    assert not any(item.kind == "scan_unhealthy" for item in healthy.items)

    session.execute(
        insert(models.scan_runs).values(
            run_id="scan-failed",
            started_at=(NOW + timedelta(minutes=2)).isoformat(),
            finished_at=(NOW + timedelta(minutes=3)).isoformat(),
            status="FAILED",
            vaults_seen=0,
            strategies_seen=0,
            pairs_seen=0,
            pairs_succeeded=0,
            pairs_failed=1,
        )
    )
    session.commit()
    failed = AlertService(session=session, settings=_settings()).evaluate(
        now=NOW + timedelta(minutes=4)
    )
    assert any(item.kind == "scan_unhealthy" for item in failed.items)
    assert any(
        message.delivery_key == "scan_failed:scan-failed"
        for message in failed.transitions
    )


def test_repeated_item_identity_includes_source_type(session) -> None:
    for index in range(3):
        run_id = f"scan-{index}"
        at = NOW + timedelta(minutes=index)
        _add_successful_scan(session, run_id, at=at)
        session.execute(
            insert(models.scan_item_errors).values(
                run_id=run_id,
                source_type="strategy",
                source_address=SOURCE,
                strategy_address=SOURCE,
                token_address=TOKEN,
                stage="BALANCE_READ",
                error_code="balance_read_failed",
                error_message=f"volatile text {index}",
                created_at=at.isoformat(),
            )
        )
        session.commit()
    evaluation = AlertService(session=session, settings=_settings()).evaluate(
        now=NOW + timedelta(minutes=4)
    )
    repeated = [
        item for item in evaluation.items if item.kind == "scan_item_repeated_failure"
    ]
    assert len(repeated) == 1
    assert repeated[0].scope["sourceType"] == "strategy"

    first_occurrence_id = repeated[0].occurrence_id
    fourth_at = NOW + timedelta(minutes=4)
    _add_successful_scan(session, "scan-3", at=fourth_at)
    session.execute(
        insert(models.scan_item_errors).values(
            run_id="scan-3",
            source_type="strategy",
            source_address=SOURCE,
            strategy_address=SOURCE,
            token_address=TOKEN,
            stage="BALANCE_READ",
            error_code="balance_read_failed",
            error_message="another volatile detail",
            created_at=fourth_at.isoformat(),
        )
    )
    session.commit()
    continuing = AlertService(session=session, settings=_settings()).evaluate(
        now=NOW + timedelta(minutes=5)
    )
    continued_item = next(
        item for item in continuing.items if item.kind == "scan_item_repeated_failure"
    )
    assert continued_item.occurrence_id == first_occurrence_id


def test_repeated_price_failures_remain_in_logs_without_becoming_alerts(
    session,
) -> None:
    for index in range(3):
        run_id = f"scan-price-{index}"
        at = NOW + timedelta(minutes=index)
        _add_successful_scan(session, run_id, at=at)
        session.execute(
            insert(models.scan_item_errors).values(
                run_id=run_id,
                token_address=TOKEN,
                stage="PRICE_READ",
                error_code="token_price_lookup_failed",
                error_message=f"provider failure {index}",
                created_at=at.isoformat(),
            )
        )
        session.commit()

    evaluation = AlertService(session=session, settings=_settings()).evaluate(
        now=NOW + timedelta(minutes=4)
    )

    assert not any(
        item.kind == "scan_item_repeated_failure" for item in evaluation.items
    )
    assert not any(
        message.delivery_key.startswith("scan_item_repeated:")
        for message in evaluation.transitions
    )


def test_ignored_and_inactive_pairs_are_suppressed(session) -> None:
    _add_successful_scan(session)
    _add_no_fill(session, 1, 0)
    ignored_settings = _settings(stale_minutes=180)
    ignored_settings.kick_config.ignore_policy = IgnorePolicy(
        frozenset(),
        frozenset(),
        frozenset({(AUCTION, TOKEN)}),
    )
    ignored = AlertService(session=session, settings=ignored_settings).evaluate(
        now=NOW + timedelta(hours=2)
    )
    assert not any(item.kind == "auction_retry" for item in ignored.items)

    session.execute(
        models.strategies.update()
        .where(models.strategies.c.address == SOURCE)
        .values(active=0)
    )
    session.commit()
    inactive = AlertService(
        session=session, settings=_settings(stale_minutes=180)
    ).evaluate(now=NOW + timedelta(hours=2))
    assert not any(item.kind == "auction_retry" for item in inactive.items)


class _Sink:
    destination_codes = ("admin_alerts", "operations_alerts")

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_admin = True

    async def send(self, destination_code: str, message: AlertMessage) -> None:
        del message
        self.calls.append(destination_code)
        if destination_code == "admin_alerts" and self.fail_admin:
            raise RuntimeError("secret transport detail")


@pytest.mark.asyncio
async def test_dispatcher_deduplicates_success_and_retries_only_failed_destination(
    session,
) -> None:
    sink = _Sink()
    dispatcher = AlertDispatcher(session=session, sink=sink)
    message = AlertMessage("key-1", "occ-1", "warning", "Title", "Summary", None, ())
    await dispatcher.dispatch((message,))
    sink.fail_admin = False
    await dispatcher.dispatch((message,))
    assert sink.calls == ["admin_alerts", "operations_alerts", "admin_alerts"]
    rows = session.execute(select(models.alert_deliveries)).mappings().all()
    assert len(rows) == 2
    assert all(row["sent_at"] for row in rows)
    assert all("secret" not in str(row["last_error"] or "") for row in rows)


@pytest.mark.asyncio
async def test_dispatcher_stops_after_three_failed_attempts(session) -> None:
    sink = _Sink()
    dispatcher = AlertDispatcher(session=session, sink=sink)
    message = AlertMessage("key-2", "occ-2", "warning", "Title", "Summary", None, ())
    for _ in range(5):
        await dispatcher.dispatch((message,))
    assert sink.calls.count("admin_alerts") == 3
    assert sink.calls.count("operations_alerts") == 1


def test_telegram_configuration_is_all_or_none() -> None:
    assert build_alert_sink(Settings()).destination_codes == ()
    with pytest.raises(ValueError):
        Settings(TELEGRAM_BOT_TOKEN="token")


@pytest.mark.asyncio
async def test_telegram_message_is_compact_escaped_and_disables_previews(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:  # noqa: ANN002
            del args

        async def post(self, url: str, *, json: dict[str, object]):
            captured["url"] = url
            captured["payload"] = json
            return Response()

    monkeypatch.setattr("tidal.alerts.telegram.httpx.AsyncClient", Client)
    sink = TelegramAlertSink(
        bot_token="secret-token",
        admin_alert_chat_id="admin-chat",
        operations_alert_chat_id="operations-chat",
        alerts_url="https://tidal.wavey.info/alerts",
    )
    message = AlertMessage(
        "key",
        "occurrence",
        "warning",
        "Auction <review>",
        "Evidence is ambiguous & automation is paused.",
        None,
        (
            "/logs?kick_id=1",
            "https://etherscan.io/tx/0xabc",
            "https://auctionscan.info/auction/1/0xabc",
        ),
    )

    await sink.send("admin_alerts", message)

    assert captured["payload"] == {
        "chat_id": "admin-chat",
        "text": (
            "<b>[TIDAL WARNING] Auction &lt;review&gt;</b>\n"
            "Evidence is ambiguous &amp; automation is paused.\n\n"
            '<a href="https://tidal.wavey.info/alerts">Tidal Alerts</a> · '
            '<a href="https://etherscan.io/tx/0xabc">Etherscan</a> · '
            '<a href="https://auctionscan.info/auction/1/0xabc">AuctionScan</a>'
        ),
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }


def test_alerts_endpoint_is_public_and_read_only(tmp_path) -> None:
    settings = Settings(DB_PATH=tmp_path / "api.db")
    app = create_app(settings)
    models.metadata.create_all(app.state.database.engine)
    with TestClient(app) as client:
        response = client.get("/api/v1/tidal/alerts")
    assert response.status_code == 200
    assert response.json()["data"]["needsActionCount"] == 1
