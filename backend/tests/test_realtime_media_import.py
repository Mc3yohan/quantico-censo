"""Zero-Trust WebSockets, Redis Pub/Sub isolation, hardened media upload, and CSV import tests."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.websockets import WebSocketDisconnect

from app.db.models import Territory


def test_websocket_rejects_missing_invalid_reused_tickets_and_bad_origin(
    client: TestClient,
    auth_headers,
) -> None:
    # 1) Missing ticket -> fail-closed 4401 (never falls back to global channel)
    with pytest.raises(WebSocketDisconnect) as exc_missing:
        with client.websocket_connect("/api/v1/ws/alerts"):
            pass
    assert exc_missing.value.code == 4401

    # 2) Disallowed Origin -> 4403
    headers = auth_headers("manager_north")
    ticket = client.post("/api/v1/auth/ws-ticket", headers=headers).json()["ticket"]
    with pytest.raises(WebSocketDisconnect) as exc_origin:
        with client.websocket_connect(
            f"/api/v1/ws/alerts?ticket={ticket}",
            headers={"origin": "https://evil-attacker.example"},
        ):
            pass
    assert exc_origin.value.code == 4403

    # 3) Valid ticket succeeds once, then reuse is rejected (4401)
    with client.websocket_connect(
        f"/api/v1/ws/alerts?ticket={ticket}",
        headers={"origin": "http://localhost:8095"},
    ) as ws:
        hello = ws.receive_json()
        assert hello["event"] == "CONNECTED"
        assert hello["region_id"] is not None

    with pytest.raises(WebSocketDisconnect) as exc_reuse:
        with client.websocket_connect(f"/api/v1/ws/alerts?ticket={ticket}"):
            pass
    assert exc_reuse.value.code == 4401


def test_realtime_sos_pubsub_delivers_within_region_and_isolates_other_regions(
    client: TestClient,
    auth_headers,
) -> None:
    north_headers = auth_headers("manager_north")
    downtown_agent_headers = auth_headers("agent_downtown")
    harbor_agent_headers = auth_headers("agent_harbor")

    ticket = client.post("/api/v1/auth/ws-ticket", headers=north_headers).json()["ticket"]

    with client.websocket_connect(f"/api/v1/ws/alerts?ticket={ticket}") as ws:
        hello = ws.receive_json()
        assert hello["event"] == "CONNECTED"

        # Coastal East agent triggers SOS -> must NOT appear on Metro North socket
        east_sos = client.post(
            "/api/v1/sos/trigger",
            headers=harbor_agent_headers,
            json={"latitude": 40.681, "longitude": -74.002, "message": "Coastal East test"},
        )
        assert east_sos.status_code == 201

        # Metro North agent triggers SOS -> MUST arrive immediately on Metro North socket
        north_sos = client.post(
            "/api/v1/sos/trigger",
            headers=downtown_agent_headers,
            json={"latitude": 40.713, "longitude": -74.004, "message": "Downtown assistance"},
        )
        assert north_sos.status_code == 201
        north_sos_id = north_sos.json()["id"]

        msg = ws.receive_json()
        assert msg["event"] == "SOS_TRIGGERED"
        assert msg["alert_id"] == north_sos_id
        assert msg["message"] == "Downtown assistance"

        # Resolve SOS -> MUST broadcast SOS_RESOLVED
        resolved = client.post(
            f"/api/v1/sos/{north_sos_id}/resolve",
            headers=north_headers,
            json={"resolution_note": "Support unit arrived on site."},
        )
        assert resolved.status_code == 200
        resolved_msg = ws.receive_json()
        assert resolved_msg["event"] == "SOS_RESOLVED"
        assert resolved_msg["alert_id"] == north_sos_id


def test_media_upload_rejects_fake_bytes_and_enforces_tenant_auth_on_download(
    client: TestClient,
    auth_headers,
) -> None:
    downtown_headers = auth_headers("agent_downtown")
    east_headers = auth_headers("manager_east")

    # Reject arbitrary non-image bytes
    bad = client.post(
        "/api/v1/media/photos",
        headers=downtown_headers,
        files={"file": ("shell.jpg", b"not-a-real-image-payload", "image/jpeg")},
    )
    assert bad.status_code == 415

    # Generate valid in-memory PNG with Pillow
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color=(56, 189, 248)).save(buf, format="PNG")
    good = client.post(
        "/api/v1/media/photos",
        headers=downtown_headers,
        files={"file": ("shelf.png", buf.getvalue(), "image/png")},
    )
    assert good.status_code == 201
    media_url = good.json()["url"]
    assert good.json()["content_type"] == "image/webp"

    # Unauthenticated download blocked (401)
    assert client.get(media_url).status_code == 401
    # Cross-region download blocked (403)
    assert client.get(media_url, headers=east_headers).status_code == 403
    # Same-territory download succeeds (200)
    ok_dl = client.get(media_url, headers=downtown_headers)
    assert ok_dl.status_code == 200
    assert ok_dl.headers["content-type"] == "image/webp"


def test_csv_import_neutralizes_formula_injection_and_path_traversal(
    client: TestClient,
    db_factory: sessionmaker[Session],
    auth_headers,
) -> None:
    sup_headers = auth_headers("sup_downtown")
    east_headers = auth_headers("manager_east")

    db = db_factory()
    try:
        ter = db.execute(select(Territory).where(Territory.code == "TER-DOWNTOWN")).scalar_one()
        ter_id = ter.id
    finally:
        db.close()

    csv_content = (
        "outlet_code,business_name,category,address,micro_location,latitude,longitude\n"
        "OUT-2001,=CMD|' /C calc'!A0,supermarket,+123 Main St,@Unit 5,40.715,-74.002\n"
        "OUT-1012,Cross Territory Hijack,supermarket,92 Marina Blvd,Anchor,40.691,-73.997\n"
        "BAD,X,other,A,B,999,999\n"
    ).encode("utf-8")

    resp = client.post(
        f"/api/v1/outlets/import?territory_id={ter_id}",
        headers=sup_headers,
        files={"file": ("../../etc/passwd.csv", csv_content, "text/csv")},
    )
    assert resp.status_code == 201
    job = resp.json()
    assert job["filename"] == "passwd.csv"
    assert job["rows_total"] == 3
    assert job["rows_created"] == 1
    assert job["rows_rejected"] == 2

    # Verify formula prefix was neutralized on stored outlet
    outlets = client.get("/api/v1/outlets?q=OUT-2001", headers=sup_headers).json()
    assert len(outlets) == 1
    assert not outlets[0]["business_name"].startswith(("=", "+", "-", "@"))
    assert not outlets[0]["address"].startswith(("=", "+", "-", "@"))

    # Cross-region manager cannot read Downtown's import job status
    assert client.get(f"/api/v1/outlets/imports/{job['id']}", headers=east_headers).status_code == 403


def test_horizontal_field_agent_media_download_and_attach_blocked(
    client: TestClient,
    db_factory: sessionmaker[Session],
    auth_headers,
) -> None:
    admin_headers = auth_headers("admin_hq")
    agent1_headers = auth_headers("agent_downtown")

    # Create a second field agent in the exact same territory (TER-DOWNTOWN)
    db = db_factory()
    try:
        ter = db.execute(select(Territory).where(Territory.code == "TER-DOWNTOWN")).scalar_one()
        reg_id, ter_id = ter.region_id, ter.id
    finally:
        db.close()

    create_resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": "agent_downtown_2",
            "full_name": "Peer Field Agent",
            "email": "agent.downtown2@solstice-demo.example",
            "password": "QuanticoDemo2026!",
            "role": "field_agent",
            "region_id": reg_id,
            "territory_id": ter_id,
        },
    )
    assert create_resp.status_code == 201
    agent2_headers = auth_headers("agent_downtown_2")

    buf = io.BytesIO()
    Image.new("RGB", (48, 48), color=(0, 123, 255)).save(buf, format="PNG")
    up = client.post(
        "/api/v1/media/photos",
        headers=agent1_headers,
        files={"file": ("storefront.png", buf.getvalue(), "image/png")},
    )
    assert up.status_code == 201
    media_id = up.json()["id"]
    media_url = up.json()["url"]

    # Peer agent in the SAME territory MUST be blocked from downloading agent1's evidence photo
    assert client.get(media_url, headers=agent2_headers).status_code == 403

    # Peer agent MUST also be blocked from attaching agent1's photo_media_id to their audit
    attach = client.post(
        "/api/v1/audits",
        headers=agent2_headers,
        json={
            "sync_uuid": "peer-media-attach-test-001",
            "outlet_code": "OUT-1001",
            "business_name": "Aurora Fresh Market",
            "category": "supermarket",
            "address": "114 Mercer Ave",
            "latitude": 40.712,
            "longitude": -74.006,
            "photo_media_id": media_id,
        },
    )
    assert attach.status_code == 400


def test_csv_import_same_file_duplicate_upsert_and_create_outlet_409(
    client: TestClient,
    db_factory: sessionmaker[Session],
    auth_headers,
) -> None:
    sup_headers = auth_headers("sup_downtown")
    db = db_factory()
    try:
        ter = db.execute(select(Territory).where(Territory.code == "TER-DOWNTOWN")).scalar_one()
        ter_id = ter.id
    finally:
        db.close()

    csv_content = (
        " outlet_code , business_name , category , address , latitude , longitude \n"
        "OUT-3001,First Name,supermarket,100 Broadway,40.714,-74.003\n"
        "OUT-3001,Updated Name,supermarket,100 Broadway Suite B,40.714,-74.003\n"
    ).encode("utf-8")

    resp = client.post(
        f"/api/v1/outlets/import?territory_id={ter_id}",
        headers=sup_headers,
        files={"file": ("batch.csv", csv_content, "text/csv")},
    )
    assert resp.status_code == 201
    job = resp.json()
    assert job["rows_created"] == 1
    assert job["rows_updated"] == 1
    assert job["rows_rejected"] == 0

    dup = client.post(
        "/api/v1/outlets",
        headers=sup_headers,
        json={
            "outlet_code": "OUT-3001",
            "business_name": "Duplicate Attempt",
            "category": "supermarket",
            "address": "100 Broadway",
            "latitude": 40.714,
            "longitude": -74.003,
        },
    )
    assert dup.status_code == 409
