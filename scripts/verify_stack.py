#!/usr/bin/env python3
"""
End-to-End Live CQRS + PostGIS + Redis Pub/Sub Verification Script against Docker Compose gateway.
Verifies:
  1. Healthcheck (PostgreSQL/PostGIS up + Redis up + read node role)
  2. Auth login & single-use WS ticket minting
  3. Cross-container CQRS Realtime Bridge:
       SOS triggered on `backend-sync` -> Redis Pub/Sub -> received live on `backend-read` WebSocket!
  4. Native PostGIS ST_Covers geofencing:
       Audit outside territory polygon -> `outside_territory` + `quarantined`!
  5. Cross-container SOS resolution broadcast (`SOS_RESOLVED`)!
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

import httpx
import websockets

BASE_URL = "http://127.0.0.1:8095"
WS_BASE = "ws://127.0.0.1:8095"


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15.0) as client:
        # 1. Health & Frontend SPA served via Nginx gateway
        health = await client.get("/api/v1/health")
        assert health.status_code == 200, f"Health failed: {health.text}"
        hdata = health.json()
        assert hdata["database"] == "up" and hdata["redis"] == "up", f"Unhealthy stack: {hdata}"
        assert hdata["server_role"] == "read", f"Expected read plane on /health, got {hdata}"
        print(f"[1/5] Gateway health OK -> {hdata}")

        spa = await client.get("/")
        assert spa.status_code == 200 and "Quantico Censo" in spa.text
        print("[2/5] Compiled Vite frontend served via Nginx gateway OK")

        # 2. Authenticate Manager (Metro North) & Agent (Downtown)
        mgr_login = await client.post(
            "/api/v1/auth/login",
            json={"username": "manager_north", "password": "QuanticoDemo2026!"},
        )
        assert mgr_login.status_code == 200, mgr_login.text
        mgr_headers = {"Authorization": f"Bearer {mgr_login.json()['access_token']}"}

        agent_login = await client.post(
            "/api/v1/auth/login",
            json={"username": "agent_downtown", "password": "QuanticoDemo2026!"},
        )
        assert agent_login.status_code == 200, agent_login.text
        agent_headers = {"Authorization": f"Bearer {agent_login.json()['access_token']}"}

        # 3. Mint single-use WS ticket & connect to backend-read WebSocket
        ticket_resp = await client.post("/api/v1/auth/ws-ticket", headers=mgr_headers)
        assert ticket_resp.status_code == 200, ticket_resp.text
        ticket = ticket_resp.json()["ticket"]

        async with websockets.connect(
            f"{WS_BASE}/api/v1/ws/alerts?ticket={ticket}",
            additional_headers={"Origin": BASE_URL},
        ) as ws:
            connected = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert connected["event"] == "CONNECTED", connected
            print(f"[3/5] Zero-Trust WebSocket connected on backend-read -> {connected['channels']}")

            # 4. Trigger SOS on backend-sync -> verify cross-container Redis Pub/Sub delivery
            sos_resp = await client.post(
                "/api/v1/sos/trigger",
                headers=agent_headers,
                json={
                    "latitude": 40.7135,
                    "longitude": -74.0045,
                    "gps_fallback": False,
                    "message": "CQRS E2E Redis Pub/Sub verification drill",
                },
            )
            assert sos_resp.status_code == 201, sos_resp.text
            sos_id = sos_resp.json()["id"]

            live_event = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert live_event["event"] == "SOS_TRIGGERED" and live_event["alert_id"] == sos_id, live_event
            print(f"[4/5] Cross-container CQRS SOS_TRIGGERED delivered via Redis Pub/Sub (alert_id={sos_id})")

            # Resolve SOS on backend-sync -> verify SOS_RESOLVED arrives on backend-read WS
            res_resp = await client.post(
                f"/api/v1/sos/{sos_id}/resolve",
                headers=mgr_headers,
                json={"resolution_note": "E2E verification resolved cleanly."},
            )
            assert res_resp.status_code == 200, res_resp.text
            resolved_event = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert resolved_event["event"] == "SOS_RESOLVED" and resolved_event["alert_id"] == sos_id

        # 5. Native PostGIS ST_Covers geofencing test via backend-sync batch endpoint
        spoof_sync = await client.post(
            "/api/v1/audits/sync",
            headers=agent_headers,
            json={
                "items": [
                    {
                        "sync_uuid": str(uuid.uuid4()),
                        "outlet_code": "OUT-1001",
                        "business_name": "Aurora Fresh Market",
                        "category": "supermarket",
                        "address": "114 Mercer Ave",
                        "latitude": 40.82,
                        "longitude": -73.85,
                        "shelf_share_pct": 60,
                    }
                ]
            },
        )
        assert spoof_sync.status_code == 200, spoof_sync.text
        item = spoof_sync.json()["results"][0]
        assert item["geofence_status"] == "outside_territory", item
        assert item["review_status"] == "quarantined", item
        print(f"[5/5] Native PostGIS ST_Covers anti-fraud verified -> {item}")

    print("\n[VERIFY_STACK] ALL 5 LIVE CQRS / POSTGIS / REDIS PUBSUB CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
