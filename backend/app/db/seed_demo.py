"""100% Synthetic Demo Dataset Seeder for Quantico Censo (Solstice Retail Distribution)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.rbac import Role
from app.core.security import hash_password
from app.db.models import (
    DispatchRoute,
    FieldAudit,
    Outlet,
    Region,
    SosAlert,
    SupportTicket,
    Territory,
    TicketMessage,
    TriageItem,
    User,
)


def _poly_geojson(ring: list[list[float]]) -> str:
    return json.dumps({"type": "Polygon", "coordinates": [ring]})


def _geom_polygon(db: Session, geojson_str: str):
    if db.bind and db.bind.dialect.name == "postgresql":
        return func.ST_SetSRID(func.ST_GeomFromGeoJSON(geojson_str), 4326)
    return geojson_str


def _geom_point(db: Session, lon: float, lat: float):
    if db.bind and db.bind.dialect.name == "postgresql":
        return func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
    return f"POINT({lon} {lat})"


SYNTHETIC_TERRITORIES = [
    # Region 1: Metro North — organic urban polygons aligned to avenues & waterfronts
    {
        "region_code": "REG-NORTH",
        "code": "TER-DOWNTOWN",
        "name": "Downtown Core",
        "center": (40.7185, -74.0005),
        "ring": [
            [-74.0175, 40.7035],
            [-74.0065, 40.7010],
            [-73.9920, 40.7068],
            [-73.9830, 40.7125],
            [-73.9845, 40.7245],
            [-73.9935, 40.7335],
            [-74.0085, 40.7330],
            [-74.0145, 40.7225],
            [-74.0175, 40.7035],
        ],
    },
    {
        "region_code": "REG-NORTH",
        "code": "TER-TECH",
        "name": "Innovation District",
        "center": (40.7185, -73.9595),
        "ring": [
            [-73.9770, 40.7045],
            [-73.9635, 40.7030],
            [-73.9465, 40.7095],
            [-73.9410, 40.7215],
            [-73.9475, 40.7328],
            [-73.9610, 40.7340],
            [-73.9745, 40.7265],
            [-73.9785, 40.7145],
            [-73.9770, 40.7045],
        ],
    },
    # Region 2: Coastal East — organic harbor & midtown district polygons
    {
        "region_code": "REG-EAST",
        "code": "TER-HARBOR",
        "name": "Harbor Strip",
        "center": (40.6835, -73.9970),
        "ring": [
            [-74.0160, 40.6730],
            [-74.0035, 40.6675],
            [-73.9875, 40.6705],
            [-73.9815, 40.6820],
            [-73.9840, 40.6955],
            [-73.9965, 40.6980],
            [-74.0105, 40.6925],
            [-74.0160, 40.6730],
        ],
    },
    {
        "region_code": "REG-EAST",
        "code": "TER-MIDTOWN",
        "name": "Midtown Commons",
        "center": (40.6815, -73.9595),
        "ring": [
            [-73.9755, 40.6700],
            [-73.9615, 40.6665],
            [-73.9455, 40.6705],
            [-73.9430, 40.6835],
            [-73.9495, 40.6960],
            [-73.9645, 40.6975],
            [-73.9750, 40.6880],
            [-73.9755, 40.6700],
        ],
    },
]

SYNTHETIC_OUTLETS = [
    ("OUT-1001", "Aurora Fresh Market", "supermarket", "114 Mercer Ave", "Ground Floor", "TER-DOWNTOWN", 40.712, -74.006, True),
    ("OUT-1002", "Vanguard Pharma Plus", "pharmacy", "220 Liberty Plaza", "Unit 102", "TER-DOWNTOWN", 40.718, -74.001, True),
    ("OUT-1003", "Cornerstone Express 24h", "convenience", "45 Cedar St", "Corner Suite", "TER-DOWNTOWN", 40.725, -73.992, True),
    ("OUT-1004", "Golden Crust Artisan Bakery", "bakery", "88 Fulton Blvd", "Local B", "TER-DOWNTOWN", 40.709, -73.988, False),
    ("OUT-1005", "Apex Hardware & Tools", "hardware", "310 Canal Way", "Bay 3", "TER-DOWNTOWN", 40.728, -74.011, True),
    ("OUT-1006", "PixelByte Electronics", "electronics", "19 Hudson Mall", "Level 2, Suite 14", "TER-TECH", 40.714, -73.968, True),
    ("OUT-1007", "Verde Organic Grocer", "supermarket", "410 Foundry Rd", "Main Hall", "TER-TECH", 40.723, -73.958, True),
    ("OUT-1008", "Pulse Quick Mart", "convenience", "77 Silicon Sq", "Kiosk 4", "TER-TECH", 40.729, -73.951, True),
    ("OUT-1009", "NovaCare Pharmacy", "pharmacy", "125 Innovation Pkwy", "Lobby West", "TER-TECH", 40.707, -73.972, True),
    ("OUT-1010", "Boreal Coffee & Pastries", "bakery", "52 Tech Walk", "Terrace 1", "TER-TECH", 40.719, -73.962, False),
    ("OUT-1011", "Pier 9 Marine Supply", "hardware", "800 Dockside Rd", "Warehouse A", "TER-HARBOR", 40.682, -74.008, True),
    ("OUT-1012", "Bayfront Supermarket", "supermarket", "92 Marina Blvd", "Anchor Store", "TER-HARBOR", 40.691, -73.997, True),
    ("OUT-1013", "Seabreeze Convenience", "convenience", "14 Lighthouse Ave", "Unit 2", "TER-HARBOR", 40.674, -73.991, True),
    ("OUT-1014", "Harbor Health Pharmacy", "pharmacy", "205 Quay St", "Suite 105", "TER-HARBOR", 40.686, -73.986, True),
    ("OUT-1015", "Midtown SuperSaver", "supermarket", "501 Commons Ave", "Plaza North", "TER-MIDTOWN", 40.684, -73.965, True),
    ("OUT-1016", "CircuitHub Retail", "electronics", "620 Grand Mall", "Floor 1, Unit 8", "TER-MIDTOWN", 40.693, -73.955, True),
    ("OUT-1017", "Sunrise Corner Bakery", "bakery", "38 Oak Terrace", "Storefront", "TER-MIDTOWN", 40.676, -73.969, False),
    ("OUT-1018", "MetroFix Hardware", "hardware", "712 Parkside Rd", "Suite C", "TER-MIDTOWN", 40.671, -73.949, True),
]


def seed_demo_data(db: Session) -> dict[str, int]:
    """Idempotently seed synthetic regions, territories, users, outlets, routes, and audits."""
    existing = db.execute(select(Region.id).limit(1)).scalar_one_or_none()
    if existing is not None:
        return {"seeded": 0}

    now = datetime.now(timezone.utc)
    reg_north = Region(code="REG-NORTH", name="Metro North", country_code="US", is_active=True)
    reg_east = Region(code="REG-EAST", name="Coastal East", country_code="US", is_active=True)
    db.add_all([reg_north, reg_east])
    db.flush()

    region_map = {"REG-NORTH": reg_north, "REG-EAST": reg_east}
    territory_map: dict[str, Territory] = {}

    for spec in SYNTHETIC_TERRITORIES:
        poly = _poly_geojson(spec["ring"])
        center_lat, center_lon = spec["center"]
        region = region_map[spec["region_code"]]
        territory = Territory(
            region_id=region.id,
            code=spec["code"],
            name=spec["name"],
            polygon_geojson=poly,
            boundary_geom=_geom_polygon(db, poly),
            center_lat=center_lat,
            center_lon=center_lon,
            is_active=True,
        )
        db.add(territory)
        db.flush()
        territory_map[spec["code"]] = territory

    # Demo credentials (clearly documented synthetic demo accounts)
    demo_pw = hash_password("QuanticoDemo2026!")
    users = [
        User(
            username="admin_hq",
            full_name="Elena Vance (HQ Admin)",
            email="admin@solstice-demo.example",
            hashed_password=demo_pw,
            role=Role.SUPER_ADMIN,
            region_id=None,
            territory_id=None,
        ),
        User(
            username="manager_north",
            full_name="Marcus Reid (Metro North Manager)",
            email="manager.north@solstice-demo.example",
            hashed_password=demo_pw,
            role=Role.REGIONAL_MANAGER,
            region_id=reg_north.id,
            territory_id=None,
        ),
        User(
            username="manager_east",
            full_name="Sofia Morales (Coastal East Manager)",
            email="manager.east@solstice-demo.example",
            hashed_password=demo_pw,
            role=Role.REGIONAL_MANAGER,
            region_id=reg_east.id,
            territory_id=None,
        ),
        User(
            username="sup_downtown",
            full_name="Daniela Ortega (Downtown Supervisor)",
            email="sup.downtown@solstice-demo.example",
            hashed_password=demo_pw,
            role=Role.SUPERVISOR,
            region_id=reg_north.id,
            territory_id=territory_map["TER-DOWNTOWN"].id,
        ),
        User(
            username="agent_downtown",
            full_name="Carlos Mendez (Field Agent)",
            email="agent.downtown@solstice-demo.example",
            hashed_password=demo_pw,
            role=Role.FIELD_AGENT,
            region_id=reg_north.id,
            territory_id=territory_map["TER-DOWNTOWN"].id,
        ),
        User(
            username="agent_tech",
            full_name="Mateo Rivas (Field Agent)",
            email="agent.tech@solstice-demo.example",
            hashed_password=demo_pw,
            role=Role.FIELD_AGENT,
            region_id=reg_north.id,
            territory_id=territory_map["TER-TECH"].id,
        ),
        User(
            username="agent_harbor",
            full_name="Lucia Navarro (Field Agent)",
            email="agent.harbor@solstice-demo.example",
            hashed_password=demo_pw,
            role=Role.FIELD_AGENT,
            region_id=reg_east.id,
            territory_id=territory_map["TER-HARBOR"].id,
        ),
        User(
            username="agent_midtown",
            full_name="Valeria Paredes (Field Agent)",
            email="agent.midtown@solstice-demo.example",
            hashed_password=demo_pw,
            role=Role.FIELD_AGENT,
            region_id=reg_east.id,
            territory_id=territory_map["TER-MIDTOWN"].id,
        ),
        User(
            username="viewer_north",
            full_name="Andres Silva (Read-Only Analyst)",
            email="viewer.north@solstice-demo.example",
            hashed_password=demo_pw,
            role=Role.VIEWER,
            region_id=reg_north.id,
            territory_id=None,
        ),
    ]
    db.add_all(users)
    db.flush()
    user_map = {u.username: u for u in users}

    outlet_map: dict[str, Outlet] = {}
    for code, name, cat, addr, micro, ter_code, lat, lon, pos_ok in SYNTHETIC_OUTLETS:
        ter = territory_map[ter_code]
        outlet = Outlet(
            outlet_code=code,
            business_name=name,
            category=cat,
            address=addr,
            micro_location=micro,
            region_id=ter.region_id,
            territory_id=ter.id,
            latitude=lat,
            longitude=lon,
            location_geom=_geom_point(db, lon, lat),
            status="active",
            pos_terminal_active=pos_ok,
            contact_name="Store Manager",
            contact_phone="+1-555-0100",
        )
        db.add(outlet)
        db.flush()
        outlet_map[code] = outlet

    today_str = now.strftime("%Y-%m-%d")
    routes = [
        DispatchRoute(
            code="RT-2026-001",
            title="Morning Retail Audit — Aurora Fresh Market",
            region_id=reg_north.id,
            territory_id=territory_map["TER-DOWNTOWN"].id,
            agent_id=user_map["agent_downtown"].id,
            supervisor_id=user_map["sup_downtown"].id,
            outlet_id=outlet_map["OUT-1001"].id,
            scheduled_date=today_str,
            status="assigned",
            priority="high",
            notes="Verify seasonal beverage shelf share and POS terminal connectivity.",
        ),
        DispatchRoute(
            code="RT-2026-002",
            title="POS Signage Verification — Vanguard Pharma Plus",
            region_id=reg_north.id,
            territory_id=territory_map["TER-DOWNTOWN"].id,
            agent_id=user_map["agent_downtown"].id,
            supervisor_id=user_map["sup_downtown"].id,
            outlet_id=outlet_map["OUT-1002"].id,
            scheduled_date=today_str,
            status="assigned",
            priority="normal",
            notes="Check new promotional display at counter.",
        ),
        DispatchRoute(
            code="RT-2026-003",
            title="Harbor Stockout Check — Bayfront Supermarket",
            region_id=reg_east.id,
            territory_id=territory_map["TER-HARBOR"].id,
            agent_id=user_map["agent_harbor"].id,
            supervisor_id=user_map["manager_east"].id,
            outlet_id=outlet_map["OUT-1012"].id,
            scheduled_date=today_str,
            status="in_progress",
            priority="urgent",
            notes="Audit cold-chain aisle replenishment.",
        ),
    ]
    db.add_all(routes)
    db.flush()

    # Seed realistic initial field audits (1 approved, 1 quarantined stockout, 1 quarantined geofence drift)
    audits = [
        FieldAudit(
            sync_uuid=str(uuid.uuid5(uuid.NAMESPACE_DNS, "quantico-censo-seed-1")),
            outlet_id=outlet_map["OUT-1003"].id,
            outlet_code="OUT-1003",
            business_name="Cornerstone Express 24h",
            category="convenience",
            address="45 Cedar St",
            micro_location="Corner Suite",
            region_id=reg_north.id,
            territory_id=territory_map["TER-DOWNTOWN"].id,
            agent_id=user_map["agent_downtown"].id,
            latitude=40.7251,
            longitude=-73.9919,
            location_geom=_geom_point(db, -73.9919, 40.7251),
            gps_accuracy_m=9.5,
            visit_status="open",
            shelf_share_pct=78,
            stockout_alert=False,
            pos_active=True,
            signage_compliant=True,
            notes="Full stock, promotional end-cap visible.",
            geofence_status="inside_territory",
            geofence_distance_m=14.2,
            review_status="approved",
            captured_at=now - timedelta(hours=3),
            synced_at=now - timedelta(hours=3),
        ),
        FieldAudit(
            sync_uuid=str(uuid.uuid5(uuid.NAMESPACE_DNS, "quantico-censo-seed-2")),
            outlet_id=outlet_map["OUT-1004"].id,
            outlet_code="OUT-1004",
            business_name="Golden Crust Artisan Bakery",
            category="bakery",
            address="88 Fulton Blvd",
            micro_location="Local B",
            region_id=reg_north.id,
            territory_id=territory_map["TER-DOWNTOWN"].id,
            agent_id=user_map["agent_downtown"].id,
            latitude=40.7092,
            longitude=-73.9881,
            location_geom=_geom_point(db, -73.9881, 40.7092),
            gps_accuracy_m=11.0,
            visit_status="open",
            shelf_share_pct=22,
            stockout_alert=True,
            pos_active=False,
            signage_compliant=True,
            notes="Critical stockout on flagship SKU; POS terminal offline.",
            geofence_status="inside_territory",
            geofence_distance_m=23.5,
            review_status="quarantined",
            captured_at=now - timedelta(hours=1, minutes=20),
            synced_at=now - timedelta(hours=1, minutes=15),
        ),
        FieldAudit(
            sync_uuid=str(uuid.uuid5(uuid.NAMESPACE_DNS, "quantico-censo-seed-3")),
            outlet_id=None,
            outlet_code="PROSPECT-901",
            business_name="Harbor Pop-Up Kiosk",
            category="convenience",
            address="Pier 12 Walkway",
            micro_location="Outdoor Stand",
            region_id=reg_east.id,
            territory_id=territory_map["TER-HARBOR"].id,
            agent_id=user_map["agent_harbor"].id,
            latitude=40.688,
            longitude=-73.972,  # Deliberately outside TER-HARBOR east boundary (-73.980)
            location_geom=_geom_point(db, -73.972, 40.688),
            gps_accuracy_m=18.0,
            visit_status="unmapped_prospect",
            shelf_share_pct=0,
            stockout_alert=False,
            pos_active=True,
            signage_compliant=False,
            notes="Newly opened high-footfall kiosk near border.",
            geofence_status="outside_territory",
            geofence_distance_m=675.0,
            review_status="quarantined",
            captured_at=now - timedelta(minutes=45),
            synced_at=now - timedelta(minutes=40),
        ),
    ]
    db.add_all(audits)
    db.flush()

    triage_items = [
        TriageItem(
            audit_id=audits[1].id,
            region_id=reg_north.id,
            territory_id=territory_map["TER-DOWNTOWN"].id,
            priority="high",
            alert_type="stockout_critical",
            suggested_outlet_id=outlet_map["OUT-1004"].id,
            similarity_score=1.0,
            status="open",
        ),
        TriageItem(
            audit_id=audits[2].id,
            region_id=reg_east.id,
            territory_id=territory_map["TER-HARBOR"].id,
            priority="critical",
            alert_type="out_of_territory",
            suggested_outlet_id=None,
            similarity_score=None,
            status="open",
        ),
    ]
    db.add_all(triage_items)

    # Seed one resolved historical SOS and one active support ticket
    db.add(
        SosAlert(
            region_id=reg_north.id,
            territory_id=territory_map["TER-DOWNTOWN"].id,
            agent_id=user_map["agent_downtown"].id,
            latitude=40.715,
            longitude=-74.003,
            gps_fallback=False,
            message="Test drill — perimeter check completed safely.",
            status="resolved",
            resolved_by_id=user_map["sup_downtown"].id,
            resolution_note="Drill acknowledged and verified via voice check.",
            created_at=now - timedelta(hours=5),
            resolved_at=now - timedelta(hours=4, minutes=52),
        )
    )

    ticket = SupportTicket(
        region_id=reg_north.id,
        territory_id=territory_map["TER-DOWNTOWN"].id,
        created_by_id=user_map["agent_downtown"].id,
        assigned_to_id=user_map["sup_downtown"].id,
        audit_id=audits[1].id,
        subject="POS terminal reboot needed at Golden Crust",
        status="claimed",
    )
    db.add(ticket)
    db.flush()
    db.add(
        TicketMessage(
            ticket_id=ticket.id,
            sender_id=user_map["agent_downtown"].id,
            body="Merchant reported POS screen frozen during checkout audit.",
        )
    )

    db.commit()
    return {
        "seeded": 1,
        "regions": 2,
        "territories": len(territory_map),
        "users": len(users),
        "outlets": len(outlet_map),
        "routes": len(routes),
        "audits": len(audits),
    }
