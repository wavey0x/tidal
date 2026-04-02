from tidal.config import load_client_settings, load_server_settings
from tidal.control_plane.outbox import default_action_report_outbox_path
from tidal.paths import default_txn_lock_path


def _clear_runtime_env(monkeypatch) -> None:
    for key in (
        "RPC_URL",
        "DB_PATH",
        "TIDAL_API_HOST",
        "TIDAL_API_PORT",
        "TOKEN_PRICE_AGG_BASE_URL",
        "AUCTIONSCAN_BASE_URL",
        "AUCTIONSCAN_API_BASE_URL",
        "TXN_KEYSTORE_PATH",
        "TXN_KEYSTORE_PASSPHRASE",
        "TIDAL_HOME",
        "TIDAL_CONFIG",
        "TIDAL_ENV_FILE",
        "PREPARED_ACTION_MAX_AGE_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)


def test_load_client_settings_defaults_to_tidal_home_paths(tmp_path, monkeypatch) -> None:
    home_root = tmp_path / "home"
    app_home = home_root / ".tidal"
    cli_home = app_home / "cli"
    cli_home.mkdir(parents=True)
    (cli_home / "config.yaml").write_text(
        "txn_keystore_path: keys/ops.json\n",
        encoding="utf-8",
    )
    (cli_home / ".env").write_text("RPC_URL=https://example-rpc.invalid\n", encoding="utf-8")

    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("HOME", str(home_root))

    settings = load_client_settings()

    assert settings.resolved_home_path == app_home
    assert settings.resolved_config_path == cli_home / "config.yaml"
    assert settings.resolved_env_path == cli_home / ".env"
    assert settings.resolved_db_path == app_home / "server" / "tidal.db"
    assert settings.resolved_txn_keystore_path == cli_home / "keys" / "ops.json"
    assert settings.prepared_action_max_age_seconds == 300
    assert settings.rpc_url == "https://example-rpc.invalid"


def test_load_client_settings_uses_tidal_config_override_and_config_local_env(tmp_path, monkeypatch) -> None:
    home_root = tmp_path / "home"
    home_root.mkdir(parents=True)
    app_home = home_root / ".tidal"
    cli_home = app_home / "cli"
    cli_home.mkdir(parents=True)
    (cli_home / "config.yaml").write_text("chain_id: 1\n", encoding="utf-8")
    (cli_home / ".env").write_text("RPC_URL=https://home.invalid\n", encoding="utf-8")

    config_dir = tmp_path / "custom-config"
    config_dir.mkdir()
    config_path = config_dir / "client.yaml"
    config_path.write_text(
        "txn_keystore_path: keys/override.json\n",
        encoding="utf-8",
    )
    (config_dir / ".env").write_text("RPC_URL=https://config-dir.invalid\n", encoding="utf-8")

    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.setenv("TIDAL_CONFIG", str(config_path))

    settings = load_client_settings()

    assert settings.resolved_config_path == config_path
    assert settings.resolved_env_path == config_dir / ".env"
    assert settings.resolved_db_path == app_home / "server" / "tidal.db"
    assert settings.resolved_txn_keystore_path == config_dir / "keys" / "override.json"
    assert settings.rpc_url == "https://config-dir.invalid"


def test_load_client_settings_uses_explicit_env_override(tmp_path, monkeypatch) -> None:
    home_root = tmp_path / "home"
    app_home = home_root / ".tidal"
    cli_home = app_home / "cli"
    cli_home.mkdir(parents=True)
    (cli_home / "config.yaml").write_text("chain_id: 1\n", encoding="utf-8")
    (cli_home / ".env").write_text("RPC_URL=https://home.invalid\n", encoding="utf-8")

    explicit_env_path = tmp_path / "secrets.env"
    explicit_env_path.write_text("RPC_URL=https://override.invalid\n", encoding="utf-8")

    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.setenv("TIDAL_ENV_FILE", str(explicit_env_path))

    settings = load_client_settings()

    assert settings.resolved_env_path == explicit_env_path
    assert settings.rpc_url == "https://override.invalid"


def test_load_client_settings_reads_prepared_action_max_age_seconds_from_config(tmp_path, monkeypatch) -> None:
    home_root = tmp_path / "home"
    app_home = home_root / ".tidal"
    cli_home = app_home / "cli"
    cli_home.mkdir(parents=True)
    (cli_home / "config.yaml").write_text(
        "prepared_action_max_age_seconds: 45\n",
        encoding="utf-8",
    )

    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("HOME", str(home_root))

    settings = load_client_settings()

    assert settings.prepared_action_max_age_seconds == 45


def test_load_server_settings_uses_project_config_and_embedded_kick(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "repo"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)
    home_root = tmp_path / "home"
    server_home = home_root / ".tidal" / "server"
    server_home.mkdir(parents=True)
    (project_root / "pyproject.toml").write_text("[project]\nname='tidal'\nversion='0'\n", encoding="utf-8")
    (config_dir / "server.yaml").write_text(
        """
chain_id: 1
monitored_fee_burners:
  - address: "0xb911Fcce8D5AFCEc73E072653107260bb23C1eE8"
    want_address: "0xf939e0a03fb07f59a73314e73794be0e57ac1b4e"
    label: "yCRV Fee Burner"
kick:
  default_profile: volatile
  profiles:
    volatile:
      start_price_buffer_bps: 1000
      min_price_buffer_bps: 500
      step_decay_rate_bps: 25
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (server_home / ".env").write_text("RPC_URL=https://server.invalid\n", encoding="utf-8")

    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.chdir(project_root)

    settings = load_server_settings()

    assert settings.resolved_config_path == config_dir / "server.yaml"
    assert settings.resolved_env_path == server_home / ".env"
    assert settings.rpc_url == "https://server.invalid"
    assert settings.tidal_api_host == "0.0.0.0"
    assert settings.tidal_api_port == 8787
    assert settings.token_price_agg_base_url == "https://prices.wavey.info"
    assert settings.auctionscan_base_url == "https://auctionscan.info"
    assert settings.auctionscan_api_base_url == "https://auctionscan.info/api"
    assert settings.multicall_auction_batch_calls == 100
    assert settings.kick_config.pricing_policy.default_profile_name == "volatile"


def test_load_server_settings_does_not_fall_back_to_client_env_file(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "repo"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)
    home_root = tmp_path / "home"
    app_home = home_root / ".tidal"
    cli_home = app_home / "cli"
    cli_home.mkdir(parents=True)
    (project_root / "pyproject.toml").write_text("[project]\nname='tidal'\nversion='0'\n", encoding="utf-8")
    (config_dir / "server.yaml").write_text(
        """
chain_id: 1
kick:
  default_profile: volatile
  profiles:
    volatile:
      start_price_buffer_bps: 1000
      min_price_buffer_bps: 500
      step_decay_rate_bps: 25
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (cli_home / ".env").write_text("RPC_URL=https://client.invalid\n", encoding="utf-8")

    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.chdir(project_root)

    settings = load_server_settings()

    assert settings.resolved_env_path == app_home / "server" / ".env"
    assert settings.rpc_url is None


def test_load_server_settings_requires_kick_mapping(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "repo"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)
    (project_root / "pyproject.toml").write_text("[project]\nname='tidal'\nversion='0'\n", encoding="utf-8")
    (config_dir / "server.yaml").write_text("chain_id: 1\n", encoding="utf-8")

    _clear_runtime_env(monkeypatch)
    monkeypatch.chdir(project_root)

    try:
        load_server_settings()
    except ValueError as exc:
        assert "kick" in str(exc)
    else:
        raise AssertionError("expected load_server_settings to require a kick mapping")


def test_default_outbox_and_lock_paths_live_under_tidal_home(tmp_path, monkeypatch) -> None:
    home_root = tmp_path / "home"
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("HOME", str(home_root))

    app_home = home_root / ".tidal"
    assert default_action_report_outbox_path() == app_home / "server" / "action_outbox.db"
    assert default_txn_lock_path() == app_home / "server" / "txn_daemon.lock"
