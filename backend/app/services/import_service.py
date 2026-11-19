"""Hardened CSV Outlet Catalog Bulk Importer with formula-injection neutralization and ImportJob tracking."""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.rbac import AuthContext
from app.core.redis_bus import redis_bus
from app.db.models import ImportJob, Outlet

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
MAX_CSV_ROWS = 5_000
VALID_CODE_RE = re.compile(r"^[A-Z0-9-]{3,32}$")
VALID_CATEGORIES = {
    "supermarket",
    "pharmacy",
    "convenience",
    "hardware",
    "bakery",
    "electronics",
    "other",
}


def sanitize_csv_cell(value: str | None) -> str:
    """Neutralize spreadsheet formula injection payloads while preserving readable text."""
    text_val = (value or "").strip()
    if not text_val:
        return ""
    # Allow negative numeric coordinates when parsed separately; strip formula prefixes on text fields
    while text_val and text_val[0] in FORMULA_PREFIXES:
        text_val = text_val[1:].lstrip()
    return text_val


def sanitize_filename(original_name: str | None) -> str:
    """Strip directory traversal components and unsafe characters from uploaded filenames."""
    base = Path(original_name or "outlets.csv").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return cleaned[:160] or "outlets.csv"


def _geom_point(db: Session, lon: float, lat: float):
    if db.bind and db.bind.dialect.name == "postgresql":
        return func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
    return f"POINT({lon} {lat})"


def import_outlets_csv(
    db: Session,
    *,
    auth: AuthContext,
    raw_bytes: bytes,
    original_filename: str | None,
    region_id: int,
    territory_id: int,
) -> ImportJob:
    """Stream-parse a CSV file and upsert outlets strictly inside the authorized region/territory."""
    settings = get_settings()
    if not raw_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CSV file is empty.")
    if len(raw_bytes) > settings.max_csv_import_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="CSV file exceeds maximum allowed size.",
        )

    safe_name = sanitize_filename(original_filename)
    job = ImportJob(
        id=str(uuid.uuid4()),
        region_id=region_id,
        territory_id=territory_id,
        created_by_id=auth.user_id,
        filename=safe_name,
        status="queued",
    )
    db.add(job)
    db.flush()

    try:
        decoded = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        job.status = "failed"
        job.error_summary = "CSV must be UTF-8 encoded."
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded.") from exc

    reader = csv.DictReader(io.StringIO(decoded))
    required_cols = {"outlet_code", "business_name", "category", "address", "latitude", "longitude"}
    if not reader.fieldnames:
        job.status = "failed"
        job.error_summary = f"Missing required CSV headers: {sorted(required_cols)}"
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(status_code=400, detail=job.error_summary)
    reader.fieldnames = [c.strip() for c in reader.fieldnames]
    if not required_cols.issubset(set(reader.fieldnames)):
        job.status = "failed"
        job.error_summary = f"Missing required CSV headers: {sorted(required_cols)}"
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(status_code=400, detail=job.error_summary)

    created = 0
    updated = 0
    rejected = 0
    total = 0
    errors: list[str] = []
    seen_by_code: dict[str, Outlet] = {}

    parsed_rows: list[tuple[int, dict[str, str | None]]] = []
    for row_num, row in enumerate(reader, start=2):
        if len(parsed_rows) >= MAX_CSV_ROWS:
            job.status = "failed"
            job.error_summary = f"CSV exceeds maximum row limit ({MAX_CSV_ROWS} rows)."
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=job.error_summary)
        parsed_rows.append((row_num, row))

    # Prefetch all referenced outlet_codes in bounded chunks (eliminates N+1 queries)
    candidate_codes = list(
        {
            sanitize_csv_cell(r.get("outlet_code")).upper()
            for _, r in parsed_rows
            if sanitize_csv_cell(r.get("outlet_code"))
        }
    )
    for i in range(0, len(candidate_codes), 500):
        chunk = candidate_codes[i : i + 500]
        if chunk:
            for existing_row in db.execute(select(Outlet).where(Outlet.outlet_code.in_(chunk))).scalars().all():
                seen_by_code[existing_row.outlet_code] = existing_row

    for row_num, row in parsed_rows:
        total += 1
        code = sanitize_csv_cell(row.get("outlet_code")).upper()
        name = sanitize_csv_cell(row.get("business_name"))
        category = sanitize_csv_cell(row.get("category")).lower() or "other"
        address = sanitize_csv_cell(row.get("address"))
        micro_location = sanitize_csv_cell(row.get("micro_location"))

        if (
            not VALID_CODE_RE.match(code)
            or not (2 <= len(name) <= 160)
            or not (3 <= len(address) <= 255)
            or len(micro_location) > 120
        ):
            rejected += 1
            if len(errors) < 10:
                errors.append(f"Row {row_num}: invalid outlet_code, business_name, or address.")
            continue

        if category not in VALID_CATEGORIES:
            category = "other"

        try:
            lat = float((row.get("latitude") or "").strip())
            lon = float((row.get("longitude") or "").strip())
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                raise ValueError("Out of WGS84 bounds")
        except ValueError:
            rejected += 1
            if len(errors) < 10:
                errors.append(f"Row {row_num}: invalid coordinates.")
            continue

        existing = seen_by_code.get(code)
        if existing is not None:
            if existing.region_id != region_id or existing.territory_id != territory_id:
                rejected += 1
                if len(errors) < 10:
                    errors.append(f"Row {row_num}: outlet_code {code} belongs to another territory.")
                continue
            existing.business_name = name
            existing.category = category
            existing.address = address
            existing.micro_location = micro_location
            existing.latitude = lat
            existing.longitude = lon
            existing.location_geom = _geom_point(db, lon, lat)
            updated += 1
        else:
            new_outlet = Outlet(
                outlet_code=code,
                business_name=name,
                category=category,
                address=address,
                micro_location=micro_location,
                region_id=region_id,
                territory_id=territory_id,
                latitude=lat,
                longitude=lon,
                location_geom=_geom_point(db, lon, lat),
                status="active",
                pos_terminal_active=True,
            )
            db.add(new_outlet)
            seen_by_code[code] = new_outlet
            created += 1

    job.status = "completed"
    job.rows_total = total
    job.rows_created = created
    job.rows_updated = updated
    job.rows_rejected = rejected
    job.error_summary = "; ".join(errors) if errors else None
    job.completed_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent CSV import conflict on outlet_code; please retry.",
        ) from exc
    db.refresh(job)

    redis_bus.invalidate_prefix("qc:map:")
    redis_bus.invalidate_prefix("qc:kpi:")
    return job
