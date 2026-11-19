"""SQLAlchemy 2.x engine and session management supporting PostGIS and SQLite tests."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all Quantico Censo ORM models."""


def build_engine(database_url: str | None = None) -> Engine:
    """Create a properly sized SQLAlchemy engine without import-time schema side-effects."""
    url = database_url or get_settings().database_url
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        kwargs: dict[str, object] = {"connect_args": connect_args, "future": True}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
        eng = create_engine(url, **kwargs)

        @event.listens_for(eng, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return eng

    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=12,
        max_overflow=18,
        pool_recycle=1800,
        future=True,
    )


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a clean SQLAlchemy session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
