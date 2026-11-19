"""RBAC-hardened User CRUD: strict hierarchy, pre-mutation scope checks, session revocation & soft-delete."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging_config import get_logger
from app.core.rbac import (
    AuthContext,
    Role,
    apply_scope_filter,
    assert_can_manage_target_user,
)
from app.core.redis_bus import redis_bus
from app.core.security import hash_password, require_roles
from app.db.models import Region, Territory, User
from app.db.session import get_db
from app.schemas.domain import UserCreateRequest, UserProfile, UserUpdateRequest

router = APIRouter()
logger = get_logger(__name__)


def _validate_scope_consistency(
    db: Session,
    *,
    role: Role,
    region_id: int | None,
    territory_id: int | None,
) -> None:
    if role == Role.SUPER_ADMIN:
        return
    if region_id is None:
        raise HTTPException(status_code=400, detail="Non-admin roles require a valid region_id.")
    region = db.get(Region, region_id)
    if region is None:
        raise HTTPException(status_code=404, detail="Target region not found.")

    if role in (Role.SUPERVISOR, Role.FIELD_AGENT):
        if territory_id is None:
            raise HTTPException(status_code=400, detail="Supervisors and field agents require a territory_id.")
        territory = db.get(Territory, territory_id)
        if territory is None or territory.region_id != region_id:
            raise HTTPException(status_code=400, detail="Territory does not belong to the specified region.")
    elif territory_id is not None:
        territory = db.get(Territory, territory_id)
        if territory is None or territory.region_id != region_id:
            raise HTTPException(status_code=400, detail="Territory does not belong to the specified region.")


@router.get("", response_model=list[UserProfile])
def list_users(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR)),
    db: Session = Depends(get_db),
) -> list[UserProfile]:
    """List non-deleted users within the caller's authorized territorial scope."""
    stmt = apply_scope_filter(select(User).where(User.deleted_at.is_(None)), User, auth)
    rows = db.execute(stmt.order_by(User.id.asc()).offset(offset).limit(limit)).scalars().all()
    return [UserProfile.model_validate(u) for u in rows]


@router.post("", response_model=UserProfile, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateRequest,
    auth: AuthContext = Depends(
        require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR, is_write=True)
    ),
    db: Session = Depends(get_db),
) -> UserProfile:
    """Create a user strictly below the actor's role level and inside the actor's scope."""
    assert_can_manage_target_user(
        auth,
        target_role=payload.role,
        target_region_id=payload.region_id,
        target_territory_id=payload.territory_id,
    )
    _validate_scope_consistency(
        db,
        role=payload.role,
        region_id=payload.region_id,
        territory_id=payload.territory_id,
    )

    exists = db.execute(
        select(User.id)
        .where((User.username == payload.username) | (User.email == payload.email))
        .limit(1)
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=409, detail="Username or email already exists.")

    user = User(
        username=payload.username,
        full_name=payload.full_name,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role=payload.role,
        region_id=payload.region_id,
        territory_id=payload.territory_id,
        is_active=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username or email already exists.") from exc
    db.refresh(user)
    logger.info("user_created", actor=auth.username, target_user_id=user.id, role=user.role.value)
    return UserProfile.model_validate(user)


@router.put("/{user_id}", response_model=UserProfile)
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    auth: AuthContext = Depends(
        require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR, is_write=True)
    ),
    db: Session = Depends(get_db),
) -> UserProfile:
    """
    Update user metadata or credentials.
    Validates actor permissions against BOTH existing target state AND proposed new state BEFORE mutation.
    Never logs plaintext passwords; bumps token_version on security-sensitive changes.
    """
    target = db.get(User, user_id)
    if target is None or target.deleted_at is not None:
        raise HTTPException(status_code=404, detail="User not found.")

    # 1) Check authorization on EXISTING user state FIRST (prevents order-of-operations hijack)
    assert_can_manage_target_user(
        auth,
        target_role=target.role,
        target_region_id=target.region_id,
        target_territory_id=target.territory_id,
    )

    proposed_role = payload.role if payload.role is not None else target.role
    proposed_region_id = payload.region_id if "region_id" in payload.model_fields_set else target.region_id
    proposed_territory_id = (
        payload.territory_id if "territory_id" in payload.model_fields_set else target.territory_id
    )

    # 2) Check authorization on PROPOSED target state BEFORE applying any change
    assert_can_manage_target_user(
        auth,
        target_role=proposed_role,
        target_region_id=proposed_region_id,
        target_territory_id=proposed_territory_id,
    )
    _validate_scope_consistency(
        db,
        role=proposed_role,
        region_id=proposed_region_id,
        territory_id=proposed_territory_id,
    )

    revoke_sessions = False
    if payload.full_name is not None:
        target.full_name = payload.full_name
    if payload.email is not None and payload.email != target.email:
        email_taken = db.execute(
            select(User.id).where(User.email == payload.email, User.id != target.id).limit(1)
        ).scalar_one_or_none()
        if email_taken is not None:
            raise HTTPException(status_code=409, detail="Email is already in use.")
        target.email = payload.email
    if payload.password is not None:
        target.hashed_password = hash_password(payload.password)
        revoke_sessions = True
    if payload.role is not None and payload.role != target.role:
        target.role = payload.role
        revoke_sessions = True
    if proposed_region_id != target.region_id:
        target.region_id = proposed_region_id
        revoke_sessions = True
    if proposed_territory_id != target.territory_id:
        target.territory_id = proposed_territory_id
        revoke_sessions = True
    if payload.is_active is not None and payload.is_active != target.is_active:
        target.is_active = payload.is_active
        revoke_sessions = True

    if revoke_sessions:
        target.token_version += 1

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email is already in use.") from exc
    db.refresh(target)
    if revoke_sessions:
        redis_bus.publish_event(f"qc:user:{target.id}", {"event": "SESSION_REVOKED", "user_id": target.id})
    safe_fields = sorted(k for k in payload.model_fields_set if k != "password")
    logger.info(
        "user_updated",
        actor=auth.username,
        target_user_id=target.id,
        fields=safe_fields,
        password_rotated=payload.password is not None,
    )
    return UserProfile.model_validate(target)


@router.delete("/{user_id}")
def soft_delete_user(
    user_id: int,
    auth: AuthContext = Depends(
        require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR, is_write=True)
    ),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Soft-delete a user to revoke sessions while preserving immutable SOS & audit history."""
    target = db.get(User, user_id)
    if target is None or target.deleted_at is not None:
        raise HTTPException(status_code=404, detail="User not found.")

    assert_can_manage_target_user(
        auth,
        target_role=target.role,
        target_region_id=target.region_id,
        target_territory_id=target.territory_id,
    )

    target.is_active = False
    target.deleted_at = datetime.now(timezone.utc)
    target.token_version += 1
    db.commit()
    redis_bus.publish_event(f"qc:user:{target.id}", {"event": "SESSION_REVOKED", "user_id": target.id})
    logger.info("user_soft_deleted", actor=auth.username, target_user_id=target.id)
    return {"status": "deactivated"}
