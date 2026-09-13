"""Retained legacy preview-to-operation mapping, independent of retired HTTP reports."""
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from tidal.api.app import create_app
from tidal.api.services.action_audit import create_prepared_action, record_broadcast
from test_api_control_plane import _make_settings, _init_db, _seed_dashboard_data


def _record_action_transaction(client, headers, *, action_id, tx_index, tx_hash, block_number=123):
    # Build retained pre-consolidation evidence. Network-facing API writes have
    # been retired; native migration/evidence tests separately verify adoption.
    del headers, block_number
    with Session(create_engine(client.app.state.settings.database_url)) as session:
        record_broadcast(session, action_id, tx_index=tx_index, tx_hash=tx_hash,
                         broadcast_at=f"2026-03-28T00:0{tx_index + 1}:00+00:00")


def test_legacy_kick_action_without_tx_index_materializes_through_logs_kicks(tmp_path: Path) -> None:
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

    _record_action_transaction(client, headers, action_id=action_id, tx_index=0, tx_hash=tx_hash)

    logs_response = client.get("/api/v1/tidal/logs/kicks", headers=headers)
    assert logs_response.status_code == 200
    payload = logs_response.json()
    assert payload["status"] == "ok"
    assert payload["data"]["total"] == 1
    # Client reports cannot manufacture mined evidence without RPC verification.
    assert payload["data"]["kicks"][0]["status"] == "SUBMITTED"
    assert payload["data"]["kicks"][0]["txHash"] == tx_hash
    assert payload["data"]["kicks"][0]["tokenSymbol"] == "CRV"
    assert payload["data"]["kicks"][0]["wantSymbol"] == "USDC"
    assert "prices.example.com/v1/quote" in payload["data"]["kicks"][0]["quoteResponseJson"]



def test_batch_kick_action_materializes_multiple_rows_with_shared_tx_index(tmp_path: Path) -> None:
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
            request_payload={"sender": "0x6000000000000000000000000000000000000006"},
            preview_payload={
                "preparedOperations": [
                    {
                        "operation": "kick",
                        "txIndex": 0,
                        "sourceType": "strategy",
                        "sourceAddress": "0x2000000000000000000000000000000000000002",
                        "auctionAddress": "0x3000000000000000000000000000000000000003",
                        "tokenAddress": "0x5000000000000000000000000000000000000005",
                        "tokenSymbol": "CRV",
                        "wantAddress": "0x4000000000000000000000000000000000000004",
                        "wantSymbol": "USDC",
                        "sellAmount": "100",
                    },
                    {
                        "operation": "kick",
                        "txIndex": 0,
                        "sourceType": "strategy",
                        "sourceAddress": "0x2000000000000000000000000000000000000002",
                        "auctionAddress": "0x3000000000000000000000000000000000000003",
                        "tokenAddress": "0x8000000000000000000000000000000000000008",
                        "tokenSymbol": "YFI",
                        "wantAddress": "0x4000000000000000000000000000000000000004",
                        "wantSymbol": "USDC",
                        "sellAmount": "200",
                    },
                ]
            },
            transactions=[
                {
                    "operation": "kick",
                    "to": "0x7000000000000000000000000000000000000007",
                    "data": "0xdeadbeef",
                    "value": "0x0",
                    "chainId": 1,
                }
            ],
            auction_address="0x3000000000000000000000000000000000000003",
            source_address="0x2000000000000000000000000000000000000002",
        )

    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-token"}
    _record_action_transaction(client, headers, action_id=action_id, tx_index=0, tx_hash="0xbatch")

    payload = client.get("/api/v1/tidal/logs/kicks", headers=headers).json()["data"]
    rows_by_symbol = {row["tokenSymbol"]: row for row in payload["kicks"]}
    assert payload["total"] == 2
    assert rows_by_symbol["CRV"]["txHash"] == "0xbatch"
    assert rows_by_symbol["YFI"]["txHash"] == "0xbatch"



def test_enable_tokens_action_materializes_batch_rows_by_tx_index(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    _init_db(settings)
    _seed_dashboard_data(settings)
    app = create_app(settings)
    engine = create_engine(settings.database_url, future=True)
    with Session(engine, future=True) as session:
        action_id = create_prepared_action(
            session,
            operator_id="tester",
            action_type="enable_tokens",
            sender="0x6000000000000000000000000000000000000006",
            request_payload={"sender": "0x6000000000000000000000000000000000000006"},
            preview_payload={
                "preparedOperations": [
                    {
                        "operation": "enable-tokens",
                        "txIndex": 0,
                        "sourceType": "strategy",
                        "sourceAddress": "0x2000000000000000000000000000000000000002",
                        "auctionAddress": "0x3000000000000000000000000000000000000003",
                        "tokenAddress": "0x5000000000000000000000000000000000000005",
                        "tokenSymbol": "CRV",
                        "wantAddress": "0x4000000000000000000000000000000000000004",
                        "wantSymbol": "USDC",
                        "normalizedBalance": "1",
                    },
                    {
                        "operation": "enable-tokens",
                        "txIndex": 0,
                        "sourceType": "strategy",
                        "sourceAddress": "0x2000000000000000000000000000000000000002",
                        "auctionAddress": "0x3000000000000000000000000000000000000003",
                        "tokenAddress": "0x8000000000000000000000000000000000000008",
                        "tokenSymbol": "YFI",
                        "wantAddress": "0x4000000000000000000000000000000000000004",
                        "wantSymbol": "USDC",
                        "normalizedBalance": "2",
                    },
                    {
                        "operation": "enable-tokens",
                        "txIndex": 1,
                        "sourceType": "strategy",
                        "sourceAddress": "0x2000000000000000000000000000000000000002",
                        "auctionAddress": "0x3000000000000000000000000000000000000003",
                        "tokenAddress": "0x9000000000000000000000000000000000000009",
                        "tokenSymbol": "BAL",
                        "wantAddress": "0x4000000000000000000000000000000000000004",
                        "wantSymbol": "USDC",
                        "normalizedBalance": "3",
                    },
                ]
            },
            transactions=[
                {"operation": "enable-tokens", "to": "0x7000000000000000000000000000000000000007", "data": "0x01", "value": "0x0", "chainId": 1},
                {"operation": "enable-tokens", "to": "0x7000000000000000000000000000000000000007", "data": "0x02", "value": "0x0", "chainId": 1},
            ],
            auction_address="0x3000000000000000000000000000000000000003",
            source_address="0x2000000000000000000000000000000000000002",
        )

    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-token"}
    _record_action_transaction(client, headers, action_id=action_id, tx_index=0, tx_hash="0xenable0")
    _record_action_transaction(client, headers, action_id=action_id, tx_index=1, tx_hash="0xenable1", block_number=124)

    payload = client.get("/api/v1/tidal/logs/kicks", headers=headers).json()["data"]
    rows_by_symbol = {row["tokenSymbol"]: row for row in payload["kicks"]}
    assert payload["total"] == 3
    assert rows_by_symbol["CRV"]["operationType"] == "enable_tokens"
    assert rows_by_symbol["CRV"]["txHash"] == "0xenable0"
    assert rows_by_symbol["YFI"]["txHash"] == "0xenable0"
    assert rows_by_symbol["BAL"]["txHash"] == "0xenable1"



def test_multi_tx_settle_action_materializes_only_exact_tx_index_matches(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    _init_db(settings)
    _seed_dashboard_data(settings)
    app = create_app(settings)
    engine = create_engine(settings.database_url, future=True)
    with Session(engine, future=True) as session:
        action_id = create_prepared_action(
            session,
            operator_id="tester",
            action_type="settle",
            sender="0x6000000000000000000000000000000000000006",
            request_payload={"sender": "0x6000000000000000000000000000000000000006"},
            preview_payload={
                "preparedOperations": [
                    {
                        "operation": "resolve-auction",
                        "txIndex": 0,
                        "auctionAddress": "0x3000000000000000000000000000000000000003",
                        "tokenAddress": "0x5000000000000000000000000000000000000005",
                        "tokenSymbol": "CRV",
                        "reason": "inactive kicked lot",
                    },
                    {
                        "operation": "resolve-auction",
                        "txIndex": 1,
                        "auctionAddress": "0x3000000000000000000000000000000000000003",
                        "tokenAddress": "0x8000000000000000000000000000000000000008",
                        "tokenSymbol": "YFI",
                        "reason": "inactive kicked lot",
                    },
                ]
            },
            transactions=[
                {"operation": "resolve-auction", "to": "0x7000000000000000000000000000000000000007", "data": "0x01", "value": "0x0", "chainId": 1},
                {"operation": "resolve-auction", "to": "0x7000000000000000000000000000000000000007", "data": "0x02", "value": "0x0", "chainId": 1},
            ],
            auction_address="0x3000000000000000000000000000000000000003",
        )

    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-token"}
    _record_action_transaction(client, headers, action_id=action_id, tx_index=0, tx_hash="0xsettle0")
    _record_action_transaction(client, headers, action_id=action_id, tx_index=1, tx_hash="0xsettle1", block_number=124)

    payload = client.get("/api/v1/tidal/logs/kicks", headers=headers).json()["data"]
    rows_by_symbol = {row["tokenSymbol"]: row for row in payload["kicks"]}
    assert payload["total"] == 2
    assert rows_by_symbol["CRV"]["operationType"] == "resolve_auction"
    assert rows_by_symbol["CRV"]["txHash"] == "0xsettle0"
    assert rows_by_symbol["YFI"]["txHash"] == "0xsettle1"



def test_sweep_action_materializes_with_tx_index_zero(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    _init_db(settings)
    _seed_dashboard_data(settings)
    app = create_app(settings)
    engine = create_engine(settings.database_url, future=True)
    with Session(engine, future=True) as session:
        action_id = create_prepared_action(
            session,
            operator_id="tester",
            action_type="sweep",
            sender="0x6000000000000000000000000000000000000006",
            request_payload={"sender": "0x6000000000000000000000000000000000000006"},
            preview_payload={
                "preparedOperations": [
                    {
                        "operation": "sweep-auction",
                        "txIndex": 0,
                        "auctionAddress": "0x3000000000000000000000000000000000000003",
                        "tokenAddress": "0x5000000000000000000000000000000000000005",
                        "tokenSymbol": "CRV",
                        "reason": "manual auction sweep",
                    }
                ]
            },
            transactions=[
                {"operation": "sweep-auction", "to": "0x7000000000000000000000000000000000000007", "data": "0x01", "value": "0x0", "chainId": 1},
            ],
            auction_address="0x3000000000000000000000000000000000000003",
        )

    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-token"}
    _record_action_transaction(client, headers, action_id=action_id, tx_index=0, tx_hash="0xsweep")

    payload = client.get("/api/v1/tidal/logs/kicks", headers=headers).json()["data"]
    assert payload["total"] == 1
    assert payload["kicks"][0]["operationType"] == "sweep_auction"
    assert payload["kicks"][0]["txHash"] == "0xsweep"



def test_settle_action_broadcast_and_receipt_materialize_kick_logs(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    _init_db(settings)
    _seed_dashboard_data(settings)
    app = create_app(settings)
    engine = create_engine(settings.database_url, future=True)
    with Session(engine, future=True) as session:
        action_id = create_prepared_action(
            session,
            operator_id="tester",
            action_type="settle",
            sender="0x6000000000000000000000000000000000000006",
                request_payload={
                    "auctionAddress": "0x3000000000000000000000000000000000000003",
                    "sender": "0x6000000000000000000000000000000000000006",
                    "tokenAddress": "0x5000000000000000000000000000000000000005",
                    "force": True,
                },
                preview_payload={
                    "inspection": {
                        "auction_address": "0x3000000000000000000000000000000000000003",
                        "is_active_auction": True,
                        "enabled_tokens": ["0x5000000000000000000000000000000000000005"],
                    },
                    "decision": {
                        "status": "actionable",
                        "operations": [
                            {
                                "operation_type": "resolve_auction",
                                "token_address": "0x5000000000000000000000000000000000000005",
                                "path": 3,
                                "reason": "live funded lot",
                                "balance_raw": 1000000000000000000,
                                "requires_force": True,
                                "receiver": "0x1000000000000000000000000000000000000001",
                            }
                        ],
                        "reason": "live funded lot",
                    },
                    "requestedForce": True,
                    "preparedOperations": [
                        {
                            "operation": "resolve-auction",
                            "auctionAddress": "0x3000000000000000000000000000000000000003",
                            "tokenAddress": "0x5000000000000000000000000000000000000005",
                            "reason": "live funded lot",
                            "path": 3,
                            "requiresForce": True,
                            "balanceRaw": "1000000000000000000",
                            "receiver": "0x1000000000000000000000000000000000000001",
                        }
                    ],
                },
            transactions=[
                {
                    "operation": "resolve-auction",
                    "to": "0x7000000000000000000000000000000000000007",
                    "data": "0xdeadbeef",
                    "value": "0x0",
                    "chainId": 1,
                    "gasEstimate": 150000,
                    "gasLimit": 180000,
                }
            ],
            resource_address="0x3000000000000000000000000000000000000003",
            auction_address="0x3000000000000000000000000000000000000003",
            token_address="0x5000000000000000000000000000000000000005",
        )

    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-token"}
    tx_hash = "0xdef"

    _record_action_transaction(client, headers, action_id=action_id, tx_index=0, tx_hash=tx_hash)

    logs_response = client.get("/api/v1/tidal/logs/kicks", headers=headers)
    assert logs_response.status_code == 200
    payload = logs_response.json()
    assert payload["status"] == "ok"
    assert payload["data"]["total"] == 1
    assert payload["data"]["kicks"][0]["status"] == "SUBMITTED"
    assert payload["data"]["kicks"][0]["txHash"] == tx_hash
    assert payload["data"]["kicks"][0]["operationType"] == "resolve_auction"
    assert payload["data"]["kicks"][0]["tokenSymbol"] == "CRV"
    assert payload["data"]["kicks"][0]["wantSymbol"] == "USDC"
    assert payload["data"]["kicks"][0]["sourceName"] == "Test Strategy"
    assert payload["data"]["kicks"][0]["stuckAbortReason"] == "live funded lot"
