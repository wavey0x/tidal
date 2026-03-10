"""Database engine/session helpers."""

from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker


class Database:
    """Small wrapper around SQLAlchemy engine and session factory."""

    def __init__(self, database_url: str):
        self.engine = create_engine(database_url, future=True)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", self._configure_sqlite_connection)
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
    def _configure_sqlite_connection(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        del connection_record
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return

        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()
