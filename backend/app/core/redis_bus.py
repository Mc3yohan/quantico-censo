"""Redis cache-aside, single-use WebSocket tickets, and CQRS Pub/Sub event bus."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import redis
import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)


class RedisBus:
    """
    Unified Redis bridge for CQRS nodes (`backend-sync` -> `backend-read`).

    - Cache operations fail open cleanly if Redis is temporarily unavailable.
    - Pub/Sub bridges real-time SOS, audit, triage, dispatch, and ticket events across containers.
    - Single-use WebSocket tickets expire in 30s and are atomically consumed via GETDEL.
    - When `REDIS_URL` is unset (e.g. fast local unit tests), uses a thread-safe in-memory broker.
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self._redis_url = (redis_url if redis_url is not None else get_settings().redis_url).strip()
        self._sync_client: redis.Redis[str] | None = None
        self._async_client: aioredis.Redis[str] | None = None
        self._lock = threading.Lock()
        self._async_lock: asyncio.Lock | None = None
        self._mem_cache: dict[str, tuple[float, str]] = {}
        self._mem_tickets: dict[str, tuple[float, str]] = {}
        self._local_subscribers: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]]] = {}

    def _get_sync_client(self) -> redis.Redis[str] | None:
        if not self._redis_url:
            return None
        if self._sync_client is not None:
            return self._sync_client
        with self._lock:
            if self._sync_client is None:
                try:
                    client = redis.from_url(
                        self._redis_url,
                        decode_responses=True,
                        socket_connect_timeout=1.5,
                        socket_timeout=2.0,
                    )
                    client.ping()
                    self._sync_client = client
                except Exception as exc:
                    logger.warning("redis_sync_unavailable", error=str(exc))
                    return None
        return self._sync_client

    async def _get_async_client(self) -> aioredis.Redis[str] | None:
        if not self._redis_url:
            return None
        if self._async_client is not None:
            return self._async_client
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        async with self._async_lock:
            if self._async_client is not None:
                return self._async_client
            try:
                client = aioredis.from_url(
                    self._redis_url,
                    decode_responses=True,
                    socket_connect_timeout=1.5,
                    socket_timeout=5.0,
                )
                await client.ping()
                self._async_client = client
            except Exception as exc:
                logger.warning("redis_async_unavailable", error=str(exc))
                return None
        return self._async_client

    def ping(self) -> bool:
        """Return True if Redis responds or in-memory test mode is active."""
        if not self._redis_url:
            return True
        client = self._get_sync_client()
        if client is None:
            return False
        try:
            return bool(client.ping())
        except Exception as exc:
            logger.warning("redis_ping_failed", error=str(exc))
            return False

    # ── Cache-Aside ──────────────────────────────────────────────────────────

    def get_json(self, key: str) -> dict[str, Any] | list[Any] | None:
        client = self._get_sync_client()
        if client is not None:
            try:
                raw = client.get(key)
                return json.loads(raw) if raw else None
            except Exception as exc:
                logger.warning("redis_get_failed", key=key, error=str(exc))
                return None

        with self._lock:
            entry = self._mem_cache.get(key)
            if not entry:
                return None
            expires_at, raw = entry
            if time.monotonic() > expires_at:
                self._mem_cache.pop(key, None)
                return None
            return json.loads(raw)

    def set_json(self, key: str, value: dict[str, Any] | list[Any], ttl_seconds: int = 60) -> None:
        raw = json.dumps(value, default=str)
        client = self._get_sync_client()
        if client is not None:
            try:
                client.setex(key, ttl_seconds, raw)
                return
            except Exception as exc:
                logger.warning("redis_set_failed", key=key, error=str(exc))
                return

        with self._lock:
            now = time.monotonic()
            if len(self._mem_cache) >= 2000:
                expired = [k for k, (exp, _) in self._mem_cache.items() if now > exp]
                for k in expired:
                    self._mem_cache.pop(k, None)
                if len(self._mem_cache) >= 2000:
                    self._mem_cache.pop(next(iter(self._mem_cache)), None)
            self._mem_cache[key] = (now + ttl_seconds, raw)

    def invalidate_prefix(self, prefix: str) -> int:
        client = self._get_sync_client()
        if client is not None:
            deleted = 0
            try:
                cursor = 0
                pattern = f"{prefix}*"
                while True:
                    cursor, keys = client.scan(cursor=cursor, match=pattern, count=100)
                    if keys:
                        deleted += int(client.delete(*keys))
                    if cursor == 0:
                        break
            except Exception as exc:
                logger.warning("redis_invalidate_failed", prefix=prefix, error=str(exc))
            return deleted

        with self._lock:
            keys = [k for k in self._mem_cache if k.startswith(prefix)]
            for key in keys:
                self._mem_cache.pop(key, None)
            return len(keys)

    # ── Single-Use WebSocket Tickets ─────────────────────────────────────────

    def store_ws_ticket(self, ticket: str, payload: dict[str, Any], ttl_seconds: int = 30) -> None:
        raw = json.dumps(payload)
        key = f"qc:ws_ticket:{ticket}"
        client = self._get_sync_client()
        if client is not None:
            try:
                client.setex(key, ttl_seconds, raw)
                return
            except Exception as exc:
                logger.warning("redis_ticket_store_failed", error=str(exc))

        with self._lock:
            now = time.monotonic()
            if len(self._mem_tickets) >= 2000:
                expired = [k for k, (exp, _) in self._mem_tickets.items() if now > exp]
                for k in expired:
                    self._mem_tickets.pop(k, None)
                if len(self._mem_tickets) >= 2000:
                    self._mem_tickets.pop(next(iter(self._mem_tickets)), None)
            self._mem_tickets[key] = (now + ttl_seconds, raw)

    def consume_ws_ticket(self, ticket: str) -> dict[str, Any] | None:
        key = f"qc:ws_ticket:{ticket}"
        client = self._get_sync_client()
        if client is not None:
            try:
                raw = client.getdel(key)
                if raw:
                    return json.loads(raw)
                return None
            except Exception as exc:
                logger.warning("redis_ticket_consume_failed", error=str(exc))

        with self._lock:
            entry = self._mem_tickets.pop(key, None)
            if not entry:
                return None
            expires_at, raw = entry
            if time.monotonic() > expires_at:
                return None
            return json.loads(raw)

    # ── CQRS Real-Time Pub/Sub ───────────────────────────────────────────────

    def publish_event(self, channel: str, payload: dict[str, Any]) -> None:
        """Publish an event from any node (`backend-sync` or `backend-read`)."""
        raw = json.dumps(payload, default=str)
        client = self._get_sync_client()
        if client is not None:
            try:
                client.publish(channel, raw)
                return
            except Exception as exc:
                logger.warning("redis_publish_failed", channel=channel, error=str(exc))

        self._publish_local(channel, payload)

    def _publish_local(self, channel: str, payload: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._local_subscribers.get(channel, set()))
        for loop, queue in targets:
            if not loop.is_closed():
                loop.call_soon_threadsafe(queue.put_nowait, payload)

    async def subscribe(self, channels: list[str]) -> AsyncIterator[dict[str, Any]]:
        """Yield live events from Redis Pub/Sub or local in-memory channels."""
        client = await self._get_async_client()
        if client is not None:
            pubsub = client.pubsub()
            await pubsub.subscribe(*channels)
            try:
                while True:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg and msg.get("type") == "message":
                        data = msg.get("data")
                        if isinstance(data, str):
                            try:
                                yield json.loads(data)
                            except json.JSONDecodeError:
                                logger.warning("redis_pubsub_bad_json", channel=msg.get("channel"))
                    else:
                        await asyncio.sleep(0.05)
            finally:
                try:
                    await pubsub.unsubscribe(*channels)
                except Exception as exc:
                    logger.debug("redis_pubsub_unsubscribe_error", error=str(exc))
                try:
                    await pubsub.aclose()
                except Exception as exc:
                    logger.debug("redis_pubsub_close_error", error=str(exc))
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        entry = (loop, queue)
        with self._lock:
            for ch in channels:
                self._local_subscribers.setdefault(ch, set()).add(entry)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            with self._lock:
                for ch in channels:
                    subs = self._local_subscribers.get(ch)
                    if subs:
                        subs.discard(entry)

    async def close(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None
        if self._sync_client is not None:
            self._sync_client.close()
            self._sync_client = None


redis_bus = RedisBus()
