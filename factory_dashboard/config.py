"""Runtime configuration loading.

Precedence (highest wins): env vars > YAML config > Python defaults.

Secrets (RPC_URL, keystore, Telegram tokens, etc.) live in ``.env`` and are
promoted to real environment variables by ``load_dotenv()`` before the
Settings model is constructed.  Operational knobs live in ``config.yaml``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings.

    Env vars (including ``.env`` via dotenv) take highest priority,
    then YAML config values, then the defaults declared here.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    rpc_url: str | None = Field(default=None, alias="RPC_URL")
    db_path: Path = Field(default=Path("./factory_dashboard.db"), alias="DB_PATH")
    chain_id: int = Field(default=1, alias="CHAIN_ID")

    scan_interval_seconds: int = Field(default=300, alias="SCAN_INTERVAL_SECONDS")
    scan_concurrency: int = Field(default=20, alias="SCAN_CONCURRENCY")
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

    telegram_alerts_enabled: bool = Field(default=False, alias="TELEGRAM_ALERTS_ENABLED")
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")

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
    txn_max_data_age_seconds: int = Field(default=600, alias="TXN_MAX_DATA_AGE_SECONDS")
    txn_keystore_path: str | None = Field(default=None, alias="TXN_KEYSTORE_PATH")
    txn_keystore_passphrase: str | None = Field(default=None, alias="TXN_KEYSTORE_PASSPHRASE")

    txn_cooldown_seconds: int = Field(default=3600, alias="TXN_COOLDOWN_SECONDS")

    @property
    def resolved_db_path(self) -> Path:
        db_path = self.db_path
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        return db_path

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.resolved_db_path}"


def _load_yaml_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Config file must contain a mapping object: {path}")
    return raw


_DEFAULT_CONFIG_PATH = Path("config.yaml")


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from YAML config with env-var overrides for secrets.

    1. ``load_dotenv()`` promotes ``.env`` secrets to real env vars.
    2. YAML values are passed as init kwargs (lower priority than env vars).
    3. Env vars always win — so secrets in ``.env`` override any YAML key.

    When no explicit path is given, falls back to ``config.yaml`` in the
    current working directory if it exists.
    """
    load_dotenv()

    if config_path is None:
        candidate = Path.cwd() / _DEFAULT_CONFIG_PATH
        if candidate.is_file():
            config_path = candidate

    if config_path is None:
        return Settings()

    config_data = _load_yaml_config(config_path)
    return Settings(**config_data)
