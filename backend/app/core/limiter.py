"""Trusted-proxy-aware client IP extraction and SlowAPI rate limiter."""

from __future__ import annotations

import ipaddress

from fastapi import Request
from slowapi import Limiter

from app.core.config import get_settings


def _is_trusted_proxy(ip_str: str, trusted_entries: list[str]) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    for entry in trusted_entries:
        try:
            if "/" in entry:
                if ip_obj in ipaddress.ip_network(entry, strict=False):
                    return True
            elif ip_obj == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def _parse_valid_ip(raw_ip: str) -> str | None:
    try:
        return str(ipaddress.ip_address(raw_ip.strip()))
    except ValueError:
        return None


def get_real_client_ip(request: Request) -> str:
    """Resolve real client IP without trusting spoofed headers from untrusted peers."""
    settings = get_settings()
    peer_ip = request.client.host if request.client else "127.0.0.1"
    if not _is_trusted_proxy(peer_ip, settings.trusted_proxy_networks):
        return peer_ip

    if settings.trust_cloudflare_headers:
        cf_ip = _parse_valid_ip(request.headers.get("cf-connecting-ip", ""))
        if cf_ip:
            return cf_ip

    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        hops = [hop.strip() for hop in xff.split(",") if hop.strip()]
        for hop in reversed(hops):
            if not _is_trusted_proxy(hop, settings.trusted_proxy_networks):
                parsed = _parse_valid_ip(hop)
                if parsed:
                    return parsed
                break
        first_valid = _parse_valid_ip(hops[0]) if hops else None
        if first_valid:
            return first_valid

    real_ip = _parse_valid_ip(request.headers.get("x-real-ip", ""))
    return real_ip or peer_ip


_settings = get_settings()
limiter = Limiter(
    key_func=get_real_client_ip,
    enabled=_settings.rate_limit_enabled,
    storage_uri=_settings.redis_url if _settings.redis_url else "memory://",
)
