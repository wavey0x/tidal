"""Runtime configuration loading.

Precedence (highest wins): env vars > YAML config > Python defaults.

Client commands load ``~/.tidal/config.yaml`` by default.
Server commands load ``config/server.yaml`` by default.
Secrets live in an explicitly resolved ``.env`` file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import AliasChoices, BaseModel, Field, PrivateAttr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tidal.paths import default_config_path, default_env_path, default_server_config_path, resolve_path, tidal_home
from tidal.transaction_service.kick_policy import KickConfig, build_kick_config


class MonitoredFeeBurner(BaseModel):
    """Static fee burner registration from server config."""

    address: str
    want_address: str
    label: str | None = None


class Settings(BaseSettings):
    """Application settings.

    Env vars (including ``.env`` via dotenv) take highest priority,
    then YAML config values, then the defaults declared here.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    _resolved_home_path: Path = PrivateAttr(default_factory=tidal_home)
    _resolved_config_path: Path = PrivateAttr(default_factory=default_config_path)
    _resolved_env_path: Path = PrivateAttr(default_factory=default_env_path)
    _kick_config: KickConfig | None = PrivateAttr(default=None)

    rpc_url: str | None = Field(default=None, alias="RPC_URL")
    db_path: Path | None = Field(default=None, alias="DB_PATH")
    chain_id: int = Field(default=1, alias="CHAIN_ID")

    scan_interval_seconds: int = Field(default=300, alias="SCAN_INTERVAL_SECONDS")
    scan_concurrency: int = Field(default=20, alias="SCAN_CONCURRENCY")
    scan_auto_settle_enabled: bool = Field(default=False, alias="SCAN_AUTO_SETTLE_ENABLED")
    rpc_timeout_seconds: int = Field(default=10, alias="RPC_TIMEOUT_SECONDS")
    rpc_retry_attempts: int = Field(default=3, alias="RPC_RETRY_ATTEMPTS")
    multicall_enabled: bool = Field(default=True, alias="MULTICALL_ENABLED")
    multicall_address: str = Field(
        default="0xca11bde05977b3631167028862be2a173976ca11",
        alias="MULTICALL_ADDRESS",
    )
    multicall_discovery_batch_calls: int = Field(
        default=800,
        alias="MULTICALL_DISCOVERY_BATCH_CALLS",
    )
    multicall_rewards_batch_calls: int = Field(
        default=500,
        alias="MULTICALL_REWARDS_BATCH_CALLS",
    )
    multicall_rewards_index_max: int = Field(
        default=16,
        alias="MULTICALL_REWARDS_INDEX_MAX",
    )
    multicall_balance_batch_calls: int = Field(
        default=1000,
        alias="MULTICALL_BALANCE_BATCH_CALLS",
    )
    multicall_overflow_queue_max: int = Field(
        default=32,
        alias="MULTICALL_OVERFLOW_QUEUE_MAX",
    )
    multicall_auction_batch_calls: int = Field(
        default=500,
        alias="MULTICALL_AUCTION_BATCH_CALLS",
    )
    auction_factory_address: str = Field(
        default="0xe87af17acba165686e5aa7de2cec523864c25712",
        alias="AUCTION_FACTORY_ADDRESS",
    )
    price_refresh_enabled: bool = Field(default=True, alias="PRICE_REFRESH_ENABLED")
    token_price_agg_base_url: str = Field(
        default="https://prices.wavey.info",
        alias="TOKEN_PRICE_AGG_BASE_URL",
        validation_alias=AliasChoices("TOKEN_PRICE_AGG_BASE_URL", "CURVE_API_BASE_URL"),
    )
    token_price_agg_key: str | None = Field(default=None, alias="TOKEN_PRICE_AGG_KEY")
    price_timeout_seconds: int = Field(default=10, alias="PRICE_TIMEOUT_SECONDS")
    price_retry_attempts: int = Field(default=3, alias="PRICE_RETRY_ATTEMPTS")
    price_concurrency: int = Field(default=10, alias="PRICE_CONCURRENCY")
    price_delay_seconds: float = Field(default=0, alias="PRICE_DELAY_SECONDS")
    auctionscan_base_url: str = Field(default="https://auctionscan.info", alias="AUCTIONSCAN_BASE_URL")
    auctionscan_api_base_url: str = Field(
        default="https://auctionscan.info/api",
        alias="AUCTIONSCAN_API_BASE_URL",
    )
    auctionscan_recheck_seconds: int = Field(default=90, alias="AUCTIONSCAN_RECHECK_SECONDS")

    auction_kicker_address: str = Field(
        default="0x2a76c6ad151af2edbe16755fc3bff67176f01071",
        alias="AUCTION_KICKER_ADDRESS",
    )
    txn_usd_threshold: float = Field(default=100.0, alias="TXN_USD_THRESHOLD")
    txn_max_base_fee_gwei: float = Field(default=0.5, alias="TXN_MAX_BASE_FEE_GWEI")
    txn_max_priority_fee_gwei: int = Field(default=2, alias="TXN_MAX_PRIORITY_FEE_GWEI")
    txn_max_gas_limit: int = Field(default=500000, alias="TXN_MAX_GAS_LIMIT")
    txn_start_price_buffer_bps: int = Field(default=1000, alias="TXN_START_PRICE_BUFFER_BPS")
    txn_min_price_buffer_bps: int = Field(default=500, alias="TXN_MIN_PRICE_BUFFER_BPS")
    txn_quote_spot_warning_threshold_pct: float = Field(
        default=2.0,
        alias="TXN_QUOTE_SPOT_WARNING_THRESHOLD_PCT",
    )
    txn_max_data_age_seconds: int = Field(default=600, alias="TXN_MAX_DATA_AGE_SECONDS")
    prepared_action_max_age_seconds: int = Field(default=300, alias="PREPARED_ACTION_MAX_AGE_SECONDS")
    txn_keystore_path: str | None = Field(default=None, alias="TXN_KEYSTORE_PATH")
    txn_keystore_passphrase: str | None = Field(default=None, alias="TXN_KEYSTORE_PASSPHRASE")

    txn_require_curve_quote: bool = Field(default=True, alias="TXN_REQUIRE_CURVE_QUOTE")

    max_batch_kick_size: int = Field(default=5, alias="MAX_BATCH_KICK_SIZE")
    batch_kick_delay_seconds: float = Field(default=5, alias="BATCH_KICK_DELAY_SECONDS")
    monitored_fee_burners: list[MonitoredFeeBurner] = Field(
        default_factory=list,
        alias="MONITORED_FEE_BURNERS",
    )
    tidal_api_base_url: str | None = Field(default="https://api.tidal.wavey.info", alias="TIDAL_API_BASE_URL")
    tidal_api_key: str | None = Field(default=None, alias="TIDAL_API_KEY")
    tidal_api_host: str = Field(default="0.0.0.0", alias="TIDAL_API_HOST")
    tidal_api_port: int = Field(default=8787, alias="TIDAL_API_PORT")
    tidal_api_request_timeout_seconds: int = Field(default=30, alias="TIDAL_API_REQUEST_TIMEOUT_SECONDS")
    tidal_api_receipt_reconcile_interval_seconds: int = Field(
        default=30,
        alias="TIDAL_API_RECEIPT_RECONCILE_INTERVAL_SECONDS",
    )
    tidal_api_receipt_reconcile_threshold_seconds: int = Field(
        default=60,
        alias="TIDAL_API_RECEIPT_RECONCILE_THRESHOLD_SECONDS",
    )
    tidal_api_cors_allowed_origins: list[str] = Field(
        default_factory=list,
        alias="TIDAL_API_CORS_ALLOWED_ORIGINS",
    )

    @field_validator("tidal_api_cors_allowed_origins", mode="before")
    @classmethod
    def _coerce_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return value
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @property
    def resolved_config_path(self) -> Path:
        return self._resolved_config_path

    @property
    def resolved_home_path(self) -> Path:
        return self._resolved_home_path

    @property
    def resolved_config_dir(self) -> Path:
        return self.resolved_config_path.parent

    @property
    def resolved_env_path(self) -> Path:
        return self._resolved_env_path

    @property
    def resolved_db_path(self) -> Path:
        if self.db_path is None:
            return (self.resolved_home_path / "state" / "tidal.db").resolve()
        return self._resolve_config_relative_path(self.db_path)

    @property
    def resolved_txn_keystore_path(self) -> Path | None:
        if not self.txn_keystore_path:
            return None
        return self._resolve_config_relative_path(self.txn_keystore_path)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.resolved_db_path}"

    @property
    def kick_config(self) -> KickConfig:
        if self._kick_config is None:
            raise RuntimeError("server kick policy is not loaded for these settings")
        return self._kick_config

    def bind_runtime_paths(
        self,
        *,
        home_path: Path,
        config_path: Path,
        env_path: Path,
    ) -> None:
        self._resolved_home_path = home_path
        self._resolved_config_path = config_path
        self._resolved_env_path = env_path

    def bind_kick_config(self, kick_config: KickConfig | None) -> None:
        self._kick_config = kick_config

    def _resolve_config_relative_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if path.is_absolute():
            return path.resolve()
        return (self.resolved_config_dir / path).resolve()


def _load_yaml_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Config file must contain a mapping object: {path}")
    return raw


def _resolve_explicit_file_path(path: str | Path, *, label: str) -> Path:
    resolved = resolve_path(path)
    if not resolved.is_file():
        raise FileNotFoundError(f"{label} not found: {resolved}")
    return resolved


def _resolve_explicit_or_env_config_path(config_path: Path | None = None) -> Path | None:
    if config_path is not None:
        return _resolve_explicit_file_path(config_path, label="Config file")

    env_override = os.getenv("TIDAL_CONFIG")
    if env_override:
        return _resolve_explicit_file_path(env_override, label="Config file")

    return None


def _resolve_server_config_path(config_path: Path | None = None) -> Path:
    explicit = _resolve_explicit_or_env_config_path(config_path)
    if explicit is not None:
        return explicit

    default_path = default_server_config_path()
    if default_path is not None and default_path.is_file():
        return default_path

    hint = default_path if default_path is not None else Path("config/server.yaml")
    raise FileNotFoundError(f"Server config file not found. Pass --config or create {hint}.")


def _resolve_env_path(
    config_path: Path,
    *,
    mode: Literal["client", "server"],
    use_home_fallback: bool,
) -> Path:
    env_override = os.getenv("TIDAL_ENV_FILE")
    if env_override:
        return _resolve_explicit_file_path(env_override, label="Environment file")

    config_dir_env_path = (config_path.parent / ".env").resolve()
    if config_dir_env_path.is_file():
        return config_dir_env_path
    if mode == "server" or not use_home_fallback:
        return config_dir_env_path

    return default_env_path()


def load_settings(
    config_path: Path | None = None,
    *,
    mode: Literal["client", "server"] = "client",
) -> Settings:
    """Load client or server settings from resolved config and env paths."""
    if mode == "server":
        resolved_config_path = _resolve_server_config_path(config_path)
        use_home_env_fallback = False
    else:
        explicit = _resolve_explicit_or_env_config_path(config_path)
        if explicit is not None:
            resolved_config_path = explicit
            use_home_env_fallback = False
        else:
            resolved_config_path = default_config_path()
            use_home_env_fallback = True
    resolved_env_path = _resolve_env_path(
        resolved_config_path,
        mode=mode,
        use_home_fallback=use_home_env_fallback,
    )

    config_data: dict[str, Any] = {}
    if resolved_config_path.is_file():
        config_data = _load_yaml_config(resolved_config_path)
    kick_raw = config_data.pop("kick", None)
    if mode == "server" and not isinstance(kick_raw, dict):
        raise ValueError(f"Server config must define a 'kick' mapping: {resolved_config_path}")

    env_data: dict[str, Any] = {}
    if resolved_env_path.is_file():
        env_data = {
            key: value
            for key, value in dotenv_values(resolved_env_path).items()
            if value is not None
        }

    settings = Settings(**{**config_data, **env_data})
    settings.bind_runtime_paths(
        home_path=tidal_home(),
        config_path=resolved_config_path,
        env_path=resolved_env_path,
    )
    settings.bind_kick_config(build_kick_config(kick_raw) if mode == "server" else None)
    return settings


def load_client_settings(config_path: Path | None = None) -> Settings:
    return load_settings(config_path, mode="client")


def load_server_settings(config_path: Path | None = None) -> Settings:
    return load_settings(config_path, mode="server")
