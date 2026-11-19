"""Dispatch route RBAC & tactical support ticket IDOR + audit preservation tests."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import DispatchRoute, Outlet, SupportTicket, User


def test_dispatch_assign_reassign_revoke_enforces_territory_boundaries(
    client: TestClient,
    db_factory: sessionmaker[Session],
    auth_headers,
) -> None:
    sup_headers = auth_headers("sup_downtown")
    east_headers = auth_headers("manager_east")
    agent_headers = auth_headers("agent_downtown")

    db = db_factory()
    try:
        agent_downtown = db.execute(select(User).where(User.username == "agent_downtown")).scalar_one()
        agent_harbor = db.execute(select(User).where(User.username == "agent_harbor")).scalar_one()
        outlet_downtown = db.execute(select(Outlet).where(Outlet.outlet_code == "OUT-1005")).scalar_one()
        agent_downtown_id = agent_downtown.id
        agent_harbor_id = agent_harbor.id
        outlet_downtown_id = outlet_downtown.id
    finally:
        db.close()

    # Downtown supervisor assigns route inside Downtown -> 201
    created = client.post(
        "/api/v1/dispatch",
        headers=sup_headers,
        json={
            "title": "Afternoon POS Check — Apex Hardware",
            "agent_id": agent_downtown_id,
            "outlet_id": outlet_downtown_id,
            "scheduled_date": "2026-11-19",
            "priority": "high",
            "notes": "Verify barcode scanner & signage.",
        },
    )
    assert created.status_code == 201
    route_id = created.json()["id"]

    # Coastal East manager cannot reassign or revoke Downtown route (403)
    assert (
        client.put(
            f"/api/v1/dispatch/{route_id}/reassign",
            headers=east_headers,
            json={"agent_id": agent_harbor_id},
        ).status_code
        == 403
    )
    assert client.post(f"/api/v1/dispatch/{route_id}/revoke", headers=east_headers).status_code == 403

    # Downtown agent can mark own route in_progress, but cannot revoke it
    assert (
        client.patch(
            f"/api/v1/dispatch/{route_id}/status",
            headers=agent_headers,
            json={"status": "in_progress"},
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/v1/dispatch/{route_id}/status",
            headers=agent_headers,
            json={"status": "revoked"},
        ).status_code
        == 403
    )

    # Downtown supervisor revokes route cleanly
    revoked = client.post(f"/api/v1/dispatch/{route_id}/revoke", headers=sup_headers)
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"


def test_support_ticket_idor_blocked_and_reopen_preserves_audit_trail(
    client: TestClient,
    db_factory: sessionmaker[Session],
    auth_headers,
) -> None:
    downtown_agent = auth_headers("agent_downtown")
    harbor_agent = auth_headers("agent_harbor")
    sup_downtown = auth_headers("sup_downtown")

    created = client.post(
        "/api/v1/tickets",
        headers=downtown_agent,
        json={"subject": "Scanner battery issue", "body": "Need replacement power brick."},
    )
    assert created.status_code == 201
    ticket_id = created.json()["id"]

    # Cross-territory agent cannot read, post, claim, or close ticket (403)
    assert client.get(f"/api/v1/tickets/{ticket_id}/messages", headers=harbor_agent).status_code == 403
    assert (
        client.post(
            f"/api/v1/tickets/{ticket_id}/messages",
            headers=harbor_agent,
            json={"body": "Spoofed cross-region message"},
        ).status_code
        == 403
    )
    assert client.post(f"/api/v1/tickets/{ticket_id}/claim", headers=harbor_agent).status_code == 403

    # Supervisor closes ticket with resolution note, then reopens -> closure metadata preserved!
    closed = client.post(
        f"/api/v1/tickets/{ticket_id}/close",
        headers=sup_downtown,
        json={"resolution_note": "Dispatched spare battery pack."},
    )
    assert closed.status_code == 200
    assert closed.json()["closed_by_id"] is not None

    reopened = client.post(f"/api/v1/tickets/{ticket_id}/reopen", headers=sup_downtown)
    assert reopened.status_code == 200
    body = reopened.json()
    assert body["status"] == "open"
    assert body["reopen_count"] == 1
    assert body["closed_by_id"] is not None
    assert body["resolution_note"] == "Dispatched spare battery pack."

    db = db_factory()
    try:
        assert db.get(SupportTicket, ticket_id) is not None
        assert db.execute(select(DispatchRoute)).scalars().first() is not None
    finally:
        db.close()
