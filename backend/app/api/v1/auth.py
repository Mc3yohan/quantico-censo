"""Authentication endpoints: login (with kill-switch checks), logout, profile, and single-use WS tickets."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.limiter import limiter
from app.core.rbac import AuthContext, Role
from app.core.redis_bus import redis_bus
from app.core.security import (
    AUTH_COOKIE_NAME,
    create_access_token,
    decode_access_token,
    get_current_user,
    hash_password,
    issue_ws_ticket,
    verify_password,
)
from app.db.models import User
from app.db.session import get_db
from app.schemas.domain import LoginRequest, TokenResponse, UserProfile, WsTicketResponse

router = APIRouter()
_DUMMY_PASSWORD_HASH = hash_password("quantico-constant-time-dummy-password-2026")


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
def login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    db: Session = Depends(get_db),
) -> TokenResponse:
    """Authenticate an active user and enforce region/territory kill-switches."""
    stmt = (
        select(User)
        .options(selectinload(User.region), selectinload(User.territory))
        .where(User.username == payload.username)
    )
    user = db.execute(stmt).scalar_one_or_none()

    if user is None:
        # Constant-time mitigation against username timing oracle
        verify_password(payload.password, _DUMMY_PASSWORD_HASH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    if not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    if user.deleted_at is not None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is inactive.",
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

    token, expires_in = create_access_token(user)
    request.state.auth = AuthContext(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
        role=user.role,
        region_id=user.region_id,
        territory_id=user.territory_id,
        token_version=user.token_version,
    )
    settings = get_settings()
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
        max_age=expires_in,
        path="/",
    )

    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        user=UserProfile.model_validate(user),
    )


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Revoke current active session token_version (preventing stale-token replay DoS) and clear cookie."""
    settings = get_settings()
    raw_token = request.cookies.get(AUTH_COOKIE_NAME)
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        raw_token = auth_header[7:].strip()

    if raw_token:
        try:
            payload = decode_access_token(raw_token)
            user_id = int(payload["sub"])
            token_version = int(payload["tv"])
            user = db.execute(select(User).where(User.id == user_id).with_for_update()).scalar_one_or_none()
            if (
                user is not None
                and user.deleted_at is None
                and user.is_active
                and user.token_version == token_version
            ):
                user.token_version += 1
                db.commit()
                request.state.auth = AuthContext(
                    user_id=user.id,
                    username=user.username,
                    full_name=user.full_name,
                    role=user.role,
                    region_id=user.region_id,
                    territory_id=user.territory_id,
                    token_version=user.token_version,
                )
                redis_bus.publish_event(f"qc:user:{user.id}", {"event": "SESSION_REVOKED", "user_id": user.id})
        except (HTTPException, KeyError, TypeError, ValueError):
            # Already expired, revoked, or malformed token still clears browser cookie safely
            response.delete_cookie(
                key=AUTH_COOKIE_NAME,
                path="/",
                httponly=True,
                secure=settings.app_env == "production",
                samesite="lax",
            )
            return {"status": "signed_out"}

    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
    )
    return {"status": "signed_out"}


@router.get("/me", response_model=UserProfile)
def get_me(
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserProfile:
    """Return the authenticated operator profile."""
    user = db.get(User, auth.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    return UserProfile.model_validate(user)


@router.post("/ws-ticket", response_model=WsTicketResponse)
def create_ws_ticket(
    auth: AuthContext = Depends(get_current_user),
) -> WsTicketResponse:
    """Mint a 30-second single-use WebSocket handshake ticket (no raw JWTs in WS query strings)."""
    ticket, ttl = issue_ws_ticket(auth)
    channels = (
        ["qc:alerts:global"]
        if auth.role == Role.SUPER_ADMIN
        else ([f"qc:alerts:region:{auth.region_id}"] if auth.region_id is not None else [])
    )
    return WsTicketResponse(ticket=ticket, expires_in=ttl, channels=channels)
