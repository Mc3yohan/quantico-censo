"""Hardened evidence media upload & tenant-scoped retrieval (Pillow magic-bytes + WebP)."""

from __future__ import annotations

import hashlib
import io
import uuid
from pathlib import Path

from fastapi import HTTPException, status
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.rbac import AuthContext, assert_record_in_scope
from app.db.models import MediaAsset

MAX_IMAGE_DIMENSION = 1280
Image.MAX_IMAGE_PIXELS = 25_000_000


def validate_and_store_photo(
    db: Session,
    *,
    auth: AuthContext,
    raw_bytes: bytes,
    region_id: int,
    territory_id: int,
) -> MediaAsset:
    """
    Validate magic bytes via Pillow, strip EXIF, resize/re-encode to WebP, and persist tenant-scoped metadata.
    """
    settings = get_settings()
    if not raw_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file upload.")
    if len(raw_bytes) > settings.max_photo_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Image exceeds maximum allowed size ({settings.max_photo_bytes} bytes).",
        )

    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img.verify()
        with Image.open(io.BytesIO(raw_bytes)) as img:
            if img.format not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail="Only JPEG, PNG, and WebP images are accepted.",
                )
            oriented = ImageOps.exif_transpose(img)
            rgb = oriented.convert("RGB")
            rgb.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
            output = io.BytesIO()
            rgb.save(output, format="WEBP", quality=80, method=4)
            encoded = output.getvalue()
    except HTTPException:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Invalid, oversized, or corrupted image file.",
        ) from exc

    media_id = str(uuid.uuid4())
    storage_filename = f"{media_id}.webp"
    media_root = settings.media_root.resolve()
    media_root.mkdir(parents=True, exist_ok=True)
    target_path = (media_root / storage_filename).resolve()
    if not target_path.is_relative_to(media_root):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid storage path.")

    target_path.write_bytes(encoded)
    digest = hashlib.sha256(encoded).hexdigest()

    asset = MediaAsset(
        id=media_id,
        region_id=region_id,
        territory_id=territory_id,
        uploaded_by_id=auth.user_id,
        storage_filename=storage_filename,
        content_type="image/webp",
        size_bytes=len(encoded),
        sha256=digest,
    )
    db.add(asset)
    try:
        db.commit()
    except Exception:
        db.rollback()
        target_path.unlink(missing_ok=True)
        raise
    db.refresh(asset)
    return asset


def resolve_authorized_media_path(
    db: Session,
    *,
    auth: AuthContext,
    media_id: str,
) -> tuple[MediaAsset, Path]:
    """Verify media ownership/scope and return safe resolved filesystem path."""
    asset = db.get(MediaAsset, media_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found.")

    assert_record_in_scope(
        auth,
        region_id=asset.region_id,
        territory_id=asset.territory_id,
        owner_user_id=asset.uploaded_by_id,
        require_owner_for_agent=True,
    )

    media_root = get_settings().media_root.resolve()
    file_path = (media_root / Path(asset.storage_filename).name).resolve()
    if not file_path.is_relative_to(media_root) or not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file missing on disk.")

    return asset, file_path
