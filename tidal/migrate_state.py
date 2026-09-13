"""Explicit held transition; preserve original inputs before retiring old jobs."""
from contextlib import closing
from pathlib import Path
import sqlite3
from urllib.parse import quote

from tidal.legacy_import import import_legacy, read_source
from tidal.lifecycle import SCHEMA_REVISION, LifecycleError, clear_activation, execution_lock, inspect_database, result
from tidal.migrations import run_migrations
from tidal.persistence.db import Database


def migrate_state(settings, *, source_database: Path | None = None, outbox: Path | None = None) -> dict:
    target = settings.resolved_db_path
    if not target.is_file():
        raise LifecycleError("MISSING_DATABASE", "Database is missing; use db init or restore explicitly.")
    if (source_database is None) != (outbox is None):
        raise LifecycleError("LEGACY_SOURCES_REQUIRED", "Supply both protected original --source-database and --outbox.")
    if source_database is not None:
        sources = [source_database.expanduser().resolve(), outbox.expanduser().resolve()]
        if any(not path.is_file() for path in sources):
            raise LifecycleError("INVALID_LEGACY_SOURCE", "Both protected original files must exist.")
        if any(path.samefile(target) for path in sources) or sources[0].samefile(sources[1]):
            raise LifecycleError("INVALID_LEGACY_SOURCE", "Original DB, outbox and migration target must be separate files.")
        # Validate before changing schema; malformed or missing sources cannot
        # leave the target half migrated. No network or secret input is needed.
        read_source(sources[0], "api_actions")
        read_source(sources[0], "api_action_transactions")
        read_source(sources[1], "action_report_outbox")
    with execution_lock(settings.resolved_home_path / "execution.lock"):
        clear_activation(settings.resolved_home_path / "activation.json")
        uri = f"file:{quote(str(target.resolve()), safe='/')}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            try:
                revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            except (sqlite3.Error, TypeError) as exc:
                raise LifecycleError("INVALID_DATABASE", "Database has no recognized migration revision.") from exc
        if revision != SCHEMA_REVISION and source_database is None:
            raise LifecycleError("LEGACY_SOURCES_REQUIRED", "Preserve the coherent original DB/outbox pair, then pass --source-database and --outbox.")
        imported = {}
        if revision != SCHEMA_REVISION:
            run_migrations(settings.database_url, "0030_recovery_notifications")
        if source_database is not None:
            database = Database(settings.database_url)
            try:
                with database.session() as session:
                    imported = import_legacy(settings=settings, session=session,
                        source_database=sources[0], outbox=sources[1])
            finally:
                database.engine.dispose()
        run_migrations(settings.database_url, allow_action_retirement=True)
        return result("MIGRATED_AND_HELD", data={**inspect_database(target), **imported, "activated": False})
