from typer.testing import CliRunner

from tidal.cli import app


def test_scan_once_requires_rpc_url(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RPC_URL", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("RPC_URL: ''\nDB_PATH: ./test.db\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "once", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "RPC_URL is required" in result.output
