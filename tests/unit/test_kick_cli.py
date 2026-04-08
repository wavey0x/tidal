from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_utils import to_checksum_address
from typer.testing import CliRunner

from tidal.cli import app as operator_app
from tidal.transaction_service.types import TxIntent
import tidal.kick_cli as operator_kick_cli_module


def _write_config(tmp_path: Path, *, extra: str = "") -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"db_path: ./test.db\n{extra}", encoding="utf-8")
    return config_path


def _entry(
    *,
    token_address: str,
    source_address: str,
    auction_address: str,
    state: str = "ready",
    source_type: str = "strategy",
) -> dict[str, object]:
    return {
        "state": state,
        "source_type": source_type,
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
        "blocked_token_address": None,
        "blocked_token_symbol": None,
        "next_step": None,
    }


def _ready_entry(*, token_address: str, source_address: str, auction_address: str) -> dict[str, object]:
    return _entry(
        token_address=token_address,
        source_address=source_address,
        auction_address=auction_address,
        state="ready",
    )


def _deferred_entry(*, token_address: str, source_address: str, auction_address: str) -> dict[str, object]:
    return _entry(
        token_address=token_address,
        source_address=source_address,
        auction_address=auction_address,
        state="deferred_same_auction",
        source_type="fee_burner",
    )


def _resolve_first_entry(
    *,
    token_address: str,
    source_address: str,
    auction_address: str,
    detail: str,
    blocked_token_address: str | None = None,
    blocked_token_symbol: str | None = None,
    next_step: str | None = None,
) -> dict[str, object]:
    entry = _entry(
        token_address=token_address,
        source_address=source_address,
        auction_address=auction_address,
        state="resolve_first",
    )
    entry["detail"] = detail
    entry["auction_active"] = False
    entry["blocked_token_address"] = blocked_token_address
    entry["blocked_token_symbol"] = blocked_token_symbol
    entry["next_step"] = next_step
    return entry


def _blocked_live_entry(
    *,
    token_address: str,
    source_address: str,
    auction_address: str,
    detail: str,
    next_step: str | None = None,
) -> dict[str, object]:
    entry = _entry(
        token_address=token_address,
        source_address=source_address,
        auction_address=auction_address,
        state="blocked_live",
    )
    entry["detail"] = detail
    entry["auction_active"] = True
    entry["active_token"] = token_address
    entry["active_tokens"] = [token_address]
    entry["blocked_token_address"] = token_address
    entry["blocked_token_symbol"] = "CRV"
    entry["next_step"] = next_step
    return entry


def _inspect_payload(
    ready: list[dict[str, object]],
    *,
    resolve_first: list[dict[str, object]] | None = None,
    blocked_live: list[dict[str, object]] | None = None,
    preview_failed: list[dict[str, object]] | None = None,
    deferred_same_auction: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    resolve_first_entries = resolve_first or []
    blocked_live_entries = blocked_live or []
    preview_failed_entries = preview_failed or []
    deferred = deferred_same_auction or []
    return {
        "source_type": None,
        "source_address": None,
        "auction_address": None,
        "limit": None,
        "eligible_count": len(ready) + len(resolve_first_entries) + len(blocked_live_entries) + len(preview_failed_entries) + len(deferred),
        "selected_count": len(ready) + len(resolve_first_entries) + len(blocked_live_entries) + len(preview_failed_entries),
        "ready_count": len(ready),
        "resolve_first_count": len(resolve_first_entries),
        "blocked_live_count": len(blocked_live_entries),
        "preview_failed_count": len(preview_failed_entries),
        "ignored_count": 0,
        "cooldown_count": 0,
        "deferred_same_auction_count": len(deferred),
        "limited_count": 0,
        "ready": ready,
        "resolve_first": resolve_first_entries,
        "blocked_live": blocked_live_entries,
        "preview_failed": preview_failed_entries,
        "ignored_skips": [],
        "cooldown_skips": [],
        "deferred_same_auction": deferred,
        "limited": [],
    }


def _broadcast_record(*, transactions: list[TxIntent], sender: str, tx_hash: str) -> dict[str, object]:
    tx = transactions[0]
    assert isinstance(tx, TxIntent)
    return {
        "operation": tx.operation,
        "sender": sender,
        "txHash": tx_hash,
        "broadcastAt": "2026-03-29T00:00:00+00:00",
        "chainId": 1,
        "gasEstimate": tx.gas_estimate,
        "receiptStatus": "CONFIRMED",
    }


class _InspectOnlyClient:
    def __init__(self) -> None:
        self.inspect_calls: list[dict[str, object]] = []

    def __enter__(self) -> "_InspectOnlyClient":
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
        raise AssertionError(f"inspect should not call prepare_kicks: {body}")


class _BroadcastClient:
    def __init__(
        self,
        ready_entries: list[dict[str, object]] | None = None,
        *,
        deferred_entries: list[dict[str, object]] | None = None,
    ) -> None:
        self.inspect_calls: list[dict[str, object]] = []
        self.prepare_calls: list[dict[str, object]] = []
        self.ready_entries = ready_entries or [
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
        self.deferred_entries = deferred_entries or []

    def __enter__(self) -> "_BroadcastClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        del exc_type, exc, tb

    def inspect_kicks(self, body: dict[str, object]) -> dict[str, object]:
        self.inspect_calls.append(body)
        return {
            "status": "ok",
            "warnings": [],
            "data": _inspect_payload(self.ready_entries, deferred_same_auction=self.deferred_entries),
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
                            "minimumPrice": "2375000000000000000",
                            "minimumQuote": "2375",
                            "quoteAmount": "2500",
                            "usdValue": "2500",
                            "bufferBps": 1000,
                            "minBufferBps": 50,
                            "pricingProfileName": "stable",
                            "stepDecayRateBps": 50,
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


class _PrepareNoopClient:
    def __enter__(self) -> "_PrepareNoopClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        del exc_type, exc, tb

    def inspect_kicks(self, body: dict[str, object]) -> dict[str, object]:
        del body
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
        del body
        return {
            "status": "noop",
            "warnings": [],
            "data": {
                "preview": {
                    "skippedDuringPrepare": [
                        {
                            "sourceName": "Yearn Fee Burner",
                            "sourceAddress": "0x1111111111111111111111111111111111111111",
                            "auctionAddress": "0x2222222222222222222222222222222222222222",
                            "tokenSymbol": "CRV",
                            "wantSymbol": "USDC",
                            "reason": "candidate was skipped during prepare",
                        }
                    ]
                },
                "transactions": [],
            },
        }


class _PrepareNoopWithSweepHintClient(_PrepareNoopClient):
    def prepare_kicks(self, body: dict[str, object]) -> dict[str, object]:
        del body
        return {
            "status": "noop",
            "warnings": ["Gas estimate failed: call to 0x2222…2222 failed: Amount is zero."],
            "data": {
                "preview": {
                    "skippedDuringPrepare": [
                        {
                            "sourceName": "yCRV Fee Burner",
                            "sourceAddress": "0x1111111111111111111111111111111111111111",
                            "auctionAddress": "0x2222222222222222222222222222222222222222",
                            "tokenSymbol": "WFRAX",
                            "wantSymbol": "crvUSD",
                            "reason": "auction requires manual sweep before kick",
                            "blockedTokenAddress": "0x1cfa5641c01406ab8ac350ded7d735ec41298372",
                            "blockedTokenSymbol": "CJPY",
                            "blockedReason": "inactive kicked lot with stranded inventory",
                            "nextStep": (
                                "tidal auction sweep "
                                "0x2222222222222222222222222222222222222222 "
                                "--token 0x1cfa5641c01406ab8ac350ded7d735ec41298372"
                            ),
                        }
                    ]
                },
                "transactions": [],
            },
        }


class _FeeBurnerSameAuctionNoopClient:
    def __init__(self) -> None:
        self.inspect_calls: list[dict[str, object]] = []
        self.prepare_calls: list[dict[str, object]] = []

    def __enter__(self) -> "_FeeBurnerSameAuctionNoopClient":
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
                    _entry(
                        token_address="0x3333333333333333333333333333333333333333",
                        source_address="0x1111111111111111111111111111111111111111",
                        auction_address="0x2222222222222222222222222222222222222222",
                        state="ready",
                        source_type="fee_burner",
                    )
                ],
                deferred_same_auction=[
                    _deferred_entry(
                        token_address="0x4444444444444444444444444444444444444444",
                        source_address="0x1111111111111111111111111111111111111111",
                        auction_address="0x2222222222222222222222222222222222222222",
                    )
                ],
            ),
        }

    def prepare_kicks(self, body: dict[str, object]) -> dict[str, object]:
        self.prepare_calls.append(body)
        return {
            "status": "noop",
            "warnings": [],
            "data": {
                "preview": {
                    "skippedDuringPrepare": [
                        {
                            "sourceName": "yCRV Fee Burner",
                            "sourceAddress": "0x1111111111111111111111111111111111111111",
                            "auctionAddress": "0x2222222222222222222222222222222222222222",
                            "tokenSymbol": "CRV",
                            "wantSymbol": "USDC",
                            "reason": "auction still active above minimumPrice",
                        }
                    ]
                },
                "transactions": [],
            },
        }


def test_operator_kick_inspect_uses_inspect_only(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _InspectOnlyClient()

    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "control_plane_client",
        lambda self, auth=True: client,
    )

    runner = CliRunner()
    result = runner.invoke(operator_app, ["kick", "inspect", "--config", str(config_path)])

    assert result.exit_code == 0
    assert len(client.inspect_calls) == 1
    assert "includeLiveInspection" not in client.inspect_calls[0]
    assert "Kick inspect:" in result.output


def test_operator_kick_inspect_renders_resolve_first_and_blocked_live_sections(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)

    class _ResolverInspectClient(_InspectOnlyClient):
        def inspect_kicks(self, body: dict[str, object]) -> dict[str, object]:
            self.inspect_calls.append(body)
            return {
                "status": "ok",
                "warnings": [],
                "data": _inspect_payload(
                    [],
                    resolve_first=[
                        _resolve_first_entry(
                            token_address="0x3333333333333333333333333333333333333333",
                            source_address="0x1111111111111111111111111111111111111111",
                            auction_address="0x2222222222222222222222222222222222222222",
                            detail="inactive kicked lot with stranded inventory",
                            blocked_token_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            blocked_token_symbol="CJPY",
                            next_step=(
                                "tidal auction settle "
                                "0x2222222222222222222222222222222222222222 "
                                "--token 0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa"
                            ),
                        )
                    ],
                    blocked_live=[
                        _blocked_live_entry(
                            token_address="0x4444444444444444444444444444444444444444",
                            source_address="0x5555555555555555555555555555555555555555",
                            auction_address="0x6666666666666666666666666666666666666666",
                            detail="live funded lot",
                            next_step=(
                                "tidal auction settle "
                                "0x6666666666666666666666666666666666666666 "
                                "--token 0x4444444444444444444444444444444444444444 --force"
                            ),
                        )
                    ],
                ),
            }

    client = _ResolverInspectClient()
    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "control_plane_client",
        lambda self, auth=True: client,
    )

    runner = CliRunner()
    result = runner.invoke(operator_app, ["kick", "inspect", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Resolve First:" in result.output
    assert "Blocked Live:" in result.output
    assert "inactive kicked lot with stranded inventory" in result.output
    assert "live funded lot" in result.output
    assert "blocked by: CJPY" in result.output
    assert "next step:  tidal auction settle 0x2222222222222222222222222222222222222222" in result.output


def test_operator_kick_run_renders_manual_sweep_hint_for_blocking_stale_lot(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _PrepareNoopWithSweepHintClient()

    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
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

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        ["kick", "run", "--source-type", "fee-burner", "--config", str(config_path)],
    )

    assert result.exit_code == 2
    assert "Auction requires manual sweep before kick" in result.output
    assert "Blocked By:" in result.output
    assert "CJPY" in result.output
    assert "Next Step:" in result.output
    assert "tidal auction sweep" in result.output


@pytest.mark.parametrize(
    ("flag_args", "expected"),
    [
        ([], None),
        (["--require-curve"], True),
        (["--no-require-curve"], False),
    ],
)
def test_operator_kick_run_threads_curve_quote_override(tmp_path, monkeypatch, flag_args, expected) -> None:
    config_path = _write_config(tmp_path)
    client = _BroadcastClient()

    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
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
    monkeypatch.setattr(operator_kick_cli_module.typer, "confirm", lambda *args, **kwargs: False)

    runner = CliRunner()
    result = runner.invoke(operator_app, ["kick", "run", "--config", str(config_path), *flag_args])

    assert result.exit_code == 2
    assert client.prepare_calls
    if expected is None:
        assert all("requireCurveQuote" not in call for call in client.prepare_calls)
    else:
        assert all(call["requireCurveQuote"] is expected for call in client.prepare_calls)


def test_operator_kick_run_continues_across_distinct_auctions_in_interactive_mode(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _BroadcastClient()
    prepared_actions: list[tuple[str, list[TxIntent]]] = []

    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
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
        return [_broadcast_record(transactions=kwargs["transactions"], sender=kwargs["sender"], tx_hash=f"0x{len(prepared_actions):064x}")]

    monkeypatch.setattr(
        operator_kick_cli_module,
        "execute_prepared_action_sync",
        fake_execute_prepared_action_sync,
    )

    runner = CliRunner()
    result = runner.invoke(operator_app, ["kick", "run", "--config", str(config_path)])

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
    assert "Kick (2 of 2)" in result.output
    assert "Auction details" in result.output
    assert "Send details" in result.output
    assert f"Auction:     {to_checksum_address('0x2222222222222222222222222222222222222222')}" in result.output
    assert f"From:        {to_checksum_address('0x9999999999999999999999999999999999999999')}" in result.output
    assert "Quote out:   2,500.00 USDC" in result.output
    assert "Start quote: 2,750 USDC (+10.00% buffer)" in result.output
    assert "Min quote:   2,375 USDC (-0.50% buffer)" in result.output
    assert "Submitting transaction..." in result.output
    assert "Confirmed" in result.output
    assert "Gas limit:   252,000" in result.output
    assert "max 2.50 gwei" in result.output
    assert result.output.count("Confirmed") == 2
    assert "Kick transaction sent. Ending run after the first submitted candidate." not in result.output
    assert "No kick transactions were sent." not in result.output
    assert "Explorer:" not in result.output
    assert "Block:" not in result.output
    assert "Gas used:" not in result.output
    assert "Gas estimate:" not in result.output


def test_operator_kick_run_no_confirmation_stops_after_first_successful_broadcast(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _BroadcastClient()
    prepared_actions: list[tuple[str, list[TxIntent]]] = []

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

    def fake_execute_prepared_action_sync(**kwargs):  # noqa: ANN003
        prepared_actions.append((kwargs["action_id"], kwargs["transactions"]))
        return [_broadcast_record(transactions=kwargs["transactions"], sender=kwargs["sender"], tx_hash="0x" + "1" * 64)]

    monkeypatch.setattr(
        operator_kick_cli_module,
        "execute_prepared_action_sync",
        fake_execute_prepared_action_sync,
    )

    runner = CliRunner()
    result = runner.invoke(operator_app, ["kick", "run", "--no-confirmation", "--config", str(config_path)])

    assert result.exit_code == 0
    assert [call["tokenAddress"] for call in client.prepare_calls] == [
        "0x3333333333333333333333333333333333333333",
    ]
    assert [action_id for action_id, _ in prepared_actions] == ["action-1"]
    assert "Kick transaction sent. Ending run after the first submitted candidate." in result.output


def test_operator_kick_run_queues_deferred_same_auction_candidates_for_review(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _BroadcastClient(
        ready_entries=[
            _ready_entry(
                token_address="0x3333333333333333333333333333333333333333",
                source_address="0x1111111111111111111111111111111111111111",
                auction_address="0x2222222222222222222222222222222222222222",
            )
        ],
        deferred_entries=[
            _deferred_entry(
                token_address="0x4444444444444444444444444444444444444444",
                source_address="0x1111111111111111111111111111111111111111",
                auction_address="0x2222222222222222222222222222222222222222",
            )
        ],
    )
    confirm_answers = iter([False, True])
    prepared_actions: list[tuple[str, list[TxIntent]]] = []

    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
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
    monkeypatch.setattr(operator_kick_cli_module.typer, "confirm", lambda *args, **kwargs: next(confirm_answers))

    def fake_execute_prepared_action_sync(**kwargs):  # noqa: ANN003
        prepared_actions.append((kwargs["action_id"], kwargs["transactions"]))
        return [_broadcast_record(transactions=kwargs["transactions"], sender=kwargs["sender"], tx_hash="0x" + "1" * 64)]

    monkeypatch.setattr(
        operator_kick_cli_module,
        "execute_prepared_action_sync",
        fake_execute_prepared_action_sync,
    )

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        ["kick", "run", "--source-type", "fee-burner", "--config", str(config_path)],
    )

    assert result.exit_code == 0
    assert [call["tokenAddress"] for call in client.prepare_calls] == [
        "0x3333333333333333333333333333333333333333",
        "0x4444444444444444444444444444444444444444",
    ]
    assert [action_id for action_id, _ in prepared_actions] == ["action-2"]
    assert "Kick (1 of 2)" in result.output
    assert "Kick (2 of 2)" in result.output
    assert "Confirmed" in result.output
    assert "Kick transaction sent. Ending run after the first submitted candidate." not in result.output


def test_operator_kick_run_prepare_noop_does_not_repeat_generic_footer(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _PrepareNoopClient()

    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
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

    runner = CliRunner()
    result = runner.invoke(operator_app, ["kick", "run", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "Skip" in result.output
    assert "Candidate was skipped during prepare" in result.output
    assert "Attempted Pair: CRV -> USDC" in result.output
    assert "Source:         Yearn Fee Burner (0x1111…1111)" in result.output
    assert "Auction:        0x2222222222222222222222222222222222222222" in result.output
    assert "No kick transactions were sent." not in result.output


def test_operator_kick_run_fee_burner_active_auction_skip_stops_after_first_candidate(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _FeeBurnerSameAuctionNoopClient()

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
    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: pytest.fail("kick run should not preflight authenticated API access"),
    )
    monkeypatch.setattr(
        operator_kick_cli_module,
        "_resolve_preview_fee_context",
        lambda *_args, **_kwargs: pytest.fail("fee preview should not load when prepare returns noop"),
    )

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        ["kick", "run", "--source-type", "fee-burner", "--config", str(config_path)],
    )

    assert result.exit_code == 2
    assert [call["tokenAddress"] for call in client.prepare_calls] == [
        "0x3333333333333333333333333333333333333333",
    ]
    assert result.output.count("Skip") == 1
    assert "Auction still active above minimumPrice" in result.output
    assert "Ending review for the remaining same-auction candidates." in result.output
    assert "Auction is still active above minimumPrice. Ending review" not in result.output
    assert "Kick (2 of 2)" not in result.output
    assert "No kick transactions were sent." not in result.output


def test_operator_kick_run_fee_burner_active_auction_skip_continues_to_distinct_auction(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _BroadcastClient(
        ready_entries=[
            _entry(
                token_address="0x3333333333333333333333333333333333333333",
                source_address="0x1111111111111111111111111111111111111111",
                auction_address="0x2222222222222222222222222222222222222222",
                state="ready",
                source_type="fee_burner",
            ),
            _entry(
                token_address="0x5555555555555555555555555555555555555555",
                source_address="0x6666666666666666666666666666666666666666",
                auction_address="0x7777777777777777777777777777777777777777",
                state="ready",
                source_type="fee_burner",
            ),
        ],
        deferred_entries=[
            _deferred_entry(
                token_address="0x4444444444444444444444444444444444444444",
                source_address="0x1111111111111111111111111111111111111111",
                auction_address="0x2222222222222222222222222222222222222222",
            )
        ],
    )
    confirm_answers = iter([True])
    prepared_actions: list[str] = []

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
    monkeypatch.setattr(operator_kick_cli_module.typer, "confirm", lambda *args, **kwargs: next(confirm_answers))

    original_prepare = client.prepare_kicks

    def prepare_with_first_skip(body: dict[str, object]) -> dict[str, object]:
        if body["tokenAddress"] == "0x3333333333333333333333333333333333333333":
            client.prepare_calls.append(body)
            return {
                "status": "noop",
                "warnings": [],
                "data": {
                    "preview": {
                        "skippedDuringPrepare": [
                            {
                                "sourceName": "yCRV Fee Burner",
                                "sourceAddress": "0x1111111111111111111111111111111111111111",
                                "auctionAddress": "0x2222222222222222222222222222222222222222",
                                "tokenSymbol": "CRV",
                                "wantSymbol": "USDC",
                                "reason": "auction still active above minimumPrice",
                            }
                        ]
                    },
                    "transactions": [],
                },
            }
        return original_prepare(body)

    def fake_execute_prepared_action_sync(**kwargs):  # noqa: ANN003
        prepared_actions.append(kwargs["action_id"])
        return [_broadcast_record(transactions=kwargs["transactions"], sender=kwargs["sender"], tx_hash="0x" + "2" * 64)]

    monkeypatch.setattr(client, "prepare_kicks", prepare_with_first_skip)
    monkeypatch.setattr(
        operator_kick_cli_module,
        "execute_prepared_action_sync",
        fake_execute_prepared_action_sync,
    )

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        ["kick", "run", "--source-type", "fee-burner", "--config", str(config_path)],
    )

    assert result.exit_code == 0
    assert [call["tokenAddress"] for call in client.prepare_calls] == [
        "0x3333333333333333333333333333333333333333",
        "0x5555555555555555555555555555555555555555",
    ]
    assert prepared_actions == ["action-2"]
    assert "Ending review for the remaining same-auction candidates." in result.output
    assert "Kick (2 of 3)" in result.output
    assert "Kick (3 of 3)" not in result.output


def test_operator_kick_run_declined_confirmations_reports_skipped_summary(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _BroadcastClient()

    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
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
    monkeypatch.setattr(operator_kick_cli_module.typer, "confirm", lambda *args, **kwargs: False)

    runner = CliRunner()
    result = runner.invoke(operator_app, ["kick", "run", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "All prepared kick transactions were skipped." in result.output
    assert "No kick transactions were sent." not in result.output


def test_operator_kick_run_warns_and_skips_stale_prepared_transaction(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _BroadcastClient(
        [
            _ready_entry(
                token_address="0x3333333333333333333333333333333333333333",
                source_address="0x1111111111111111111111111111111111111111",
                auction_address="0x2222222222222222222222222222222222222222",
            )
        ]
    )
    monotonic_values = iter([100.0, 401.0])

    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
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
    monkeypatch.setattr(operator_kick_cli_module, "_current_monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        operator_kick_cli_module,
        "execute_prepared_action_sync",
        lambda **kwargs: pytest.fail(f"stale prepared action should not broadcast: {kwargs}"),
    )

    runner = CliRunner()
    result = runner.invoke(operator_app, ["kick", "run", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "Warnings" in result.output
    assert "Prepared transaction expired after 5 minutes" in result.output
    assert "All prepared kick transactions were skipped." in result.output


def test_operator_kick_run_uses_configured_prepared_transaction_age_limit(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path, extra="prepared_action_max_age_seconds: 1\n")
    client = _BroadcastClient(
        [
            _ready_entry(
                token_address="0x3333333333333333333333333333333333333333",
                source_address="0x1111111111111111111111111111111111111111",
                auction_address="0x2222222222222222222222222222222222222222",
            )
        ]
    )
    monotonic_values = iter([100.0, 102.0])

    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
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
    monkeypatch.setattr(operator_kick_cli_module, "_current_monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        operator_kick_cli_module,
        "execute_prepared_action_sync",
        lambda **kwargs: pytest.fail(f"stale prepared action should not broadcast: {kwargs}"),
    )

    runner = CliRunner()
    result = runner.invoke(operator_app, ["kick", "run", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "Prepared transaction expired after 1 second" in result.output


def test_operator_kick_run_no_confirmation_still_blocks_stale_prepared_transaction(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = _write_config(tmp_path)
    client = _BroadcastClient(
        [
            _ready_entry(
                token_address="0x3333333333333333333333333333333333333333",
                source_address="0x1111111111111111111111111111111111111111",
                auction_address="0x2222222222222222222222222222222222222222",
            )
        ]
    )
    monotonic_values = iter([100.0, 401.0])

    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: None,
    )
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
    monkeypatch.setattr(
        operator_kick_cli_module.typer,
        "confirm",
        lambda *args, **kwargs: pytest.fail("confirmation prompt should not run with --no-confirmation"),
    )
    monkeypatch.setattr(operator_kick_cli_module, "_current_monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        operator_kick_cli_module,
        "execute_prepared_action_sync",
        lambda **kwargs: pytest.fail(f"stale prepared action should not broadcast: {kwargs}"),
    )

    runner = CliRunner()
    result = runner.invoke(
        operator_app,
        ["kick", "run", "--no-confirmation", "--config", str(config_path)],
    )

    assert result.exit_code == 2
    assert "Prepared transaction expired after 5 minutes" in result.output


def test_operator_kick_run_does_not_preflight_api_auth_before_resolving_execution(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    client = _PrepareNoopClient()

    monkeypatch.setattr(
        operator_kick_cli_module.CLIContext,
        "verify_authenticated_api_access",
        lambda self: pytest.fail("kick run should not preflight authenticated API access"),
    )
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

    runner = CliRunner()
    result = runner.invoke(operator_app, ["kick", "run", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "Skip" in result.output


def test_operator_kick_run_json_requires_no_confirmation(tmp_path) -> None:
    config_path = _write_config(tmp_path)

    runner = CliRunner()
    result = runner.invoke(operator_app, ["kick", "run", "--json", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "Invalid value for --json" in result.output
    assert "--no-confirmation" in result.output
