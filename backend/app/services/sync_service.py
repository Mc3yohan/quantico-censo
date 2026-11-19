"""Idempotent offline-first audit ingestion service with geofencing, triage, and Redis Pub/Sub."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.rbac import AuthContext, Role, assert_record_in_scope
from app.core.redis_bus import redis_bus
from app.db.models import DispatchRoute, FieldAudit, MediaAsset, Outlet, Territory
from app.schemas.domain import AuditSyncResultItem, FieldAuditCreateRequest
from app.services.geofence_service import evaluate_geofence
from app.services.triage_service import create_triage_if_needed


def _geom_point(db: Session, lon: float, lat: float):
    if db.bind and db.bind.dialect.name == "postgresql":
        return func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
    return f"POINT({lon} {lat})"


def _resolve_target_scope(
    db: Session,
    auth: AuthContext,
    payload: FieldAuditCreateRequest,
) -> tuple[int, int, Outlet | None, DispatchRoute | None]:
    """Derive authoritative region_id/territory_id from AuthContext (never spoofable by agents)."""
    outlet: Outlet | None = None
    if payload.outlet_id is not None:
        outlet = db.get(Outlet, payload.outlet_id)
        if outlet is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Referenced outlet not found.")
    elif payload.outlet_code:
        outlet = db.execute(
            select(Outlet).where(Outlet.outlet_code == payload.outlet_code)
        ).scalar_one_or_none()

    route: DispatchRoute | None = None
    if payload.route_id is not None:
        route = db.get(DispatchRoute, payload.route_id)
        if route is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Referenced route not found.")

    if auth.role == Role.SUPER_ADMIN:
        if outlet is not None:
            region_id, territory_id = outlet.region_id, outlet.territory_id
        elif route is not None:
            region_id, territory_id = route.region_id, route.territory_id
        else:
            first_ter = db.execute(
                select(Territory).where(Territory.is_active.is_(True)).order_by(Territory.id.asc()).limit(1)
            ).scalar_one_or_none()
            if first_ter is None:
                raise HTTPException(status_code=400, detail="No active territory configured.")
            region_id, territory_id = first_ter.region_id, first_ter.id
    elif auth.role == Role.REGIONAL_MANAGER:
        if auth.region_id is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Regional manager missing region scope.")
        if outlet is not None:
            assert_record_in_scope(auth, region_id=outlet.region_id, territory_id=outlet.territory_id)
            region_id, territory_id = outlet.region_id, outlet.territory_id
        elif route is not None:
            assert_record_in_scope(auth, region_id=route.region_id, territory_id=route.territory_id)
            region_id, territory_id = route.region_id, route.territory_id
        else:
            first_reg_ter = db.execute(
                select(Territory)
                .where(Territory.region_id == auth.region_id, Territory.is_active.is_(True))
                .order_by(Territory.id.asc())
                .limit(1)
            ).scalar_one_or_none()
            if first_reg_ter is None:
                raise HTTPException(status_code=400, detail="No active territory configured in your region.")
            region_id, territory_id = first_reg_ter.region_id, first_reg_ter.id
    else:
        if auth.region_id is None or auth.territory_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Operator must be assigned to both a region and territory to submit field audits.",
            )
        region_id, territory_id = auth.region_id, auth.territory_id

    if outlet is not None:
        assert_record_in_scope(auth, region_id=outlet.region_id, territory_id=outlet.territory_id)
    if route is not None:
        assert_record_in_scope(
            auth,
            region_id=route.region_id,
            territory_id=route.territory_id,
            owner_user_id=route.agent_id,
            require_owner_for_agent=True,
        )
    if outlet is not None and route is not None and outlet.territory_id != route.territory_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Referenced outlet and route must belong to the same territory.",
        )

    return region_id, territory_id, outlet, route


def ingest_field_audit(
    db: Session,
    auth: AuthContext,
    payload: FieldAuditCreateRequest,
) -> tuple[FieldAudit | None, AuditSyncResultItem]:
    """
    Process a single field audit with UUID idempotency and conflict detection.

    - Same sync_uuid + same agent + same outlet_code -> idempotent_duplicate (safe offline retry).
    - Same sync_uuid + mismatched agent or outlet_code -> conflict (409 semantics).
    """
    existing = db.execute(
        select(FieldAudit).where(FieldAudit.sync_uuid == payload.sync_uuid)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.agent_id == auth.user_id and existing.outlet_code == payload.outlet_code:
            return existing, AuditSyncResultItem(
                sync_uuid=payload.sync_uuid,
                status="idempotent_duplicate",
                audit_id=existing.id,
                geofence_status=existing.geofence_status,
                review_status=existing.review_status,
                detail="Audit already synchronized.",
            )
        return None, AuditSyncResultItem(
            sync_uuid=payload.sync_uuid,
            status="conflict",
            audit_id=None,
            detail="sync_uuid collision with an existing audit record.",
        )

    region_id, territory_id, outlet, route = _resolve_target_scope(db, auth, payload)
    if payload.photo_media_id is not None:
        asset = db.get(MediaAsset, payload.photo_media_id)
        if (
            asset is None
            or asset.region_id != region_id
            or asset.territory_id != territory_id
            or (auth.role == Role.FIELD_AGENT and asset.uploaded_by_id != auth.user_id)
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Referenced photo_media_id is invalid or outside operator scope.",
            )
    territory = db.get(Territory, territory_id)
    geo = evaluate_geofence(
        db,
        territory=territory,
        latitude=payload.latitude,
        longitude=payload.longitude,
        reference_outlet=outlet,
    )

    now = datetime.now(timezone.utc)
    audit = FieldAudit(
        sync_uuid=payload.sync_uuid,
        outlet_id=outlet.id if outlet else None,
        outlet_code=payload.outlet_code,
        business_name=payload.business_name,
        category=payload.category,
        address=payload.address,
        micro_location=payload.micro_location,
        region_id=region_id,
        territory_id=territory_id,
        agent_id=auth.user_id,
        route_id=route.id if route else None,
        latitude=payload.latitude,
        longitude=payload.longitude,
        location_geom=_geom_point(db, payload.longitude, payload.latitude),
        gps_accuracy_m=payload.gps_accuracy_m,
        visit_status=payload.visit_status,
        shelf_share_pct=payload.shelf_share_pct,
        stockout_alert=payload.stockout_alert,
        pos_active=payload.pos_active,
        signage_compliant=payload.signage_compliant,
        notes=payload.notes,
        photo_media_id=payload.photo_media_id,
        geofence_status=geo.status,
        geofence_distance_m=geo.distance_m,
        review_status="quarantined",
        captured_at=payload.captured_at or now,
        synced_at=now,
    )
    try:
        db.add(audit)
        db.flush()

        triage_item = create_triage_if_needed(db, audit)
        if route is not None and route.status in ("assigned", "in_progress"):
            route.status = "completed"

        db.commit()
    except IntegrityError:
        db.rollback()
        raced = db.execute(
            select(FieldAudit).where(FieldAudit.sync_uuid == payload.sync_uuid)
        ).scalar_one_or_none()
        if raced is not None and raced.agent_id == auth.user_id and raced.outlet_code == payload.outlet_code:
            return raced, AuditSyncResultItem(
                sync_uuid=payload.sync_uuid,
                status="idempotent_duplicate",
                audit_id=raced.id,
                geofence_status=raced.geofence_status,
                review_status=raced.review_status,
                detail="Audit already synchronized.",
            )
        return None, AuditSyncResultItem(
            sync_uuid=payload.sync_uuid,
            status="conflict",
            audit_id=None,
            detail="sync_uuid collision with an existing audit record.",
        )
    db.refresh(audit)

    # Invalidate cached map & KPIs across CQRS read nodes
    redis_bus.invalidate_prefix("qc:map:")
    redis_bus.invalidate_prefix("qc:kpi:")

    event = {
        "event": "AUDIT_SYNCED",
        "audit_id": audit.id,
        "agent_id": audit.agent_id,
        "region_id": audit.region_id,
        "territory_id": audit.territory_id,
        "outlet_code": audit.outlet_code,
        "business_name": audit.business_name,
        "geofence_status": audit.geofence_status,
        "review_status": audit.review_status,
        "triage_id": triage_item.id if triage_item else None,
        "triage_priority": triage_item.priority if triage_item else None,
        "latitude": audit.latitude,
        "longitude": audit.longitude,
        "timestamp": now.isoformat(),
    }
    redis_bus.publish_event(f"qc:alerts:region:{audit.region_id}", event)
    redis_bus.publish_event("qc:alerts:global", event)

    return audit, AuditSyncResultItem(
        sync_uuid=audit.sync_uuid,
        status="created",
        audit_id=audit.id,
        geofence_status=audit.geofence_status,
        review_status=audit.review_status,
        detail="Audit synchronized successfully.",
    )
