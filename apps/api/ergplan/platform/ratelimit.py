"""Who is asking, by network address, and how often (Stage 13).

The address. The API is reached through Caddy (docker/caddy/Caddyfile) or, in development, through
the web server's /api proxy; both connect from the compose network or this machine and both set
X-Forwarded-For. Caddy, for a caller it does not trust (every caller: no `trusted_proxies` is
configured), replaces whatever X-Forwarded-For the caller sent with the address it saw. So the
header is believed only when the connection itself comes from a trusted proxy
(`ERGPLAN_TRUSTED_PROXIES`), and then only its last entry, the one the proxy wrote. A caller that
connects directly from anywhere else is known by the address it connected from, whatever it sends.
uvicorn is started without `--forwarded-allow-ips` (docker/api.Dockerfile), so it does not rewrite
the peer address itself.

The limit. A sliding window per key, held in this process: the API runs as one uvicorn process, and
a restart forgetting a minute of counts is harmless. (The lockout after wrong passwords is a
different thing and is kept in the database: platform/admin.py.) A refused request gets 429 with
words and a Retry-After header."""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import deque
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from ..core.settings import get_settings
from .auth import Caller, caller

_lock = threading.Lock()
_hits: dict[tuple[str, str], deque[float]] = {}


def _trusted(peer: str) -> bool:
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(peer)
    except ValueError:
        ip = None
    for entry in get_settings().ergplan_trusted_proxies.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry == peer:
            return True
        if ip is not None:
            try:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
    return False


def client_address(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if _trusted(peer):
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        if forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass  # not an address: fall back to the proxy's own
    return peer[:64]


def check(bucket: str, key: str, limit: int, window_s: int) -> None:
    """Count one request for `key` in `bucket`; raise 429 when it is over `limit` per `window_s`."""
    if limit <= 0 or window_s <= 0:
        return
    now = time.monotonic()
    with _lock:
        hits = _hits.setdefault((bucket, key), deque())
        while hits and now - hits[0] >= window_s:
            hits.popleft()
        if len(hits) >= limit:
            wait = max(1, math.ceil(window_s - (now - hits[0])))
            raise HTTPException(
                429,
                f"too many requests from this address; try again in {wait} seconds",
                headers={"Retry-After": str(wait)},
            )
        hits.append(now)
        if len(_hits) > 10_000:  # callers long gone: forget them
            for k in [k for k, v in _hits.items() if not v or now - v[-1] >= max(window_s, 3600)]:
                del _hits[k]


def reset() -> None:
    """Forget every count (the tests, between cases)."""
    with _lock:
        _hits.clear()


def signin_limit(request: Request) -> None:
    """The sign-in routes (/auth/login, /auth/request, /auth/verify): per address, each route apart."""
    st = get_settings()
    route = getattr(request.scope.get("route"), "path", request.url.path)
    check(
        f"signin:{route}",
        client_address(request),
        st.ergplan_signin_rate_limit,
        st.ergplan_signin_rate_window_s,
    )


def upload_limit(request: Request, who: Annotated[Caller, Depends(caller)]) -> None:
    """Bill upload (/bills, /bill-reads): per signed-in account and address."""
    st = get_settings()
    check(
        "upload",
        f"{who.actor}|{client_address(request)}",
        st.ergplan_upload_rate_limit,
        st.ergplan_upload_rate_window_s,
    )
