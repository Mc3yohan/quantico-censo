"""Pure ASGI Audit Middleware — zero response-body buffering, safe with streaming & WebSockets."""

from __future__ import annotations

import json
from typing import Any

import anyio
from fastapi import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.limiter import get_real_client_ip
from app.core.logging_config import get_logger
from app.db import session as db_session
from app.db.models import AuditLog

logger = get_logger(__name__)
AUDITED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class PureAsgiAuditMiddleware:
    """
    Lightweight pure-ASGI middleware that records state-changing HTTP calls and 401/403 denials
    without buffering responses in memory, blocking the event loop, or breaking WebSockets.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", "/"))
        status_code = 500

        async def _send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        await self.app(scope, receive, _send_wrapper)

        should_audit = path.startswith("/api/") and (
            method in AUDITED_METHODS or status_code in (401, 403)
        )
        if not should_audit:
            return

        req = Request(scope)
        user_agent = req.headers.get("user-agent", "")[:255]
        ip_address = get_real_client_ip(req)[:64]
        state: dict[str, Any] = scope.get("state", {})
        auth = state.get("auth")

        await anyio.to_thread.run_sync(
            self._write_audit_row,
            auth,
            method,
            path[:255],
            status_code,
            ip_address,
            user_agent,
        )

    @staticmethod
    def _write_audit_row(
        auth: Any,
        method: str,
        path: str,
        status_code: int,
        ip_address: str,
        user_agent: str,
    ) -> None:
        db = db_session.SessionLocal()
        try:
            action = f"{method}:{path}"
            row = AuditLog(
                region_id=getattr(auth, "region_id", None),
                territory_id=getattr(auth, "territory_id", None),
                user_id=getattr(auth, "user_id", None),
                username=getattr(auth, "username", "anonymous"),
                action=action[:80],
                method=method,
                path=path,
                status_code=status_code,
                ip_address=ip_address,
                user_agent=user_agent,
                detail_json=json.dumps({"role": getattr(getattr(auth, "role", None), "value", "anonymous")}),
            )
            db.add(row)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("audit_log_write_failed", path=path, error=str(exc))
        finally:
            db.close()
