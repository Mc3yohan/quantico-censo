"""Authenticated, tenant-scoped evidence photo upload and download endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.rbac import AuthContext, Role, assert_record_in_scope
from app.core.security import get_current_user, require_roles
from app.db.models import Territory
from app.db.session import get_db
from app.schemas.domain import MediaUploadResponse
from app.services.media_service import resolve_authorized_media_path, validate_and_store_photo

router = APIRouter()


@router.post("/photos", response_model=MediaUploadResponse, status_code=status.HTTP_201_CREATED)
def upload_photo(
    territory_id: int | None = Query(default=None, gt=0),
    file: UploadFile = File(...),
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
) -> MediaUploadResponse:
    """Validate image magic bytes, strip EXIF, store as WebP, and return an authenticated media URL."""
    target_territory_id = territory_id if auth.role in (Role.SUPER_ADMIN, Role.REGIONAL_MANAGER) else auth.territory_id
    if target_territory_id is None:
        raise HTTPException(status_code=400, detail="territory_id is required.")

    territory = db.get(Territory, target_territory_id)
    if territory is None or not territory.is_active:
        raise HTTPException(status_code=404, detail="Active territory not found.")
    assert_record_in_scope(auth, region_id=territory.region_id, territory_id=territory.id)

    max_bytes = get_settings().max_photo_bytes
    raw_bytes = file.file.read(max_bytes + 1)
    if len(raw_bytes) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Image exceeds maximum allowed size ({max_bytes} bytes).",
        )
    asset = validate_and_store_photo(
        db,
        auth=auth,
        raw_bytes=raw_bytes,
        region_id=territory.region_id,
        territory_id=territory.id,
    )
    return MediaUploadResponse(
        id=asset.id,
        content_type=asset.content_type,
        size_bytes=asset.size_bytes,
        sha256=asset.sha256,
        url=f"/api/v1/media/{asset.id}",
        created_at=asset.created_at,
    )


@router.get("/{media_id}")
def download_photo(
    media_id: str,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Serve an evidence photo strictly after verifying JWT identity and territorial scope."""
    asset, file_path = resolve_authorized_media_path(db, auth=auth, media_id=media_id)
    return FileResponse(
        path=str(file_path),
        media_type=asset.content_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )
