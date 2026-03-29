import json
from types import SimpleNamespace
from unittest.mock import patch

from eth_utils import to_checksum_address

from tidal.cli_renderers import BroadcastRecord, render_broadcast_records, render_kick_run_summary
from tidal.kick_cli import _make_confirm_fn


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
                "starting_price_display": "2,750 USDC (+10% buffer)",
                "minimum_price": "2375",
                "minimum_price_display": "2,375 USDC (-5% buffer)",
                "want_symbol": "USDC",
                "want_price_usd": "1",
                "buffer_bps": 1000,
                "min_buffer_bps": 500,
                "step_decay_rate_bps": 1,
                "pricing_profile_name": "stable",
                "quote_amount": "2500",
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
    }

    with patch("tidal.kick_cli.typer.confirm", return_value=True) as confirm_mock:
        result = confirm_fn(summary)

    output = capsys.readouterr().out
    assert result is True
    assert "1 candidate ready for submission" in output
    assert "Auction details" in output
    assert "Send details" in output
    assert f"Auction:     {to_checksum_address('0x2222222222222222222222222222222222222222')}" in output
    assert "Quote out:   2,500.00 USDC (~$2,500.00)" in output
    assert "From:        -" in output
    assert "Gas limit:   252,000" in output
    assert "Rate:        2.5000 quoted | 2.7500 start | 2.3750 floor USDC/CRV" in output
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
                "starting_price_display": "1,725 crvUSD (+10% buffer)",
                "minimum_price": "1489",
                "minimum_price_display": "1,489 crvUSD (-5% buffer)",
                "want_symbol": "crvUSD",
                "want_price_usd": "1",
                "buffer_bps": 1000,
                "min_buffer_bps": 500,
                "step_decay_rate_bps": 50,
                "pricing_profile_name": "volatile",
                "quote_amount": "1568",
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
    }

    with patch("tidal.kick_cli.typer.confirm", return_value=False):
        _ = confirm_fn(summary)

    output = capsys.readouterr().out
    assert "Sell amount: 3,473.41 WFRAX (~$10,000.00)" in output
    assert "Quote out:   1,568.00 crvUSD (~$1,568.00)" in output
    assert "⚠️  Warning: sell value and quote value differ by 84.3%" in output
    assert output.index("⚠️  Warning:") < output.index("Kick (1 of 1)")


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
                broadcast_at="2026-03-28T15:04:05+00:00",
                receipt_status="CONFIRMED",
                block_number=12345,
                gas_used=210000,
                gas_estimate=240000,
            )
        ]
    )

    output = capsys.readouterr().out
    assert "Transaction:" in output
    assert "Operation:    settle" in output
    assert "Sender:       0x1111111111111111111111111111111111111111" in output
    assert "Tx hash:      0xabc" in output
    assert "Broadcast at: 2026-03-28T15:04:05+00:00" in output
    assert "Receipt:      CONFIRMED" in output
    assert "Block:        12,345" in output
    assert "Gas used:     210,000" in output
    assert "Gas estimate: 240,000" in output


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
    assert "Transaction:" in output
    assert "Operation:    kick" in output
    assert "Sender:       0x1111111111111111111111111111111111111111" in output
    assert "Tx hash:      0xabc" in output
    assert "Broadcast at: 2026-03-28T15:04:05+00:00" in output
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
