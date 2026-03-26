from unittest.mock import patch

from factory_dashboard.cli import _make_confirm_fn


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

    with patch("factory_dashboard.cli.typer.confirm", return_value=False) as confirm_mock:
        result = confirm_fn(summary)

    output = capsys.readouterr().out
    assert result is False
    assert "Profile:     stable | decay 0.01%" in output
    confirm_mock.assert_called_once_with("Send this transaction?", default=False)
