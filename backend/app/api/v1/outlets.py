"""Commercial Outlet (PDV) catalog CRUD and hardened CSV bulk import endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.rbac import AuthContext, Role, apply_scope_filter, assert_record_in_scope
from app.core.redis_bus import redis_bus
from app.core.security import get_current_user, require_roles
from app.db.models import ImportJob, Outlet, Territory
from app.db.session import get_db
from app.schemas.domain import ImportJobResponse, OutletCreateRequest, OutletResponse
from app.services.import_service import import_outlets_csv

router = APIRouter()


def _geom_point(db: Session, lon: float, lat: float):
    if db.bind and db.bind.dialect.name == "postgresql":
        return func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
    return f"POINT({lon} {lat})"


@router.get("", response_model=list[OutletResponse])
def list_outlets(
    q: str | None = Query(default=None, max_length=100),
    category: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[OutletResponse]:
    """List authorized retail outlets with bounded SQL pagination."""
    stmt = apply_scope_filter(select(Outlet), Outlet, auth)
    if q:
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                Outlet.business_name.ilike(pattern, escape="\\"),
                Outlet.outlet_code.ilike(pattern, escape="\\"),
            )
        )
    if category:
        stmt = stmt.where(Outlet.category == category.strip().lower())
    rows = db.execute(stmt.order_by(Outlet.id.asc()).offset(offset).limit(limit)).scalars().all()
    return [OutletResponse.model_validate(r) for r in rows]


@router.post("", response_model=OutletResponse, status_code=status.HTTP_201_CREATED)
def create_outlet(
    payload: OutletCreateRequest,
    auth: AuthContext = Depends(
        require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR, is_write=True)
    ),
    db: Session = Depends(get_db),
) -> OutletResponse:
    """Create a commercial outlet strictly inside the caller's authorized territory."""
    territory_id = payload.territory_id if auth.role in (Role.SUPER_ADMIN, Role.REGIONAL_MANAGER) else auth.territory_id
    if territory_id is None:
        raise HTTPException(status_code=400, detail="territory_id is required.")

    territory = db.get(Territory, territory_id)
    if territory is None or not territory.is_active:
        raise HTTPException(status_code=404, detail="Active territory not found.")

    assert_record_in_scope(auth, region_id=territory.region_id, territory_id=territory.id)

    duplicate = db.execute(select(Outlet.id).where(Outlet.outlet_code == payload.outlet_code)).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="An outlet with this outlet_code already exists.")

    outlet = Outlet(
        outlet_code=payload.outlet_code,
        business_name=payload.business_name,
        category=payload.category,
        address=payload.address,
        micro_location=payload.micro_location,
        region_id=territory.region_id,
        territory_id=territory.id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        location_geom=_geom_point(db, payload.longitude, payload.latitude),
        status=payload.status,
        pos_terminal_active=payload.pos_terminal_active,
        contact_name=payload.contact_name,
        contact_phone=payload.contact_phone,
    )
    db.add(outlet)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="An outlet with this outlet_code already exists.") from exc
    db.refresh(outlet)

    redis_bus.invalidate_prefix("qc:map:")
    redis_bus.invalidate_prefix("qc:kpi:")
    return OutletResponse.model_validate(outlet)


@router.post("/import", response_model=ImportJobResponse, status_code=status.HTTP_201_CREATED)
def bulk_import_outlets(
    territory_id: int = Query(..., gt=0),
    file: UploadFile = File(...),
    auth: AuthContext = Depends(
        require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR, is_write=True)
    ),
    db: Session = Depends(get_db),
) -> ImportJobResponse:
    """Bulk-upsert outlets from a sanitized UTF-8 CSV into an authorized territory."""
    territory = db.get(Territory, territory_id)
    if territory is None or not territory.is_active:
        raise HTTPException(status_code=404, detail="Active territory not found.")
    assert_record_in_scope(auth, region_id=territory.region_id, territory_id=territory.id)

    max_bytes = get_settings().max_csv_import_bytes
    raw_bytes = file.file.read(max_bytes + 1)
    if len(raw_bytes) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="CSV file exceeds maximum allowed size.",
        )
    job = import_outlets_csv(
        db,
        auth=auth,
        raw_bytes=raw_bytes,
        original_filename=file.filename,
        region_id=territory.region_id,
        territory_id=territory.id,
    )
    return ImportJobResponse.model_validate(job)


@router.get("/imports/{job_id}", response_model=ImportJobResponse)
def get_import_job(
    job_id: str,
    auth: AuthContext = Depends(require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR)),
    db: Session = Depends(get_db),
) -> ImportJobResponse:
    """Fetch a tenant-scoped bulk import job (dedicated ImportJob table, never AuditLog)."""
    job = db.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Import job not found.")
    assert_record_in_scope(auth, region_id=job.region_id, territory_id=job.territory_id)
    return ImportJobResponse.model_validate(job)
