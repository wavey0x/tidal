from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import tidal.scan_cli as scan_cli_module
from tidal.migrations import run_migrations
from tidal.persistence.db import Database
from tidal.persistence.repositories import KickTxRepository
from tidal.server_cli import app


AUCTION = "0x00000000000000000000000000000000000000a1"
TOKEN = "0x00000000000000000000000000000000000000b1"


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
            "  no_fill:\n"
            "    retry_delays_minutes: [720, 1440]\n"
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


def test_clear_no_fill_suspension_previews_then_applies(tmp_path, monkeypatch) -> None:
    _isolate_runtime_env(tmp_path, monkeypatch)
    monkeypatch.delenv("DB_PATH", raising=False)
    db_path = tmp_path / "tidal.db"
    config_path = tmp_path / "server.yaml"
    config_path.write_text(
        (
            f"db_path: {db_path}\n"
            "kick:\n"
            "  default_profile: volatile\n"
            "  no_fill:\n"
            "    retry_delays_minutes: [720, 1440]\n"
            "  profiles:\n"
            "    volatile:\n"
            "      start_price_buffer_bps: 1000\n"
            "      min_price_buffer_bps: 500\n"
            "      step_decay_rate_bps: 25\n"
        ),
        encoding="utf-8",
    )
    database_url = f"sqlite:///{db_path}"
    run_migrations(database_url)
    database = Database(database_url)
    with database.session() as session:
        repo = KickTxRepository(session)
        kick_id = repo.insert(
            {
                "run_id": "kick-run",
                "operation_type": "kick",
                "token_address": TOKEN,
                "auction_address": AUCTION,
                "sell_amount": "100",
                "requested_sell_amount": "100",
                "status": "CONFIRMED",
                "block_number": 100,
                "transaction_index": 0,
                "mined_at": "2026-08-01T00:00:00+00:00",
                "created_at": "2026-08-01T00:00:00+00:00",
            }
        )
        repo.insert(
            {
                "run_id": "resolve-run",
                "operation_type": "resolve_auction",
                "token_address": TOKEN,
                "auction_address": AUCTION,
                "sell_amount": "100",
                "status": "CONFIRMED",
                "block_number": 101,
                "transaction_index": 0,
                "round_kick_id": kick_id,
                "resolution_path": 1,
                "mined_at": "2026-08-01T01:00:00+00:00",
                "created_at": "2026-08-01T01:00:00+00:00",
            }
        )

    args = [
        "db",
        "clear-no-fill-suspension",
        "--auction",
        AUCTION,
        "--token",
        TOKEN,
        "--config",
        str(config_path),
    ]
    runner = CliRunner()
    preview = runner.invoke(app, args)
    assert preview.exit_code == 0, preview.output
    assert "preview only" in preview.output
    with database.session() as session:
        assert KickTxRepository(session).get(kick_id)["historical_baseline"] == 0

    applied = runner.invoke(app, [*args, "--apply"])
    assert applied.exit_code == 0, applied.output
    assert "applied" in applied.output
    with database.session() as session:
        row = KickTxRepository(session).get(kick_id)
        assert row["historical_baseline"] == 1
        assert (
            row["historical_baseline_reason"] == "OPERATOR_CLEARED_NO_FILL_SUSPENSION"
        )


class _FakeScannerService:
    async def scan_once(self, **kwargs):  # noqa: ANN003
        del kwargs
        return SimpleNamespace(status="SUCCESS")


def test_scan_run_requires_rpc_url(tmp_path, monkeypatch) -> None:
    _isolate_runtime_env(tmp_path, monkeypatch)
    monkeypatch.delenv("RPC_URL", raising=False)
    config_path = tmp_path / "server.yaml"
    config_path.write_text(
        "RPC_URL: ''\nDB_PATH: ./test.db\nkick:\n  default_profile: volatile\n  no_fill:\n    retry_delays_minutes: [720, 1440]\n  profiles:\n    volatile:\n      start_price_buffer_bps: 1000\n      min_price_buffer_bps: 500\n      step_decay_rate_bps: 25\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "run", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "RPC_URL is required" in result.output


def test_scan_run_requires_keystore_when_auto_settle_requested(tmp_path, monkeypatch) -> None:
    _isolate_runtime_env(tmp_path, monkeypatch)
    monkeypatch.setenv("RPC_URL", "https://example-rpc.invalid")
    monkeypatch.delenv("TXN_KEYSTORE_PATH", raising=False)
    monkeypatch.delenv("TXN_KEYSTORE_PASSPHRASE", raising=False)
    config_path = tmp_path / "server.yaml"
    config_path.write_text(
        "db_path: ./test.db\n"
        "txn_keystore_path: ''\n"
        "txn_keystore_passphrase: ''\n"
        "kick:\n"
        "  default_profile: volatile\n"
        "  no_fill:\n"
        "    retry_delays_minutes: [720, 1440]\n"
        "  profiles:\n"
        "    volatile:\n"
        "      start_price_buffer_bps: 1000\n"
        "      min_price_buffer_bps: 500\n"
        "      step_decay_rate_bps: 25\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "run", "--auto-settle", "--no-confirmation", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "TXN_KEYSTORE_PATH and TXN_KEYSTORE_PASSPHRASE are required" in result.output


def test_scan_run_requires_keystore_when_auto_enable_tokens_requested(tmp_path, monkeypatch) -> None:
    _isolate_runtime_env(tmp_path, monkeypatch)
    monkeypatch.setenv("RPC_URL", "https://example-rpc.invalid")
    monkeypatch.delenv("TXN_KEYSTORE_PATH", raising=False)
    monkeypatch.delenv("TXN_KEYSTORE_PASSPHRASE", raising=False)
    config_path = tmp_path / "server.yaml"
    config_path.write_text(
        "db_path: ./test.db\n"
        "txn_keystore_path: ''\n"
        "txn_keystore_passphrase: ''\n"
        "kick:\n"
        "  default_profile: volatile\n"
        "  no_fill:\n"
        "    retry_delays_minutes: [720, 1440]\n"
        "  profiles:\n"
        "    volatile:\n"
        "      start_price_buffer_bps: 1000\n"
        "      min_price_buffer_bps: 500\n"
        "      step_decay_rate_bps: 25\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["scan", "run", "--auto-enable-tokens", "--no-confirmation", "--config", str(config_path)],
    )

    assert result.exit_code == 1
    assert "TXN_KEYSTORE_PATH and TXN_KEYSTORE_PASSPHRASE are required" in result.output


def test_scan_run_requires_no_confirmation_when_auto_settle_requested(tmp_path, monkeypatch) -> None:
    _isolate_runtime_env(tmp_path, monkeypatch)
    monkeypatch.setenv("RPC_URL", "https://example-rpc.invalid")
    config_path = tmp_path / "server.yaml"
    config_path.write_text(
        "db_path: ./test.db\n"
        "kick:\n"
        "  default_profile: volatile\n"
        "  no_fill:\n"
        "    retry_delays_minutes: [720, 1440]\n"
        "  profiles:\n"
        "    volatile:\n"
        "      start_price_buffer_bps: 1000\n"
        "      min_price_buffer_bps: 500\n"
        "      step_decay_rate_bps: 25\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "run", "--auto-settle", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "--no-confirmation" in result.output


def test_scan_run_requires_no_confirmation_when_auto_enable_tokens_requested(tmp_path, monkeypatch) -> None:
    _isolate_runtime_env(tmp_path, monkeypatch)
    monkeypatch.setenv("RPC_URL", "https://example-rpc.invalid")
    config_path = tmp_path / "server.yaml"
    config_path.write_text(
        "db_path: ./test.db\n"
        "kick:\n"
        "  default_profile: volatile\n"
        "  no_fill:\n"
        "    retry_delays_minutes: [720, 1440]\n"
        "  profiles:\n"
        "    volatile:\n"
        "      start_price_buffer_bps: 1000\n"
        "      min_price_buffer_bps: 500\n"
        "      step_decay_rate_bps: 25\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "run", "--auto-enable-tokens", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "--no-confirmation" in result.output


@pytest.mark.parametrize(
    ("flag_args", "expected_auto_settle", "expected_auto_enable_tokens"),
    [
        ([], False, False),
        (["--auto-settle", "--no-confirmation"], True, False),
        (["--auto-enable-tokens", "--no-confirmation"], False, True),
        (["--auto-settle", "--auto-enable-tokens", "--no-confirmation"], True, True),
    ],
)
def test_scan_run_threads_transaction_automation_flags(
    tmp_path,
    monkeypatch,
    flag_args,
    expected_auto_settle,
    expected_auto_enable_tokens,
) -> None:
    _isolate_runtime_env(tmp_path, monkeypatch)
    monkeypatch.setenv("RPC_URL", "https://example-rpc.invalid")
    config_path = tmp_path / "server.yaml"
    config_path.write_text(
        "db_path: ./test.db\n"
        "txn_keystore_path: ./ops.json\n"
        "txn_keystore_passphrase: secret\n"
        "kick:\n"
        "  default_profile: volatile\n"
        "  no_fill:\n"
        "    retry_delays_minutes: [720, 1440]\n"
        "  profiles:\n"
        "    volatile:\n"
        "      start_price_buffer_bps: 1000\n"
        "      min_price_buffer_bps: 500\n"
        "      step_decay_rate_bps: 25\n",
        encoding="utf-8",
    )
    captured = {}

    def fake_build_scanner_service(
        settings,
        session,
        *,
        auto_settle=False,
        auto_enable_tokens=False,
    ):  # noqa: ANN001
        del settings, session
        captured["auto_settle"] = auto_settle
        captured["auto_enable_tokens"] = auto_enable_tokens
        return _FakeScannerService()

    monkeypatch.setattr(scan_cli_module, "build_scanner_service", fake_build_scanner_service)
    monkeypatch.setattr(scan_cli_module, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(scan_cli_module, "render_scan_summary", lambda result: None)

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "run", "--config", str(config_path), *flag_args])

    assert result.exit_code == 0
    assert captured["auto_settle"] is expected_auto_settle
    assert captured["auto_enable_tokens"] is expected_auto_enable_tokens


def test_scan_help_does_not_list_daemon() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["scan", "--help"])

    assert result.exit_code == 0
    assert "daemon" not in result.output
