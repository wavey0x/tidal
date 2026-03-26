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
                "starting_price_display": "2,750 USDC (incl. 10% buffer)",
                "minimum_price": "2375",
                "minimum_price_display": "2,375 USDC (minus 5% buffer)",
                "want_symbol": "USDC",
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
    assert "Profile:     stable | decay 0.01%" in output
    assert "Submitting transaction..." in output
    confirm_mock.assert_called_once_with("Send this transaction?", default=False)


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
