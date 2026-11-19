"""Zero-Trust WebSocket endpoints backed by Redis Pub/Sub (cross-node CQRS) and single-use tickets."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.core.rbac import AuthContext, Role, assert_record_in_scope
from app.core.redis_bus import redis_bus
from app.core.security import consume_ws_ticket
from app.db import session as db_session
from app.db.models import SupportTicket, User

router = APIRouter()
logger = get_logger(__name__)


def _origin_is_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    normalized = origin.strip().rstrip("/")
    allowed = set(get_settings().cors_origins_list)
    return normalized in allowed


def _reverify_live_ws_auth(auth: AuthContext) -> bool:
    """Re-verify live user active state, token_version, and tenant kill-switches in a short-lived DB session."""
    db = db_session.SessionLocal()
    try:
        user = db.execute(
            select(User)
            .options(selectinload(User.region), selectinload(User.territory))
            .where(User.id == auth.user_id)
        ).scalar_one_or_none()
        if user is None or user.deleted_at is not None or not user.is_active:
            return False
        if user.token_version != auth.token_version:
            return False
        if user.region is not None and not user.region.is_active:
            return False
        if user.territory is not None and not user.territory.is_active:
            return False
        return True
    finally:
        db.close()


def _event_allowed_for_auth(auth: AuthContext, event: dict[str, object]) -> bool:
    """Enforce strict RBAC, territorial isolation, and field-agent owner boundaries on live events."""
    ev_type = str(event.get("event", ""))
    if ev_type == "SESSION_REVOKED":
        return event.get("user_id") == auth.user_id

    if auth.role == Role.SUPER_ADMIN:
        return True

    ev_territory = event.get("territory_id")
    if auth.role in (Role.SUPERVISOR, Role.FIELD_AGENT):
        if auth.territory_id is None or ev_territory != auth.territory_id:
            return False
        if auth.role == Role.FIELD_AGENT:
            if ev_type.startswith("TRIAGE_"):
                return False
            if event.get("agent_id") != auth.user_id:
                return False
        return True

    if auth.role == Role.VIEWER and auth.territory_id is not None:
        return ev_territory == auth.territory_id

    return True


async def _run_pubsub_bridge(
    websocket: WebSocket,
    channels: list[str],
    auth: AuthContext,
    *,
    apply_alert_filter: bool,
) -> None:
    """Bridge Redis Pub/Sub and client pings cleanly; terminate immediately if either side fails."""

    async def _forward() -> None:
        async for event in redis_bus.subscribe(channels):
            if apply_alert_filter and not _event_allowed_for_auth(auth, event):
                continue
            if event.get("event") == "SESSION_REVOKED" and event.get("user_id") == auth.user_id:
                await websocket.close(code=4401, reason="Session revoked")
                return
            await websocket.send_json(event)

    async def _receive() -> None:
        while True:
            incoming = await websocket.receive_text()
            if incoming.strip().lower() == "ping":
                await websocket.send_json({"event": "PONG"})

    forward_task = asyncio.create_task(_forward())
    receive_task = asyncio.create_task(_receive())
    try:
        done, pending = await asyncio.wait(
            {forward_task, receive_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                logger.warning("ws_bridge_error", user=auth.username, error=str(exc))
                try:
                    await websocket.close(code=1011, reason="Realtime stream interrupted")
                except RuntimeError:
                    logger.debug("ws_already_closed", user=auth.username)
    finally:
        for task in (forward_task, receive_task):
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, WebSocketDisconnect, Exception) as exc:
                logger.debug("ws_task_closed", user=auth.username, err_type=type(exc).__name__)


def _authorize_ticket_ws(auth: AuthContext, ticket_id: int) -> tuple[int | None, int | None, str]:
    """Verify ticket existence & scope in a short-lived DB session off the asyncio loop."""
    db = db_session.SessionLocal()
    try:
        ticket = db.get(SupportTicket, ticket_id)
        if ticket is None:
            return None, 4404, "Ticket not found"
        try:
            assert_record_in_scope(
                auth,
                region_id=ticket.region_id,
                territory_id=ticket.territory_id,
                owner_user_id=ticket.created_by_id,
                require_owner_for_agent=True,
            )
        except HTTPException:
            return None, 4403, "Ticket outside authorized scope"
        return ticket.id, None, ""
    finally:
        db.close()


@router.websocket("/ws/alerts")
async def alerts_websocket(websocket: WebSocket) -> None:
    """
    Real-time regional/global alert stream bridged across CQRS containers via Redis Pub/Sub.
    Requires a single-use ticket minted by POST /api/v1/auth/ws-ticket (fail-closed).
    """
    if not _origin_is_allowed(websocket):
        await websocket.close(code=4403, reason="Origin not allowed")
        return

    auth = await asyncio.to_thread(consume_ws_ticket, websocket.query_params.get("ticket"))
    if auth is None or not await asyncio.to_thread(_reverify_live_ws_auth, auth):
        await websocket.close(code=4401, reason="Invalid or revoked WebSocket ticket")
        return

    if auth.role in (Role.SUPERVISOR, Role.FIELD_AGENT) and auth.territory_id is None:
        await websocket.close(code=4403, reason="Operator missing required territory assignment")
        return

    if auth.role == Role.SUPER_ADMIN:
        channels = ["qc:alerts:global", f"qc:user:{auth.user_id}"]
    elif auth.region_id is not None:
        channels = [f"qc:alerts:region:{auth.region_id}", f"qc:user:{auth.user_id}"]
    else:
        await websocket.close(code=4403, reason="No authorized territorial channel")
        return

    await websocket.accept()
    await websocket.send_json(
        {
            "event": "CONNECTED",
            "channels": [c for c in channels if c.startswith("qc:alerts:")],
            "role": auth.role.value,
            "region_id": auth.region_id,
        }
    )
    await _run_pubsub_bridge(websocket, channels, auth, apply_alert_filter=True)


@router.websocket("/ws/tickets/{ticket_id}")
async def ticket_websocket(websocket: WebSocket, ticket_id: int) -> None:
    """
    Authenticated, tenant-scoped real-time stream for a support ticket.
    Closes its DB lookup session off the event loop BEFORE accepting the long-lived WebSocket.
    """
    if not _origin_is_allowed(websocket):
        await websocket.close(code=4403, reason="Origin not allowed")
        return

    auth = await asyncio.to_thread(consume_ws_ticket, websocket.query_params.get("ticket"))
    if auth is None or not await asyncio.to_thread(_reverify_live_ws_auth, auth):
        await websocket.close(code=4401, reason="Invalid or revoked WebSocket ticket")
        return

    resolved_ticket_id, close_code, close_reason = await asyncio.to_thread(_authorize_ticket_ws, auth, ticket_id)
    if resolved_ticket_id is None:
        await websocket.close(code=close_code or 4403, reason=close_reason)
        return

    await websocket.accept()
    channel = f"qc:ticket:{resolved_ticket_id}"
    await websocket.send_json({"event": "CONNECTED", "channel": channel, "ticket_id": resolved_ticket_id})
    await _run_pubsub_bridge(
        websocket,
        [channel, f"qc:user:{auth.user_id}"],
        auth,
        apply_alert_filter=False,
    )
