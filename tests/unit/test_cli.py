import json
from types import SimpleNamespace
from unittest.mock import patch

from eth_utils import to_checksum_address

from tidal.cli_renderers import (
    BroadcastRecord,
    render_broadcast_records,
    render_kick_run_summary,
    render_prepared_action_summary,
)
from tidal.kick_cli import _make_confirm_fn, _make_execution_report_fn
from tidal.transaction_service.types import TransactionExecutionReport


def test_make_confirm_fn_displays_pricing_profile(capsys):
    confirm_fn = _make_confirm_fn()
    summary = {
        "kicks": [
            {
                "source": "0x1111111111111111111111111111111111111111",
                "source_name": "Test Strategy",
                "token_symbol": "CRV",
                "auction": "0x2222222222222222222222222222222222222222",
                "sell_amount": "1000",
                "usd_value": "2500",
                "starting_price": "2750",
                "starting_price_display": "2,750 USDC (+10.00% buffer)",
                "minimum_price": "2375000000000000000",
                "minimum_quote": "2375",
                "minimum_quote_display": "2,375 USDC (-5.00% buffer)",
                "minimum_price_display": "2,375 USDC (-5.00% buffer)",
                "want_symbol": "USDC",
                "want_price_usd": "1",
                "buffer_bps": 1000,
                "min_buffer_bps": 500,
                "step_decay_rate_bps": 1,
                "pricing_profile_name": "stable",
                "quote_amount": "2500",
                "floor_rate": "2.375",
            }
        ],
        "batch_size": 1,
        "total_usd": "2500",
        "gas_estimate": 210000,
        "gas_limit": 252000,
        "base_fee_gwei": 10,
        "priority_fee_gwei": 2,
        "max_fee_per_gas_gwei": 12,
        "gas_cost_eth": 0.0021,
        "quote_spot_warning_threshold_pct": 2,
    }

    with patch("tidal.kick_cli.typer.confirm", return_value=True) as confirm_mock:
        result = confirm_fn(summary)

    output = capsys.readouterr().out
    assert result is True
    assert "Auction details" in output
    assert "Send details" in output
    assert f"Auction:     {to_checksum_address('0x2222222222222222222222222222222222222222')}" in output
    assert "Quote out:   2,500.00 USDC (~$2,500.00)" in output
    assert "From:        -" in output
    assert "Gas limit:   252,000" in output
    assert "Rate:        2.5000 quoted | 2.7500 start | 2.3750 floor USDC/CRV" in output
    assert "Min quote:   2,375 USDC (-5.00% buffer)" in output
    assert "Profile:     stable | decay 0.01%" in output
    assert "Submitting transaction..." in output
    confirm_mock.assert_called_once_with("Send this transaction?", default=False)


def test_make_confirm_fn_warns_on_sell_vs_quote_mismatch(capsys):
    confirm_fn = _make_confirm_fn()
    summary = {
        "kicks": [
            {
                "source": "0x1111111111111111111111111111111111111111",
                "source_name": "Test Strategy",
                "token_symbol": "WFRAX",
                "auction": "0x2222222222222222222222222222222222222222",
                "sell_amount": "3473.41",
                "usd_value": "10000",
                "starting_price": "1725",
                "starting_price_display": "1,725 crvUSD (+10.00% buffer)",
                "minimum_price": "428685355313654305",
                "minimum_quote": "1489",
                "minimum_quote_display": "1,489 crvUSD (-5.00% buffer)",
                "minimum_price_display": "1,489 crvUSD (-5.00% buffer)",
                "want_symbol": "crvUSD",
                "want_price_usd": "1",
                "buffer_bps": 1000,
                "min_buffer_bps": 500,
                "step_decay_rate_bps": 50,
                "pricing_profile_name": "volatile",
                "quote_amount": "1568",
                "floor_rate": "0.428685355313654305",
            }
        ],
        "batch_size": 1,
        "total_usd": "10000",
        "gas_estimate": 210000,
        "gas_limit": 252000,
        "base_fee_gwei": 0.1,
        "priority_fee_gwei": 0.05,
        "max_fee_per_gas_gwei": 2.5,
        "gas_cost_eth": 0.000021,
        "quote_spot_warning_threshold_pct": 2,
    }

    with patch("tidal.kick_cli.typer.confirm", return_value=False):
        _ = confirm_fn(summary)

    output = capsys.readouterr().out
    assert "Sell amount: 3,473.41 WFRAX (~$10,000.00)" in output
    assert "Quote out:   1,568.00 crvUSD (~$1,568.00)" in output
    assert (
        "⚠️  Warning: live quote is 84.3% lower than evaluated spot "
        "(1,568.00 crvUSD quoted vs 10,000.00 crvUSD at spot)"
    ) in output
    assert output.index("⚠️  Warning:") < output.index("Kick (1 of 1)")


def test_make_confirm_fn_respects_quote_spot_warning_threshold(capsys):
    confirm_fn = _make_confirm_fn()
    summary = {
        "kicks": [
            {
                "source": "0x1111111111111111111111111111111111111111",
                "source_name": "Test Strategy",
                "token_symbol": "CRV",
                "auction": "0x2222222222222222222222222222222222222222",
                "sell_amount": "1000",
                "usd_value": "1000",
                "starting_price": "1090",
                "starting_price_display": "1,090 USDC (+10.00% buffer)",
                "minimum_price": "940000000000000000",
                "minimum_quote": "940",
                "minimum_quote_display": "940 USDC (-5.00% buffer)",
                "minimum_price_display": "940 USDC (-5.00% buffer)",
                "want_symbol": "USDC",
                "want_price_usd": "1",
                "buffer_bps": 1000,
                "min_buffer_bps": 500,
                "step_decay_rate_bps": 50,
                "pricing_profile_name": "volatile",
                "quote_amount": "1010",
                "floor_rate": "0.94",
            }
        ],
        "batch_size": 1,
        "total_usd": "1000",
        "gas_estimate": 210000,
        "gas_limit": 252000,
        "base_fee_gwei": 0.1,
        "priority_fee_gwei": 0.05,
        "max_fee_per_gas_gwei": 2.5,
        "gas_cost_eth": 0.000021,
        "quote_spot_warning_threshold_pct": 2,
    }

    with patch("tidal.kick_cli.typer.confirm", return_value=False):
        _ = confirm_fn(summary)

    output = capsys.readouterr().out
    assert "Quote out:   1,010.00 USDC (~$1,010.00)" in output
    assert "⚠️  Warning:" not in output


def test_make_execution_report_fn_renders_confirmed_panel(capsys):
    report_fn = _make_execution_report_fn()

    report_fn(
        TransactionExecutionReport(
            operation="kick",
            sender="0x1111111111111111111111111111111111111111",
            tx_hash="0xabc",
            broadcast_at="2026-03-29T18:57:32+00:00",
            chain_id=1,
            gas_estimate=227159,
            receipt_status="CONFIRMED",
            block_number=24765182,
            gas_used=224212,
        )
    )

    output = capsys.readouterr().out
    assert "Confirmed" in output
    assert "Operation:    kick" in output
    assert "Broadcast at: Mar 29, 2026 18:57:32 UTC" in output
    assert "Explorer:" not in output
    assert "Gas used:" not in output
    assert "Gas estimate:" not in output


def test_render_kick_run_summary_for_aborted_confirm(capsys):
    result = SimpleNamespace(
        run_id="run-123",
        candidates_found=1,
        kicks_attempted=0,
        kicks_succeeded=0,
        kicks_failed=0,
        failure_summary=None,
    )

    render_kick_run_summary(
        result=result,
        live=True,
        source_type="fee_burner",
        source_address=None,
        auction_address=None,
        run_rows=[{"status": "USER_SKIPPED", "tx_hash": None}],
        verbose=False,
    )

    output = capsys.readouterr().out
    assert "Skipped by operator. No transaction sent." in output
    assert "Run ID:       run-123" in output
    assert "Type:         fee-burner" in output
    assert "Candidates:   1" in output
    assert "Attempted:    0" in output
    assert "Skipped:      1" in output


def test_render_kick_run_summary_for_mixed_confirm_and_skip(capsys):
    result = SimpleNamespace(
        run_id="run-456",
        candidates_found=2,
        kicks_attempted=1,
        kicks_succeeded=1,
        kicks_failed=0,
        failure_summary=None,
    )

    render_kick_run_summary(
        result=result,
        live=True,
        source_type="strategy",
        source_address=None,
        auction_address=None,
        run_rows=[
            {"status": "USER_SKIPPED", "tx_hash": None},
            {"status": "CONFIRMED", "tx_hash": "0xabc"},
        ],
        verbose=False,
    )

    output = capsys.readouterr().out
    assert "Confirmed." in output
    assert "Skipped:      1" in output
    assert "Aborted. No transaction sent." not in output


def test_render_kick_run_summary_reports_deferred_same_auction_tokens(capsys):
    result = SimpleNamespace(
        run_id="run-789",
        candidates_found=1,
        kicks_attempted=1,
        kicks_succeeded=1,
        kicks_failed=0,
        eligible_candidates_found=4,
        deferred_same_auction_count=3,
        failure_summary=None,
    )

    render_kick_run_summary(
        result=result,
        live=True,
        source_type="fee_burner",
        source_address=None,
        auction_address=None,
        run_rows=[{"status": "CONFIRMED", "tx_hash": "0xabc"}],
        verbose=False,
    )

    output = capsys.readouterr().out
    assert "Eligible:     4" in output
    assert "Candidates:   1" in output
    assert "Deferred:     3" in output
    assert "only one lot per auction can be kicked at a time" in output


def test_render_kick_run_summary_reports_target_filters(capsys):
    result = SimpleNamespace(
        run_id="run-999",
        candidates_found=1,
        kicks_attempted=1,
        kicks_succeeded=1,
        kicks_failed=0,
        failure_summary=None,
    )

    render_kick_run_summary(
        result=result,
        live=True,
        source_type="strategy",
        source_address="0x1111111111111111111111111111111111111111",
        auction_address="0x2222222222222222222222222222222222222222",
        run_rows=[{"status": "CONFIRMED", "tx_hash": "0xabc"}],
        verbose=False,
    )

    output = capsys.readouterr().out
    assert "Source:       0x1111111111111111111111111111111111111111" in output
    assert "Auction:      0x2222222222222222222222222222222222222222" in output


def test_render_broadcast_records_includes_sender_hash_and_datetime(capsys):
    render_broadcast_records(
        [
            BroadcastRecord(
                operation="settle",
                sender="0x1111111111111111111111111111111111111111",
                tx_hash="0xabc",
                broadcast_at="2026-03-28T15:04:05.123456+00:00",
                chain_id=1,
                receipt_status="CONFIRMED",
                block_number=12345,
                gas_used=210000,
                gas_estimate=240000,
            )
        ]
    )

    output = capsys.readouterr().out
    assert "Confirmed" in output
    assert "Operation:    settle" in output
    assert "Sender:       0x1111111111111111111111111111111111111111" in output
    assert "Tx hash:      0xabc" in output
    assert "Broadcast at: Mar 28, 2026 15:04:05 UTC" in output
    assert "Receipt:      CONFIRMED" in output
    assert "Explorer:" not in output
    assert "Block:" not in output
    assert "Gas used:" not in output
    assert "Gas estimate:" not in output


def test_render_broadcast_records_pending_confirmation(capsys):
    render_broadcast_records(
        [
            BroadcastRecord(
                operation="kick",
                sender="0x1111111111111111111111111111111111111111",
                tx_hash="0xabc",
                broadcast_at="2026-03-28T15:04:05+00:00",
                chain_id=1,
            )
        ]
    )

    output = capsys.readouterr().out
    assert "Pending Confirmation" in output
    assert "Tx hash:      0xabc" in output
    assert "Receipt:      pending" in output


def test_render_prepared_action_summary_for_deploy(capsys):
    render_prepared_action_summary(
        {
            "actionId": "action-deploy",
            "actionType": "deploy",
            "preview": {
                "factoryAddress": "0x1111111111111111111111111111111111111111",
                "want": "0x2222222222222222222222222222222222222222",
                "receiver": "0x3333333333333333333333333333333333333333",
                "governance": "0x4444444444444444444444444444444444444444",
                "startingPrice": 1234,
                "salt": "0xabc",
                "predictedAuctionAddress": "0x5555555555555555555555555555555555555555",
                "predictedAuctionAddressExists": False,
                "existingMatches": [
                    {
                        "auction_address": "0x6666666666666666666666666666666666666666",
                        "factory_address": "0x7777777777777777777777777777777777777777",
                        "starting_price": 1200,
                        "version": "1.0.0",
                    }
                ],
            },
            "transactions": [
                {
                    "operation": "deploy",
                    "to": "0x1111111111111111111111111111111111111111",
                    "sender": "0x8888888888888888888888888888888888888888",
                    "chainId": 1,
                    "gasEstimate": 210000,
                    "gasLimit": 252000,
                }
            ],
        },
        heading="Prepared action",
    )

    output = capsys.readouterr().out
    assert "Prepared action" in output
    assert "deploy · 1 transaction" in output
    assert "Review details" in output
    assert "Auction:" in output
    assert "Start price: 1,234" in output
    assert "Matches:     1" in output
    assert "Send details" in output


def test_render_prepared_action_summary_for_settle(capsys):
    render_prepared_action_summary(
        {
            "actionId": "action-settle",
            "actionType": "settle",
            "preview": {
                "inspection": {
                    "auction_address": "0x1111111111111111111111111111111111111111",
                    "is_active_auction": True,
                    "active_tokens": ["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
                    "active_token": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "active_available_raw": 0,
                    "active_price_public_raw": 125,
                    "minimum_price_scaled_1e18": 100,
                    "minimum_price_public_raw": 100,
                },
                "decision": {
                    "status": "actionable",
                    "operation_type": "settle",
                    "token_address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "reason": "active lot is sold out",
                },
            },
            "transactions": [
                {
                    "operation": "settle",
                    "to": "0x1111111111111111111111111111111111111111",
                    "sender": "0x9999999999999999999999999999999999999999",
                    "chainId": 1,
                    "gasEstimate": 150000,
                    "gasLimit": 180000,
                }
            ],
        },
        heading="Prepared action",
    )

    output = capsys.readouterr().out
    assert "settle · 1 transaction" in output
    assert "Review details" in output
    assert "Operation:   settle" in output
    assert "Reason:      active lot is sold out" in output
    assert "Auction:" in output


def test_render_kick_run_summary_shows_transaction_block(capsys):
    result = SimpleNamespace(
        run_id="run-tx",
        candidates_found=1,
        kicks_attempted=1,
        kicks_succeeded=1,
        kicks_failed=0,
        failure_summary=None,
    )

    render_kick_run_summary(
        result=result,
        live=True,
        source_type="strategy",
        source_address=None,
        auction_address=None,
        run_rows=[
            {
                "status": "CONFIRMED",
                "tx_hash": "0xabc",
                "operation_type": "kick",
                "block_number": 12345,
                "gas_used": 180000,
                "created_at": "2026-03-28T15:04:05+00:00",
            }
        ],
        verbose=False,
        sender="0x1111111111111111111111111111111111111111",
    )

    output = capsys.readouterr().out
    assert "Confirmed" in output
    assert "Operation:    kick" in output
    assert "Sender:       0x1111111111111111111111111111111111111111" in output
    assert "Tx hash:      0xabc" in output
    assert "Broadcast at: Mar 28, 2026 15:04:05 UTC" in output
    assert "Receipt:      CONFIRMED" in output


def test_render_kick_run_summary_reports_single_failure_detail_and_quote_url(capsys):
    result = SimpleNamespace(
        run_id="run-fail",
        candidates_found=1,
        kicks_attempted=1,
        kicks_succeeded=0,
        kicks_failed=1,
        failure_summary={"curve quote unavailable (status: error)": 1},
    )

    render_kick_run_summary(
        result=result,
        live=True,
        source_type="strategy",
        source_address="0x1111111111111111111111111111111111111111",
        auction_address=None,
        run_rows=[
            {
                "status": "ERROR",
                "tx_hash": None,
                "error_message": "curve quote unavailable (status: error)",
                "quote_response_json": json.dumps(
                    {
                        "requestUrl": (
                            "https://prices.example.com/v1/quote"
                            "?token_in=0xaaa&token_out=0xbbb&amount_in=1000&chain_id=1&use_underlying=true&timeout_ms=7000"
                        )
                    }
                ),
            }
        ],
        verbose=False,
    )

    output = capsys.readouterr().out
    assert "Failed." in output
    assert "Failure:      curve quote unavailable (status: error)" in output
    assert "Quote URL:\nhttps://prices.example.com/v1/quote?token_in=0xaaa&token_out=0xbbb&amount_in=1000&chain_id=1&use_underlying=true&timeout_ms=7000" in output
