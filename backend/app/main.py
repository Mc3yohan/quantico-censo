"""Quantico Censo — FastAPI Application Entrypoint (CQRS-Ready, Lifespan Managed, Zero-Leak)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text

from app.api.v1 import (
    audits,
    auth,
    dispatch,
    hierarchy,
    media,
    outlets,
    realtime,
    sos,
    tickets,
    triage,
    users,
)
from app.core.config import get_settings
from app.core.limiter import limiter
from app.core.logging_config import configure_logging, get_logger
from app.core.redis_bus import redis_bus
from app.db import session as db_session
from app.db.seed_demo import seed_demo_data
from app.middleware.audit import PureAsgiAuditMiddleware

logger = get_logger(__name__)


def _bootstrap_database() -> None:
    """Initialize schema & synthetic demo data atomically under PostgreSQL advisory lock."""
    settings = get_settings()
    if settings.app_env == "test":
        return

    with db_session.engine.connect() as lock_conn:
        is_pg = lock_conn.dialect.name == "postgresql"
        if is_pg:
            lock_conn.execute(text("SELECT pg_advisory_lock(84291042)"))
            lock_conn.commit()
        try:
            with db_session.engine.begin() as ddl_conn:
                if is_pg:
                    ddl_conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
                db_session.Base.metadata.create_all(bind=ddl_conn)
                if is_pg:
                    ddl_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_territories_boundary_gist ON territories USING GIST (boundary_geom)"))
                    ddl_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_outlets_location_gist ON outlets USING GIST (location_geom)"))
                    ddl_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_field_audits_location_gist ON field_audits USING GIST (location_geom)"))
            if settings.auto_seed_demo and settings.server_role in ("sync", "all"):
                db = db_session.SessionLocal()
                try:
                    summary = seed_demo_data(db)
                    logger.info("demo_seed_checked", **summary)
                finally:
                    db.close()
        finally:
            if is_pg:
                lock_conn.execute(text("SELECT pg_advisory_unlock(84291042)"))
                lock_conn.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.media_root.mkdir(parents=True, exist_ok=True)
    _bootstrap_database()
    logger.info("quantico_censo_started", role=settings.server_role, env=settings.app_env)
    yield
    await redis_bus.close()
    logger.info("quantico_censo_stopped", role=settings.server_role)


def create_app() -> FastAPI:
    """Application factory ensuring clean import without DB side-effects."""
    settings = get_settings()
    app = FastAPI(
        title=f"{settings.app_name} API",
        version=settings.app_version,
        description="Geospatial Retail Census, Offline-First PWA Sync & Multi-Tenant Field Operations API.",
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(PureAsgiAuditMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning("validation_error", method=request.method, path=request.url.path, count=len(exc.errors()))
        return JSONResponse(
            status_code=422,
            content={"detail": "Invalid request payload. Verify required fields and formats."},
        )

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(_request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": f"Rate limit exceeded: {exc.detail}"},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", method=request.method, path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error."},
        )

    # ── API v1 Routers ───────────────────────────────────────────────────────
    app.include_router(auth.router, prefix="/api/v1/auth", tags=["Auth"])
    app.include_router(hierarchy.router, prefix="/api/v1/hierarchy", tags=["Hierarchy & KPIs"])
    app.include_router(outlets.router, prefix="/api/v1/outlets", tags=["Outlets & CSV Import"])
    app.include_router(audits.router, prefix="/api/v1/audits", tags=["Field Audits & Map"])
    app.include_router(triage.router, prefix="/api/v1/triage", tags=["Quarantine & Triage"])
    app.include_router(dispatch.router, prefix="/api/v1/dispatch", tags=["Route Dispatch"])
    app.include_router(sos.router, prefix="/api/v1/sos", tags=["Field Safety SOS"])
    app.include_router(tickets.router, prefix="/api/v1/tickets", tags=["Support Tickets"])
    app.include_router(users.router, prefix="/api/v1/users", tags=["Users & RBAC"])
    app.include_router(media.router, prefix="/api/v1/media", tags=["Authenticated Media"])
    app.include_router(realtime.router, prefix="/api/v1", tags=["Realtime WebSockets"])

    @app.get("/health", tags=["Health"])
    @app.get("/api/v1/health", tags=["Health"])
    def health_check() -> JSONResponse:
        db = db_session.SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            db_ok = True
        except Exception as exc:
            logger.error("health_db_failed", error=str(exc))
            db_ok = False
        finally:
            db.close()

        redis_ok = redis_bus.ping()
        status_code = 200 if db_ok else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ok" if db_ok else "unhealthy",
                "database": "up" if db_ok else "down",
                "redis": "up" if redis_ok else "degraded",
                "server_role": settings.server_role,
                "version": settings.app_version,
            },
        )

    # Serve compiled Vite frontend bundle if present (Docker / full-stack mode)
    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.exists():
        assets_dir = frontend_dist / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")

        @app.get("/", include_in_schema=False)
        def serve_spa() -> FileResponse:
            return FileResponse(str(frontend_dist / "index.html"))

        manifest_path = frontend_dist / "manifest.webmanifest"
        if manifest_path.exists():

            @app.get("/manifest.webmanifest", include_in_schema=False)
            def serve_manifest() -> FileResponse:
                return FileResponse(str(manifest_path), media_type="application/manifest+json")

    return app


app = create_app()
