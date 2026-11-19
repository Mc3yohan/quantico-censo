"""Field safety SOS panic alerts with strict scoping and Redis Pub/Sub CQRS broadcasts."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.rbac import AuthContext, Role, apply_scope_filter, assert_record_in_scope
from app.core.redis_bus import redis_bus
from app.core.security import get_current_user, require_roles
from app.db.models import SosAlert, Territory, User
from app.db.session import get_db
from app.schemas.domain import SosAlertResponse, SosResolveRequest, SosTriggerRequest

router = APIRouter()


def _serialize_sos(alert: SosAlert, agent_name: str) -> SosAlertResponse:
    out = SosAlertResponse.model_validate(alert)
    out.agent_name = agent_name
    return out


def _broadcast_sos(event_name: str, alert: SosAlert, agent_name: str, actor_username: str) -> None:
    payload = {
        "event": event_name,
        "alert_id": alert.id,
        "region_id": alert.region_id,
        "territory_id": alert.territory_id,
        "agent_id": alert.agent_id,
        "agent_name": agent_name,
        "latitude": alert.latitude,
        "longitude": alert.longitude,
        "gps_fallback": alert.gps_fallback,
        "message": alert.message,
        "status": alert.status,
        "resolution_note": alert.resolution_note,
        "actor": actor_username,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    redis_bus.publish_event(f"qc:alerts:region:{alert.region_id}", payload)
    redis_bus.publish_event("qc:alerts:global", payload)


@router.get("/active", response_model=list[SosAlertResponse])
def list_active_sos(
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[SosAlertResponse]:
    """List active SOS alerts strictly scoped to the caller's authorized region/territory."""
    stmt = apply_scope_filter(
        select(SosAlert).where(SosAlert.status == "active"),
        SosAlert,
        auth,
        owner_column=SosAlert.agent_id,
        restrict_agent_to_owner=True,
    )
    rows = db.execute(stmt.order_by(SosAlert.created_at.desc()).limit(100)).scalars().all()
    agent_ids = {r.agent_id for r in rows}
    names: dict[int, str] = {}
    if agent_ids:
        for uid, fname in db.execute(select(User.id, User.full_name).where(User.id.in_(agent_ids))).all():
            names[int(uid)] = str(fname)
    return [_serialize_sos(r, names.get(r.agent_id, "")) for r in rows]


@router.post("/trigger", response_model=SosAlertResponse, status_code=status.HTTP_201_CREATED)
def trigger_sos(
    payload: SosTriggerRequest,
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
) -> SosAlertResponse:
    """Trigger a field safety SOS alert and publish across CQRS nodes via Redis Pub/Sub."""
    if auth.region_id is not None and auth.territory_id is not None:
        region_id, territory_id = auth.region_id, auth.territory_id
    elif auth.role in (Role.SUPER_ADMIN, Role.REGIONAL_MANAGER):
        ter: Territory | None = None
        if payload.territory_id is not None:
            ter = db.get(Territory, payload.territory_id)
            if ter is None or not ter.is_active:
                raise HTTPException(status_code=404, detail="Active territory not found.")
            assert_record_in_scope(auth, region_id=ter.region_id, territory_id=ter.id)
        else:
            stmt = select(Territory).where(Territory.is_active.is_(True)).order_by(Territory.id.asc()).limit(1)
            if auth.role == Role.REGIONAL_MANAGER:
                if auth.region_id is None:
                    raise HTTPException(status_code=403, detail="Regional manager missing region assignment.")
                stmt = stmt.where(Territory.region_id == auth.region_id)
            ter = db.execute(stmt).scalar_one_or_none()
        if ter is None:
            raise HTTPException(status_code=400, detail="No active territory available for SOS alert.")
        region_id, territory_id = ter.region_id, ter.id
    else:
        raise HTTPException(
            status_code=400,
            detail="SOS trigger requires an operator assigned to a region and territory.",
        )

    alert = SosAlert(
        region_id=region_id,
        territory_id=territory_id,
        agent_id=auth.user_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        gps_fallback=payload.gps_fallback,
        message=payload.message,
        status="active",
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    redis_bus.invalidate_prefix("qc:map:")
    redis_bus.invalidate_prefix("qc:kpi:")
    _broadcast_sos("SOS_TRIGGERED", alert, auth.full_name, auth.username)
    return _serialize_sos(alert, auth.full_name)


@router.post("/{alert_id}/resolve", response_model=SosAlertResponse)
def resolve_sos(
    alert_id: int,
    payload: SosResolveRequest,
    auth: AuthContext = Depends(
        require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR, is_write=True)
    ),
    db: Session = Depends(get_db),
) -> SosAlertResponse:
    """Resolve an active SOS alert inside caller's scope and broadcast SOS_RESOLVED via Redis Pub/Sub."""
    alert = db.execute(select(SosAlert).where(SosAlert.id == alert_id).with_for_update()).scalar_one_or_none()
    if alert is None:
        raise HTTPException(status_code=404, detail="SOS alert not found.")
    assert_record_in_scope(auth, region_id=alert.region_id, territory_id=alert.territory_id)
    if alert.status != "active":
        raise HTTPException(status_code=409, detail="SOS alert is already closed.")

    alert.status = "resolved"
    alert.resolved_by_id = auth.user_id
    alert.resolution_note = payload.resolution_note
    alert.resolved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(alert)

    agent = db.get(User, alert.agent_id)
    agent_name = agent.full_name if agent else ""
    redis_bus.invalidate_prefix("qc:map:")
    redis_bus.invalidate_prefix("qc:kpi:")
    _broadcast_sos("SOS_RESOLVED", alert, agent_name, auth.username)
    return _serialize_sos(alert, agent_name)


@router.post("/{alert_id}/cancel", response_model=SosAlertResponse)
def cancel_sos(
    alert_id: int,
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
) -> SosAlertResponse:
    """Allow the originating field agent (or authorized manager) to cancel an active SOS alert."""
    alert = db.execute(select(SosAlert).where(SosAlert.id == alert_id).with_for_update()).scalar_one_or_none()
    if alert is None:
        raise HTTPException(status_code=404, detail="SOS alert not found.")
    assert_record_in_scope(
        auth,
        region_id=alert.region_id,
        territory_id=alert.territory_id,
        owner_user_id=alert.agent_id,
        require_owner_for_agent=True,
    )
    if alert.status != "active":
        raise HTTPException(status_code=409, detail="SOS alert is already closed.")

    alert.status = "cancelled"
    alert.resolved_by_id = auth.user_id
    alert.resolution_note = "Cancelled by originating operator."
    alert.resolved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(alert)

    agent = db.get(User, alert.agent_id)
    agent_name = agent.full_name if agent else ""
    redis_bus.invalidate_prefix("qc:map:")
    redis_bus.invalidate_prefix("qc:kpi:")
    _broadcast_sos("SOS_CANCELLED", alert, agent_name, auth.username)
    return _serialize_sos(alert, agent_name)
