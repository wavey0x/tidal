"""Runtime configuration loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from `.env` and optional config file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    rpc_url: str | None = Field(default=None, alias="RPC_URL")
    db_path: Path = Field(default=Path("./tidal.db"), alias="DB_PATH")
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
    auction_cache_path: Path | None = Field(default=None, alias="AUCTION_CACHE_PATH")
    price_refresh_enabled: bool = Field(default=True, alias="PRICE_REFRESH_ENABLED")
    curve_api_base_url: str = Field(
        default="https://prices.curve.finance",
        alias="CURVE_API_BASE_URL",
    )
    price_timeout_seconds: int = Field(default=10, alias="PRICE_TIMEOUT_SECONDS")
    price_retry_attempts: int = Field(default=3, alias="PRICE_RETRY_ATTEMPTS")
    price_concurrency: int = Field(default=8, alias="PRICE_CONCURRENCY")

    telegram_alerts_enabled: bool = Field(default=False, alias="TELEGRAM_ALERTS_ENABLED")
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")

    @property
    def resolved_db_path(self) -> Path:
        db_path = self.db_path
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        return db_path

    @property
    def resolved_auction_cache_path(self) -> Path:
        configured_path = self.auction_cache_path
        if configured_path is not None:
            if configured_path.is_absolute():
                return configured_path
            return Path.cwd() / configured_path
        return self.resolved_db_path.with_name("strategy_auction_map.json")

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.resolved_db_path}"


def _load_yaml_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Config file must contain a mapping object: {path}")
    return raw


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from env, optionally overriding with a YAML config file."""

    if config_path is None:
        return Settings()

    config_data = _load_yaml_config(config_path)
    return Settings(**config_data)
