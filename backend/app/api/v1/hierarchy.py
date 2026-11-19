"""Territorial hierarchy, SQL-aggregated scoped executive KPIs, and immutable audit log endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.rbac import AuthContext, Role, apply_scope_filter
from app.core.redis_bus import redis_bus
from app.core.security import get_current_user, require_roles
from app.db.models import (
    AuditLog,
    DispatchRoute,
    FieldAudit,
    Outlet,
    Region,
    SosAlert,
    TriageItem,
)
from app.db.session import get_db
from app.schemas.domain import AuditLogResponse, DashboardKpiResponse, RegionResponse

router = APIRouter()


@router.get("/regions", response_model=list[RegionResponse])
def list_regions(
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RegionResponse]:
    """Return authorized regions and territories with GeoJSON boundaries (fail-closed)."""
    if not auth.is_national:
        if auth.region_id is None:
            return []
        if auth.role in (Role.SUPERVISOR, Role.FIELD_AGENT) and auth.territory_id is None:
            return []

    stmt = select(Region).options(selectinload(Region.territories)).where(Region.is_active.is_(True))
    if not auth.is_national:
        stmt = stmt.where(Region.id == auth.region_id)
    regions = list(db.execute(stmt.order_by(Region.id.asc())).scalars().all())

    result: list[RegionResponse] = []
    for region in regions:
        territories = [t for t in region.territories if t.is_active]
        if not auth.is_national:
            if auth.territory_id is not None:
                territories = [t for t in territories if t.id == auth.territory_id]
        result.append(
            RegionResponse(
                id=region.id,
                code=region.code,
                name=region.name,
                country_code=region.country_code,
                is_active=region.is_active,
                territories=territories,  # type: ignore[arg-type]
            )
        )
    return result


@router.get("/dashboard/kpis", response_model=DashboardKpiResponse)
def get_dashboard_kpis(
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DashboardKpiResponse:
    """Return tenant-scoped operational KPIs via SQL aggregates backed by Redis cache-aside."""
    cache_key = (
        f"qc:kpi:{auth.role.value}:{auth.region_id}:{auth.territory_id}:"
        f"{auth.user_id if auth.role == Role.FIELD_AGENT else 'all'}"
    )
    cached = redis_bus.get_json(cache_key)
    if isinstance(cached, dict):
        return DashboardKpiResponse(**cached)

    outlets_stmt = apply_scope_filter(select(func.count(Outlet.id)), Outlet, auth)
    total_outlets = int(db.execute(outlets_stmt).scalar_one() or 0)

    audit_agg_stmt = apply_scope_filter(
        select(
            func.count(FieldAudit.id),
            func.sum(case((FieldAudit.review_status == "approved", 1), else_=0)),
            func.sum(case((FieldAudit.review_status == "quarantined", 1), else_=0)),
            func.sum(case((FieldAudit.stockout_alert.is_(True), 1), else_=0)),
            func.sum(case((FieldAudit.geofence_status == "inside_territory", 1), else_=0)),
            func.avg(FieldAudit.shelf_share_pct),
        ),
        FieldAudit,
        auth,
        owner_column=FieldAudit.agent_id,
        restrict_agent_to_owner=True,
    )
    row = db.execute(audit_agg_stmt).one()
    total_audits = int(row[0] or 0)
    approved_audits = int(row[1] or 0)
    quarantined_audits = int(row[2] or 0)
    stockout_alerts = int(row[3] or 0)
    inside_count = int(row[4] or 0)
    avg_shelf = round(float(row[5] or 0.0), 1) if total_audits else 0.0
    geofence_pct = round((inside_count / total_audits) * 100.0, 1) if total_audits else 100.0

    if auth.role == Role.FIELD_AGENT:
        open_triage = 0
    else:
        triage_stmt = apply_scope_filter(
            select(func.count(TriageItem.id)).where(TriageItem.status == "open"),
            TriageItem,
            auth,
        )
        open_triage = int(db.execute(triage_stmt).scalar_one() or 0)

    sos_stmt = apply_scope_filter(
        select(func.count(SosAlert.id)).where(SosAlert.status == "active"),
        SosAlert,
        auth,
        owner_column=SosAlert.agent_id,
        restrict_agent_to_owner=True,
    )
    routes_stmt = apply_scope_filter(
        select(func.count(DispatchRoute.id)).where(DispatchRoute.status.in_(["assigned", "in_progress"])),
        DispatchRoute,
        auth,
        owner_column=DispatchRoute.agent_id,
        restrict_agent_to_owner=True,
    )

    response = DashboardKpiResponse(
        total_outlets=total_outlets,
        total_audits=total_audits,
        approved_audits=approved_audits,
        quarantined_audits=quarantined_audits,
        open_triage=open_triage,
        active_sos=int(db.execute(sos_stmt).scalar_one() or 0),
        assigned_routes=int(db.execute(routes_stmt).scalar_one() or 0),
        stockout_alerts=stockout_alerts,
        avg_shelf_share_pct=avg_shelf,
        geofence_compliance_pct=geofence_pct,
        server_role=get_settings().server_role,
    )
    redis_bus.set_json(cache_key, response.model_dump(), ttl_seconds=30)
    return response


@router.get("/audit-logs", response_model=list[AuditLogResponse])
def list_audit_logs(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER)),
    db: Session = Depends(get_db),
) -> list[AuditLogResponse]:
    """Read-only access to the security audit trail scoped to the caller's region."""
    stmt = select(AuditLog)
    if not auth.is_national:
        if auth.region_id is None:
            return []
        stmt = stmt.where(AuditLog.region_id == auth.region_id)
    rows = db.execute(stmt.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)).scalars().all()
    return [AuditLogResponse.model_validate(row) for row in rows]
