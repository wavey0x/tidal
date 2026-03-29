from pathlib import Path
from types import SimpleNamespace

from eth_utils import to_checksum_address
from typer.testing import CliRunner

from tidal.cli import app as operator_app
import tidal.operator_kick_cli as operator_kick_cli_module


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("db_path: ./test.db\n", encoding="utf-8")
    return config_path


def _ready_entry(*, token_address: str, source_address: str, auction_address: str) -> dict[str, object]:
    return {
        "state": "ready",
        "source_type": "strategy",
        "source_address": source_address,
        "source_name": "Test Strategy",
        "auction_address": auction_address,
        "token_address": token_address,
        "token_symbol": "CRV",
        "want_symbol": "USDC",
        "normalized_balance": "1000",
        "usd_value": 2500.0,
        "detail": None,
        "auction_active": None,
        "active_token": None,
        "active_tokens": [],
        "minimum_price_raw": None,
    }


def _inspect_payload(ready: list[dict[str, object]]) -> dict[str, object]:
    return {
        "source_type": None,
        "source_address": None,
        "auction_address": None,
        "limit": None,
        "eligible_count": len(ready),
        "selected_count": len(ready),
        "ready_count": len(ready),
        "cooldown_count": 0,
        "deferred_same_auction_count": 0,
        "limited_count": 0,
        "ready": ready,
        "cooldown_skips": [],
        "deferred_same_auction": [],
        "limited": [],
    }


class _DryRunClient:
    def __init__(self) -> None:
        self.inspect_calls: list[dict[str, object]] = []

    def __enter__(self) -> "_DryRunClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        del exc_type, exc, tb

    def inspect_kicks(self, body: dict[str, object]) -> dict[str, object]:
        self.inspect_calls.append(body)
        return {
            "status": "ok",
            "warnings": [],
            "data": _inspect_payload(
                [
                    _ready_entry(
                        token_address="0x3333333333333333333333333333333333333333",
                        source_address="0x1111111111111111111111111111111111111111",
                        auction_address="0x2222222222222222222222222222222222222222",
                    )
                ]
            ),
        }

    def prepare_kicks(self, body: dict[str, object]) -> dict[str, object]:
        raise AssertionError(f"dry run should not call prepare_kicks: {body}")


class _BroadcastClient:
    def __init__(self) -> None:
        self.inspect_calls: list[dict[str, object]] = []
        self.prepare_calls: list[dict[str, object]] = []

    def __enter__(self) -> "_BroadcastClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        del exc_type, exc, tb

    def inspect_kicks(self, body: dict[str, object]) -> dict[str, object]:
        self.inspect_calls.append(body)
        return {
            "status": "ok",
            "warnings": [],
            "data": _inspect_payload(
                [
                    _ready_entry(
                        token_address="0x3333333333333333333333333333333333333333",
                        source_address="0x1111111111111111111111111111111111111111",
                        auction_address="0x2222222222222222222222222222222222222222",
                    ),
                    _ready_entry(
                        token_address="0x4444444444444444444444444444444444444444",
                        source_address="0x5555555555555555555555555555555555555555",
                        auction_address="0x6666666666666666666666666666666666666666",
                    ),
                ]
            ),
        }

    def prepare_kicks(self, body: dict[str, object]) -> dict[str, object]:
        self.prepare_calls.append(body)
        action_index = len(self.prepare_calls)
        token_address = str(body["tokenAddress"])
        source_address = str(body["sourceAddress"])
        auction_address = str(body["auctionAddress"])
        return {
            "status": "ok",
            "warnings": [],
            "data": {
                "actionId": f"action-{action_index}",
                "actionType": "kick",
                "preview": {
                    "preparedOperations": [
                        {
                            "operation": "kick",
                            "auctionAddress": auction_address,
                            "sourceAddress": source_address,
                            "sourceName": "Test Strategy",
                            "sourceType": "strategy",
                            "tokenAddress": token_address,
                            "tokenSymbol": "CRV",
                            "sellAmount": "1000",
                            "startingPrice": "2750",
                            "minimumPrice": "2375",
                            "quoteAmount": "2500",
                            "usdValue": "2500",
                            "pricingProfileName": "stable",
                            "stepDecayRateBps": 50,
                            "settleToken": None,
                        }
                    ]
                },
                "transactions": [
                    {
                        "operation": "kick",
                        "to": "0x7777777777777777777777777777777777777777",
                        "data": "0xdeadbeef",
                        "value": "0x0",
                        "chainId": 1,
                        "sender": body["sender"],
                        "gasEstimate": 210000,
                        "gasLimit": 252000,
                    }
                ],
            },
        }


def test_operator_kick_run_dry_run_uses_inspect_only(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _DryRunClient()

    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "control_plane_client",
        lambda self, auth=True: client,
    )
    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "resolve_execution",
        lambda self, **kwargs: SimpleNamespace(signer=None, sender=None),
    )

    runner = CliRunner()
    result = runner.invoke(operator_app, ["kick", "run", "--config", str(config_path)])

    assert result.exit_code == 0
    assert len(client.inspect_calls) == 1
    assert client.inspect_calls[0]["includeLiveInspection"] is False
    assert "just-in-time during broadcast" in result.output


def test_operator_kick_run_broadcast_prepares_candidates_one_by_one(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _BroadcastClient()
    prepared_actions: list[tuple[str, list[dict[str, object]]]] = []

    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "control_plane_client",
        lambda self, auth=True: client,
    )
    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "resolve_execution",
        lambda self, **kwargs: SimpleNamespace(
            signer=SimpleNamespace(),
            sender="0x9999999999999999999999999999999999999999",
        ),
    )
    monkeypatch.setattr(operator_kick_cli_module.typer, "confirm", lambda *args, **kwargs: True)

    def fake_execute_prepared_action_sync(**kwargs):  # noqa: ANN003
        prepared_actions.append((kwargs["action_id"], kwargs["transactions"]))
        return [
            {
                "operation": kwargs["transactions"][0]["operation"],
                "sender": kwargs["sender"],
                "txHash": f"0x{len(prepared_actions):064x}",
                "broadcastAt": "2026-03-29T00:00:00+00:00",
                "chainId": 1,
                "gasEstimate": kwargs["transactions"][0]["gasEstimate"],
                "receiptStatus": "CONFIRMED",
            }
        ]

    monkeypatch.setattr(
        operator_kick_cli_module,
        "execute_prepared_action_sync",
        fake_execute_prepared_action_sync,
    )

    runner = CliRunner()
    result = runner.invoke(operator_app, ["kick", "run", "--broadcast", "--config", str(config_path)])

    assert result.exit_code == 0
    assert len(client.inspect_calls) == 1
    assert client.inspect_calls[0]["includeLiveInspection"] is False
    assert [call["tokenAddress"] for call in client.prepare_calls] == [
        "0x3333333333333333333333333333333333333333",
        "0x4444444444444444444444444444444444444444",
    ]
    assert all(call["limit"] == 1 for call in client.prepare_calls)
    assert all(call["sender"] == "0x9999999999999999999999999999999999999999" for call in client.prepare_calls)
    assert [action_id for action_id, _ in prepared_actions] == ["action-1", "action-2"]
    assert "Kick (1 of 2)" in result.output
    assert "Auction details" in result.output
    assert "Send details" in result.output
    assert f"Auction:     {to_checksum_address('0x2222222222222222222222222222222222222222')}" in result.output
    assert f"From:        {to_checksum_address('0x9999999999999999999999999999999999999999')}" in result.output
    assert "Quote out:   2,500.00 USDC" in result.output
    assert "Start quote: 2,750 USDC (+10% buffer)" in result.output
    assert "Min price:   2,375 USDC (-5% buffer)" in result.output
    assert "Confirmation Required" in result.output
    assert "Submitting transaction..." in result.output
    assert "Confirmed" in result.output
    assert "https://etherscan.io/tx/0x0000000000000000000000000000000000000000000000000000000000000001" in result.output
    assert "Explorer:     https://etherscan.io/tx/0x0000000000000000000000000000000000000000000000000000000000000001" in result.output
    assert "Gas limit:   252,000" in result.output
    assert "max 2.50 gwei" in result.output
