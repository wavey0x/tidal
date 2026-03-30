import hashlib
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from tidal.api.app import create_app
from tidal.api.services.action_audit import create_prepared_action
from tidal.config import Settings
from tidal.persistence import models
from tidal.persistence.repositories import APIActionRepository

_TEST_API_KEY = "secret-token"


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "tidal.db",
        rpc_url="",
    )


def _init_db(settings: Settings) -> None:
    engine = create_engine(settings.database_url, future=True)
    models.metadata.create_all(engine)
    with Session(engine, future=True) as session:
        session.execute(
            models.api_keys.insert().values(
                label="tester",
                key_hash=hashlib.sha256(_TEST_API_KEY.encode()).hexdigest(),
                key_prefix=_TEST_API_KEY[:8],
                created_at="2026-03-28T00:00:00+00:00",
            )
        )
        session.commit()


def _seed_dashboard_data(settings: Settings) -> None:
    engine = create_engine(settings.database_url, future=True)
    with Session(engine, future=True) as session:
        session.execute(
            models.vaults.insert().values(
                address="0x1000000000000000000000000000000000000001",
                chain_id=1,
                name="Test Vault",
                symbol="tvTEST",
                active=1,
                first_seen_at="2026-03-28T00:00:00+00:00",
                last_seen_at="2026-03-28T00:00:00+00:00",
            )
        )
        session.execute(
            models.strategies.insert().values(
                address="0x2000000000000000000000000000000000000002",
                chain_id=1,
                vault_address="0x1000000000000000000000000000000000000001",
                name="Test Strategy",
                adapter="yearn_curve_strategy",
                active=1,
                auction_address="0x3000000000000000000000000000000000000003",
                want_address="0x4000000000000000000000000000000000000004",
                first_seen_at="2026-03-28T00:00:00+00:00",
                last_seen_at="2026-03-28T00:00:00+00:00",
            )
        )
        session.execute(
            models.tokens.insert(),
            [
                {
                    "address": "0x4000000000000000000000000000000000000004",
                    "chain_id": 1,
                    "name": "USDC",
                    "symbol": "USDC",
                    "decimals": 6,
                    "is_core_reward": 0,
                    "price_usd": "1",
                    "price_status": "SUCCESS",
                    "price_fetched_at": "2026-03-28T00:00:00+00:00",
                    "first_seen_at": "2026-03-28T00:00:00+00:00",
                    "last_seen_at": "2026-03-28T00:00:00+00:00",
                },
                {
                    "address": "0x5000000000000000000000000000000000000005",
                    "chain_id": 1,
                    "name": "CRV",
                    "symbol": "CRV",
                    "decimals": 18,
                    "is_core_reward": 0,
                    "price_usd": "0.5",
                    "price_status": "SUCCESS",
                    "price_fetched_at": "2026-03-28T00:00:00+00:00",
                    "first_seen_at": "2026-03-28T00:00:00+00:00",
                    "last_seen_at": "2026-03-28T00:00:00+00:00",
                },
            ],
        )
        session.execute(
            models.strategy_token_balances_latest.insert().values(
                strategy_address="0x2000000000000000000000000000000000000002",
                token_address="0x5000000000000000000000000000000000000005",
                raw_balance="1000000000000000000",
                normalized_balance="1.0",
                block_number=1,
                scanned_at="2026-03-28T00:00:00+00:00",
            )
        )
        session.commit()


def _seed_kick_log_rows(settings: Settings) -> None:
    engine = create_engine(settings.database_url, future=True)
    with Session(engine, future=True) as session:
        session.execute(
            models.kick_txs.insert(),
            [
                {
                    "run_id": "run-a",
                    "operation_type": "kick",
                    "source_type": "strategy",
                    "source_address": "0x2000000000000000000000000000000000000002",
                    "strategy_address": "0x2000000000000000000000000000000000000002",
                    "token_address": "0x5000000000000000000000000000000000000005",
                    "auction_address": "0x3000000000000000000000000000000000000003",
                    "token_symbol": "CRV",
                    "want_address": "0x4000000000000000000000000000000000000004",
                    "want_symbol": "USDC",
                    "status": "CONFIRMED",
                    "tx_hash": "0xaaa",
                    "quote_response_json": '{"requestUrl":"https://prices.example.com/1"}',
                    "created_at": "2026-03-28T00:03:00+00:00",
                },
                {
                    "run_id": "run-b",
                    "operation_type": "kick",
                    "source_type": "strategy",
                    "source_address": "0x2000000000000000000000000000000000000002",
                    "strategy_address": "0x2000000000000000000000000000000000000002",
                    "token_address": "0x5000000000000000000000000000000000000005",
                    "auction_address": "0x3000000000000000000000000000000000000004",
                    "token_symbol": "YFI",
                    "want_address": "0x4000000000000000000000000000000000000004",
                    "want_symbol": "USDC",
                    "status": "ERROR",
                    "tx_hash": "0xbbb",
                    "error_message": "failed",
                    "quote_response_json": '{"requestUrl":"https://prices.example.com/2"}',
                    "created_at": "2026-03-28T00:02:00+00:00",
                },
                {
                    "run_id": "run-c",
                    "operation_type": "kick",
                    "source_type": "strategy",
                    "source_address": "0x2000000000000000000000000000000000000002",
                    "strategy_address": "0x2000000000000000000000000000000000000002",
                    "token_address": "0x5000000000000000000000000000000000000005",
                    "auction_address": "0x3000000000000000000000000000000000000005",
                    "token_symbol": "CRV",
                    "want_address": "0x4000000000000000000000000000000000000004",
                    "want_symbol": "DAI",
                    "status": "CONFIRMED",
                    "tx_hash": "0xccc",
                    "quote_response_json": '{"requestUrl":"https://prices.example.com/3"}',
                    "created_at": "2026-03-28T00:01:00+00:00",
                },
            ],
        )
        session.commit()


def test_dashboard_endpoint_returns_rows(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    _init_db(settings)
    _seed_dashboard_data(settings)
    client = TestClient(create_app(settings))

    response = client.get(
        "/api/v1/tidal/dashboard",
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["data"]["summary"]["strategyCount"] == 1
    assert len(payload["data"]["rows"]) == 1
    assert payload["data"]["rows"][0]["sourceName"] == "Test Strategy"


def test_public_run_detail_redacts_secret_like_error_messages(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    _init_db(settings)
    engine = create_engine(settings.database_url, future=True)
    with Session(engine, future=True) as session:
        session.execute(
            models.scan_runs.insert().values(
                run_id="scan-secret",
                started_at="2026-03-28T00:00:00+00:00",
                finished_at="2026-03-28T00:01:00+00:00",
                status="FAILED",
                vaults_seen=0,
                strategies_seen=0,
                pairs_seen=0,
                pairs_succeeded=0,
                pairs_failed=1,
                error_summary=(
                    "RPC failed: "
                    "https://alice:supersecret@rpc.example/v1/mainnet?api_key=abc123&network=mainnet"
                ),
            )
        )
        session.execute(
            models.scan_item_errors.insert().values(
                run_id="scan-secret",
                source_type=None,
                source_address=None,
                strategy_address=None,
                token_address=None,
                stage="DISCOVERY",
                error_code="rpc_failed",
                error_message=(
                    "Authorization: Bearer secret-token "
                    "ETH_PASSWORD=hunter2 "
                    "https://user:pass@example.com/path?access_token=abc"
                ),
                created_at="2026-03-28T00:00:30+00:00",
            )
        )
        session.commit()

    client = TestClient(create_app(settings))
    response = client.get("/api/v1/tidal/logs/runs/scan-secret")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert "supersecret" not in payload["error_summary"]
    assert "abc123" not in payload["error_summary"]
    assert (
        "https://REDACTED:REDACTED@rpc.example/v1/mainnet?api_key=REDACTED&network=mainnet"
        in payload["error_summary"]
    )
    error_message = payload["errors"][0]["error_message"]
    assert "secret-token" not in error_message
    assert "hunter2" not in error_message
    assert "pass@example.com" not in error_message
    assert "Authorization: Bearer REDACTED" in error_message
    assert "ETH_PASSWORD=REDACTED" in error_message
    assert "https://REDACTED:REDACTED@example.com/path?access_token=REDACTED" in error_message


def test_kick_prepare_route_threads_curve_quote_override(tmp_path: Path, monkeypatch) -> None:
    settings = _make_settings(tmp_path)
    _init_db(settings)
    captured: dict[str, object] = {}

    async def fake_prepare_kick_action(session, settings, **kwargs):  # noqa: ANN001, ANN003
        del session, settings
        captured["require_curve_quote"] = kwargs.get("require_curve_quote")
        return "noop", [], {"preview": {}, "transactions": []}

    monkeypatch.setattr("tidal.api.routes.kick.prepare_kick_action", fake_prepare_kick_action)

    client = TestClient(create_app(settings))
    response = client.post(
        "/api/v1/tidal/kick/prepare",
        headers={"Authorization": f"Bearer {_TEST_API_KEY}"},
        json={
            "sourceType": "fee_burner",
            "auctionAddress": "0x3000000000000000000000000000000000000003",
            "tokenAddress": "0x5000000000000000000000000000000000000005",
            "sender": "0x6000000000000000000000000000000000000006",
            "requireCurveQuote": False,
        },
    )

    assert response.status_code == 200
    assert captured["require_curve_quote"] is False


def test_auction_settle_prepare_route_threads_sweep_override(tmp_path: Path, monkeypatch) -> None:
    settings = _make_settings(tmp_path)
    _init_db(settings)
    captured: dict[str, object] = {}

    async def fake_prepare_settle_action(settings, session, **kwargs):  # noqa: ANN001, ANN003
        del settings, session
        captured["sweep"] = kwargs.get("sweep")
        return "noop", [], {"preview": {}, "transactions": []}

    monkeypatch.setattr("tidal.api.routes.auctions.prepare_settle_action", fake_prepare_settle_action)

    client = TestClient(create_app(settings))
    response = client.post(
        "/api/v1/tidal/auctions/0x3000000000000000000000000000000000000003/settle/prepare",
        headers={"Authorization": f"Bearer {_TEST_API_KEY}"},
        json={
            "sender": "0x6000000000000000000000000000000000000006",
            "sweep": True,
        },
    )

    assert response.status_code == 200
    assert captured["sweep"] is True


def test_actions_broadcast_and_receipt_routes_update_status(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    _init_db(settings)
    app = create_app(settings)
    engine = create_engine(settings.database_url, future=True)
    with Session(engine, future=True) as session:
        action_id = create_prepared_action(
            session,
            operator_id="tester",
            action_type="deploy",
            sender="0x6000000000000000000000000000000000000006",
            request_payload={"receiver": "0x6000000000000000000000000000000000000006"},
            preview_payload={"receiver": "0x6000000000000000000000000000000000000006"},
            transactions=[
                {
                    "operation": "deploy",
                    "to": "0x7000000000000000000000000000000000000007",
                    "data": "0xdeadbeef",
                    "value": "0x0",
                    "chainId": 1,
                    "gasEstimate": 210000,
                    "gasLimit": 252000,
                }
            ],
            resource_address="0x6000000000000000000000000000000000000006",
        )

    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-token"}
    broadcast_response = client.post(
        f"/api/v1/tidal/actions/{action_id}/broadcast",
        headers=headers,
        json={
            "sender": "0x6000000000000000000000000000000000000006",
            "txHash": "0xabc",
            "broadcastAt": "2026-03-28T00:01:00+00:00",
            "txIndex": 0,
        },
    )
    assert broadcast_response.status_code == 200
    assert broadcast_response.json()["data"]["status"] == "BROADCAST_REPORTED"

    receipt_response = client.post(
        f"/api/v1/tidal/actions/{action_id}/receipt",
        headers=headers,
        json={
            "txIndex": 0,
            "receiptStatus": "CONFIRMED",
            "blockNumber": 123,
            "gasUsed": 210000,
            "gasPriceGwei": "0.1",
            "observedAt": "2026-03-28T00:02:00+00:00",
        },
    )
    assert receipt_response.status_code == 200
    assert receipt_response.json()["data"]["status"] == "CONFIRMED"

    list_response = client.get("/api/v1/tidal/actions", headers=headers)
    assert list_response.status_code == 200
    assert list_response.json()["data"]["items"][0]["actionId"] == action_id


def test_actions_broadcast_and_receipt_routes_are_idempotent(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    _init_db(settings)
    app = create_app(settings)
    engine = create_engine(settings.database_url, future=True)
    with Session(engine, future=True) as session:
        action_id = create_prepared_action(
            session,
            operator_id="tester",
            action_type="deploy",
            sender="0x6000000000000000000000000000000000000006",
            request_payload={"receiver": "0x6000000000000000000000000000000000000006"},
            preview_payload={"receiver": "0x6000000000000000000000000000000000000006"},
            transactions=[
                {
                    "operation": "deploy",
                    "to": "0x7000000000000000000000000000000000000007",
                    "data": "0xdeadbeef",
                    "value": "0x0",
                    "chainId": 1,
                    "gasEstimate": 210000,
                    "gasLimit": 252000,
                }
            ],
            resource_address="0x6000000000000000000000000000000000000006",
        )

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_TEST_API_KEY}"}
    broadcast_payload = {
        "sender": "0x6000000000000000000000000000000000000006",
        "txHash": "0xabc",
        "broadcastAt": "2026-03-28T00:01:00+00:00",
        "txIndex": 0,
    }
    receipt_payload = {
        "txIndex": 0,
        "receiptStatus": "CONFIRMED",
        "blockNumber": 123,
        "gasUsed": 210000,
        "gasPriceGwei": "0.1",
        "observedAt": "2026-03-28T00:02:00+00:00",
    }

    first_broadcast = client.post(f"/api/v1/tidal/actions/{action_id}/broadcast", headers=headers, json=broadcast_payload)
    assert first_broadcast.status_code == 200
    first_receipt = client.post(f"/api/v1/tidal/actions/{action_id}/receipt", headers=headers, json=receipt_payload)
    assert first_receipt.status_code == 200

    replay_broadcast = client.post(
        f"/api/v1/tidal/actions/{action_id}/broadcast",
        headers=headers,
        json={**broadcast_payload, "broadcastAt": "2026-03-28T00:03:00+00:00"},
    )
    assert replay_broadcast.status_code == 200
    assert replay_broadcast.json()["data"]["status"] == "CONFIRMED"
    assert replay_broadcast.json()["data"]["transactions"][0]["txHash"] == "0xabc"

    replay_receipt = client.post(f"/api/v1/tidal/actions/{action_id}/receipt", headers=headers, json=receipt_payload)
    assert replay_receipt.status_code == 200
    assert replay_receipt.json()["data"]["status"] == "CONFIRMED"


def test_actions_broadcast_route_rejects_conflicting_hash(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    _init_db(settings)
    app = create_app(settings)
    engine = create_engine(settings.database_url, future=True)
    with Session(engine, future=True) as session:
        action_id = create_prepared_action(
            session,
            operator_id="tester",
            action_type="deploy",
            sender="0x6000000000000000000000000000000000000006",
            request_payload={"receiver": "0x6000000000000000000000000000000000000006"},
            preview_payload={"receiver": "0x6000000000000000000000000000000000000006"},
            transactions=[
                {
                    "operation": "deploy",
                    "to": "0x7000000000000000000000000000000000000007",
                    "data": "0xdeadbeef",
                    "value": "0x0",
                    "chainId": 1,
                    "gasEstimate": 210000,
                    "gasLimit": 252000,
                }
            ],
            resource_address="0x6000000000000000000000000000000000000006",
        )

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_TEST_API_KEY}"}
    first_response = client.post(
        f"/api/v1/tidal/actions/{action_id}/broadcast",
        headers=headers,
        json={
            "sender": "0x6000000000000000000000000000000000000006",
            "txHash": "0xabc",
            "broadcastAt": "2026-03-28T00:01:00+00:00",
            "txIndex": 0,
        },
    )
    assert first_response.status_code == 200

    conflict_response = client.post(
        f"/api/v1/tidal/actions/{action_id}/broadcast",
        headers=headers,
        json={
            "sender": "0x6000000000000000000000000000000000000006",
            "txHash": "0xdef",
            "broadcastAt": "2026-03-28T00:02:00+00:00",
            "txIndex": 0,
        },
    )

    assert conflict_response.status_code == 409
    assert conflict_response.json()["status"] == "error"
    assert conflict_response.json()["detail"] == "Broadcast already recorded with a different tx hash"


def test_actions_broadcast_route_returns_json_when_database_is_locked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _make_settings(tmp_path)
    _init_db(settings)
    app = create_app(settings)
    engine = create_engine(settings.database_url, future=True)
    with Session(engine, future=True) as session:
        action_id = create_prepared_action(
            session,
            operator_id="tester",
            action_type="deploy",
            sender="0x6000000000000000000000000000000000000006",
            request_payload={"receiver": "0x6000000000000000000000000000000000000006"},
            preview_payload={"receiver": "0x6000000000000000000000000000000000000006"},
            transactions=[
                {
                    "operation": "deploy",
                    "to": "0x7000000000000000000000000000000000000007",
                    "data": "0xdeadbeef",
                    "value": "0x0",
                    "chainId": 1,
                    "gasEstimate": 210000,
                    "gasLimit": 252000,
                }
            ],
            resource_address="0x6000000000000000000000000000000000000006",
        )

    def always_locked(self, action_id: str, *, tx_index: int, tx_hash: str, broadcast_at: str) -> None:
        del self, action_id, tx_index, tx_hash, broadcast_at
        raise OperationalError(
            "UPDATE api_action_transactions SET tx_hash=?",
            {},
            sqlite3.OperationalError("database is locked"),
        )

    monkeypatch.setattr(APIActionRepository, "update_transaction_broadcast", always_locked)

    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-token"}
    response = client.post(
        f"/api/v1/tidal/actions/{action_id}/broadcast",
        headers=headers,
        json={
            "sender": "0x6000000000000000000000000000000000000006",
            "txHash": "0xabc",
            "broadcastAt": "2026-03-28T00:01:00+00:00",
            "txIndex": 0,
        },
    )

    assert response.status_code == 503
    assert response.json()["status"] == "error"
    assert response.json()["detail"] == "database is locked; retry the request"


def test_kick_action_broadcast_and_receipt_materialize_kick_logs(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    _init_db(settings)
    _seed_dashboard_data(settings)
    app = create_app(settings)
    engine = create_engine(settings.database_url, future=True)
    with Session(engine, future=True) as session:
        action_id = create_prepared_action(
            session,
            operator_id="tester",
            action_type="kick",
            sender="0x6000000000000000000000000000000000000006",
            request_payload={
                "sourceType": "strategy",
                "sourceAddress": "0x2000000000000000000000000000000000000002",
                "auctionAddress": "0x3000000000000000000000000000000000000003",
                "tokenAddress": "0x5000000000000000000000000000000000000005",
                "sender": "0x6000000000000000000000000000000000000006",
            },
            preview_payload={
                "preparedOperations": [
                    {
                        "operation": "kick",
                        "sourceType": "strategy",
                        "sourceAddress": "0x2000000000000000000000000000000000000002",
                        "sourceName": "Test Strategy",
                        "auctionAddress": "0x3000000000000000000000000000000000000003",
                        "tokenAddress": "0x5000000000000000000000000000000000000005",
                        "tokenSymbol": "CRV",
                        "wantAddress": "0x4000000000000000000000000000000000000004",
                        "wantSymbol": "USDC",
                        "sellAmount": "1.0",
                        "startingPrice": "2750",
                        "minimumPrice": "2375000000000000000000",
                        "minimumQuote": "2375",
                        "quoteAmount": "2500",
                        "quoteResponseJson": {
                            "requestUrl": (
                                "https://prices.example.com/v1/quote"
                                "?token_in=0x5000000000000000000000000000000000000005"
                                "&token_out=0x4000000000000000000000000000000000000004"
                                "&amount_in=1000000000000000000&chain_id=1&use_underlying=true&timeout_ms=7000"
                            )
                        },
                        "usdValue": "2500",
                        "bufferBps": 1000,
                        "minBufferBps": 500,
                        "stepDecayRateBps": 50,
                        "settleToken": None,
                    }
                ]
            },
            transactions=[
                {
                    "operation": "kick",
                    "to": "0x7000000000000000000000000000000000000007",
                    "data": "0xdeadbeef",
                    "value": "0x0",
                    "chainId": 1,
                    "gasEstimate": 210000,
                    "gasLimit": 252000,
                }
            ],
            resource_address="0x3000000000000000000000000000000000000003",
            auction_address="0x3000000000000000000000000000000000000003",
            source_address="0x2000000000000000000000000000000000000002",
            token_address="0x5000000000000000000000000000000000000005",
        )

    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-token"}
    tx_hash = "0xabc"

    broadcast_response = client.post(
        f"/api/v1/tidal/actions/{action_id}/broadcast",
        headers=headers,
        json={
            "sender": "0x6000000000000000000000000000000000000006",
            "txHash": tx_hash,
            "broadcastAt": "2026-03-28T00:01:00+00:00",
            "txIndex": 0,
        },
    )
    assert broadcast_response.status_code == 200

    receipt_response = client.post(
        f"/api/v1/tidal/actions/{action_id}/receipt",
        headers=headers,
        json={
            "txIndex": 0,
            "receiptStatus": "CONFIRMED",
            "blockNumber": 123,
            "gasUsed": 210000,
            "gasPriceGwei": "0.1",
            "observedAt": "2026-03-28T00:02:00+00:00",
        },
    )
    assert receipt_response.status_code == 200

    logs_response = client.get("/api/v1/tidal/logs/kicks", headers=headers)
    assert logs_response.status_code == 200
    payload = logs_response.json()
    assert payload["status"] == "ok"
    assert payload["data"]["total"] == 1
    assert payload["data"]["kicks"][0]["status"] == "CONFIRMED"
    assert payload["data"]["kicks"][0]["txHash"] == tx_hash
    assert payload["data"]["kicks"][0]["tokenSymbol"] == "CRV"
    assert payload["data"]["kicks"][0]["wantSymbol"] == "USDC"
    assert "prices.example.com/v1/quote" in payload["data"]["kicks"][0]["quoteResponseJson"]


def test_kick_logs_endpoint_supports_pagination_and_search(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    _init_db(settings)
    _seed_dashboard_data(settings)
    _seed_kick_log_rows(settings)
    client = TestClient(create_app(settings))
    headers = {"Authorization": f"Bearer {_TEST_API_KEY}"}

    first_page = client.get("/api/v1/tidal/logs/kicks?limit=2", headers=headers)
    assert first_page.status_code == 200
    first_payload = first_page.json()["data"]
    assert first_payload["total"] == 3
    assert first_payload["limit"] == 2
    assert first_payload["offset"] == 0
    assert first_payload["hasMore"] is True
    assert [row["runId"] for row in first_payload["kicks"]] == ["run-a", "run-b"]

    second_page = client.get("/api/v1/tidal/logs/kicks?limit=2&offset=2", headers=headers)
    assert second_page.status_code == 200
    second_payload = second_page.json()["data"]
    assert second_payload["hasMore"] is False
    assert [row["runId"] for row in second_payload["kicks"]] == ["run-c"]

    failed_only = client.get("/api/v1/tidal/logs/kicks?status=failed", headers=headers)
    assert failed_only.status_code == 200
    failed_payload = failed_only.json()["data"]
    assert failed_payload["total"] == 1
    assert failed_payload["kicks"][0]["runId"] == "run-b"

    search = client.get("/api/v1/tidal/logs/kicks?q=yfi", headers=headers)
    assert search.status_code == 200
    search_payload = search.json()["data"]
    assert search_payload["total"] == 1
    assert search_payload["kicks"][0]["tokenSymbol"] == "YFI"

    filtered_run = client.get("/api/v1/tidal/logs/kicks?run_id=run-c", headers=headers)
    assert filtered_run.status_code == 200
    run_payload = filtered_run.json()["data"]
    assert run_payload["total"] == 1
    assert run_payload["kicks"][0]["runId"] == "run-c"

    kick_id = first_payload["kicks"][0]["id"]
    exact_kick = client.get(f"/api/v1/tidal/logs/kicks?kick_id={kick_id}", headers=headers)
    assert exact_kick.status_code == 200
    exact_payload = exact_kick.json()["data"]
    assert exact_payload["total"] == 1
    assert exact_payload["kicks"][0]["id"] == kick_id
