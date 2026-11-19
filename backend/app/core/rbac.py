"""Single-source-of-truth Role-Based Access Control (RBAC) and fail-closed territorial scoping."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastapi import HTTPException, status


class Role(str, Enum):
    """Clean, location-decoupled RBAC roles for Quantico Censo."""

    SUPER_ADMIN = "super_admin"
    REGIONAL_MANAGER = "regional_manager"
    SUPERVISOR = "supervisor"
    FIELD_AGENT = "field_agent"
    VIEWER = "viewer"


ROLE_LEVEL: dict[Role, int] = {
    Role.SUPER_ADMIN: 50,
    Role.REGIONAL_MANAGER: 40,
    Role.SUPERVISOR: 30,
    Role.FIELD_AGENT: 20,
    Role.VIEWER: 10,
}

READ_ONLY_ROLES: frozenset[Role] = frozenset({Role.VIEWER})


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Verified, DB-backed identity and territorial scope for an authenticated request."""

    user_id: int
    username: str
    full_name: str
    role: Role
    region_id: int | None
    territory_id: int | None
    token_version: int

    @property
    def level(self) -> int:
        return ROLE_LEVEL[self.role]

    @property
    def is_read_only(self) -> bool:
        return self.role in READ_ONLY_ROLES

    @property
    def is_national(self) -> bool:
        return self.role == Role.SUPER_ADMIN


def assert_role_allowed(
    auth: AuthContext,
    allowed_roles: set[Role] | frozenset[Role],
    *,
    is_write: bool = False,
) -> None:
    """Enforce explicit role membership and strict read-only boundary."""
    if is_write and auth.is_read_only:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Read-only role cannot perform write operations.",
        )
    if auth.role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient role permissions for this operation.",
        )


def apply_scope_filter(
    stmt: Any,
    model: Any,
    auth: AuthContext,
    *,
    owner_column: Any | None = None,
    restrict_agent_to_owner: bool = False,
) -> Any:
    """
    Apply fail-closed multi-tenant territorial filters to a SQLAlchemy Select statement.

    - SUPER_ADMIN: cross-region access.
    - REGIONAL_MANAGER / VIEWER: strictly scoped to auth.region_id (and territory_id if set).
      Missing region_id fails closed (returns zero rows).
    - SUPERVISOR: strictly scoped to auth.region_id + auth.territory_id.
    - FIELD_AGENT: strictly scoped to auth.region_id + auth.territory_id (+ owner when requested).
    """
    if auth.role == Role.SUPER_ADMIN:
        return stmt

    if auth.region_id is None:
        return stmt.where(False)

    stmt = stmt.where(model.region_id == auth.region_id)

    if auth.role in (Role.SUPERVISOR, Role.FIELD_AGENT):
        if auth.territory_id is None:
            return stmt.where(False)
        stmt = stmt.where(model.territory_id == auth.territory_id)
        if auth.role == Role.FIELD_AGENT and restrict_agent_to_owner and owner_column is not None:
            stmt = stmt.where(owner_column == auth.user_id)
    elif auth.role == Role.VIEWER and auth.territory_id is not None:
        stmt = stmt.where(model.territory_id == auth.territory_id)

    return stmt


def assert_record_in_scope(
    auth: AuthContext,
    *,
    region_id: int | None,
    territory_id: int | None = None,
    owner_user_id: int | None = None,
    require_owner_for_agent: bool = False,
) -> None:
    """Fail-closed ownership & territorial scope check for single-record operations."""
    if auth.role == Role.SUPER_ADMIN:
        return

    if auth.region_id is None or region_id != auth.region_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Resource is outside your authorized region.",
        )

    if auth.role in (Role.SUPERVISOR, Role.FIELD_AGENT):
        if auth.territory_id is None or territory_id != auth.territory_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Resource is outside your authorized territory.",
            )
        if auth.role == Role.FIELD_AGENT and require_owner_for_agent and owner_user_id != auth.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Field agents may only access their own records.",
            )

    if auth.role == Role.VIEWER and auth.territory_id is not None and territory_id != auth.territory_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Resource is outside your authorized territory.",
        )


def assert_can_manage_target_user(
    actor: AuthContext,
    *,
    target_role: Role,
    target_region_id: int | None,
    target_territory_id: int | None,
) -> None:
    """
    Prevent privilege escalation, peer takeover, and cross-tenant user management.
    Must be evaluated against BOTH existing target state AND proposed new payload state.
    """
    if actor.is_read_only:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Read-only role cannot manage users.",
        )
    if actor.level <= ROLE_LEVEL[target_role]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot manage a user with equal or higher privilege level.",
        )
    if actor.role == Role.SUPER_ADMIN:
        return
    if actor.region_id is None or target_region_id != actor.region_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot manage users outside your assigned region.",
        )
    if actor.role == Role.SUPERVISOR:
        if actor.territory_id is None or target_territory_id != actor.territory_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Supervisors may only manage field agents inside their territory.",
            )
    elif actor.role == Role.FIELD_AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Field agents cannot manage users.",
        )
