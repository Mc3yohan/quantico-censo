"""Clean-room multi-tenant ORM schema for Quantico Censo (Retail & Field Operations)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.core.rbac import Role
from app.db.session import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PortableGeometry(TypeDecorator[Any]):
    """Uses native PostGIS Geometry(SRID=4326) on PostgreSQL and WKT/GeoJSON Text on SQLite."""

    impl = Text
    cache_ok = True
    spatial_index = False

    def __init__(self, geometry_type: str = "POINT", srid: int = 4326) -> None:
        super().__init__()
        self.geometry_type = geometry_type
        self.srid = srid
        self.management = False

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect is not None and getattr(dialect, "name", None) == "postgresql":
            return dialect.type_descriptor(
                Geometry(geometry_type=self.geometry_type, srid=self.srid, spatial_index=False)
            )
        return dialect.type_descriptor(Text()) if dialect is not None else Text()


class Region(Base):
    """Top-level territorial tenant boundary (e.g. Metro North, Coastal East)."""

    __tablename__ = "regions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    country_code: Mapped[str] = mapped_column(String(8), default="US", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    territories: Mapped[list[Territory]] = relationship("Territory", back_populates="region")


class Territory(Base):
    """Geofenced operational territory inside a region with GeoJSON + PostGIS polygon."""

    __tablename__ = "territories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="RESTRICT"), index=True, nullable=False)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    polygon_geojson: Mapped[str] = mapped_column(Text, nullable=False)
    boundary_geom: Mapped[Any | None] = mapped_column(PortableGeometry("POLYGON", 4326), nullable=True)
    center_lat: Mapped[float] = mapped_column(Float, nullable=False)
    center_lon: Mapped[float] = mapped_column(Float, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    region: Mapped[Region] = relationship("Region", back_populates="territories")


class User(Base):
    """Platform operator with decoupled RBAC role, scope FKs, revocation version, and soft-delete."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(140), nullable=False)
    email: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(
        SAEnum(Role, values_callable=lambda enum_cls: [e.value for e in enum_cls], native_enum=False),
        nullable=False,
    )
    region_id: Mapped[int | None] = mapped_column(ForeignKey("regions.id", ondelete="RESTRICT"), index=True, nullable=True)
    territory_id: Mapped[int | None] = mapped_column(ForeignKey("territories.id", ondelete="RESTRICT"), index=True, nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    region: Mapped[Region | None] = relationship("Region")
    territory: Mapped[Territory | None] = relationship("Territory")


class Outlet(Base):
    """Master catalog of commercial Points of Sale (PDV) & retail stores."""

    __tablename__ = "outlets"
    __table_args__ = (
        Index("ix_outlets_region_territory", "region_id", "territory_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outlet_code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    business_name: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    micro_location: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="RESTRICT"), index=True, nullable=False)
    territory_id: Mapped[int] = mapped_column(ForeignKey("territories.id", ondelete="RESTRICT"), index=True, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    location_geom: Mapped[Any | None] = mapped_column(PortableGeometry("POINT", 4326), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    pos_terminal_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    contact_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    contact_phone: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class DispatchRoute(Base):
    """Assigned field visit task dispatched by a supervisor to a field agent."""

    __tablename__ = "dispatch_routes"
    __table_args__ = (
        Index("ix_dispatch_routes_region_territory", "region_id", "territory_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="RESTRICT"), index=True, nullable=False)
    territory_id: Mapped[int] = mapped_column(ForeignKey("territories.id", ondelete="RESTRICT"), index=True, nullable=False)
    agent_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    supervisor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    outlet_id: Mapped[int | None] = mapped_column(ForeignKey("outlets.id", ondelete="SET NULL"), nullable=True)
    scheduled_date: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="assigned", nullable=False)
    priority: Mapped[str] = mapped_column(String(24), default="normal", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class FieldAudit(Base):
    """Offline-first field census / store audit submission synchronized via idempotent sync_uuid."""

    __tablename__ = "field_audits"
    __table_args__ = (
        Index("ix_field_audits_region_territory", "region_id", "territory_id"),
        Index("ix_field_audits_review_status", "review_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_uuid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    outlet_id: Mapped[int | None] = mapped_column(ForeignKey("outlets.id", ondelete="SET NULL"), nullable=True)
    outlet_code: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    business_name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    micro_location: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="RESTRICT"), index=True, nullable=False)
    territory_id: Mapped[int] = mapped_column(ForeignKey("territories.id", ondelete="RESTRICT"), index=True, nullable=False)
    agent_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    route_id: Mapped[int | None] = mapped_column(ForeignKey("dispatch_routes.id", ondelete="SET NULL"), nullable=True)

    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    location_geom: Mapped[Any | None] = mapped_column(PortableGeometry("POINT", 4326), nullable=True)
    gps_accuracy_m: Mapped[float] = mapped_column(Float, default=12.0, nullable=False)

    visit_status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    shelf_share_pct: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    stockout_alert: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pos_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    signage_compliant: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    photo_media_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    geofence_status: Mapped[str] = mapped_column(String(32), default="inside_territory", nullable=False)
    geofence_distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_status: Mapped[str] = mapped_column(String(32), default="quarantined", nullable=False)

    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TriageItem(Base):
    """Quarantine and anomaly review ticket automatically generated on field audit ingestion."""

    __tablename__ = "triage_items"
    __table_args__ = (
        Index("ix_triage_items_region_status", "region_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("field_audits.id", ondelete="CASCADE"), unique=True, nullable=False)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="RESTRICT"), index=True, nullable=False)
    territory_id: Mapped[int] = mapped_column(ForeignKey("territories.id", ondelete="RESTRICT"), index=True, nullable=False)
    priority: Mapped[str] = mapped_column(String(24), default="medium", nullable=False)
    alert_type: Mapped[str] = mapped_column(String(48), nullable=False)
    suggested_outlet_id: Mapped[int | None] = mapped_column(ForeignKey("outlets.id", ondelete="SET NULL"), nullable=True)
    similarity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SosAlert(Base):
    """Field safety panic alert with immutable safety history (ON DELETE RESTRICT on agent)."""

    __tablename__ = "sos_alerts"
    __table_args__ = (
        Index("ix_sos_alerts_region_status", "region_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="RESTRICT"), index=True, nullable=False)
    territory_id: Mapped[int] = mapped_column(ForeignKey("territories.id", ondelete="RESTRICT"), index=True, nullable=False)
    agent_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    gps_fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    message: Mapped[str] = mapped_column(String(280), default="Field safety assistance requested", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SupportTicket(Base):
    """Field-to-HQ tactical support ticket with full territorial isolation."""

    __tablename__ = "support_tickets"
    __table_args__ = (
        Index("ix_support_tickets_region_status", "region_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="RESTRICT"), index=True, nullable=False)
    territory_id: Mapped[int] = mapped_column(ForeignKey("territories.id", ondelete="RESTRICT"), index=True, nullable=False)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    audit_id: Mapped[int | None] = mapped_column(ForeignKey("field_audits.id", ondelete="SET NULL"), nullable=True)
    subject: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="open", nullable=False)
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reopen_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TicketMessage(Base):
    """Message timeline inside a SupportTicket."""

    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("support_tickets.id", ondelete="CASCADE"), index=True, nullable=False)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MediaAsset(Base):
    """Authenticated tenant-scoped evidence photo record."""

    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="RESTRICT"), index=True, nullable=False)
    territory_id: Mapped[int] = mapped_column(ForeignKey("territories.id", ondelete="RESTRICT"), index=True, nullable=False)
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    storage_filename: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ImportJob(Base):
    """Dedicated CSV/Catalog bulk ingestion job status table."""

    __tablename__ = "import_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="RESTRICT"), index=True, nullable=False)
    territory_id: Mapped[int] = mapped_column(ForeignKey("territories.id", ondelete="RESTRICT"), index=True, nullable=False)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    filename: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    rows_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """Append-only operational audit trail for security & compliance visibility."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_region_created", "region_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region_id: Mapped[int | None] = mapped_column(ForeignKey("regions.id", ondelete="SET NULL"), index=True, nullable=True)
    territory_id: Mapped[int | None] = mapped_column(ForeignKey("territories.id", ondelete="SET NULL"), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    username: Mapped[str] = mapped_column(String(64), default="anonymous", nullable=False)
    action: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    method: Mapped[str] = mapped_column(String(12), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    ip_address: Mapped[str] = mapped_column(String(64), default="unknown", nullable=False)
    user_agent: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    detail_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
