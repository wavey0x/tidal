"""Database engine/session helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

_SQLITE_BUSY_TIMEOUT_MS = 30_000


class Database:
    """Small wrapper around SQLAlchemy engine and session factory."""

    def __init__(self, database_url: str, *, create: bool = False, read_only: bool = False):
        """Open existing state by default, including on pool reconnects.

        Only explicit initialization and isolated fixtures may request creation.
        SQLite URI modes enforce this at open time, avoiding a check/open race.
        """
        url = make_url(database_url)
        options = {}
        if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
            if create and read_only:
                raise ValueError("read-only database cannot be created")
            mode = "ro" if read_only else "rwc" if create else "rw"
            uri = Path(url.database).expanduser().resolve().as_uri() + f"?mode={mode}"
            options["creator"] = lambda: sqlite3.connect(
                uri, uri=True, check_same_thread=False, timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000,
            )
        self.engine = create_engine(database_url, future=True, **options)
        if self.engine.dialect.name == "sqlite":
            event.listen(
                self.engine, "connect",
                self._configure_read_only_connection if read_only else self._configure_sqlite_connection,
            )
        self._session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            autoflush=False,
            future=True,
            class_=Session,
        )

    def session(self) -> Session:
        return self._session_factory()

    @staticmethod
    def _configure_read_only_connection(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        del connection_record
        dbapi_connection.execute("PRAGMA query_only=ON")
        dbapi_connection.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")

    @staticmethod
    def _configure_sqlite_connection(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        del connection_record
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return

        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA synchronous=FULL")
        finally:
            cursor.close()
