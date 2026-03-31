import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import tidal.auction_cli as auction_cli_module
import tidal.kick_cli as kick_cli_module
from tidal.server_cli import app
from tidal.transaction_service.types import AuctionInspection


def _write_txn_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "server.yaml"
    config_path.write_text(
        (
            f"db_path: {tmp_path / 'test.db'}\n"
            "rpc_url: https://example-rpc.invalid\n"
            "kick:\n"
            "  default_profile: volatile\n"
            "  profiles:\n"
            "    volatile:\n"
            "      start_price_buffer_bps: 1000\n"
            "      min_price_buffer_bps: 500\n"
            "      step_decay_rate_bps: 25\n"
        ),
        encoding="utf-8",
    )
    return config_path


def _isolate_runtime_env(tmp_path: Path, monkeypatch) -> None:
    home_root = tmp_path / "home"
    home_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.delenv("TIDAL_HOME", raising=False)
    monkeypatch.delenv("TIDAL_CONFIG", raising=False)
    monkeypatch.delenv("TIDAL_ENV_FILE", raising=False)


def test_db_migrate_uses_same_tidal_home_from_different_working_directories(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "repo"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)
    (project_root / "pyproject.toml").write_text("[project]\nname='tidal'\nversion='0'\n", encoding="utf-8")
    (config_dir / "server.yaml").write_text(
        (
            f"db_path: {tmp_path / 'tidal.db'}\n"
            "kick:\n"
            "  default_profile: volatile\n"
            "  profiles:\n"
            "    volatile:\n"
            "      start_price_buffer_bps: 1000\n"
            "      min_price_buffer_bps: 500\n"
            "      step_decay_rate_bps: 25\n"
        ),
        encoding="utf-8",
    )

    captured_urls: list[str] = []

    def fake_run_migrations(database_url: str) -> None:
        captured_urls.append(database_url)

    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("TIDAL_HOME", raising=False)
    monkeypatch.delenv("TIDAL_CONFIG", raising=False)
    monkeypatch.delenv("TIDAL_ENV_FILE", raising=False)
    monkeypatch.setattr("tidal.server_cli.run_migrations", fake_run_migrations)

    cwd_a = project_root / "repo-a"
    cwd_b = project_root / "repo-b"
    cwd_a.mkdir()
    cwd_b.mkdir()

    runner = CliRunner()

    monkeypatch.chdir(cwd_a)
    result_a = runner.invoke(app, ["db", "migrate"])
    monkeypatch.chdir(cwd_b)
    result_b = runner.invoke(app, ["db", "migrate"])

    assert result_a.exit_code == 0
    assert result_b.exit_code == 0
    assert captured_urls == [
        f"sqlite:///{tmp_path / 'tidal.db'}",
        f"sqlite:///{tmp_path / 'tidal.db'}",
    ]


class _FakeTxnService:
    async def run_once(self, **kwargs):  # noqa: ANN003
        return SimpleNamespace(
            run_id="run-1",
            status="DRY_RUN",
            candidates_found=0,
            kicks_attempted=0,
            kicks_succeeded=0,
            kicks_failed=0,
            failure_summary={},
        )


class _FakeWeb3Client:
    async def get_base_fee(self) -> int:
        return 0


class _StopDaemon(Exception):
    pass


def test_scan_run_requires_rpc_url(tmp_path, monkeypatch) -> None:
    _isolate_runtime_env(tmp_path, monkeypatch)
    monkeypatch.delenv("RPC_URL", raising=False)
    config_path = tmp_path / "server.yaml"
    config_path.write_text(
        "RPC_URL: ''\nDB_PATH: ./test.db\nkick:\n  default_profile: volatile\n  profiles:\n    volatile:\n      start_price_buffer_bps: 1000\n      min_price_buffer_bps: 500\n      step_decay_rate_bps: 25\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "run", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "RPC_URL is required" in result.output


def test_scan_run_requires_keystore_when_auto_settle_enabled(tmp_path, monkeypatch) -> None:
    _isolate_runtime_env(tmp_path, monkeypatch)
    monkeypatch.setenv("RPC_URL", "https://example-rpc.invalid")
    monkeypatch.delenv("TXN_KEYSTORE_PATH", raising=False)
    monkeypatch.delenv("TXN_KEYSTORE_PASSPHRASE", raising=False)
    config_path = tmp_path / "server.yaml"
    config_path.write_text(
        "db_path: ./test.db\n"
        "scan_auto_settle_enabled: true\n"
        "txn_keystore_path: ''\n"
        "txn_keystore_passphrase: ''\n"
        "kick:\n"
        "  default_profile: volatile\n"
        "  profiles:\n"
        "    volatile:\n"
        "      start_price_buffer_bps: 1000\n"
        "      min_price_buffer_bps: 500\n"
        "      step_decay_rate_bps: 25\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "run", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "TXN_KEYSTORE_PATH and TXN_KEYSTORE_PASSPHRASE are required" in result.output


def test_kick_rejects_invalid_source_address() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["kick", "run", "--source", "not-an-address"])

    assert result.exit_code != 0
    assert "invalid address" in result.output


def test_kick_rejects_invalid_auction_address() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["kick", "run", "--auction", "not-an-address"])

    assert result.exit_code != 0
    assert "invalid address" in result.output


def test_kick_rejects_bypass_confirmation_without_broadcast() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["kick", "run", "--bypass-confirmation"])

    assert result.exit_code != 0
    assert "Invalid value for --bypass-confirmation" in result.output
    assert "--broadcast" in result.output


@pytest.mark.parametrize(
    ("flag_args", "expected"),
    [
        ([], None),
        (["--require-curve-quote"], True),
        (["--allow-missing-curve-quote"], False),
    ],
)
def test_kick_threads_curve_quote_override(tmp_path, monkeypatch, flag_args, expected) -> None:
    config_path = _write_txn_config(tmp_path)
    captured = {}

    def fake_build_txn_service(settings, session, **kwargs):  # noqa: ANN001, ANN003
        del settings, session
        captured["require_curve_quote"] = kwargs.get("require_curve_quote")
        return _FakeTxnService()

    monkeypatch.setattr(kick_cli_module, "build_txn_service", fake_build_txn_service)
    monkeypatch.setattr(kick_cli_module, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(kick_cli_module, "_load_run_rows", lambda session, run_id: [])

    runner = CliRunner()
    result = runner.invoke(app, ["kick", "run", "--json", "--config", str(config_path), *flag_args])

    assert result.exit_code == 2
    assert captured["require_curve_quote"] is expected


@pytest.mark.parametrize(
    ("flag_args", "expected"),
    [
        ([], None),
        (["--require-curve-quote"], True),
        (["--allow-missing-curve-quote"], False),
    ],
)
def test_kick_daemon_threads_curve_quote_override(tmp_path, monkeypatch, flag_args, expected) -> None:
    config_path = _write_txn_config(tmp_path)
    captured = {}

    def fake_build_txn_service(settings, session, **kwargs):  # noqa: ANN001, ANN003
        del settings, session
        captured["require_curve_quote"] = kwargs.get("require_curve_quote")
        return _FakeTxnService()

    async def fake_sleep(_seconds: int | float) -> None:
        raise _StopDaemon()

    monkeypatch.setattr(kick_cli_module, "build_txn_service", fake_build_txn_service)
    monkeypatch.setattr(kick_cli_module, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(kick_cli_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(kick_cli_module, "_load_run_rows", lambda session, run_id: [])
    monkeypatch.setattr("tidal.cli_context.build_web3_client", lambda settings: _FakeWeb3Client())

    runner = CliRunner()
    result = runner.invoke(app, ["kick", "daemon", "--config", str(config_path), *flag_args])

    assert isinstance(result.exception, _StopDaemon)
    assert captured["require_curve_quote"] is expected


def test_auction_enable_tokens_requires_rpc_url(tmp_path, monkeypatch) -> None:
    _isolate_runtime_env(tmp_path, monkeypatch)
    monkeypatch.setenv("RPC_URL", "")
    config_path = tmp_path / "server.yaml"
    config_path.write_text(
        "db_path: ./test.db\nkick:\n  default_profile: volatile\n  profiles:\n    volatile:\n      start_price_buffer_bps: 1000\n      min_price_buffer_bps: 500\n      step_decay_rate_bps: 25\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["auction", "enable-tokens", "0x1111111111111111111111111111111111111111", "--config", str(config_path)],
    )

    assert result.exit_code == 1
    assert "RPC_URL is required" in result.output


def test_auction_enable_tokens_rejects_invalid_extra_token() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "auction",
            "enable-tokens",
            "0x1111111111111111111111111111111111111111",
            "--extra-token",
            "not-an-address",
        ],
    )

    assert result.exit_code != 0
    assert "invalid address" in result.output


def test_auction_enable_tokens_rejects_invalid_sender() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "auction",
            "enable-tokens",
            "0x1111111111111111111111111111111111111111",
            "--sender",
            "not-an-address",
        ],
    )

    assert result.exit_code != 0
    assert "invalid address" in result.output


def test_auction_enable_tokens_rejects_bypass_confirmation_without_broadcast() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "auction",
            "enable-tokens",
            "0x1111111111111111111111111111111111111111",
            "--bypass-confirmation",
        ],
    )

    assert result.exit_code != 0
    assert "Invalid value for --bypass-confirmation" in result.output
    assert "--broadcast" in result.output


def test_auction_settle_requires_rpc_url(tmp_path, monkeypatch) -> None:
    _isolate_runtime_env(tmp_path, monkeypatch)
    monkeypatch.setenv("RPC_URL", "")
    config_path = tmp_path / "server.yaml"
    config_path.write_text(
        "db_path: ./test.db\nkick:\n  default_profile: volatile\n  profiles:\n    volatile:\n      start_price_buffer_bps: 1000\n      min_price_buffer_bps: 500\n      step_decay_rate_bps: 25\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["auction", "settle", "0x1111111111111111111111111111111111111111", "--config", str(config_path)],
    )

    assert result.exit_code == 1
    assert "RPC_URL is required" in result.output


def test_auction_settle_rejects_invalid_token() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "auction",
            "settle",
            "0x1111111111111111111111111111111111111111",
            "--token",
            "not-an-address",
        ],
    )

    assert result.exit_code != 0
    assert "invalid address" in result.output


def test_auction_settle_rejects_legacy_method_option() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "auction",
            "settle",
            "0x1111111111111111111111111111111111111111",
            "--method",
            "auto",
        ],
    )

    assert result.exit_code != 0
    assert "No such option: --method" in result.output


def test_auction_settle_rejects_bypass_confirmation_without_broadcast() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "auction",
            "settle",
            "0x1111111111111111111111111111111111111111",
            "--bypass-confirmation",
        ],
    )

    assert result.exit_code != 0
    assert "Invalid value for --bypass-confirmation" in result.output
    assert "--broadcast" in result.output


def test_auction_settle_json_dry_run_uses_auto_method(tmp_path, monkeypatch) -> None:
    config_path = _write_txn_config(tmp_path)

    async def fake_inspect_auction_settlement(web3_client, settings, auction_address):  # noqa: ANN001, ANN201
        del web3_client, settings
        return AuctionInspection(
            auction_address=auction_address,
            is_active_auction=True,
            active_tokens=("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",),
            active_token="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            active_available_raw=0,
            active_price_public_raw=123,
            minimum_price_scaled_1e18=100,
            minimum_price_public_raw=100,
        )

    async def fake_preview_settlement_execution(**kwargs):  # noqa: ANN003, ANN201
        del kwargs
        return {
            "operation_type": "settle",
            "auction": "0x1111111111111111111111111111111111111111",
            "token": "0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa",
            "sender": None,
            "target": "0x1111111111111111111111111111111111111111",
            "data": "0xdeadbeef",
            "gas_estimate": None,
            "gas_limit": None,
            "base_fee_gwei": 0.0,
            "priority_fee_gwei": 0.0,
        }

    monkeypatch.setattr(auction_cli_module, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(auction_cli_module, "build_web3_client", lambda settings: object())
    monkeypatch.setattr(auction_cli_module, "inspect_auction_settlement", fake_inspect_auction_settlement)
    monkeypatch.setattr(auction_cli_module, "_preview_settlement_execution", fake_preview_settlement_execution)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "auction",
            "settle",
            "0x1111111111111111111111111111111111111111",
            "--json",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["command"] == "auction.settle"
    assert payload["status"] == "ok"
    assert payload["data"]["decision"]["operation_type"] == "settle"
    assert payload["data"]["execution"]["data"] == "0xdeadbeef"


def test_auction_settle_json_dry_run_uses_sweep_override_above_floor(tmp_path, monkeypatch) -> None:
    config_path = _write_txn_config(tmp_path)

    async def fake_inspect_auction_settlement(web3_client, settings, auction_address):  # noqa: ANN001, ANN201
        del web3_client, settings
        return AuctionInspection(
            auction_address=auction_address,
            is_active_auction=True,
            active_tokens=("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",),
            active_token="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            active_available_raw=10**18,
            active_price_public_raw=101,
            minimum_price_scaled_1e18=100,
            minimum_price_public_raw=100,
        )

    async def fake_preview_settlement_execution(**kwargs):  # noqa: ANN003, ANN201
        del kwargs
        return {
            "operation_type": "sweep_and_settle",
            "auction": "0x1111111111111111111111111111111111111111",
            "token": "0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa",
            "sender": None,
            "target": "0x9999999999999999999999999999999999999999",
            "data": "0xfeedface",
            "gas_estimate": None,
            "gas_limit": None,
            "base_fee_gwei": 0.0,
            "priority_fee_gwei": 0.0,
        }

    monkeypatch.setattr(auction_cli_module, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(auction_cli_module, "build_web3_client", lambda settings: object())
    monkeypatch.setattr(auction_cli_module, "inspect_auction_settlement", fake_inspect_auction_settlement)
    monkeypatch.setattr(auction_cli_module, "_preview_settlement_execution", fake_preview_settlement_execution)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "auction",
            "settle",
            "0x1111111111111111111111111111111111111111",
            "--sweep",
            "--json",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["command"] == "auction.settle"
    assert payload["status"] == "ok"
    assert payload["data"]["decision"]["operation_type"] == "sweep_and_settle"
    assert payload["data"]["decision"]["reason"] == "forced sweep requested while auction is still active above minimumPrice"
    assert payload["warnings"] == [
        "Forced sweep requested while auction is still above floor; unsold tokens will be returned to the receiver."
    ]


def test_auction_legacy_sweep_and_settle_command_is_removed() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "auction",
            "sweep-and-settle",
            "0x1111111111111111111111111111111111111111",
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ],
    )

    assert result.exit_code != 0
    assert "No such command 'sweep-and-settle'" in result.output
