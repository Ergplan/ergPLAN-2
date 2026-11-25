"""Stage 13: rate limits on the sign-in routes (per address) and on bill upload (per signed-in
account and address). The limits are settings; conftest empties the windows before every test."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ergplan.core.settings import get_settings
from ergplan.platform import ratelimit
from ergplan.platform.app import app


@pytest.fixture
def proxied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "ergplan_trusted_proxies", "testclient")


def test_the_window_slides_and_says_when_to_come_back(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock[0])
    for _ in range(3):
        ratelimit.check("t", "a", 3, 60)
    ratelimit.check("t", "b", 3, 60)  # another key has its own window
    with pytest.raises(HTTPException) as refused:
        ratelimit.check("t", "a", 3, 60)
    assert refused.value.status_code == 429 and refused.value.headers == {"Retry-After": "60"}
    clock[0] += 45
    with pytest.raises(HTTPException) as refused:
        ratelimit.check("t", "a", 3, 60)
    assert refused.value.headers == {"Retry-After": "15"}
    clock[0] += 15
    ratelimit.check("t", "a", 3, 60)  # the first three have left the window
    ratelimit.check("t", "a", 0, 60)  # 0 switches a limit off
    ratelimit.reset()
    ratelimit.check("t", "a", 1, 60)


@pytest.mark.parametrize(
    ("path", "body", "under_limit"),
    [
        ("/auth/login", {"username": "someone", "password": "fixture-only-guess"}, 403),
        ("/auth/request", {"email": "ops@acme.in"}, 200),
        ("/auth/verify", {"token": "x" * 40}, 401),
    ],
)
def test_sign_in_routes_are_rate_limited_per_address(
    proxied: None, monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, str], under_limit: int
) -> None:
    monkeypatch.setattr(get_settings(), "ergplan_signin_rate_limit", 3)
    c = TestClient(app)
    here = {"X-Forwarded-For": "203.0.113.50"}
    assert [c.post(path, json=body, headers=here).status_code for _ in range(3)] == [under_limit] * 3
    r = c.post(path, json=body, headers=here)
    assert r.status_code == 429 and "too many requests" in r.json()["detail"]
    assert 1 <= int(r.headers["retry-after"]) <= 60
    # another address is not held back, and neither is another sign-in route from this one
    assert c.post(path, json=body, headers={"X-Forwarded-For": "203.0.113.51"}).status_code == under_limit
    other = "/auth/verify" if path != "/auth/verify" else "/auth/request"
    assert c.post(other, json={"token": "x" * 40, "email": "a@b.in"}, headers=here).status_code != 429
    # reading who is signed in is not a sign-in attempt
    assert [c.get("/auth/me", headers=here).status_code for _ in range(6)] == [200] * 6


def test_bill_upload_is_rate_limited_per_account_and_address(
    proxied: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "ergplan_upload_rate_limit", 2)
    c = TestClient(app)

    def upload(path: str, actor: str, address: str) -> int:
        return c.post(
            path,
            files={"file": ("x.txt", b"not a pdf", "text/plain")},  # refused as not a PDF, and counted
            headers={"X-Ergplan-Actor": actor, "X-Forwarded-For": address},
        ).status_code

    assert upload("/bills", "ops@acme.in", "203.0.113.60") == 415
    assert upload("/bill-reads", "ops@acme.in", "203.0.113.60") == 415  # the two routes share the count
    for path in ("/bills", "/bill-reads"):
        r = c.post(
            path,
            files={"file": ("x.txt", b"not a pdf", "text/plain")},
            headers={"X-Ergplan-Actor": "ops@acme.in", "X-Forwarded-For": "203.0.113.60"},
        )
        assert r.status_code == 429 and "too many requests" in r.json()["detail"]
        assert 1 <= int(r.headers["retry-after"]) <= get_settings().ergplan_upload_rate_window_s
    # another account at the same address, and the same account at another address, go through
    assert upload("/bills", "other@acme.in", "203.0.113.60") == 415
    assert upload("/bills", "ops@acme.in", "203.0.113.61") == 415


def test_the_address_comes_from_the_proxy_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.requests import Request

    def address(peer: str, forwarded: str | None) -> str:
        headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded is not None else []
        return ratelimit.client_address(Request({"type": "http", "client": (peer, 1234), "headers": headers}))

    monkeypatch.setattr(get_settings(), "ergplan_trusted_proxies", "172.16.0.0/12, 127.0.0.1")
    assert address("172.18.0.5", "203.0.113.9") == "203.0.113.9"  # Caddy on the compose network
    assert address("172.18.0.5", "10.9.9.9, 203.0.113.9") == "203.0.113.9"  # the proxy's entry is the last
    assert address("172.18.0.5", None) == "172.18.0.5"
    assert address("172.18.0.5", "not-an-address") == "172.18.0.5"
    assert address("127.0.0.1", "2001:db8::1") == "2001:db8::1"
    assert address("198.51.100.1", "203.0.113.9") == "198.51.100.1"  # a direct caller's header is ignored
    monkeypatch.setattr(get_settings(), "ergplan_trusted_proxies", "")
    assert address("172.18.0.5", "203.0.113.9") == "172.18.0.5"
