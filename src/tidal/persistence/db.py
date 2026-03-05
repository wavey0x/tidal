"""Database engine/session helpers."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


class Database:
    """Small wrapper around SQLAlchemy engine and session factory."""

    def __init__(self, database_url: str):
        self.engine = create_engine(database_url, future=True)
        self._session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            autoflush=False,
            future=True,
            class_=Session,
        )

    def session(self) -> Session:
        return self._session_factory()
