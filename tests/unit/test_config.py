import pytest
import yaml
from tidal.config import load_settings, load_server_settings
from tidal.resources import read_template_text
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
        "TXN_DATA_FRESHNESS_LIMIT_SECONDS",
        "TXN_MAX_DATA_AGE_SECONDS",
        "TXN_BASE_FEE_CAP_GWEI",
        "TXN_MAX_BASE_FEE_GWEI",
    ):
        monkeypatch.delenv(key, raising=False)


def test_load_server_settings_reads_data_freshness_limit_seconds_from_config(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "repo"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)
    home_root = tmp_path / "home"
    (project_root / "pyproject.toml").write_text("[project]\nname='tidal'\nversion='0'\n", encoding="utf-8")
    (config_dir / "server.yaml").write_text(
        """
chain_id: 1
txn_data_freshness_limit_seconds: 1234
kick:
  default_profile: volatile
  no_fill:
    retry_delays_minutes: [720, 1440]
  profiles:
    volatile:
      start_price_buffer_bps: 1000
      min_price_buffer_bps: 500
      step_decay_rate_bps: 25
""".strip()
        + "\n",
        encoding="utf-8",
    )

    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.chdir(project_root)

    settings = load_server_settings()

    assert settings.txn_data_freshness_limit_seconds == 1234


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
  no_fill:
    retry_delays_minutes: [720, 1440]
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
    assert settings.price_delay_seconds == 0.25
    assert settings.auctionscan_base_url == "https://auctionscan.info"
    assert settings.auctionscan_api_base_url == "https://auctionscan.info/api"
    assert settings.auctionscan_enrichment_batch_size == 10
    assert settings.multicall_auction_batch_calls == 100
    assert settings.txn_usd_threshold == 250.0
    assert settings.txn_base_fee_cap_gwei == 5.0
    assert settings.kick_config.pricing_policy.default_profile_name == "volatile"
    assert settings.kick_config.no_fill_policy.retry_delays_minutes == (720, 1440)


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
  no_fill:
    retry_delays_minutes: [720, 1440]
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



@pytest.fixture
def unified(tmp_path, monkeypatch):
    _clear_runtime_env(monkeypatch)
    home = tmp_path / "home"
    monkeypatch.setenv("TIDAL_HOME", str(home))
    config = tmp_path / "config" / "server.yaml"
    config.parent.mkdir()
    values = yaml.safe_load(read_template_text("server.yaml"))
    config.write_text(yaml.safe_dump(values))
    monkeypatch.setenv("TIDAL_CONFIG", str(config))
    return home, config, values


def test_config_override_is_shared_by_every_local_command(unified):
    from tidal.cli_context import CLIContext
    home, config, _ = unified
    settings = load_settings()
    assert settings.resolved_config_path == config
    assert settings.resolved_env_path == home / "server" / ".env"
    assert CLIContext().settings.resolved_config_path == config
    assert settings.kick_config.no_fill_policy.retry_delays_minutes == (720, 1440)


@pytest.mark.parametrize("field,value", [
    ("prepared_action_max_age_seconds", 45),
    ("txn_base_fee_cap_gwei", 8),
    ("txn_data_freshness_limit_seconds", 1234),
])
def test_native_configuration_preserves_execution_limits(unified, field, value):
    _, config, values = unified
    values[field] = value
    config.write_text(yaml.safe_dump(values))
    assert getattr(load_settings(), field) == value


def test_process_environment_overrides_selected_secret_file_then_yaml(unified, monkeypatch):
    home, config, values = unified
    values["txn_base_fee_cap_gwei"] = 8
    config.write_text(yaml.safe_dump(values))
    env = home / "server" / ".env"
    env.parent.mkdir(parents=True)
    env.write_text("TXN_BASE_FEE_CAP_GWEI=7\n")
    assert load_settings().txn_base_fee_cap_gwei == 7
    monkeypatch.setenv("TXN_BASE_FEE_CAP_GWEI", "6")
    assert load_settings().txn_base_fee_cap_gwei == 6


def test_explicit_environment_and_relative_keystore_use_one_config_root(unified, monkeypatch):
    _, config, values = unified
    values["txn_keystore_path"] = "keys/native.json"
    config.write_text(yaml.safe_dump(values))
    env = config.parent / "selected.env"
    env.write_text("RPC_URL=https://selected.invalid\n")
    monkeypatch.setenv("TIDAL_ENV_FILE", str(env))
    settings = load_settings()
    assert settings.rpc_url == "https://selected.invalid"
    assert settings.resolved_env_path == env
    assert settings.resolved_txn_keystore_path == config.parent / "keys" / "native.json"


def test_removed_fee_cap_aliases_cannot_override_native_policy(unified, monkeypatch):
    _, config, values = unified
    values["txn_max_base_fee_gwei"] = 99
    config.write_text(yaml.safe_dump(values))
    monkeypatch.setenv("TXN_MAX_BASE_FEE_GWEI", "88")
    assert load_settings().txn_base_fee_cap_gwei == 5


def test_shared_lock_lives_outside_replaceable_database_directory(unified):
    home, _, _ = unified
    assert default_txn_lock_path() == home / "execution.lock"
