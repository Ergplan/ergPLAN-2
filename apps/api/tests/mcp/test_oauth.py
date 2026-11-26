"""Stage 16: who may use the MCP server. OAuth 2.1 for the connector, backed by the Stage 13
administrator account: metadata, dynamic registration, authorization code with PKCE S256, refresh
tokens that rotate, revocation; every /mcp request without a good token is refused with the pointer
to the resource metadata; with password sign-in switched off nobody gets in, and the answer says why.
The administrator here is made up inside each test."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl, SecretStr
from sqlalchemy import select, text

from ergplan.core.settings import get_settings
from ergplan.db.base import get_engine, session_scope
from ergplan.db.models import McpToken, SignInAttempt
from ergplan.platform.app import app
from tests.mcp import helpers as h


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> h.Admin:
    return h.configure(monkeypatch)


def _mcp(c: Any, token: str | None) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    return c.post("/mcp", json=body, headers=headers)  # type: ignore[no-any-return]


def test_metadata_points_the_connector_at_the_sign_in(admin: h.Admin) -> None:
    c = h.client()
    pr = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert pr["resource"] == h.MCP_URL and pr["authorization_servers"] == [h.BASE]
    assert c.get("/.well-known/oauth-protected-resource").json() == pr
    asm = c.get("/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == h.BASE
    assert asm["authorization_endpoint"] == f"{h.BASE}/oauth/authorize"
    assert asm["token_endpoint"] == f"{h.BASE}/oauth/token"
    assert asm["registration_endpoint"] == f"{h.BASE}/oauth/register"
    assert asm["revocation_endpoint"] == f"{h.BASE}/oauth/revoke"
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert set(asm["grant_types_supported"]) == {"authorization_code", "refresh_token"}


def test_a_request_without_sign_in_is_refused(admin: h.Admin) -> None:
    c = h.client()
    r = _mcp(c, None)
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    assert (
        www.startswith("Bearer ")
        and f'resource_metadata="{h.BASE}/.well-known/oauth-protected-resource/mcp"' in www
    )
    assert "sign in first" in r.json()["error_description"]
    # a made-up token, and the web app's own session token, are not MCP tokens
    r = _mcp(c, "not-a-token")
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]
    login = c.post("/auth/login", json={"username": admin.username, "password": admin.password})
    assert login.status_code == 200
    session = login.cookies.get("ergplan_session") or c.cookies.get("ergplan_session")
    assert session
    assert _mcp(h.client(), session).status_code == 401
    # GET and DELETE too
    assert c.get("/mcp").status_code == 401
    assert c.delete("/mcp").status_code == 401


def test_in_production_the_cookie_guard_lets_the_token_check_answer(
    admin: h.Admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    st = get_settings()
    monkeypatch.setattr(st, "ergplan_env", "prod")
    monkeypatch.setattr(st, "ergplan_public_url", "https://plan.example.in")
    monkeypatch.setattr(st, "ergplan_mcp_url", "https://plan.example.in/mcp")
    c = h.client()
    r = _mcp(c, None)
    assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]
    assert c.get("/.well-known/oauth-authorization-server").json()["issuer"] == "https://plan.example.in"
    # production over plain http: the password is never taken, so the connector is closed
    monkeypatch.setattr(st, "ergplan_public_url", "http://plan.example.in")
    r = _mcp(c, None)
    assert r.status_code == 401 and "not served over https" in r.json()["error_description"]


def test_with_password_sign_in_switched_off_nobody_gets_in(
    admin: h.Admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = h.client()
    t = h.tokens(c, admin)
    assert _mcp(c, t["access_token"]).status_code == 200
    st = get_settings()
    monkeypatch.setattr(st, "ergplan_admin_password", SecretStr(""))
    r = _mcp(c, t["access_token"])
    assert r.status_code == 401
    assert "ERGPLAN_ADMIN_USER and ERGPLAN_ADMIN_PASSWORD" in r.json()["error_description"]
    # the sign-in page says why, and no code is issued
    reg = h.register(c)
    _, challenge = h.pkce()
    page = c.get(
        "/oauth/authorize",
        params={
            "client_id": reg["client_id"],
            "redirect_uri": h.REDIRECT,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "x",
        },
    )
    assert page.status_code == 403 and "connector is closed" in page.text
    assert h.authorize(c, reg, admin, challenge).status_code == 403
    # the refresh token is refused too
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": t["refresh_token"],
            "client_id": t["client_id"],
        },
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


class _Memory:
    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client = client_info


def test_the_oauth_flow_end_to_end_with_the_sdk_client(admin: h.Admin) -> None:
    """The SDK's own OAuth client does what a connector does: meets the 401, reads the metadata,
    registers, sends the person to the sign-in page (here the test types the administrator's
    password), exchanges the code with PKCE, and calls a tool. Then refresh, then revoke."""
    store = _Memory()
    got: dict[str, str] = {}

    async def redirect_handler(url: str) -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=h.BASE) as web:
            page = await web.get(url)
            assert page.status_code == 200 and "Sign in and allow" in page.text
            assert "you return to <strong>claude.ai</strong>" in page.text
            q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            assert q["code_challenge_method"] == "S256" and q["resource"] == h.MCP_URL
            r = await web.post(
                "/oauth/authorize",
                data=q | {"username": admin.username, "password": admin.password, "decision": "allow"},
            )
            assert r.status_code == 303
            back = parse_qs(urlsplit(r.headers["location"]).query)
            assert back["iss"] == [h.BASE]
            got["code"], got["state"] = back["code"][0], back["state"][0]

    async def callback_handler() -> tuple[str, str | None]:
        return got["code"], got["state"]

    auth = OAuthClientProvider(
        server_url=h.MCP_URL,
        client_metadata=OAuthClientMetadata(
            redirect_uris=[AnyUrl(h.REDIRECT)],
            client_name="SDK test client",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="client_secret_post",
        ),
        storage=store,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    async def session() -> list[str]:
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=h.BASE, auth=auth) as http,
            streamable_http_client(h.MCP_URL, http_client=http) as (read, write, _),
            ClientSession(read, write) as s,
        ):
            init = await s.initialize()
            assert init.instructions and "one or two at a time" in init.instructions.lower()
            return [t.name for t in (await s.list_tools()).tools]

    names = anyio.run(session)
    assert "read_bill" in names and "run_sizing" in names
    assert store.tokens is not None and store.tokens.refresh_token and store.client is not None
    assert store.client.client_secret  # registered with a secret, sent in the form
    # only hashes are stored
    with get_engine().connect() as conn:
        dump = " ".join(str(r) for r in conn.execute(text("select * from mcp_tokens")).all())
        dump += " ".join(str(r) for r in conn.execute(text("select * from mcp_clients")).all())
    for secret in (store.tokens.access_token, store.tokens.refresh_token, store.client.client_secret):
        assert secret not in dump

    c = h.client()
    cid, csec = store.client.client_id, store.client.client_secret
    # refresh: a new pair; the old refresh token is spent
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": store.tokens.refresh_token,
            "client_id": cid,
            "client_secret": csec,
        },
    )
    assert r.status_code == 200, r.text
    fresh = r.json()
    assert fresh["token_type"] == "Bearer" and fresh["expires_in"] == 3600
    assert fresh["refresh_token"] != store.tokens.refresh_token
    assert _mcp(c, fresh["access_token"]).status_code == 200
    # revoke: the access token and its refresh token stop working
    r = c.post(
        "/oauth/revoke", data={"token": fresh["refresh_token"], "client_id": cid, "client_secret": csec}
    )
    assert r.status_code == 200
    assert _mcp(c, fresh["access_token"]).status_code == 401
    assert _mcp(c, store.tokens.access_token).status_code == 401  # the same sign-in
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": fresh["refresh_token"],
            "client_id": cid,
            "client_secret": csec,
        },
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_codes_and_refresh_tokens_work_once(admin: h.Admin) -> None:
    c = h.client()
    reg = h.register(c)
    verifier, challenge = h.pkce()
    code = h.code_of(h.authorize(c, reg, admin, challenge))
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": h.REDIRECT,
        "client_id": reg["client_id"],
    }
    # a wrong verifier is refused (and does not spend the code)
    bad = c.post("/oauth/token", data=form | {"code_verifier": h.pkce()[0]})
    assert bad.status_code == 400 and "PKCE" in bad.json()["error_description"]
    ok = c.post("/oauth/token", data=form | {"code_verifier": verifier})
    assert ok.status_code == 200
    first = ok.json()
    # the code again: refused, and everything of that sign-in is revoked
    again = c.post("/oauth/token", data=form | {"code_verifier": verifier})
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    assert _mcp(c, first["access_token"]).status_code == 401

    t = h.tokens(c, admin)
    rf = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": t["client_id"]}
    second = c.post("/oauth/token", data=rf)
    assert second.status_code == 200
    # a spent refresh token presented again: someone holds a copy; the new tokens die with it
    assert c.post("/oauth/token", data=rf).status_code == 400
    assert _mcp(c, second.json()["access_token"]).status_code == 401
    # another client cannot use this client's code or token
    other = h.register(c)
    rf2 = {
        "grant_type": "refresh_token",
        "refresh_token": second.json()["refresh_token"],
        "client_id": other["client_id"],
    }
    assert c.post("/oauth/token", data=rf2).json()["error"] == "invalid_grant"


def test_access_tokens_expire_and_a_changed_password_ends_every_grant(
    admin: h.Admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = h.client()
    t = h.tokens(c, admin)
    assert _mcp(c, t["access_token"]).status_code == 200
    with session_scope() as s:
        row = s.scalars(select(McpToken).where(McpToken.kind == "access")).one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    r = _mcp(c, t["access_token"])
    assert r.status_code == 401 and "expired" in r.json()["error_description"]

    t = h.tokens(c, admin)
    monkeypatch.setattr(get_settings(), "ergplan_admin_password", SecretStr("test-only-another-" + "x" * 12))
    from ergplan.platform import admin as admin_account

    with session_scope(actor="auth") as s:
        admin_account.ensure(s)  # what the API does at its next start
    assert _mcp(c, t["access_token"]).status_code == 401


def test_the_sign_in_page_checks_the_request_and_the_password(admin: h.Admin) -> None:
    c = h.client()
    reg = h.register(c)
    _, challenge = h.pkce()
    q = {
        "client_id": reg["client_id"],
        "redirect_uri": h.REDIRECT,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "s-123",
    }
    assert c.get("/oauth/authorize", params=q).status_code == 200
    # an unknown client or an address it did not register: a page, never a redirect
    assert c.get("/oauth/authorize", params=q | {"client_id": "nobody"}).status_code == 400
    assert (
        c.get("/oauth/authorize", params=q | {"redirect_uri": "https://evil.example/cb"}).status_code == 400
    )
    # no PKCE, or plain PKCE: back to the client with the error
    r = c.get("/oauth/authorize", params=q | {"code_challenge_method": "plain"})
    assert r.status_code == 303 and "error=invalid_request" in r.headers["location"]
    r = c.get("/oauth/authorize", params=q | {"resource": "https://elsewhere.example/mcp"})
    assert "error=invalid_target" in r.headers["location"]
    # deny
    r = c.post("/oauth/authorize", data=q | {"decision": "deny"})
    assert r.status_code == 303 and "error=access_denied" in r.headers["location"]
    # a wrong password: the page again, and the attempt in the sign-in log
    r = h.authorize(c, reg, admin, challenge, password="test-only-wrong")
    assert r.status_code == 401 and "do not match" in r.text and "code=" not in r.headers.get("location", "")
    with session_scope() as s:
        outcomes = [a.outcome for a in s.scalars(select(SignInAttempt))]
    assert outcomes == ["wrong"]


def test_wrong_passwords_lock_the_address_with_the_web_sign_in(
    admin: h.Admin, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger("ergplan.signin").disabled = False
    caplog.set_level(logging.INFO)
    c = h.client()
    reg = h.register(c)
    _, challenge = h.pkce()
    for _ in range(3):
        assert h.authorize(c, reg, admin, challenge, password="test-only-wrong").status_code == 401
    for _ in range(2):  # the same count as the web app's password sign-in
        c.post("/auth/login", json={"username": admin.username, "password": "test-only-wrong"})
    r = h.authorize(c, reg, admin, challenge)
    assert r.status_code == 429 and "Locked" in r.text
    assert admin.password not in caplog.text


def test_registration_checks_the_metadata_and_is_rate_limited(
    admin: h.Admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = h.client()
    assert c.post("/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]}).status_code == 400
    # https only to Claude's own hosts: a client someone else registers cannot collect the grant at
    # an address of its choosing (ERGPLAN_MCP_REDIRECT_HOSTS widens the list)
    assert c.post("/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]}).status_code == 400
    assert (
        c.post("/oauth/register", json={"redirect_uris": ["https://claude.ai.evil.example/cb"]}).status_code
        == 400
    )
    assert (
        c.post(
            "/oauth/register", json={"redirect_uris": ["https://claude.com/api/mcp/auth_callback"]}
        ).status_code
        == 201
    )
    assert c.post("/oauth/register", json={"redirect_uris": []}).status_code == 400
    assert (
        c.post(
            "/oauth/register", json={"redirect_uris": [h.REDIRECT], "grant_types": ["password"]}
        ).status_code
        == 400
    )
    local = c.post(
        "/oauth/register",
        json={"redirect_uris": ["http://localhost:33418/callback"], "token_endpoint_auth_method": "none"},
    )
    assert local.status_code == 201 and "client_secret" not in local.json()
    basic = c.post(
        "/oauth/register",
        json={"redirect_uris": [h.REDIRECT], "grant_types": ["authorization_code", "refresh_token"]},
    )
    assert (
        basic.json()["token_endpoint_auth_method"] == "client_secret_basic" and basic.json()["client_secret"]
    )
    # a confidential client proves itself with its secret (Basic)
    reg = basic.json()
    verifier, challenge = h.pkce()
    code = h.code_of(h.authorize(c, reg, admin, challenge))
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": h.REDIRECT,
        "code_verifier": verifier,
    }
    assert c.post("/oauth/token", data=form, auth=(reg["client_id"], "wrong")).status_code == 401
    assert c.post("/oauth/token", data=form, auth=(reg["client_id"], reg["client_secret"])).status_code == 200
    monkeypatch.setattr(get_settings(), "ergplan_signin_rate_limit", 3)
    codes = [c.post("/oauth/register", json={"redirect_uris": [h.REDIRECT]}).status_code for _ in range(4)]
    assert codes[-1] == 429
