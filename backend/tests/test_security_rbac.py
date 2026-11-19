"""Security, RBAC, Multi-Tenant IDOR, Escalation & Kill-Switch Regression Suite."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Region, SosAlert, TriageItem, User


def test_health_endpoint_returns_ok(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"


def test_unauthenticated_requests_rejected_fail_closed(client: TestClient) -> None:
    protected_gets = [
        "/api/v1/auth/me",
        "/api/v1/hierarchy/regions",
        "/api/v1/hierarchy/dashboard/kpis",
        "/api/v1/outlets",
        "/api/v1/audits",
        "/api/v1/audits/map",
        "/api/v1/triage",
        "/api/v1/dispatch",
        "/api/v1/sos/active",
        "/api/v1/tickets",
        "/api/v1/users",
    ]
    for path in protected_gets:
        resp = client.get(path)
        assert resp.status_code == 401, f"Expected 401 on {path}, got {resp.status_code}"


def test_read_only_viewer_can_read_but_cannot_mutate(client: TestClient, auth_headers) -> None:
    headers = auth_headers("viewer_north")

    # Viewer can read Metro North hierarchy, KPIs, outlets, and triage queue
    assert client.get("/api/v1/hierarchy/regions", headers=headers).status_code == 200
    assert len(client.get("/api/v1/hierarchy/regions", headers=headers).json()) == 1
    assert client.get("/api/v1/hierarchy/dashboard/kpis", headers=headers).status_code == 200
    assert client.get("/api/v1/triage", headers=headers).status_code == 200

    # Viewer is strictly blocked from EVERY write operation
    assert (
        client.post(
            "/api/v1/sos/trigger",
            headers=headers,
            json={"latitude": 40.715, "longitude": -74.001, "message": "Attempt"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/audits",
            headers=headers,
            json={
                "sync_uuid": str(uuid.uuid4()),
                "outlet_code": "OUT-1001",
                "business_name": "Viewer Write Attempt",
                "category": "supermarket",
                "address": "114 Mercer Ave",
                "latitude": 40.712,
                "longitude": -74.006,
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/triage/1/resolve",
            headers=headers,
            json={"decision": "approved", "resolution_note": "Unauthorized attempt"},
        ).status_code
        == 403
    )


def test_cross_region_idor_blocked_on_triage_routes_and_sos(
    client: TestClient,
    db_factory: sessionmaker[Session],
    auth_headers,
) -> None:
    north_headers = auth_headers("manager_north")
    harbor_agent_headers = auth_headers("agent_harbor")

    # Locate Coastal East triage item seeded in REG-EAST
    db = db_factory()
    try:
        reg_east = db.execute(select(Region).where(Region.code == "REG-EAST")).scalar_one()
        east_triage = db.execute(select(TriageItem).where(TriageItem.region_id == reg_east.id)).scalar_one()
        east_triage_id = east_triage.id
    finally:
        db.close()

    # Metro North manager cannot resolve Coastal East triage item
    resp = client.post(
        f"/api/v1/triage/{east_triage_id}/resolve",
        headers=north_headers,
        json={"decision": "approved", "resolution_note": "Cross-region hijack attempt"},
    )
    assert resp.status_code == 403

    # Coastal East agent triggers SOS -> Metro North manager cannot see or resolve it
    sos_resp = client.post(
        "/api/v1/sos/trigger",
        headers=harbor_agent_headers,
        json={"latitude": 40.682, "longitude": -74.008, "message": "Harbor alert"},
    )
    assert sos_resp.status_code == 201
    sos_id = sos_resp.json()["id"]

    north_active = client.get("/api/v1/sos/active", headers=north_headers).json()
    assert all(item["id"] != sos_id for item in north_active)

    resolve_attempt = client.post(
        f"/api/v1/sos/{sos_id}/resolve",
        headers=north_headers,
        json={"resolution_note": "Unauthorized cross-region resolve"},
    )
    assert resolve_attempt.status_code == 403


def test_order_of_operations_user_hijack_and_peer_escalation_blocked(
    client: TestClient,
    db_factory: sessionmaker[Session],
    auth_headers,
) -> None:
    sup_headers = auth_headers("sup_downtown")

    db = db_factory()
    try:
        harbor_agent = db.execute(select(User).where(User.username == "agent_harbor")).scalar_one()
        downtown_sup = db.execute(select(User).where(User.username == "sup_downtown")).scalar_one()
        downtown_agent = db.execute(select(User).where(User.username == "agent_downtown")).scalar_one()
        harbor_agent_id = harbor_agent.id
        downtown_sup_id = downtown_sup.id
        downtown_agent_id = downtown_agent.id
        north_region_id = downtown_sup.region_id
        downtown_territory_id = downtown_sup.territory_id
    finally:
        db.close()

    # Attack 1: Supervisor tries to hijack Harbor agent by overwriting region/territory in payload
    hijack = client.put(
        f"/api/v1/users/{harbor_agent_id}",
        headers=sup_headers,
        json={
            "region_id": north_region_id,
            "territory_id": downtown_territory_id,
            "password": "HijackedPassword2026!",
        },
    )
    assert hijack.status_code == 403

    # Verify Harbor agent password & scope were NOT mutated
    assert auth_headers("agent_harbor", "QuanticoDemo2026!")

    # Attack 2: Supervisor cannot modify peer/self supervisor or promote an agent to supervisor
    peer_edit = client.put(
        f"/api/v1/users/{downtown_sup_id}",
        headers=sup_headers,
        json={"full_name": "Self Escalation"},
    )
    assert peer_edit.status_code == 403

    promote = client.put(
        f"/api/v1/users/{downtown_agent_id}",
        headers=sup_headers,
        json={"role": "supervisor"},
    )
    assert promote.status_code == 403


def test_soft_delete_revokes_jwt_and_preserves_sos_audit_history(
    client: TestClient,
    db_factory: sessionmaker[Session],
    auth_headers,
) -> None:
    admin_headers = auth_headers("admin_hq")
    agent_headers = auth_headers("agent_downtown")

    # Agent triggers SOS
    sos_resp = client.post(
        "/api/v1/sos/trigger",
        headers=agent_headers,
        json={"latitude": 40.715, "longitude": -74.001, "message": "Immutable safety record"},
    )
    assert sos_resp.status_code == 201
    sos_id = sos_resp.json()["id"]

    # Admin soft-deletes agent
    db = db_factory()
    try:
        agent = db.execute(select(User).where(User.username == "agent_downtown")).scalar_one()
        agent_id = agent.id
    finally:
        db.close()

    del_resp = client.delete(f"/api/v1/users/{agent_id}", headers=admin_headers)
    assert del_resp.status_code == 200

    # Existing JWT is immediately revoked AND fresh login fails
    assert client.get("/api/v1/auth/me", headers=agent_headers).status_code == 401
    relogin = client.post(
        "/api/v1/auth/login",
        json={"username": "agent_downtown", "password": "QuanticoDemo2026!"},
    )
    assert relogin.status_code == 403

    # SosAlert record remains intact in DB
    db = db_factory()
    try:
        preserved = db.get(SosAlert, sos_id)
        assert preserved is not None
        assert preserved.agent_id == agent_id
    finally:
        db.close()


def test_region_kill_switch_blocks_login_and_existing_tokens(
    client: TestClient,
    db_factory: sessionmaker[Session],
    auth_headers,
) -> None:
    north_headers = auth_headers("manager_north")
    assert client.get("/api/v1/auth/me", headers=north_headers).status_code == 200

    db = db_factory()
    try:
        reg_north = db.execute(select(Region).where(Region.code == "REG-NORTH")).scalar_one()
        reg_north.is_active = False
        db.commit()
    finally:
        db.close()

    # Active JWT immediately rejected when region is disabled
    assert client.get("/api/v1/auth/me", headers=north_headers).status_code == 403
    # New login also blocked
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "manager_north", "password": "QuanticoDemo2026!"},
    )
    assert resp.status_code == 403


def test_stale_jwt_logout_replay_cannot_revoke_active_session_and_audit_attributes_login(
    client: TestClient,
    auth_headers,
) -> None:
    admin_headers = auth_headers("admin_hq")
    token_v1_headers = auth_headers("manager_north")

    # Logout once -> revokes v1
    out1 = client.post("/api/v1/auth/logout", headers=token_v1_headers)
    assert out1.status_code == 200
    assert client.get("/api/v1/auth/me", headers=token_v1_headers).status_code == 401

    # Fresh login mints v2
    token_v2_headers = auth_headers("manager_north")
    assert client.get("/api/v1/auth/me", headers=token_v2_headers).status_code == 200

    # Replaying revoked v1 token against /logout MUST NOT revoke active v2 session
    replay = client.post("/api/v1/auth/logout", headers=token_v1_headers)
    assert replay.status_code == 200
    assert client.get("/api/v1/auth/me", headers=token_v2_headers).status_code == 200

    # Verify AuditLog recorded manager_north's login with real username (not "anonymous")
    logs = client.get("/api/v1/hierarchy/audit-logs?limit=25", headers=admin_headers)
    assert logs.status_code == 200
    login_entries = [
        row for row in logs.json() if row["path"] == "/api/v1/auth/login" and row["username"] == "manager_north"
    ]
    assert len(login_entries) >= 1


def test_multibyte_utf8_password_over_72_bytes_returns_422_not_500(client: TestClient) -> None:
    # 30 emojis = 120 UTF-8 bytes (> 72 byte bcrypt limit) while <= 72 Unicode chars
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "admin_hq", "password": "🔐" * 30},
    )
    assert resp.status_code == 422


def test_admin_and_regional_manager_can_trigger_sos_and_open_tickets_within_scope(
    client: TestClient,
    auth_headers,
) -> None:
    admin_headers = auth_headers("admin_hq")
    north_headers = auth_headers("manager_north")

    sos = client.post(
        "/api/v1/sos/trigger",
        headers=admin_headers,
        json={"latitude": 40.715, "longitude": -74.001, "message": "HQ field safety drill"},
    )
    assert sos.status_code == 201

    tk = client.post(
        "/api/v1/tickets",
        headers=north_headers,
        json={"subject": "POS connectivity check", "body": "Checking regional gateway latency."},
    )
    assert tk.status_code == 201
    assert tk.json()["region_id"] is not None
