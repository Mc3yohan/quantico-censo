"""Quarantine & Triage quality-review endpoints with SQL pagination and strict territorial isolation."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.rbac import AuthContext, Role, apply_scope_filter, assert_record_in_scope
from app.core.redis_bus import redis_bus
from app.core.security import require_roles
from app.db.models import FieldAudit, TriageItem
from app.db.session import get_db
from app.schemas.domain import PaginatedTriageResponse, TriageItemResponse, TriageResolveRequest

router = APIRouter()


@router.get("", response_model=PaginatedTriageResponse)
def list_triage_queue(
    status_filter: str = Query(default="open", alias="status", max_length=32),
    priority: str | None = Query(default=None, max_length=24),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(
        require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR, Role.VIEWER)
    ),
    db: Session = Depends(get_db),
) -> PaginatedTriageResponse:
    """List quarantined triage items using real SQL pagination and tenant scoping."""
    base_stmt = apply_scope_filter(select(TriageItem), TriageItem, auth)
    if status_filter != "all":
        base_stmt = base_stmt.where(TriageItem.status == status_filter)
    if priority:
        base_stmt = base_stmt.where(TriageItem.priority == priority)

    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total = int(db.execute(count_stmt).scalar_one() or 0)

    rows = (
        db.execute(
            base_stmt.order_by(TriageItem.created_at.desc()).offset(offset).limit(limit)
        )
        .scalars()
        .all()
    )

    audit_ids = [r.audit_id for r in rows]
    audits_by_id: dict[int, FieldAudit] = {}
    if audit_ids:
        audits = db.execute(select(FieldAudit).where(FieldAudit.id.in_(audit_ids))).scalars().all()
        audits_by_id = {a.id: a for a in audits}

    items: list[TriageItemResponse] = []
    for row in rows:
        audit = audits_by_id.get(row.audit_id)
        item = TriageItemResponse.model_validate(row)
        if audit is not None:
            item.business_name = audit.business_name
            item.outlet_code = audit.outlet_code
            item.geofence_status = audit.geofence_status
            item.latitude = audit.latitude
            item.longitude = audit.longitude
        items.append(item)

    return PaginatedTriageResponse(total=total, limit=limit, offset=offset, items=items)


@router.post("/{triage_id}/resolve", response_model=TriageItemResponse)
def resolve_triage_item(
    triage_id: int,
    payload: TriageResolveRequest,
    auth: AuthContext = Depends(
        require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR, is_write=True)
    ),
    db: Session = Depends(get_db),
) -> TriageItemResponse:
    """Approve, reject, or request rework on a quarantined field audit within scope."""
    item = db.execute(select(TriageItem).where(TriageItem.id == triage_id).with_for_update()).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Triage item not found.")

    assert_record_in_scope(auth, region_id=item.region_id, territory_id=item.territory_id)
    if item.status != "open":
        raise HTTPException(status_code=409, detail="Triage item has already been resolved.")

    audit = db.execute(select(FieldAudit).where(FieldAudit.id == item.audit_id).with_for_update()).scalar_one_or_none()
    if audit is None:
        raise HTTPException(status_code=404, detail="Linked field audit not found.")

    now = datetime.now(timezone.utc)
    item.status = payload.decision
    item.reviewer_id = auth.user_id
    item.resolution_note = payload.resolution_note
    item.resolved_at = now
    audit.review_status = payload.decision

    db.commit()
    db.refresh(item)

    redis_bus.invalidate_prefix("qc:map:")
    redis_bus.invalidate_prefix("qc:kpi:")
    event = {
        "event": "TRIAGE_RESOLVED",
        "triage_id": item.id,
        "audit_id": audit.id,
        "region_id": item.region_id,
        "territory_id": item.territory_id,
        "decision": item.status,
        "reviewer": auth.username,
        "timestamp": now.isoformat(),
    }
    redis_bus.publish_event(f"qc:alerts:region:{item.region_id}", event)
    redis_bus.publish_event("qc:alerts:global", event)

    out = TriageItemResponse.model_validate(item)
    out.business_name = audit.business_name
    out.outlet_code = audit.outlet_code
    out.geofence_status = audit.geofence_status
    out.latitude = audit.latitude
    out.longitude = audit.longitude
    return out
