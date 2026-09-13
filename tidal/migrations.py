"""Alembic migration helpers."""

from __future__ import annotations

from alembic import command
from alembic.config import Config

from tidal.resources import migration_resource_paths


def run_migrations(database_url: str, revision: str = "head", *, allow_action_retirement: bool = False) -> None:
    with migration_resource_paths() as (alembic_ini, script_location):
        config = Config(str(alembic_ini))
        config.set_main_option("script_location", str(script_location))
        config.set_main_option("sqlalchemy.url", database_url)
        config.attributes["allow_action_retirement"] = allow_action_retirement
        command.upgrade(config, revision)
