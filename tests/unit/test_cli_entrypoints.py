from typer.testing import CliRunner

from tidal.config import load_server_settings
from tidal.cli import app as operator_app
from tidal.server_cli import app as server_app


def test_operator_cli_does_not_expose_scan_or_db_commands() -> None:
    runner = CliRunner()
    result = runner.invoke(operator_app, ["--help"])

    assert result.exit_code == 0
    assert "scan" not in result.output
    assert "db" not in result.output
    assert "logs" in result.output
    assert "kick" in result.output
    assert "auction" in result.output
    assert "init" in result.output


def test_server_cli_only_exposes_runtime_commands() -> None:
    runner = CliRunner()
    result = runner.invoke(server_app, ["--help"])

    assert result.exit_code == 0
    assert "scan" in result.output
    assert "db" in result.output
    assert "api" in result.output
    assert "auth" in result.output
    assert "kick" not in result.output
    assert "auction" not in result.output
    assert "logs" not in result.output


def test_operator_init_creates_tidal_home_layout(tmp_path, monkeypatch) -> None:
    app_home = tmp_path / "operator-home"
    monkeypatch.setenv("TIDAL_HOME", str(app_home))

    runner = CliRunner()
    result = runner.invoke(operator_app, ["init"])

    assert result.exit_code == 0
    assert (app_home / "cli" / "config.yaml").is_file()
    assert (app_home / "cli" / ".env").is_file()
    scaffold = (app_home / "cli" / "config.yaml").read_text(encoding="utf-8")
    env_scaffold = (app_home / "cli" / ".env").read_text(encoding="utf-8")
    assert "https://api.tidal.wavey.info" in scaffold
    assert "prepared_action_max_age_seconds: 300" in scaffold
    assert "auction_kicker_address:" not in scaffold
    assert env_scaffold.index("TIDAL_API_KEY") < env_scaffold.index("RPC_URL")
    assert "Client dir:" in result.output
    assert "Config:" in result.output
    assert str(app_home / "cli" / "config.yaml") in result.output


def test_server_init_config_creates_tracked_server_config(tmp_path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "pyproject.toml").write_text("[project]\nname='tidal'\nversion='0'\n", encoding="utf-8")
    monkeypatch.chdir(repo_root)

    runner = CliRunner()
    result = runner.invoke(server_app, ["init-config"])

    assert result.exit_code == 0
    assert (repo_root / "config" / "server.yaml").is_file()
    assert (repo_root / "config" / ".env.example").is_file()
    scaffold = (repo_root / "config" / "server.yaml").read_text(encoding="utf-8")
    env_scaffold = (repo_root / "config" / ".env.example").read_text(encoding="utf-8")
    assert "kick:" in scaffold
    assert "profile_overrides:" in scaffold
    assert "auction_factory_address:" in scaffold
    assert "auction_kicker_address:" in scaffold
    assert "tidal_api_host:" not in scaffold
    assert "tidal_api_port:" not in scaffold
    assert "token_price_agg_base_url:" not in scaffold
    assert "auctionscan_base_url:" not in scaffold
    assert "auctionscan_api_base_url:" not in scaffold
    assert "tidal_api_receipt_reconcile_interval_seconds:" not in scaffold
    assert "scan_concurrency:" not in scaffold
    assert "rpc_timeout_seconds:" not in scaffold
    assert "multicall_auction_batch_calls:" not in scaffold
    assert "price_timeout_seconds:" not in scaffold
    assert "TIDAL_API_HOST=" not in env_scaffold
    assert "TIDAL_API_PORT=" not in env_scaffold
    assert "TOKEN_PRICE_AGG_BASE_URL=" not in env_scaffold
    assert "AUCTIONSCAN_BASE_URL=" not in env_scaffold
    assert "AUCTIONSCAN_API_BASE_URL=" not in env_scaffold
    assert "scan_auto_settle_enabled" not in scaffold
    assert "scan_interval_seconds" not in scaffold
    settings = load_server_settings(repo_root / "config" / "server.yaml")
    assert settings.multicall_auction_batch_calls == 100
    assert settings.kick_config.pricing_policy.default_profile_name == "volatile"
    assert "Server config:" in result.output
    assert "Env example:" in result.output
