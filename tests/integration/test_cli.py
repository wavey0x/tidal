from typer.testing import CliRunner

from tidal.cli import app


def test_scan_once_requires_rpc_url(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RPC_URL", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("RPC_URL: ''\nDB_PATH: ./test.db\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "RPC_URL is required" in result.output


def test_scan_once_requires_keystore_when_auto_settle_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RPC_URL", "https://example-rpc.invalid")
    monkeypatch.delenv("TXN_KEYSTORE_PATH", raising=False)
    monkeypatch.delenv("TXN_KEYSTORE_PASSPHRASE", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "db_path: ./test.db\nscan_auto_settle_enabled: true\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "TXN_KEYSTORE_PATH and TXN_KEYSTORE_PASSPHRASE are required" in result.output


def test_txn_confirm_rejects_json_output() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["txn", "--confirm", "--output", "json"])

    assert result.exit_code != 0
    assert "interactive confirmation requires --output text" in result.output


def test_txn_rejects_invalid_source_address() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["txn", "--source", "not-an-address"])

    assert result.exit_code != 0
    assert "invalid address" in result.output


def test_txn_rejects_invalid_auction_address() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["txn", "--auction", "not-an-address"])

    assert result.exit_code != 0
    assert "invalid address" in result.output
