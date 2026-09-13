"""Read-only views of the application's single transaction ledger."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tidal.api.auth import OperatorIdentity
from tidal.api.dependencies import get_operator, get_session
from tidal.api.errors import APIError
from tidal.persistence import models
from tidal.security import redact_sensitive_data

router = APIRouter()


@router.get("/transactions")
def get_transactions(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = Query(default=None),
    profile: str | None = Query(default=None),
    session: Session = Depends(get_session),
    _operator: OperatorIdentity = Depends(get_operator),
) -> dict[str, object]:
    query = select(models.transactions).where(models.transactions.c.tx_hash.is_not(None))
    if status is not None:
        query = query.where(models.transactions.c.status == status)
    if profile is not None:
        query = query.where(models.transactions.c.profile == profile)
    total = session.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    rows = [dict(row) for row in session.execute(
        query.order_by(models.transactions.c.id.desc()).offset(offset).limit(limit)
    ).mappings()]
    return {"status": "ok" if rows else "noop", "warnings": [],
            "data": redact_sensitive_data({"items": rows, "total": total})}


@router.get("/transactions/{transaction_id}")
def get_transaction(
    transaction_id: int,
    session: Session = Depends(get_session),
    _operator: OperatorIdentity = Depends(get_operator),
) -> dict[str, object]:
    row = session.execute(select(models.transactions).where(
        models.transactions.c.id == transaction_id,
    )).mappings().first()
    if row is None:
        raise APIError("Transaction not found", status_code=404)
    operations = [dict(item) for item in session.execute(select(models.kick_txs).where(
        models.kick_txs.c.transaction_id == transaction_id,
    )).mappings()]
    return {"status": "ok", "warnings": [],
            "data": redact_sensitive_data({**dict(row), "operations": operations})}
