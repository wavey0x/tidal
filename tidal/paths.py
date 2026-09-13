"""Shared filesystem path helpers for CLI and server runtime state."""

from __future__ import annotations

import os
from pathlib import Path

_APP_HOME_DIRNAME = ".tidal"
_CLI_DIRNAME = "cli"
_SERVER_DIRNAME = "server"
_CONFIG_FILENAME = "config.yaml"
_SERVER_CONFIG_DIRNAME = "config"
_SERVER_CONFIG_FILENAME = "server.yaml"
_ENV_FILENAME = ".env"
_DB_FILENAME = "tidal.db"
_ACTION_OUTBOX_FILENAME = "action_outbox.db"
_TXN_LOCK_FILENAME = "txn_daemon.lock"


def resolve_path(path: str | Path) -> Path:
    """Expand and absolutize a user-provided path."""
    return Path(path).expanduser().resolve()


def tidal_home() -> Path:
    override = os.getenv("TIDAL_HOME")
    if override:
        return resolve_path(override)
    return (Path.home() / _APP_HOME_DIRNAME).resolve()


def default_config_path() -> Path:
    return default_cli_dir() / _CONFIG_FILENAME


def default_env_path() -> Path:
    return default_cli_dir() / _ENV_FILENAME


def default_cli_dir() -> Path:
    return tidal_home() / _CLI_DIRNAME


def default_server_data_dir() -> Path:
    return tidal_home() / _SERVER_DIRNAME


def default_server_env_path() -> Path:
    return default_server_data_dir() / _ENV_FILENAME


def find_project_root(start: str | Path | None = None) -> Path | None:
    current = resolve_path(start or Path.cwd())
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def default_server_config_path(start: str | Path | None = None) -> Path | None:
    project_root = find_project_root(start)
    if project_root is None:
        return None
    return project_root / _SERVER_CONFIG_DIRNAME / _SERVER_CONFIG_FILENAME


def default_db_path() -> Path:
    return default_server_data_dir() / _DB_FILENAME


def default_action_outbox_path() -> Path:
    return default_server_data_dir() / _ACTION_OUTBOX_FILENAME


def default_txn_lock_path() -> Path:
    return tidal_home() / "execution.lock"


def default_activation_path() -> Path:
    """Local authorization state; never an input to database backup/restore."""
    return tidal_home() / "activation.json"
