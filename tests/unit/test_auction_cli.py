import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from tidal.cli import app as operator_app
from tidal.control_plane.client import ControlPlaneError
from tidal.cli_exit_codes import EXECUTION_ERROR, PARTIAL_FAILURE, VALIDATION_ERROR
from tidal.transaction_service.types import TxIntent
import tidal.auction_cli as operator_auction_cli_module


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("statuses,expected_count,status,exit_code", [
    (["CONFIRMED"], 1, "confirmed", 0),
    (["REVERTED"], 1, "failed", EXECUTION_ERROR),
    ([None], 1, "pending", EXECUTION_ERROR),
    (["CONFIRMED", "REVERTED"], 2, "partial", PARTIAL_FAILURE),
    (["CONFIRMED", None], 3, "partial", PARTIAL_FAILURE),
])
def test_auction_text_and_json_agree_on_receipt_outcomes(
    tmp_path, monkeypatch, json_output, statuses, expected_count, status, exit_code,
):
    client = _EnableTokensClient()
    prepare = client.prepare_enable_tokens

    def prepare_many(auction, payload):
        response = prepare(auction, payload)
        response["data"]["transactions"] *= expected_count
        return response

    monkeypatch.setattr(client, "prepare_enable_tokens", prepare_many)
    monkeypatch.setattr(operator_auction_cli_module.CLIContext, "verify_authenticated_api_access", lambda _: None)
    monkeypatch.setattr(operator_auction_cli_module.CLIContext, "control_plane_client", lambda *args, **kwargs: client)
    monkeypatch.setattr(operator_auction_cli_module.CLIContext, "resolve_execution", lambda *args, **kwargs: SimpleNamespace(
        signer=SimpleNamespace(), sender="0x" + "9" * 40,
    ))

    def execute(**kwargs):
        if json_output:
            assert kwargs["progress_callback"] is None
        return [{"txHash": f"0x{index + 1:064x}", "receiptStatus": value} for index, value in enumerate(statuses)]

    monkeypatch.setattr(operator_auction_cli_module, "execute_prepared_action_sync", execute)
    args = ["auction", "enable-tokens", "0x" + "2" * 40, "--config", str(_write_config(tmp_path)), "--no-confirmation"]
    result = CliRunner().invoke(operator_app, args + (["--json"] if json_output else []))
    assert result.exit_code == exit_code, result.output
    if json_output:
        payload = json.loads(result.stdout)
        assert payload["status"] == status
        assert payload["data"]["execution"]["status"] == status
        assert payload["data"]["execution"]["unsubmitted"] == expected_count - len(statuses)
        assert "Submitting" not in result.stdout
    else:
        assert f"Execution {status.title()}" in result.output


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
                    "executionTarget": "0x846475a1b97ac57861813206749c1b0f592383ef",
                    "previewSender": payload["sender"],
                    "previewSenderAuthorized": True,
                    "authorizationTarget": "0x846475a1b97ac57861813206749c1b0f592383ef",
                    "executionPreview": {
                        "call_succeeded": False,
                        "gas_estimate": 215036,
                        "error_message": "execution reverted: !authorized",
                    },
                },
                "transactions": [
                    {
                        "operation": "enable-tokens",
                        "to": "0x846475a1b97ac57861813206749c1b0f592383ef",
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


class _UnderGasEnableTokensClient(_EnableTokensClient):
    def prepare_enable_tokens(self, auction_address: str, payload: dict[str, object]) -> dict[str, object]:
        response = super().prepare_enable_tokens(auction_address, payload)
        response["data"]["transactions"][0]["gasEstimate"] = 1_526_206
        response["data"]["transactions"][0]["gasLimit"] = 500_000
        response["data"]["preview"]["executionPreview"]["gas_estimate"] = 1_526_206
        return response


class _NoopEnableTokensClient:
    def __enter__(self) -> "_NoopEnableTokensClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        del exc_type, exc, tb

    def prepare_enable_tokens(self, auction_address: str, payload: dict[str, object]) -> dict[str, object]:
        del auction_address, payload
        return {
            "status": "noop",
            "warnings": [],
            "data": {
                "preview": {},
                "transactions": [],
            },
        }


class _NoopSettleClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __enter__(self) -> "_NoopSettleClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        del exc_type, exc, tb

    def prepare_settle(self, auction_address: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((auction_address, payload))
        return {
            "status": "noop",
            "warnings": [],
            "data": {
                "preview": {
                    "decision": {
                        "status": "noop",
                        "operations": [],
                        "reason": "auction is progressing normally",
                    },
                    "inspection": {
                        "auction_address": auction_address,
                        "is_active_auction": True,
                        "enabled_tokens": ["0xd533a949740bb3306d119cc777fa900ba034cd52"],
                    },
                    "requestedForce": False,
                    "preparedOperations": [],
                },
                "transactions": [],
            },
        }


class _PreparedResolveSettleClient:
    def __enter__(self) -> "_PreparedResolveSettleClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        del exc_type, exc, tb

    def prepare_settle(self, auction_address: str, payload: dict[str, object]) -> dict[str, object]:
        sender = payload["sender"]
        return {
            "status": "ok",
            "warnings": [],
            "data": {
                "actionId": "action-resolve",
                "actionType": "settle",
                "preview": {
                    "decision": {
                        "status": "actionable",
                        "operations": [
                            {
                                "operation_type": "resolve_auction",
                                "token_address": "0x1cfa5641c01406ab8ac350ded7d735ec41298372",
                                "path": 5,
                                "reason": "inactive kicked lot with stranded inventory",
                                "balance_raw": 117240663299393522411314,
                                "requires_force": False,
                                "receiver": "0x3333333333333333333333333333333333333333",
                            }
                        ],
                        "reason": "inactive kicked lot with stranded inventory",
                    },
                    "inspection": {
                        "auction_address": auction_address,
                        "is_active_auction": False,
                        "enabled_tokens": ["0x1cfa5641c01406ab8ac350ded7d735ec41298372"],
                    },
                    "requestedForce": False,
                    "preparedOperations": [
                        {
                            "operation": "resolve-auction",
                            "auctionAddress": auction_address,
                            "tokenAddress": "0x1cfa5641c01406ab8ac350ded7d735ec41298372",
                            "reason": "inactive kicked lot with stranded inventory",
                            "path": 5,
                            "requiresForce": False,
                            "balanceRaw": "117240663299393522411314",
                            "receiver": "0x3333333333333333333333333333333333333333",
                        }
                    ],
                },
                "transactions": [
                    {
                        "operation": "resolve-auction",
                        "to": "0x846475a1b97ac57861813206749c1b0f592383ef",
                        "data": "0xfeedface",
                        "value": "0x0",
                        "chainId": 1,
                        "sender": sender,
                        "gasEstimate": 210000,
                        "gasLimit": 252000,
                    }
                ],
            },
        }


class _PreparedSweepClient:
    def __enter__(self) -> "_PreparedSweepClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        del exc_type, exc, tb

    def prepare_sweep(self, auction_address: str, payload: dict[str, object]) -> dict[str, object]:
        sender = payload["sender"]
        token = payload["tokenAddress"]
        return {
            "status": "ok",
            "warnings": [],
            "data": {
                "actionId": "action-sweep",
                "actionType": "sweep",
                "preview": {
                    "decision": {
                        "status": "actionable",
                        "reason": "manual sweep prepared",
                    },
                    "inspection": {
                        "auction_address": auction_address,
                        "is_active_auction": False,
                        "enabled_tokens": [token],
                    },
                    "preparedOperations": [
                        {
                            "operation": "sweep-auction",
                            "auctionAddress": auction_address,
                            "tokenAddress": token,
                            "tokenSymbol": "CJPY",
                            "reason": "manual auction sweep",
                            "path": 5,
                            "balanceRaw": "117240663299393522411314",
                            "receiver": "0x3333333333333333333333333333333333333333",
                        }
                    ],
                },
                "transactions": [
                    {
                        "operation": "sweep-auction",
                        "to": "0x846475a1b97ac57861813206749c1b0f592383ef",
                        "data": "0xfacefeed",
                        "value": "0x0",
                        "chainId": 1,
                        "sender": sender,
                        "gasEstimate": 180000,
                        "gasLimit": 216000,
                    }
                ],
            },
        }


def test_operator_auction_enable_tokens_uses_styled_submission_flow(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _EnableTokensClient()

    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
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
        tx = kwargs["transactions"][0]
        assert isinstance(tx, TxIntent)
        return [
            {
                "operation": tx.operation,
                "sender": kwargs["sender"],
                "txHash": "0x" + "1" * 64,
                "broadcastAt": "2026-03-29T00:00:00+00:00",
                "chainId": 1,
                "gasEstimate": tx.gas_estimate,
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
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0
    assert client.calls[0][0] == "0xe92af59d00becd5f70d2ba11ae1a74751503a185"
    assert "Prepared action" in result.output
    assert "enable-tokens · 1 transaction" in result.output
    assert "Review details" in result.output
    assert "Auction:" in result.output
    assert "Tokens:" in result.output
    assert "Execution:" in result.output
    assert "Keeper auth:" in result.output
    assert "Warnings" in result.output
    assert "Submitting transaction..." in result.output
    assert "Confirmed" in result.output
    assert "Explorer:" not in result.output
    assert "Block:" not in result.output
    assert "Gas used:" not in result.output
    assert "Gas estimate:" not in result.output


def test_operator_auction_enable_tokens_forwards_repeated_extra_tokens(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _EnableTokensClient()

    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
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
    monkeypatch.setattr(
        operator_auction_cli_module,
        "execute_prepared_action_sync",
        lambda **kwargs: [{"txHash": "0x" + "1" * 64, "receiptStatus": "CONFIRMED"}],
    )

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        [
            "auction",
            "enable-tokens",
            "0xe92af59d00becd5f70d2ba11ae1a74751503a185",
            "--extra-token",
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "--extra-token",
            "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "--config",
            str(config_path),
            "--no-confirmation",
        ],
    )

    assert result.exit_code == 0
    assert client.calls[0][1]["extraTokens"] == [
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ]


def test_operator_auction_enable_tokens_forwards_client_gas_cap(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("db_path: ./test.db\ntxn_max_gas_limit: 2000000\n", encoding="utf-8")
    client = _EnableTokensClient()

    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
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
    monkeypatch.setattr(
        operator_auction_cli_module,
        "execute_prepared_action_sync",
        lambda **kwargs: [{"txHash": "0x" + "1" * 64, "receiptStatus": "CONFIRMED"}],
    )

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        [
            "auction",
            "enable-tokens",
            "0xe92af59d00becd5f70d2ba11ae1a74751503a185",
            "--config",
            str(config_path),
            "--no-confirmation",
        ],
    )

    assert result.exit_code == 0
    assert client.calls[0][1]["txnMaxGasLimit"] == 2_000_000


def test_operator_auction_enable_tokens_rejects_under_gassed_prepare(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _UnderGasEnableTokensClient()
    executed = False

    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
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

    def fake_execute_prepared_action_sync(**kwargs):  # noqa: ANN003
        nonlocal executed
        executed = True
        return []

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
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == VALIDATION_ERROR
    assert executed is False
    assert "Preparation Failed" in result.output
    assert "gas limit 500,000 is below estimated gas 1,526,206" in result.output
    assert "Send this transaction?" not in result.output


def test_operator_auction_enable_tokens_help_mentions_repeatable_extra_token() -> None:
    runner = CliRunner()
    result = runner.invoke(operator_app, ["auction", "enable-tokens", "--help"])

    assert result.exit_code == 0
    assert "fee-burner address/want aliases" in result.output
    assert "custom token address in enable" in result.output
    assert "Repeat to add more than one." in result.output


def test_operator_auction_enable_tokens_noop_skips_prepared_panel(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _NoopEnableTokensClient()

    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "control_plane_client",
        lambda self, auth=True: client,
    )
    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "resolve_execution",
        lambda self, **kwargs: SimpleNamespace(signer=None, sender=None),
    )

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        [
            "auction",
            "enable-tokens",
            "0xe92af59d00becd5f70d2ba11ae1a74751503a185",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 2
    assert "Prepared action" not in result.output
    assert "No Transaction Prepared" in result.output
    assert "No transaction was prepared." in result.output


class _ErrorEnableTokensClient:
    def __enter__(self) -> "_ErrorEnableTokensClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        del exc_type, exc, tb

    def prepare_enable_tokens(self, auction_address: str, payload: dict[str, object]) -> dict[str, object]:
        del auction_address, payload
        return {
            "status": "error",
            "warnings": ["governance mismatch"],
            "data": {
                "preview": {},
                "transactions": [],
            },
        }


def test_operator_auction_enable_tokens_error_renders_failure_panel(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _ErrorEnableTokensClient()

    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "control_plane_client",
        lambda self, auth=True: client,
    )
    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "resolve_execution",
        lambda self, **kwargs: SimpleNamespace(signer=None, sender=None),
    )

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        [
            "auction",
            "enable-tokens",
            "0xe92af59d00becd5f70d2ba11ae1a74751503a185",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == EXECUTION_ERROR
    assert "Preparation Failed" in result.output
    assert "governance mismatch" in result.output


def test_operator_auction_settle_noop_shows_reason_and_price_state(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _NoopSettleClient()

    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "control_plane_client",
        lambda self, auth=True: client,
    )
    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "resolve_execution",
        lambda self, **kwargs: SimpleNamespace(signer=None, sender=None),
    )

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        [
            "auction",
            "settle",
            "0xeb3746f59befef1f5834239fb65a2a4d88fdb251",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 2
    assert "No Transaction Prepared" in result.output
    assert "No transaction was prepared." in result.output
    assert "Reason:        auction is progressing normally" in result.output


def test_operator_auction_settle_force_threads_payload(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _NoopSettleClient()

    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "control_plane_client",
        lambda self, auth=True: client,
    )
    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "resolve_execution",
        lambda self, **kwargs: SimpleNamespace(signer=None, sender=None),
    )

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        [
            "auction",
            "settle",
            "0xeb3746f59befef1f5834239fb65a2a4d88fdb251",
            "--token",
            "0xd533a949740bb3306d119cc777fa900ba034cd52",
            "--force",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 2
    assert client.calls == [
        (
            "0xeb3746f59befef1f5834239fb65a2a4d88fdb251",
            {
                "sender": None,
                "tokenAddress": "0xd533a949740bb3306d119cc777fa900ba034cd52",
                "force": True,
            },
        )
    ]

def test_operator_auction_settle_preview_renders_inactive_balance_state(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _PreparedResolveSettleClient()

    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "control_plane_client",
        lambda self, auth=True: client,
    )
    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "resolve_execution",
        lambda self, **kwargs: SimpleNamespace(signer=SimpleNamespace(), sender="0x9999999999999999999999999999999999999999"),
    )
    monkeypatch.setattr(operator_auction_cli_module.typer, "confirm", lambda *args, **kwargs: False)

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        [
            "auction",
            "settle",
            "0xa00e6b35c23442fa9d5149cba5dd94623ffe6693",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 2
    assert "Prepared action" in result.output
    assert "Operations:  1" in result.output
    assert "inactive kicked lot with stranded inventory" in result.output
    assert "1. Token:" in result.output
    assert "Send this transaction?" not in result.output


def test_operator_auction_sweep_preview_renders_manual_sweep_state(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _PreparedSweepClient()

    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "control_plane_client",
        lambda self, auth=True: client,
    )
    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "resolve_execution",
        lambda self, **kwargs: SimpleNamespace(signer=SimpleNamespace(), sender="0x9999999999999999999999999999999999999999"),
    )
    monkeypatch.setattr(operator_auction_cli_module.typer, "confirm", lambda *args, **kwargs: False)

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        [
            "auction",
            "sweep",
            "0xa00e6b35c23442fa9d5149cba5dd94623ffe6693",
            "--token",
            "0x1cfa5641c01406ab8ac350ded7d735ec41298372",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 2
    assert "Prepared action" in result.output
    assert "sweep · 1 transaction" in result.output
    assert "manual sweep prepared" in result.output
    assert "manual auction sweep" in result.output


def test_operator_auction_deploy_checks_api_auth_before_resolving_execution(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    call_order: list[str] = []

    def fake_verify(self) -> None:  # noqa: ANN001
        call_order.append("verify")
        raise ControlPlaneError("TIDAL_API_KEY is invalid for Tidal API at https://api.example.com", status_code=401)

    def fail_resolve(self, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("resolve_execution should not be reached when auth validation fails")

    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "verify_authenticated_api_access",
        fake_verify,
    )
    monkeypatch.setattr(
        operator_auction_cli_module.CLIContext,
        "resolve_execution",
        fail_resolve,
    )

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        [
            "auction",
            "deploy",
            "--want",
            "0x1111111111111111111111111111111111111111",
            "--receiver",
            "0x2222222222222222222222222222222222222222",
            "--starting-price",
            "1234",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 1
    assert "TIDAL_API_KEY is invalid" in result.output
    assert call_order == ["verify"]


def test_operator_auction_deploy_json_requires_no_confirmation(tmp_path) -> None:
    config_path = _write_config(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        [
            "auction",
            "deploy",
            "--want",
            "0x1111111111111111111111111111111111111111",
            "--receiver",
            "0x2222222222222222222222222222222222222222",
            "--starting-price",
            "1234",
            "--json",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code != 0
    assert "Invalid value for --json" in result.output
    assert "--no-confirmation" in result.output
