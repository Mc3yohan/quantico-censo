"""Offline-first field audit ingestion, idempotent batch sync, and cached geospatial map snapshot."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.rbac import AuthContext, Role, apply_scope_filter, assert_record_in_scope
from app.core.redis_bus import redis_bus
from app.core.security import get_current_user, require_roles
from app.db.models import FieldAudit, Outlet, Region, SosAlert
from app.db.session import get_db
from app.schemas.domain import (
    AuditSyncResultItem,
    FieldAuditBatchRequest,
    FieldAuditBatchResponse,
    FieldAuditCreateRequest,
    FieldAuditResponse,
    MapPinResponse,
    MapSnapshotResponse,
    RegionResponse,
)
from app.services.sync_service import ingest_field_audit

router = APIRouter()


@router.post("", response_model=FieldAuditResponse, status_code=status.HTTP_201_CREATED)
def submit_single_audit(
    payload: FieldAuditCreateRequest,
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
) -> FieldAuditResponse:
    """Submit a single field audit with sync_uuid idempotency and geofence verification."""
    audit, result = ingest_field_audit(db, auth, payload)
    if result.status == "conflict" or audit is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result.detail)
    return FieldAuditResponse.model_validate(audit)


@router.post("/sync", response_model=FieldAuditBatchResponse)
def sync_audit_batch(
    payload: FieldAuditBatchRequest,
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
) -> FieldAuditBatchResponse:
    """Idempotent offline-PWA batch upload (handles retries and 409 conflicts per item)."""
    results = []
    created = 0
    duplicates = 0
    conflicts = 0

    for item in payload.items:
        try:
            _audit, res = ingest_field_audit(db, auth, item)
        except HTTPException as exc:
            db.rollback()
            res = AuditSyncResultItem(
                sync_uuid=item.sync_uuid,
                status="error",
                detail=str(exc.detail),
            )
        results.append(res)
        if res.status == "created":
            created += 1
        elif res.status == "idempotent_duplicate":
            duplicates += 1
        elif res.status == "conflict":
            conflicts += 1

    return FieldAuditBatchResponse(
        processed=len(results),
        created=created,
        duplicates=duplicates,
        conflicts=conflicts,
        results=results,
    )


@router.get("", response_model=list[FieldAuditResponse])
def list_audits(
    review_status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[FieldAuditResponse]:
    """List field audits with strict pagination and territorial isolation."""
    stmt = apply_scope_filter(
        select(FieldAudit),
        FieldAudit,
        auth,
        owner_column=FieldAudit.agent_id,
        restrict_agent_to_owner=True,
    )
    if review_status:
        stmt = stmt.where(FieldAudit.review_status == review_status)
    rows = db.execute(stmt.order_by(FieldAudit.synced_at.desc()).offset(offset).limit(limit)).scalars().all()
    return [FieldAuditResponse.model_validate(r) for r in rows]


@router.get("/map", response_model=MapSnapshotResponse)
def get_map_snapshot(
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MapSnapshotResponse:
    """Return Redis-cached slim map snapshot (territory polygons + outlets + audits + active SOS)."""
    cache_key = f"qc:map:{auth.role.value}:{auth.region_id}:{auth.territory_id}:{auth.user_id if auth.role == Role.FIELD_AGENT else 'all'}"
    cached = redis_bus.get_json(cache_key)
    if isinstance(cached, dict):
        cached["cached"] = True
        return MapSnapshotResponse(**cached)

    reg_stmt = select(Region).options(selectinload(Region.territories)).where(Region.is_active.is_(True))
    if not auth.is_national:
        if auth.region_id is None or (
            auth.role in (Role.SUPERVISOR, Role.FIELD_AGENT) and auth.territory_id is None
        ):
            return MapSnapshotResponse(regions=[], pins=[], generated_at=datetime.now(timezone.utc))
        reg_stmt = reg_stmt.where(Region.id == auth.region_id)

    regions_orm = db.execute(reg_stmt.order_by(Region.id.asc())).scalars().all()
    regions_out: list[RegionResponse] = []
    for region in regions_orm:
        territories = [t for t in region.territories if t.is_active]
        if auth.territory_id is not None and not auth.is_national:
            territories = [t for t in territories if t.id == auth.territory_id]
        regions_out.append(
            RegionResponse(
                id=region.id,
                code=region.code,
                name=region.name,
                country_code=region.country_code,
                is_active=region.is_active,
                territories=territories,  # type: ignore[arg-type]
            )
        )

    pins: list[MapPinResponse] = []
    outlets = db.execute(apply_scope_filter(select(Outlet), Outlet, auth).limit(500)).scalars().all()
    for o in outlets:
        pins.append(
            MapPinResponse(
                id=o.id,
                kind="outlet",
                code=o.outlet_code,
                label=o.business_name,
                category=o.category,
                region_id=o.region_id,
                territory_id=o.territory_id,
                latitude=o.latitude,
                longitude=o.longitude,
                status=o.status,
            )
        )

    audits = db.execute(
        apply_scope_filter(
            select(FieldAudit),
            FieldAudit,
            auth,
            owner_column=FieldAudit.agent_id,
            restrict_agent_to_owner=True,
        )
        .order_by(FieldAudit.synced_at.desc())
        .limit(500)
    ).scalars().all()
    for a in audits:
        pins.append(
            MapPinResponse(
                id=a.id,
                kind="audit",
                code=a.outlet_code,
                label=a.business_name,
                category=a.category,
                region_id=a.region_id,
                territory_id=a.territory_id,
                latitude=a.latitude,
                longitude=a.longitude,
                status=a.review_status,
                geofence_status=a.geofence_status,
                stockout_alert=a.stockout_alert,
            )
        )

    sos_alerts = db.execute(
        apply_scope_filter(
            select(SosAlert).where(SosAlert.status == "active"),
            SosAlert,
            auth,
            owner_column=SosAlert.agent_id,
            restrict_agent_to_owner=True,
        ).limit(100)
    ).scalars().all()
    for s in sos_alerts:
        pins.append(
            MapPinResponse(
                id=s.id,
                kind="sos",
                code=f"SOS-{s.id}",
                label=s.message,
                category="safety",
                region_id=s.region_id,
                territory_id=s.territory_id,
                latitude=s.latitude,
                longitude=s.longitude,
                status=s.status,
            )
        )

    snapshot = MapSnapshotResponse(
        regions=regions_out,
        pins=pins,
        generated_at=datetime.now(timezone.utc),
        cached=False,
    )
    redis_bus.set_json(cache_key, snapshot.model_dump(mode="json"), ttl_seconds=45)
    return snapshot


@router.get("/{audit_id}", response_model=FieldAuditResponse)
def get_audit_detail(
    audit_id: int,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FieldAuditResponse:
    """Fetch a single field audit after strict territorial and ownership verification."""
    audit = db.get(FieldAudit, audit_id)
    if audit is None:
        raise HTTPException(status_code=404, detail="Field audit not found.")
    assert_record_in_scope(
        auth,
        region_id=audit.region_id,
        territory_id=audit.territory_id,
        owner_user_id=audit.agent_id,
        require_owner_for_agent=True,
    )
    return FieldAuditResponse.model_validate(audit)
