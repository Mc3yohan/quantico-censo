"""Isolated in-memory pytest fixtures for Quantico Censo (zero external DB/Redis required)."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

# Set safe isolated test environment BEFORE importing app modules
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["REDIS_URL"] = ""
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["AUTO_SEED_DEMO"] = "false"

from app.core.config import get_settings  # noqa: E402
from app.core.redis_bus import redis_bus  # noqa: E402
from app.db import session as db_session  # noqa: E402
from app.db.seed_demo import seed_demo_data  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def isolated_env(tmp_path: Path) -> Generator[tuple[TestClient, sessionmaker[Session]], None, None]:
    """Provide a fresh seeded in-memory SQLite DB, temp media root, and FastAPI TestClient."""
    get_settings.cache_clear()
    os.environ["MEDIA_ROOT"] = str(tmp_path / "media")
    settings = get_settings()
    settings.media_root.mkdir(parents=True, exist_ok=True)

    # Clear in-memory cache/tickets between tests
    redis_bus.invalidate_prefix("qc:")

    eng = db_session.build_engine("sqlite:///:memory:")
    db_session.Base.metadata.create_all(bind=eng)
    testing_session_local = sessionmaker(bind=eng, autoflush=False, autocommit=False, expire_on_commit=False)

    original_engine = db_session.engine
    original_session_local = db_session.SessionLocal
    db_session.engine = eng
    db_session.SessionLocal = testing_session_local

    seed_db = testing_session_local()
    try:
        seed_demo_data(seed_db)
    finally:
        seed_db.close()

    app = create_app()

    def _override_get_db() -> Generator[Session, None, None]:
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[db_session.get_db] = _override_get_db

    with TestClient(app) as client:
        yield client, testing_session_local

    app.dependency_overrides.clear()
    db_session.engine = original_engine
    db_session.SessionLocal = original_session_local
    eng.dispose()
    get_settings.cache_clear()


@pytest.fixture()
def client(isolated_env: tuple[TestClient, sessionmaker[Session]]) -> TestClient:
    return isolated_env[0]


@pytest.fixture()
def db_factory(isolated_env: tuple[TestClient, sessionmaker[Session]]) -> sessionmaker[Session]:
    return isolated_env[1]


@pytest.fixture()
def auth_headers(client: TestClient):
    """Helper factory returning Bearer headers for any seeded demo username."""

    def _login(username: str, password: str = "QuanticoDemo2026!") -> dict[str, str]:
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        assert resp.status_code == 200, f"Login failed for {username}: {resp.text}"
        token = resp.json()["access_token"]
        client.cookies.clear()
        return {"Authorization": f"Bearer {token}"}

    return _login
