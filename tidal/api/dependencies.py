"""FastAPI dependency helpers."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from tidal.api.auth import OperatorIdentity, authenticate_operator, security
from tidal.config import Settings
from tidal.persistence.db import Database


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_session(database: Database = Depends(get_database)) -> Iterator[Session]:
    with database.session() as session:
        yield session


def get_operator(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    session: Session = Depends(get_session),
) -> OperatorIdentity:
    return authenticate_operator(credentials, session)
