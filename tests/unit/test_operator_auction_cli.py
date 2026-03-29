from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from tidal.cli import app as operator_app
import tidal.operator_auction_cli as operator_auction_cli_module


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("db_path: ./test.db\n", encoding="utf-8")
    return config_path


class _EnableTokensClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __enter__(self) -> "_EnableTokensClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        del exc_type, exc, tb

    def prepare_enable_tokens(self, auction_address: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((auction_address, payload))
        return {
            "status": "ok",
            "warnings": ["execution reverted: !authorized"],
            "data": {
                "actionId": "action-enable",
                "actionType": "enable_tokens",
                "preview": {
                    "inspection": {
                        "auction_address": auction_address,
                        "governance": "0xb634316e06cc0b358437cbadd4dc94f1d3a92b3b",
                        "want": "0x1111111111111111111111111111111111111111",
                        "receiver": "0x2222222222222222222222222222222222222222",
                        "version": "1.0.0",
                        "in_configured_factory": True,
                        "governance_matches_required": True,
                        "enabled_tokens": [],
                    },
                    "source": {
                        "source_type": "strategy",
                        "source_address": "0x3333333333333333333333333333333333333333",
                        "source_name": "Test Strategy",
                    },
                    "probes": [
                        {
                            "token_address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            "symbol": "CRV",
                            "status": "eligible",
                            "reasonLabel": "eligible",
                        }
                    ],
                    "selectedTokens": ["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
                    "commandsCount": 1,
                    "stateSlots": 2,
                    "preview": {
                        "call_succeeded": False,
                        "gas_estimate": 215036,
                        "error_message": "execution reverted: !authorized",
                    },
                },
                "transactions": [
                    {
                        "operation": "enable-tokens",
                        "to": "0xb634316e06cc0b358437cbadd4dc94f1d3a92b3b",
                        "data": "0xdeadbeef",
                        "value": "0x0",
                        "chainId": 1,
                        "sender": payload["sender"],
                        "gasEstimate": 215036,
                        "gasLimit": 258043,
                    }
                ],
            },
        }


def test_operator_auction_enable_tokens_uses_styled_submission_flow(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _EnableTokensClient()

    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "control_plane_client",
        lambda self, auth=True: client,
    )
    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "resolve_execution",
        lambda self, **kwargs: SimpleNamespace(
            signer=SimpleNamespace(),
            sender="0x9999999999999999999999999999999999999999",
        ),
    )
    monkeypatch.setattr(operator_auction_cli_module.typer, "confirm", lambda *args, **kwargs: True)

    def fake_execute_prepared_action_sync(**kwargs):  # noqa: ANN003
        return [
            {
                "operation": kwargs["transactions"][0]["operation"],
                "sender": kwargs["sender"],
                "txHash": "0x" + "1" * 64,
                "broadcastAt": "2026-03-29T00:00:00+00:00",
                "chainId": 1,
                "gasEstimate": kwargs["transactions"][0]["gasEstimate"],
                "receiptStatus": "CONFIRMED",
                "blockNumber": 12345,
                "gasUsed": 210000,
            }
        ]

    monkeypatch.setattr(
        operator_auction_cli_module,
        "execute_prepared_action_sync",
        fake_execute_prepared_action_sync,
    )

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        [
            "auction",
            "enable-tokens",
            "0xe92af59d00becd5f70d2ba11ae1a74751503a185",
            "--broadcast",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0
    assert client.calls[0][0] == "0xe92af59d00becd5f70d2ba11ae1a74751503a185"
    assert "Prepared action" in result.output
    assert "Action Type:  enable-tokens" in result.output
    assert "Auction details" in result.output
    assert "Token plan" in result.output
    assert "Warnings" in result.output
    assert "Confirmation Required" in result.output
    assert "Submitting transaction..." in result.output
    assert "Confirmed" in result.output
    assert "Transaction" in result.output
    assert "Explorer:     https://etherscan.io/tx/0x1111111111111111111111111111111111111111111111111111111111111111" in result.output
