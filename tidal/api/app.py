"""FastAPI application for the Tidal control plane."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from tidal.api.errors import APIError
from tidal.api.routes.actions import router as actions_router
from tidal.api.routes.alerts import router as alerts_router
from tidal.api.routes.auctions import router as auctions_router
from tidal.api.routes.dashboard import router as dashboard_router
from tidal.api.routes.kick import router as kick_router
from tidal.api.routes.logs import router as logs_router
from tidal.api.services.action_reconcile import run_action_reconciler
from tidal.config import Settings
from tidal.persistence.db import Database
from tidal.security import redact_sensitive_text


def _is_sqlite_locked_error(exc: OperationalError) -> bool:
    original = getattr(exc, "orig", None)
    if isinstance(original, sqlite3.OperationalError) and "database is locked" in str(original).lower():
        return True
    return "database is locked" in str(exc).lower()


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    database = Database(resolved_settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        task = asyncio.create_task(run_action_reconciler(database, resolved_settings)) if resolved_settings.rpc_url else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            database.engine.dispose()

    app = FastAPI(title="Tidal Control Plane", version="1.0.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.database = database

    if resolved_settings.tidal_api_cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved_settings.tidal_api_cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(APIError)
    async def handle_api_error(_request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"status": "error", "warnings": [], "data": None, "detail": redact_sensitive_text(exc.message)},
        )

    @app.exception_handler(OperationalError)
    async def handle_operational_error(_request: Request, exc: OperationalError) -> JSONResponse:
        if _is_sqlite_locked_error(exc):
            detail = "database is locked; retry the request"
            status_code = 503
        else:
            detail = "database operation failed"
            status_code = 500
        return JSONResponse(
            status_code=status_code,
            content={"status": "error", "warnings": [], "data": None, "detail": detail},
        )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "warnings": [], "data": {"ready": True}}

    prefix = "/api/v1/tidal"
    app.include_router(dashboard_router, prefix=prefix, tags=["dashboard"])
    app.include_router(alerts_router, prefix=prefix, tags=["alerts"])
    app.include_router(logs_router, prefix=prefix, tags=["logs"])
    app.include_router(kick_router, prefix=prefix, tags=["kick"])
    app.include_router(auctions_router, prefix=prefix, tags=["auctions"])
    app.include_router(actions_router, prefix=prefix, tags=["actions"])
    return app


app = create_app(Settings())
