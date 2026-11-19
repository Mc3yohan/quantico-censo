"""Unified Dispatch Route management with strict multi-tenant scoping on assign, reassign, and revoke."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.rbac import AuthContext, Role, apply_scope_filter, assert_record_in_scope
from app.core.redis_bus import redis_bus
from app.core.security import get_current_user, require_roles
from app.db.models import DispatchRoute, Outlet, User
from app.db.session import get_db
from app.schemas.domain import (
    DispatchRouteCreateRequest,
    DispatchRouteReassignRequest,
    DispatchRouteResponse,
    DispatchRouteStatusRequest,
)

router = APIRouter()


@router.get("", response_model=list[DispatchRouteResponse])
def list_routes(
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[DispatchRouteResponse]:
    """List dispatch routes scoped by role (field agents see only their own routes)."""
    stmt = apply_scope_filter(
        select(DispatchRoute),
        DispatchRoute,
        auth,
        owner_column=DispatchRoute.agent_id,
        restrict_agent_to_owner=True,
    )
    if status_filter:
        stmt = stmt.where(DispatchRoute.status == status_filter)
    rows = db.execute(stmt.order_by(DispatchRoute.created_at.desc()).offset(offset).limit(limit)).scalars().all()
    return [DispatchRouteResponse.model_validate(r) for r in rows]


@router.post("", response_model=DispatchRouteResponse, status_code=status.HTTP_201_CREATED)
def assign_route(
    payload: DispatchRouteCreateRequest,
    auth: AuthContext = Depends(
        require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR, is_write=True)
    ),
    db: Session = Depends(get_db),
) -> DispatchRouteResponse:
    """Assign a field visit route to an active Field Agent strictly within the caller's scope."""
    agent = db.get(User, payload.agent_id)
    if agent is None or agent.deleted_at is not None or not agent.is_active:
        raise HTTPException(status_code=404, detail="Target field agent not found or inactive.")
    if agent.role != Role.FIELD_AGENT or agent.region_id is None or agent.territory_id is None:
        raise HTTPException(status_code=400, detail="Target user must be a territory-assigned field agent.")

    assert_record_in_scope(auth, region_id=agent.region_id, territory_id=agent.territory_id)

    if payload.outlet_id is not None:
        outlet = db.get(Outlet, payload.outlet_id)
        if outlet is None:
            raise HTTPException(status_code=404, detail="Target outlet not found.")
        assert_record_in_scope(auth, region_id=outlet.region_id, territory_id=outlet.territory_id)
        if outlet.territory_id != agent.territory_id:
            raise HTTPException(status_code=400, detail="Outlet and field agent must belong to the same territory.")

    route: DispatchRoute | None = None
    for _ in range(3):
        candidate = DispatchRoute(
            code=f"RT-{datetime.now(timezone.utc).strftime('%Y%m')}-{secrets.token_hex(4).upper()}",
            title=payload.title,
            region_id=agent.region_id,
            territory_id=agent.territory_id,
            agent_id=agent.id,
            supervisor_id=auth.user_id,
            outlet_id=payload.outlet_id,
            scheduled_date=payload.scheduled_date,
            status="assigned",
            priority=payload.priority,
            notes=payload.notes,
        )
        db.add(candidate)
        try:
            db.commit()
            db.refresh(candidate)
            route = candidate
            break
        except IntegrityError:
            db.rollback()
    if route is None:
        raise HTTPException(status_code=409, detail="Could not generate unique route code; retry.")

    redis_bus.invalidate_prefix("qc:kpi:")
    event = {
        "event": "ROUTE_ASSIGNED",
        "route_id": route.id,
        "code": route.code,
        "title": route.title,
        "region_id": route.region_id,
        "territory_id": route.territory_id,
        "agent_id": route.agent_id,
        "priority": route.priority,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    redis_bus.publish_event(f"qc:alerts:region:{route.region_id}", event)
    redis_bus.publish_event("qc:alerts:global", event)
    return DispatchRouteResponse.model_validate(route)


def _broadcast_route(event_name: str, route: DispatchRoute) -> None:
    event = {
        "event": event_name,
        "route_id": route.id,
        "code": route.code,
        "title": route.title,
        "region_id": route.region_id,
        "territory_id": route.territory_id,
        "agent_id": route.agent_id,
        "status": route.status,
        "priority": route.priority,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    redis_bus.publish_event(f"qc:alerts:region:{route.region_id}", event)
    redis_bus.publish_event("qc:alerts:global", event)


@router.put("/{route_id}/reassign", response_model=DispatchRouteResponse)
def reassign_route(
    route_id: int,
    payload: DispatchRouteReassignRequest,
    auth: AuthContext = Depends(
        require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR, is_write=True)
    ),
    db: Session = Depends(get_db),
) -> DispatchRouteResponse:
    """Reassign a route to another active field agent in the same authorized territory."""
    route = db.execute(select(DispatchRoute).where(DispatchRoute.id == route_id).with_for_update()).scalar_one_or_none()
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found.")
    assert_record_in_scope(auth, region_id=route.region_id, territory_id=route.territory_id)
    if route.status in ("completed", "revoked"):
        raise HTTPException(status_code=409, detail="Cannot reassign a completed or revoked route.")

    new_agent = db.get(User, payload.agent_id)
    if new_agent is None or new_agent.deleted_at is not None or not new_agent.is_active:
        raise HTTPException(status_code=404, detail="New field agent not found or inactive.")
    if new_agent.role != Role.FIELD_AGENT:
        raise HTTPException(status_code=400, detail="Target user must be a field agent.")
    assert_record_in_scope(auth, region_id=new_agent.region_id, territory_id=new_agent.territory_id)
    if new_agent.territory_id != route.territory_id:
        raise HTTPException(status_code=400, detail="New agent must belong to the route's territory.")

    route.agent_id = new_agent.id
    if payload.notes is not None:
        route.notes = payload.notes
    db.commit()
    db.refresh(route)
    redis_bus.invalidate_prefix("qc:kpi:")
    _broadcast_route("ROUTE_REASSIGNED", route)
    return DispatchRouteResponse.model_validate(route)


@router.post("/{route_id}/revoke", response_model=DispatchRouteResponse)
def revoke_route(
    route_id: int,
    auth: AuthContext = Depends(
        require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR, is_write=True)
    ),
    db: Session = Depends(get_db),
) -> DispatchRouteResponse:
    """Revoke an active route strictly within the caller's territorial scope."""
    route = db.execute(select(DispatchRoute).where(DispatchRoute.id == route_id).with_for_update()).scalar_one_or_none()
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found.")
    assert_record_in_scope(auth, region_id=route.region_id, territory_id=route.territory_id)
    if route.status in ("completed", "revoked"):
        raise HTTPException(status_code=409, detail="Cannot revoke a completed or already revoked route.")
    route.status = "revoked"
    db.commit()
    db.refresh(route)
    redis_bus.invalidate_prefix("qc:kpi:")
    _broadcast_route("ROUTE_REVOKED", route)
    return DispatchRouteResponse.model_validate(route)


@router.patch("/{route_id}/status", response_model=DispatchRouteResponse)
def update_route_status(
    route_id: int,
    payload: DispatchRouteStatusRequest,
    auth: AuthContext = Depends(
        require_roles(
            Role.SUPER_ADMIN,
            Role.REGIONAL_MANAGER,
            Role.SUPERVISOR,
            Role.FIELD_AGENT,
            is_write=True,
        )
    ),
    db: Session = Depends(get_db),
) -> DispatchRouteResponse:
    """Update route status (agents may update only their own routes to in_progress/completed)."""
    route = db.execute(select(DispatchRoute).where(DispatchRoute.id == route_id).with_for_update()).scalar_one_or_none()
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found.")
    assert_record_in_scope(
        auth,
        region_id=route.region_id,
        territory_id=route.territory_id,
        owner_user_id=route.agent_id,
        require_owner_for_agent=True,
    )
    if route.status in ("completed", "revoked"):
        raise HTTPException(status_code=409, detail="Cannot change status of a completed or revoked route.")
    if auth.role == Role.FIELD_AGENT and payload.status not in ("in_progress", "completed"):
        raise HTTPException(status_code=403, detail="Field agents can only set in_progress or completed.")
    route.status = payload.status
    db.commit()
    db.refresh(route)
    redis_bus.invalidate_prefix("qc:kpi:")
    _broadcast_route("ROUTE_UPDATED", route)
    return DispatchRouteResponse.model_validate(route)
