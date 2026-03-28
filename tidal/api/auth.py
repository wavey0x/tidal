"""Bearer-token auth for operator endpoints."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from tidal.persistence import models

security = HTTPBearer(auto_error=False)


@dataclass(slots=True)
class OperatorIdentity:
    operator_id: str


def authenticate_operator(
    credentials: HTTPAuthorizationCredentials | None,
    session: Session,
) -> OperatorIdentity:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bearer token required")

    rows = session.execute(
        select(models.api_keys.c.label, models.api_keys.c.key_hash).where(
            models.api_keys.c.revoked_at.is_(None)
        )
    ).all()

    if not rows:
        raise HTTPException(status_code=503, detail="No API keys configured")

    incoming_hash = hashlib.sha256(credentials.credentials.encode()).hexdigest()
    for label, key_hash in rows:
        if secrets.compare_digest(incoming_hash, key_hash):
            return OperatorIdentity(operator_id=label)

    raise HTTPException(status_code=401, detail="Invalid bearer token")
