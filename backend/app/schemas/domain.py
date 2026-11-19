"""Strict Pydantic v2 request/response schemas (zero Any types, mass-assignment hardened)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.rbac import Role


class StrictInput(BaseModel):
    """Base input model rejecting unexpected fields to prevent mass assignment."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StrictOutput(BaseModel):
    """Base ORM-compatible response model."""

    model_config = ConfigDict(from_attributes=True)


# ── Auth & Identity ──────────────────────────────────────────────────────────


EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def _validate_bcrypt_byte_limit(value: str | None) -> str | None:
    if value is not None and len(value.encode("utf-8")) > 72:
        raise ValueError("Password exceeds 72 UTF-8 bytes maximum.")
    return value


class LoginRequest(StrictInput):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def check_password_bytes(cls, v: str) -> str:
        _validate_bcrypt_byte_limit(v)
        return v


class UserProfile(StrictOutput):
    id: int
    username: str
    full_name: str
    email: str
    role: Role
    region_id: int | None
    territory_id: int | None
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserProfile


class WsTicketResponse(BaseModel):
    ticket: str
    expires_in: int
    channels: list[str]


# ── Hierarchy (Regions & Territories) ────────────────────────────────────────


class TerritoryResponse(StrictOutput):
    id: int
    region_id: int
    code: str
    name: str
    polygon_geojson: str
    center_lat: float
    center_lon: float
    is_active: bool


class RegionResponse(StrictOutput):
    id: int
    code: str
    name: str
    country_code: str
    is_active: bool
    territories: list[TerritoryResponse] = Field(default_factory=list)


# ── Users CRUD (RBAC Hardened) ───────────────────────────────────────────────


class UserCreateRequest(StrictInput):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    full_name: str = Field(min_length=2, max_length=140)
    email: str = Field(min_length=5, max_length=160, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=10, max_length=72)
    role: Role
    region_id: int | None = None
    territory_id: int | None = None

    @field_validator("password")
    @classmethod
    def check_password_bytes(cls, v: str) -> str:
        _validate_bcrypt_byte_limit(v)
        return v


class UserUpdateRequest(StrictInput):
    full_name: str | None = Field(default=None, min_length=2, max_length=140)
    email: str | None = Field(default=None, min_length=5, max_length=160, pattern=EMAIL_PATTERN)
    password: str | None = Field(default=None, min_length=10, max_length=72)
    role: Role | None = None
    region_id: int | None = None
    territory_id: int | None = None
    is_active: bool | None = None

    @field_validator("password")
    @classmethod
    def check_password_bytes(cls, v: str | None) -> str | None:
        return _validate_bcrypt_byte_limit(v)


# ── Commercial Outlets (PDV Master) ──────────────────────────────────────────


OutletCategory = Literal[
    "supermarket",
    "pharmacy",
    "convenience",
    "hardware",
    "bakery",
    "electronics",
    "other",
]
OutletStatus = Literal["active", "prospect", "inactive"]


class OutletCreateRequest(StrictInput):
    outlet_code: str = Field(min_length=3, max_length=32, pattern=r"^[A-Z0-9-]+$")
    business_name: str = Field(min_length=2, max_length=160)
    category: OutletCategory
    address: str = Field(min_length=3, max_length=255)
    micro_location: str = Field(default="", max_length=120)
    region_id: int | None = None
    territory_id: int | None = None
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    status: OutletStatus = "active"
    pos_terminal_active: bool = True
    contact_name: str = Field(default="", max_length=120)
    contact_phone: str = Field(default="", max_length=40)


class OutletResponse(StrictOutput):
    id: int
    outlet_code: str
    business_name: str
    category: str
    address: str
    micro_location: str
    region_id: int
    territory_id: int
    latitude: float
    longitude: float
    status: str
    pos_terminal_active: bool
    contact_name: str
    contact_phone: str
    created_at: datetime


# ── Offline-First Field Audits & Sync ────────────────────────────────────────


VisitStatus = Literal["open", "closed_temp", "relocated", "unmapped_prospect"]
ReviewStatus = Literal["quarantined", "approved", "rejected", "rework_requested"]
GeofenceStatus = Literal["inside_territory", "outside_territory", "gps_drift", "unknown"]


class FieldAuditCreateRequest(StrictInput):
    sync_uuid: str = Field(min_length=8, max_length=64)
    outlet_id: int | None = None
    outlet_code: str = Field(min_length=3, max_length=32)
    business_name: str = Field(min_length=2, max_length=160)
    category: OutletCategory
    address: str = Field(min_length=3, max_length=255)
    micro_location: str = Field(default="", max_length=120)
    route_id: int | None = None
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    gps_accuracy_m: float = Field(default=12.0, ge=0.0, le=5000.0)
    visit_status: VisitStatus = "open"
    shelf_share_pct: int = Field(default=50, ge=0, le=100)
    stockout_alert: bool = False
    pos_active: bool = True
    signage_compliant: bool = True
    notes: str = Field(default="", max_length=1000)
    photo_media_id: str | None = Field(default=None, max_length=64)
    captured_at: datetime | None = None


class FieldAuditBatchRequest(StrictInput):
    items: list[FieldAuditCreateRequest] = Field(min_length=1, max_length=25)


class FieldAuditResponse(StrictOutput):
    id: int
    sync_uuid: str
    outlet_id: int | None
    outlet_code: str
    business_name: str
    category: str
    address: str
    micro_location: str
    region_id: int
    territory_id: int
    agent_id: int
    route_id: int | None
    latitude: float
    longitude: float
    gps_accuracy_m: float
    visit_status: str
    shelf_share_pct: int
    stockout_alert: bool
    pos_active: bool
    signage_compliant: bool
    notes: str
    photo_media_id: str | None
    geofence_status: str
    geofence_distance_m: float | None
    review_status: str
    captured_at: datetime
    synced_at: datetime


class AuditSyncResultItem(BaseModel):
    sync_uuid: str
    status: Literal["created", "idempotent_duplicate", "conflict", "error"]
    audit_id: int | None = None
    geofence_status: str | None = None
    review_status: str | None = None
    detail: str = ""


class FieldAuditBatchResponse(BaseModel):
    processed: int
    created: int
    duplicates: int
    conflicts: int
    results: list[AuditSyncResultItem]


class MapPinResponse(BaseModel):
    id: int
    kind: Literal["outlet", "audit", "sos"]
    code: str
    label: str
    category: str
    region_id: int
    territory_id: int
    latitude: float
    longitude: float
    status: str
    geofence_status: str | None = None
    stockout_alert: bool = False


class MapSnapshotResponse(BaseModel):
    regions: list[RegionResponse]
    pins: list[MapPinResponse]
    generated_at: datetime
    cached: bool = False


# ── Triage & Quarantine ──────────────────────────────────────────────────────


class TriageItemResponse(StrictOutput):
    id: int
    audit_id: int
    region_id: int
    territory_id: int
    priority: str
    alert_type: str
    suggested_outlet_id: int | None
    similarity_score: float | None
    status: str
    reviewer_id: int | None
    resolution_note: str | None
    created_at: datetime
    resolved_at: datetime | None
    business_name: str = ""
    outlet_code: str = ""
    geofence_status: str = ""
    latitude: float = 0.0
    longitude: float = 0.0


class TriageResolveRequest(StrictInput):
    decision: Literal["approved", "rejected", "rework_requested"]
    resolution_note: str = Field(min_length=3, max_length=500)


class PaginatedTriageResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[TriageItemResponse]


# ── Dispatch Routes ──────────────────────────────────────────────────────────


RouteStatus = Literal["assigned", "in_progress", "completed", "revoked"]
RoutePriority = Literal["normal", "high", "urgent"]


class DispatchRouteCreateRequest(StrictInput):
    title: str = Field(min_length=3, max_length=160)
    agent_id: int = Field(gt=0)
    outlet_id: int | None = Field(default=None, gt=0)
    scheduled_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    priority: RoutePriority = "normal"
    notes: str = Field(default="", max_length=500)


class DispatchRouteReassignRequest(StrictInput):
    agent_id: int = Field(gt=0)
    notes: str | None = Field(default=None, max_length=500)


class DispatchRouteStatusRequest(StrictInput):
    status: RouteStatus


class DispatchRouteResponse(StrictOutput):
    id: int
    code: str
    title: str
    region_id: int
    territory_id: int
    agent_id: int
    supervisor_id: int
    outlet_id: int | None
    scheduled_date: str
    status: str
    priority: str
    notes: str
    created_at: datetime
    updated_at: datetime


# ── SOS Panic Alerts ─────────────────────────────────────────────────────────


class SosTriggerRequest(StrictInput):
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    gps_fallback: bool = False
    message: str = Field(default="Field safety assistance requested", min_length=3, max_length=280)
    territory_id: int | None = Field(default=None, gt=0)


class SosResolveRequest(StrictInput):
    resolution_note: str = Field(min_length=3, max_length=500)


class SosAlertResponse(StrictOutput):
    id: int
    region_id: int
    territory_id: int
    agent_id: int
    agent_name: str = ""
    latitude: float
    longitude: float
    gps_fallback: bool
    message: str
    status: str
    resolved_by_id: int | None
    resolution_note: str | None
    created_at: datetime
    resolved_at: datetime | None


# ── Tactical Support Tickets ─────────────────────────────────────────────────


class TicketCreateRequest(StrictInput):
    subject: str = Field(min_length=3, max_length=160)
    body: str = Field(min_length=1, max_length=1000)
    audit_id: int | None = Field(default=None, gt=0)
    territory_id: int | None = Field(default=None, gt=0)


class TicketMessageCreateRequest(StrictInput):
    body: str = Field(min_length=1, max_length=1000)


class TicketCloseRequest(StrictInput):
    resolution_note: str = Field(min_length=3, max_length=500)


class TicketMessageResponse(StrictOutput):
    id: int
    ticket_id: int
    sender_id: int
    sender_name: str = ""
    body: str
    created_at: datetime


class SupportTicketResponse(StrictOutput):
    id: int
    region_id: int
    territory_id: int
    created_by_id: int
    assigned_to_id: int | None
    audit_id: int | None
    subject: str
    status: str
    closed_by_id: int | None
    resolution_note: str | None
    reopen_count: int
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


# ── Media, Imports, Dashboard & Audit Logs ───────────────────────────────────


class MediaUploadResponse(StrictOutput):
    id: str
    content_type: str
    size_bytes: int
    sha256: str
    url: str
    created_at: datetime


class ImportJobResponse(StrictOutput):
    id: str
    region_id: int
    territory_id: int
    created_by_id: int
    filename: str
    status: str
    rows_total: int
    rows_created: int
    rows_updated: int
    rows_rejected: int
    error_summary: str | None
    created_at: datetime
    completed_at: datetime | None


class DashboardKpiResponse(BaseModel):
    total_outlets: int
    total_audits: int
    approved_audits: int
    quarantined_audits: int
    open_triage: int
    active_sos: int
    assigned_routes: int
    stockout_alerts: int
    avg_shelf_share_pct: float
    geofence_compliance_pct: float
    server_role: str


class AuditLogResponse(StrictOutput):
    id: int
    region_id: int | None
    territory_id: int | None
    user_id: int | None
    username: str
    action: str
    method: str
    path: str
    status_code: int
    ip_address: str
    user_agent: str
    created_at: datetime

    @field_validator("user_agent", mode="before")
    @classmethod
    def _truncate_ua(cls, value: str | None) -> str:
        return (value or "")[:255]
