"""Geofencing anti-fraud, offline sync idempotency, triage quarantine, and map cache tests."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def test_geofence_auto_approve_vs_drift_vs_outside_territory(client: TestClient, auth_headers) -> None:
    agent_headers = auth_headers("agent_downtown")

    # 1) Exact match inside TER-DOWNTOWN & near OUT-1001 (40.712, -74.006) -> auto-approved
    ok_resp = client.post(
        "/api/v1/audits",
        headers=agent_headers,
        json={
            "sync_uuid": str(uuid.uuid4()),
            "outlet_code": "OUT-1001",
            "business_name": "Aurora Fresh Market",
            "category": "supermarket",
            "address": "114 Mercer Ave",
            "latitude": 40.7122,
            "longitude": -74.0059,
            "shelf_share_pct": 80,
            "stockout_alert": False,
        },
    )
    assert ok_resp.status_code == 201
    ok_body = ok_resp.json()
    assert ok_body["geofence_status"] == "inside_territory"
    assert ok_body["review_status"] == "approved"

    # 2) Inside TER-DOWNTOWN polygon, but >150m away from OUT-1001 -> gps_drift + quarantined
    drift_resp = client.post(
        "/api/v1/audits",
        headers=agent_headers,
        json={
            "sync_uuid": str(uuid.uuid4()),
            "outlet_code": "OUT-1001",
            "business_name": "Aurora Fresh Market",
            "category": "supermarket",
            "address": "114 Mercer Ave",
            "latitude": 40.7200,
            "longitude": -73.9950,
            "shelf_share_pct": 65,
            "stockout_alert": False,
        },
    )
    assert drift_resp.status_code == 201
    drift_body = drift_resp.json()
    assert drift_body["geofence_status"] == "gps_drift"
    assert drift_body["review_status"] == "quarantined"

    # 3) Completely outside TER-DOWNTOWN bounds -> outside_territory + quarantined (critical triage)
    spoof_resp = client.post(
        "/api/v1/audits",
        headers=agent_headers,
        json={
            "sync_uuid": str(uuid.uuid4()),
            "outlet_code": "OUT-1001",
            "business_name": "Aurora Fresh Market",
            "category": "supermarket",
            "address": "114 Mercer Ave",
            "latitude": 40.8500,
            "longitude": -73.8000,
            "shelf_share_pct": 50,
            "stockout_alert": False,
        },
    )
    assert spoof_resp.status_code == 201
    spoof_body = spoof_resp.json()
    assert spoof_body["geofence_status"] == "outside_territory"
    assert spoof_body["review_status"] == "quarantined"


def test_offline_batch_sync_idempotency_and_conflict_detection(client: TestClient, auth_headers) -> None:
    agent_headers = auth_headers("agent_downtown")
    shared_uuid = str(uuid.uuid4())
    second_uuid = str(uuid.uuid4())

    first_batch = client.post(
        "/api/v1/audits/sync",
        headers=agent_headers,
        json={
            "items": [
                {
                    "sync_uuid": shared_uuid,
                    "outlet_code": "OUT-1002",
                    "business_name": "Vanguard Pharma Plus",
                    "category": "pharmacy",
                    "address": "220 Liberty Plaza",
                    "latitude": 40.7181,
                    "longitude": -74.0011,
                    "shelf_share_pct": 70,
                }
            ]
        },
    )
    assert first_batch.status_code == 200
    assert first_batch.json()["created"] == 1

    # Retry same UUID (idempotent duplicate) + same UUID with mismatched outlet_code (conflict) + new item
    second_batch = client.post(
        "/api/v1/audits/sync",
        headers=agent_headers,
        json={
            "items": [
                {
                    "sync_uuid": shared_uuid,
                    "outlet_code": "OUT-1002",
                    "business_name": "Vanguard Pharma Plus",
                    "category": "pharmacy",
                    "address": "220 Liberty Plaza",
                    "latitude": 40.7181,
                    "longitude": -74.0011,
                },
                {
                    "sync_uuid": shared_uuid,
                    "outlet_code": "OUT-1003",
                    "business_name": "Collision Attempt",
                    "category": "convenience",
                    "address": "45 Cedar St",
                    "latitude": 40.725,
                    "longitude": -73.992,
                },
                {
                    "sync_uuid": second_uuid,
                    "outlet_code": "OUT-1005",
                    "business_name": "Apex Hardware & Tools",
                    "category": "hardware",
                    "address": "310 Canal Way",
                    "latitude": 40.7281,
                    "longitude": -74.0111,
                },
            ]
        },
    )
    assert second_batch.status_code == 200
    summary = second_batch.json()
    assert summary["processed"] == 3
    assert summary["created"] == 1
    assert summary["duplicates"] == 1
    assert summary["conflicts"] == 1


def test_triage_resolution_and_map_cache_invalidation(client: TestClient, auth_headers) -> None:
    sup_headers = auth_headers("sup_downtown")
    agent_headers = auth_headers("agent_downtown")

    # Warm map cache
    snap1 = client.get("/api/v1/audits/map", headers=sup_headers)
    assert snap1.status_code == 200
    assert snap1.json()["cached"] is False

    snap2 = client.get("/api/v1/audits/map", headers=sup_headers)
    assert snap2.status_code == 200
    assert snap2.json()["cached"] is True

    # Fetch open triage queue for Downtown
    queue = client.get("/api/v1/triage?status=open&limit=10", headers=sup_headers).json()
    assert queue["total"] >= 1
    triage_id = queue["items"][0]["id"]

    # Resolve triage item -> updates audit status & invalidates map cache
    resolved = client.post(
        f"/api/v1/triage/{triage_id}/resolve",
        headers=sup_headers,
        json={"decision": "approved", "resolution_note": "Verified stockout replenishment scheduled."},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "approved"

    # Re-resolving returns 409 Conflict
    second = client.post(
        f"/api/v1/triage/{triage_id}/resolve",
        headers=sup_headers,
        json={"decision": "rejected", "resolution_note": "Double resolve"},
    )
    assert second.status_code == 409

    # Map cache was invalidated cleanly
    snap3 = client.get("/api/v1/audits/map", headers=sup_headers)
    assert snap3.status_code == 200
    assert snap3.json()["cached"] is False

    # Field agent cannot read supervisor triage queue
    assert client.get("/api/v1/triage", headers=agent_headers).status_code == 403
