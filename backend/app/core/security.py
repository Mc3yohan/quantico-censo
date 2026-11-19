"""Authentication, password hashing, JWT issuance/revocation, and single-use WS tickets."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.rbac import AuthContext, Role, assert_role_allowed
from app.core.redis_bus import redis_bus
from app.db.models import User
from app.db.session import get_db

AUTH_COOKIE_NAME = "qc_access_token"
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(plain_password: str) -> str:
    """Hash a password using bcrypt (12 rounds)."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain_password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against its bcrypt digest."""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user: User) -> tuple[str, int]:
    """Issue a signed HS256 JWT containing identity, role, scope, and revocation version."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_delta = timedelta(minutes=settings.jwt_expire_minutes)
    exp = now + expires_delta
    payload: dict[str, Any] = {
        "iss": settings.jwt_issuer,
        "sub": str(user.id),
        "username": user.username,
        "role": user.role.value if isinstance(user.role, Role) else str(user.role),
        "region_id": user.region_id,
        "territory_id": user.territory_id,
        "tv": user.token_version,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": secrets.token_hex(12),
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, int(expires_delta.total_seconds())


def decode_access_token(token: str) -> dict[str, Any]:
    """Strictly verify signature, issuer, expiration, and required claims."""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "exp", "iat", "role", "tv"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return payload


def _extract_raw_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str:
    if credentials and credentials.credentials:
        return credentials.credentials
    cookie_token = request.cookies.get(AUTH_COOKIE_NAME)
    if cookie_token:
        return cookie_token
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthContext:
    """
    Validate token signature and re-verify live user, token_version, and tenant kill-switches.
    """
    raw_token = _extract_raw_token(request, credentials)
    payload = decode_access_token(raw_token)

    try:
        user_id = int(payload["sub"])
        token_version = int(payload["tv"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token subject.",
        ) from exc

    stmt = (
        select(User)
        .options(selectinload(User.region), selectinload(User.territory))
        .where(User.id == user_id)
    )
    user = db.execute(stmt).scalar_one_or_none()

    if user is None or user.deleted_at is not None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is inactive or unavailable.",
        )

    if user.token_version != token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been revoked. Please sign in again.",
        )

    if user.region is not None and not user.region.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Assigned region is currently disabled.",
        )

    if user.territory is not None and not user.territory.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Assigned territory is currently disabled.",
        )

    role = user.role if isinstance(user.role, Role) else Role(user.role)
    auth = AuthContext(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
        role=role,
        region_id=user.region_id,
        territory_id=user.territory_id,
        token_version=user.token_version,
    )
    request.state.auth = auth
    return auth


def require_roles(*allowed_roles: Role, is_write: bool = False):
    """Dependency factory enforcing RBAC role membership and read-only guardrails."""
    role_set = frozenset(allowed_roles)

    def _dependency(auth: AuthContext = Depends(get_current_user)) -> AuthContext:
        assert_role_allowed(auth, role_set, is_write=is_write)
        return auth

    return _dependency


def issue_ws_ticket(auth: AuthContext) -> tuple[str, int]:
    """Mint a 30-second single-use WebSocket handshake ticket stored in Redis."""
    ticket = secrets.token_urlsafe(32)
    ttl = 30
    redis_bus.store_ws_ticket(
        ticket,
        {
            "user_id": auth.user_id,
            "username": auth.username,
            "full_name": auth.full_name,
            "role": auth.role.value,
            "region_id": auth.region_id,
            "territory_id": auth.territory_id,
            "token_version": auth.token_version,
        },
        ttl_seconds=ttl,
    )
    return ticket, ttl


def consume_ws_ticket(ticket: str | None) -> AuthContext | None:
    """Atomically redeem a single-use WS ticket. Returns None (fail-closed) on any failure."""
    if not ticket or len(ticket) < 16:
        return None
    data = redis_bus.consume_ws_ticket(ticket)
    if not data:
        return None
    try:
        return AuthContext(
            user_id=int(data["user_id"]),
            username=str(data["username"]),
            full_name=str(data["full_name"]),
            role=Role(data["role"]),
            region_id=data["region_id"],
            territory_id=data["territory_id"],
            token_version=int(data["token_version"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
