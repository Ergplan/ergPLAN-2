"""The MCP tests' tools: an administrator account made up inside the test (never a real one), the
OAuth flow by hand, and the official SDK's client speaking to the app in process."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from pydantic import SecretStr

from ergplan.core.settings import get_settings
from ergplan.platform.app import app

BASE = "http://testserver"
MCP_URL = f"{BASE}/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@dataclass(frozen=True)
class Admin:
    username: str
    password: str


def configure(monkeypatch: pytest.MonkeyPatch) -> Admin:
    """A throwaway administrator, generated here, and the connector's address in the test server."""
    who = Admin("admin-" + secrets.token_hex(4), "test-only-" + secrets.token_urlsafe(18))
    st = get_settings()
    monkeypatch.setattr(st, "ergplan_admin_user", who.username)
    monkeypatch.setattr(st, "ergplan_admin_password", SecretStr(who.password))
    monkeypatch.setattr(st, "ergplan_mcp_url", MCP_URL)
    return who


def client() -> TestClient:
    return TestClient(app, base_url=BASE, follow_redirects=False)


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(c: TestClient, method: str = "none", grants: list[str] | None = None) -> dict[str, Any]:
    r = c.post(
        "/oauth/register",
        json={
            "redirect_uris": [REDIRECT],
            "client_name": "Claude",
            "token_endpoint_auth_method": method,
            "grant_types": grants or ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


def authorize(
    c: TestClient, reg: dict[str, Any], who: Admin, challenge: str, password: str | None = None
) -> httpx.Response:
    form = {
        "client_id": reg["client_id"],
        "redirect_uri": REDIRECT,
        "state": "s-123",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "response_type": "code",
        "scope": "ergplan",
        "resource": MCP_URL,
        "username": who.username,
        "password": who.password if password is None else password,
        "decision": "allow",
    }
    return c.post("/oauth/authorize", data=form)


def code_of(r: httpx.Response) -> str:
    assert r.status_code == 303, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["state"] == ["s-123"]
    return q["code"][0]


def tokens(c: TestClient, who: Admin) -> dict[str, Any]:
    """Register, sign in and exchange the code: what a connector holds after the person allows it."""
    reg = register(c)
    verifier, challenge = pkce()
    code = code_of(authorize(c, reg, who, challenge))
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": reg["client_id"],
            "code_verifier": verifier,
            "resource": MCP_URL,
        },
    )
    assert r.status_code == 200, r.text
    return dict(r.json()) | {"client_id": reg["client_id"]}


async def _call(token: str | None, name: str, args: dict[str, Any]) -> CallToolResult:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE, headers=headers, timeout=600
        ) as http,
        streamable_http_client(MCP_URL, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return await session.call_tool(name, args)


def call(token: str, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """A tool through the real /mcp endpoint; its structured result (an error result fails)."""
    res = anyio.run(_call, token, name, args or {})
    assert not res.isError, res.content
    assert res.structuredContent is not None
    out = res.structuredContent
    # the text content is the same result, for clients that read only text
    assert json.loads(res.content[0].text) == out  # type: ignore[union-attr]
    return dict(out)


def call_error(token: str, name: str, args: dict[str, Any] | None = None) -> str:
    res = anyio.run(_call, token, name, args or {})
    assert res.isError
    return str(res.content[0].text)  # type: ignore[union-attr]


async def _tools(token: str) -> list[Any]:
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE,
            headers={"Authorization": f"Bearer {token}"},
        ) as http,
        streamable_http_client(MCP_URL, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        listed = await session.list_tools()
        return [init, listed.tools]


def initialize_and_list(token: str) -> tuple[Any, list[Any]]:
    init, tools = anyio.run(_tools, token)
    return init, tools
