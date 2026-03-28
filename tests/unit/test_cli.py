import json
from types import SimpleNamespace
from unittest.mock import patch

from tidal.cli import _echo_txn_text_summary, _make_confirm_fn, _resolve_txn_output_mode
from tidal.logging import OutputMode


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

    with patch("tidal.cli.typer.confirm", return_value=True) as confirm_mock:
        result = confirm_fn(summary)

    output = capsys.readouterr().out
    assert result is True
    assert "1 candidate ready for submission" in output
    assert "Quote out:   2,500.00 USDC (~$2,500.00)" in output
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

    with patch("tidal.cli.typer.confirm", return_value=False):
        _ = confirm_fn(summary)

    output = capsys.readouterr().out
    assert "Sell amount: 3,473.41 WFRAX (~$10,000.00)" in output
    assert "Quote out:   1,568.00 crvUSD (~$1,568.00)" in output
    assert "⚠️  Warning: sell value and quote value differ by 84.3%" in output
    assert output.index("⚠️  Warning:") < output.index("Kick (1 of 1)")


def test_resolve_txn_output_mode_defaults_to_text_for_confirm():
    assert _resolve_txn_output_mode(requested=None, confirm=True) is OutputMode.TEXT


def test_echo_txn_text_summary_for_aborted_confirm(capsys):
    result = SimpleNamespace(
        run_id="run-123",
        candidates_found=1,
        kicks_attempted=0,
        kicks_succeeded=0,
        kicks_failed=0,
        failure_summary=None,
    )

    _echo_txn_text_summary(
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


def test_echo_txn_text_summary_for_mixed_confirm_and_skip(capsys):
    result = SimpleNamespace(
        run_id="run-456",
        candidates_found=2,
        kicks_attempted=1,
        kicks_succeeded=1,
        kicks_failed=0,
        failure_summary=None,
    )

    _echo_txn_text_summary(
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


def test_echo_txn_text_summary_reports_deferred_same_auction_tokens(capsys):
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

    _echo_txn_text_summary(
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


def test_echo_txn_text_summary_reports_target_filters(capsys):
    result = SimpleNamespace(
        run_id="run-999",
        candidates_found=1,
        kicks_attempted=1,
        kicks_succeeded=1,
        kicks_failed=0,
        failure_summary=None,
    )

    _echo_txn_text_summary(
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


def test_echo_txn_text_summary_reports_single_failure_detail_and_quote_url(capsys):
    result = SimpleNamespace(
        run_id="run-fail",
        candidates_found=1,
        kicks_attempted=1,
        kicks_succeeded=0,
        kicks_failed=1,
        failure_summary={"curve quote unavailable (status: error)": 1},
    )

    _echo_txn_text_summary(
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
                            "?token_in=0xaaa&token_out=0xbbb&amount_in=1000&chain_id=1&use_underlying=true"
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
    assert "Quote URL:\nhttps://prices.example.com/v1/quote?token_in=0xaaa&token_out=0xbbb&amount_in=1000&chain_id=1&use_underlying=true" in output
