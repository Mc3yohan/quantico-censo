"""Geospatial anti-fraud geofencing using native PostGIS ST_Covers with Shapely fallback."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from shapely.geometry import Point, shape
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Outlet, Territory


@dataclass(frozen=True, slots=True)
class GeofenceEvaluation:
    """Result of evaluating a field GPS capture against its territory polygon and reference outlet."""

    status: str  # inside_territory | outside_territory | gps_drift | unknown
    distance_m: float | None
    inside_polygon: bool


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate great-circle distance in meters between two WGS84 coordinates."""
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * radius * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def _evaluate_polygon_postgis(
    db: Session,
    territory_id: int,
    latitude: float,
    longitude: float,
) -> tuple[bool, float]:
    """Execute native PostGIS ST_Covers + geography ST_Distance in PostgreSQL."""
    row = db.execute(
        text(
            """
            SELECT
                ST_Covers(
                    boundary_geom,
                    ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
                ) AS inside,
                ST_Distance(
                    boundary_geom::geography,
                    ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography
                ) AS dist_m
            FROM territories
            WHERE id = :territory_id AND boundary_geom IS NOT NULL
            """
        ),
        {"lon": longitude, "lat": latitude, "territory_id": territory_id},
    ).fetchone()
    if row is None:
        return False, 0.0
    return bool(row.inside), round(float(row.dist_m or 0.0), 2)


def _evaluate_polygon_shapely(
    territory: Territory,
    latitude: float,
    longitude: float,
) -> tuple[bool, float]:
    """Evaluate GeoJSON polygon containment using Shapely (for SQLite tests and fallback)."""
    geom = shape(json.loads(territory.polygon_geojson))
    pt = Point(longitude, latitude)
    inside = bool(geom.covers(pt))
    if inside:
        return True, 0.0

    # Approximate meters from nearest point on polygon boundary
    nearest_lon, nearest_lat = geom.exterior.interpolate(geom.exterior.project(pt)).coords[0]
    dist_m = haversine_meters(latitude, longitude, nearest_lat, nearest_lon)
    return False, round(dist_m, 2)


def evaluate_geofence(
    db: Session,
    *,
    territory: Territory | None,
    latitude: float,
    longitude: float,
    reference_outlet: Outlet | None = None,
) -> GeofenceEvaluation:
    """Validate whether a field GPS point lies inside its territory and within drift tolerance."""
    if territory is None:
        return GeofenceEvaluation(status="unknown", distance_m=None, inside_polygon=False)

    is_postgres = bool(db.bind and db.bind.dialect.name == "postgresql")
    if is_postgres and territory.boundary_geom is not None:
        inside, poly_dist_m = _evaluate_polygon_postgis(db, territory.id, latitude, longitude)
    else:
        inside, poly_dist_m = _evaluate_polygon_shapely(territory, latitude, longitude)

    if not inside:
        return GeofenceEvaluation(
            status="outside_territory",
            distance_m=poly_dist_m,
            inside_polygon=False,
        )

    if reference_outlet is not None:
        outlet_dist = round(
            haversine_meters(latitude, longitude, reference_outlet.latitude, reference_outlet.longitude),
            2,
        )
        drift_threshold = get_settings().geofence_drift_meters
        if outlet_dist > drift_threshold:
            return GeofenceEvaluation(
                status="gps_drift",
                distance_m=outlet_dist,
                inside_polygon=True,
            )
        return GeofenceEvaluation(
            status="inside_territory",
            distance_m=outlet_dist,
            inside_polygon=True,
        )

    center_dist = round(
        haversine_meters(latitude, longitude, territory.center_lat, territory.center_lon),
        2,
    )
    return GeofenceEvaluation(
        status="inside_territory",
        distance_m=center_dist,
        inside_polygon=True,
    )
