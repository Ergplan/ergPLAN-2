"""OAuth 2.1 for the MCP connector, backed by the one administrator account of Stage 13.

What a connector (claude.ai, Claude Code) does, and what each step here answers:

1. `POST /mcp` without a token: 401 with `WWW-Authenticate: Bearer resource_metadata=...` (RFC 9728).
2. `GET /.well-known/oauth-protected-resource/mcp`: the resource and its authorization server.
3. `GET /.well-known/oauth-authorization-server` (RFC 8414): the endpoints below; PKCE S256 only.
4. `POST /oauth/register` (RFC 7591): a client id, and a secret only for a client that asks for one
   (stored as its SHA-256). Registering grants nothing.
5. `GET /oauth/authorize`: a page asking for the administrator's username and password. The same
   check, lockout and log as `POST /auth/login` (platform.admin): five wrong attempts from an address
   lock it for fifteen minutes, whichever of the two pages they were typed into. When password
   sign-in is switched off (ERGPLAN_ADMIN_USER or ERGPLAN_ADMIN_PASSWORD empty, or production over
   plain http) the page says so and nobody gets a code.
6. `POST /oauth/token`: the code (five minutes, once) and its PKCE verifier for an access token (one
   hour) and a refresh token (thirty days). A refresh token is spent when used and a new one comes
   back; a spent code or refresh token presented again revokes everything of that sign-in.
7. `POST /oauth/revoke` (RFC 7009): revokes the token and everything of the same sign-in.

Every token is stored as its SHA-256 only (`mcp_tokens`). A token is good only while password
sign-in is on, for the administrator it was issued to, and while that administrator's stored
password hash is the one it was issued under (a changed password ends every grant). The endpoints
that take input are rate-limited per address (platform.ratelimit, as sign-in).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..core.ids import new_id
from ..core.settings import get_settings
from ..db.base import session_scope
from ..db.models import McpClient, McpToken, User
from ..platform import admin
from ..platform.auth import sha
from ..platform.ratelimit import client_address, signin_limit

router = APIRouter(include_in_schema=False)

SCOPE = "ergplan"
CODE_LIFE = timedelta(minutes=5)
ACCESS_LIFE = timedelta(hours=1)
REFRESH_LIFE = timedelta(days=30)
GRANT_TYPES = ("authorization_code", "refresh_token")
AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
_VERIFIER = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9\-_]{43}$")  # base64url of a SHA-256, unpadded
_LOOPBACK = ("localhost", "127.0.0.1", "::1")


# ---------------------------------------------------------------- addresses


def resource_url() -> str:
    """The connector's address: what the person adds in Claude, and the audience of every token."""
    st = get_settings()
    return (st.ergplan_mcp_url.strip() or st.ergplan_public_url.rstrip("/") + "/mcp").rstrip("/")


def issuer() -> str:
    """The authorization server: the scheme and host of the connector's address."""
    p = urlsplit(resource_url())
    return f"{p.scheme}://{p.netloc}"


def resource_metadata_url() -> str:
    return issuer() + "/.well-known/oauth-protected-resource" + urlsplit(resource_url()).path


def closed_reason() -> str | None:
    """Why the MCP connector refuses everyone on this server, in words; None when it is open to the
    administrator."""
    st = get_settings()
    if not admin.username() or not st.ergplan_admin_password.get_secret_value():
        return (
            "the ergPLAN MCP connector is closed: it signs in with the administrator's username and "
            "password, and none is set on this server (ERGPLAN_ADMIN_USER and ERGPLAN_ADMIN_PASSWORD "
            "in .env, then restart the api)"
        )
    if admin.off_reason() is not None:
        return (
            "the ergPLAN MCP connector is closed: the administrator's password is accepted only over "
            "https, and this server is not served over https"
        )
    return None


# ---------------------------------------------------------------- the bearer check on /mcp


@dataclass(frozen=True)
class Grant:
    actor: str
    client_id: str
    grant_id: str
    scope: str


@dataclass(frozen=True)
class Refusal:
    reason: str
    error: str | None = None  # "invalid_token" when a token was given and is not good


def credential(user: User) -> str:
    return sha(user.password_hash or "")


def _current(s: Session, row: McpToken) -> bool:
    """The token's administrator is still the configured one, with the password it was issued under."""
    if row.actor != admin.username():
        return False
    user = s.get(User, row.actor)
    return user is not None and user.password_hash is not None and credential(user) == row.credential


def bearer(authorization: str | None) -> Grant | Refusal:
    """The grant behind an `Authorization: Bearer` header on /mcp, or why there is none."""
    closed = closed_reason()
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if closed is not None:
        return Refusal(closed, "invalid_token" if token else None)
    if not token:
        return Refusal("sign in first: add the connector in Claude and sign in as the administrator")
    with session_scope() as s:
        row = s.get(McpToken, sha(token))
        now = datetime.now(UTC)
        if row is None or row.kind != "access":
            return Refusal("the access token is not known", "invalid_token")
        if row.revoked_at is not None or row.expires_at <= now:
            return Refusal("the access token has expired or was revoked", "invalid_token")
        if row.resource != resource_url():
            return Refusal("the access token is for another resource", "invalid_token")
        if not _current(s, row):
            return Refusal("the administrator's sign-in has changed; sign in again", "invalid_token")
        return Grant(actor=row.actor, client_id=row.client_id, grant_id=row.grant_id, scope=row.scope)


def unauthorized(refusal: Refusal) -> JSONResponse:
    """401 with the pointer to the protected-resource metadata (RFC 9728 section 5.1)."""
    parts = [f'resource_metadata="{resource_metadata_url()}"', f'scope="{SCOPE}"']
    if refusal.error:
        desc = refusal.reason.replace('"', "'")
        parts += [f'error="{refusal.error}"', f'error_description="{desc}"']
    return JSONResponse(
        {"error": refusal.error or "unauthorized", "error_description": refusal.reason},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer " + ", ".join(parts), "Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------- metadata


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource() -> JSONResponse:
    return JSONResponse(
        {
            "resource": resource_url(),
            "authorization_servers": [issuer()],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": "jouleWise | ergPLAN",
        }
    )


@router.get("/.well-known/oauth-authorization-server")
def authorization_server() -> JSONResponse:
    base = issuer()
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "revocation_endpoint": f"{base}/oauth/revoke",
            "scopes_supported": [SCOPE],
            "response_types_supported": ["code"],
            "grant_types_supported": list(GRANT_TYPES),
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": list(AUTH_METHODS),
            "revocation_endpoint_auth_methods_supported": list(AUTH_METHODS),
            "authorization_response_iss_parameter_supported": True,
        }
    )


# ---------------------------------------------------------------- registration


def _error(
    error: str, description: str, status: int = 400, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


def redirect_allowed(uri: str) -> bool:
    """https to a host of `ERGPLAN_MCP_REDIRECT_HOSTS` (Claude's own, by default), or http to this
    machine (a local client such as Claude Code); no fragment."""
    try:
        p = urlsplit(uri)
    except ValueError:
        return False
    if p.fragment or not p.netloc or len(uri) > 2000:
        return False
    hosts = {h.strip().lower() for h in get_settings().ergplan_mcp_redirect_hosts.split(",") if h.strip()}
    return (p.scheme == "https" and (p.hostname or "").lower() in hosts) or (
        p.scheme == "http" and p.hostname in _LOOPBACK
    )


def _strings(v: Any, default: list[str]) -> list[str] | None:
    if v is None:
        return default
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return list(v)
    return None


@router.post("/oauth/register", dependencies=[Depends(signin_limit)])
async def register(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return _error("invalid_client_metadata", "the registration is a JSON object")
    if not isinstance(body, dict):
        return _error("invalid_client_metadata", "the registration is a JSON object")
    uris = _strings(body.get("redirect_uris"), [])
    if not uris or len(uris) > 10:
        return _error("invalid_redirect_uri", "one to ten redirect_uris are needed")
    bad = [u for u in uris if not redirect_allowed(u)]
    if bad:
        return _error(
            "invalid_redirect_uri",
            "a redirect URI is https to Claude (ERGPLAN_MCP_REDIRECT_HOSTS), or http to localhost: "
            + bad[0][:200],
        )
    grants = _strings(body.get("grant_types"), ["authorization_code"])
    if grants is None or "authorization_code" not in grants or any(g not in GRANT_TYPES for g in grants):
        return _error(
            "invalid_client_metadata", "grant_types: authorization_code, and refresh_token if wanted"
        )
    responses = _strings(body.get("response_types"), ["code"])
    if responses is None or any(r != "code" for r in responses):
        return _error("invalid_client_metadata", "response_types: code only")
    method = body.get("token_endpoint_auth_method", "client_secret_basic")
    if method not in AUTH_METHODS:
        return _error(
            "invalid_client_metadata", f"token_endpoint_auth_method: one of {', '.join(AUTH_METHODS)}"
        )
    name = body.get("client_name")
    name = name[:200] if isinstance(name, str) else None
    client_id = "mcp-" + secrets.token_urlsafe(24)
    secret = None if method == "none" else secrets.token_urlsafe(32)
    address = client_address(request)

    def store() -> datetime:
        with session_scope(actor="mcp-oauth") as s:
            row = McpClient(
                client_id=client_id,
                secret_sha256=sha(secret) if secret else None,
                client_name=name,
                redirect_uris=uris,
                grant_types=grants,
                auth_method=method,
                registered_from=address[:64],
            )
            s.add(row)
            s.flush()
            s.refresh(row)
            return row.created_at

    created = await run_in_threadpool(store)
    out: dict[str, Any] = {
        "client_id": client_id,
        "client_id_issued_at": int(created.timestamp()),
        "client_name": name,
        "redirect_uris": uris,
        "grant_types": grants,
        "response_types": ["code"],
        "token_endpoint_auth_method": method,
        "scope": SCOPE,
    }
    if secret:
        out |= {"client_secret": secret, "client_secret_expires_at": 0}
    return JSONResponse(out, status_code=201, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- the authorize page


@dataclass(frozen=True)
class _Ask:
    """A checked authorization request."""

    client_id: str
    client_name: str | None
    redirect_uri: str
    state: str | None
    code_challenge: str
    scope: str
    resource: str


def _with_query(uri: str, params: dict[str, str | None]) -> str:
    q = urlencode({k: v for k, v in params.items() if v is not None})
    return uri + ("&" if "?" in uri else "?") + q


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex'>"
        f"<title>{html.escape(title)} | jouleWise ergPLAN</title>"
        "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem;"
        "color:#1d2a33}h1{font-size:1.25rem}label{display:block;margin:.75rem 0 .25rem}"
        "input{width:100%;padding:.5rem;font:inherit;box-sizing:border-box}"
        "button{margin-top:1rem;padding:.5rem 1rem;font:inherit}.note{color:#5b6b75}"
        ".error{color:#a4262c}</style></head><body>"
        f"<p class='note'>jouleWise | ergPLAN</p><h1>{html.escape(title)}</h1>{body}</body></html>",
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def _redirect_error(ask_uri: str, state: str | None, error: str, description: str) -> RedirectResponse:
    return RedirectResponse(
        _with_query(
            ask_uri, {"error": error, "error_description": description, "state": state, "iss": issuer()}
        ),
        status_code=303,
    )


def _check(s: Session, q: dict[str, str]) -> _Ask | Response:
    """The authorization request, checked. Until the client and redirect URI are known good the
    answer is a page (never a redirect to an unchecked address); after, errors go back to the client."""
    client = s.get(McpClient, q.get("client_id", ""))
    if client is None:
        return _page(
            "Unknown connector", "<p>This connector is not registered here. Add it again in Claude.</p>", 400
        )
    uri = q.get("redirect_uri") or (client.redirect_uris[0] if len(client.redirect_uris) == 1 else "")
    if uri not in client.redirect_uris:
        return _page(
            "Unknown return address",
            "<p>The connector asked to return to an address it did not register.</p>",
            400,
        )
    state = q.get("state")
    if q.get("response_type") != "code":
        return _redirect_error(uri, state, "unsupported_response_type", "response_type is code")
    challenge = q.get("code_challenge", "")
    if q.get("code_challenge_method") != "S256" or not _CHALLENGE.match(challenge):
        return _redirect_error(
            uri, state, "invalid_request", "PKCE with code_challenge_method S256 is required"
        )
    resource = q.get("resource") or resource_url()
    if resource.rstrip("/") != resource_url():
        return _redirect_error(
            uri, state, "invalid_target", "this server grants access to its own MCP address only"
        )
    scopes = set((q.get("scope") or SCOPE).split())
    if not scopes <= {SCOPE}:
        return _redirect_error(uri, state, "invalid_scope", f"the only scope is {SCOPE}")
    return _Ask(client.client_id, client.client_name, uri, state, challenge, SCOPE, resource_url())


def _form(ask: _Ask, message: str = "") -> str:
    hidden = {
        "client_id": ask.client_id,
        "redirect_uri": ask.redirect_uri,
        "state": ask.state or "",
        "code_challenge": ask.code_challenge,
        "code_challenge_method": "S256",
        "response_type": "code",
        "scope": ask.scope,
        "resource": ask.resource,
    }
    fields = "".join(
        f"<input type='hidden' name='{k}' value='{html.escape(v, quote=True)}'>" for k, v in hidden.items()
    )
    who = html.escape(ask.client_name or "A connector")
    return (
        f"<p>{who} asks to use ergPLAN for you: read bills through billx, answer the plan's questions, "
        "size plans and share them. Sign in as the administrator to allow it.</p>"
        # the name is the connector's own claim; where the sign-in returns is not
        f"<p class='note'>After you allow it, you return to "
        f"<strong>{html.escape(urlsplit(ask.redirect_uri).netloc)}</strong>. Allow it only if that is "
        "Claude (claude.ai) or your own computer (localhost).</p>"
        + (f"<p class='error'>{html.escape(message)}</p>" if message else "")
        + f"<form method='post' action='{html.escape(issuer())}/oauth/authorize'>{fields}"
        "<label for='u'>Username</label><input id='u' name='username' autocomplete='username' required>"
        "<label for='p'>Password</label>"
        "<input id='p' name='password' type='password' autocomplete='current-password' required>"
        "<button type='submit' name='decision' value='allow'>Sign in and allow</button> "
        "<button type='submit' name='decision' value='deny' formnovalidate>Deny</button></form>"
    )


def _authorize_page(q: dict[str, str]) -> Response:
    with session_scope() as s:
        ask = _check(s, q)
    if not isinstance(ask, _Ask):
        return ask
    closed = closed_reason()
    if closed is not None:
        return _page("The connector is closed", f"<p>{html.escape(closed)}.</p>", 403)
    return _page("Allow the connector", _form(ask))


@router.get("/oauth/authorize", dependencies=[Depends(signin_limit)])
async def authorize(request: Request) -> Response:
    return await run_in_threadpool(_authorize_page, dict(request.query_params))


def _sign_in(form: dict[str, str], address: str) -> Response:
    now = datetime.now(UTC)
    with session_scope(actor="auth") as s:
        ask = _check(s, form)
        if not isinstance(ask, _Ask):
            return ask
        if form.get("decision") == "deny":
            return _redirect_error(
                ask.redirect_uri, ask.state, "access_denied", "the administrator denied it"
            )
        username = form.get("username", "")[:254]
        closed = closed_reason()
        if closed is not None:
            admin.record(s, address, username, "off", now)
            return _page("The connector is closed", f"<p>{html.escape(closed)}.</p>", 403)
        admin.serialise(s, address)
        if admin.locked_until(s, address, now) is not None:
            admin.record(s, address, username, "locked", now)
            return _page(
                "Locked for fifteen minutes",
                "<p>Too many wrong attempts from this address; sign-in is locked for fifteen minutes "
                "from the fifth.</p>",
                429,
            )
        user = admin.check_password(s, username, form.get("password", "")[:200])
        if user is None:
            admin.record(s, address, username, "wrong", now)
            return _page("Allow the connector", _form(ask, "That username and password do not match."), 401)
        admin.record(s, address, username, "ok", now)
        code = secrets.token_urlsafe(32)
        s.add(
            McpToken(
                token_sha256=sha(code),
                kind="code",
                grant_id=new_id(),
                client_id=ask.client_id,
                actor=user.email,
                credential=credential(user),
                scope=ask.scope,
                resource=ask.resource,
                redirect_uri=ask.redirect_uri,
                code_challenge=ask.code_challenge,
                expires_at=now + CODE_LIFE,
            )
        )
        return RedirectResponse(
            _with_query(ask.redirect_uri, {"code": code, "state": ask.state, "iss": issuer()}),
            status_code=303,
        )


@router.post("/oauth/authorize", dependencies=[Depends(signin_limit)])
async def authorize_sign_in(request: Request) -> Response:
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    return await run_in_threadpool(_sign_in, form, client_address(request))


# ---------------------------------------------------------------- tokens


def _client(s: Session, form: dict[str, str], authorization: str | None) -> McpClient | JSONResponse:
    """The client, authenticated as it registered: by its secret (Basic or in the form), or, for a
    public client, by its id alone (PKCE proves the rest)."""
    given_id, secret, basic = form.get("client_id"), form.get("client_secret"), False
    if authorization and authorization.lower().startswith("basic "):
        try:
            raw = base64.b64decode(authorization[6:].strip(), validate=True).decode()
            cid, _, sec = raw.partition(":")
            given_id, secret, basic = unquote(cid), unquote(sec), True
        except (ValueError, UnicodeDecodeError):
            return _error("invalid_client", "the Basic credentials cannot be read", 401)
    client = s.get(McpClient, given_id or "")
    challenge = {"WWW-Authenticate": "Basic"} if basic else None
    if client is None:
        return _error("invalid_client", "this client is not registered", 401, challenge)
    if client.auth_method != "none" and (
        not secret
        or client.secret_sha256 is None
        or not hmac.compare_digest(sha(secret), client.secret_sha256)
    ):
        return _error("invalid_client", "the client's secret does not match", 401, challenge)
    return client


def _revoke_grant(s: Session, grant_id: str, now: datetime) -> None:
    s.execute(
        update(McpToken)
        .where(McpToken.grant_id == grant_id, McpToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )


def _issue(s: Session, parent: McpToken, client: McpClient, now: datetime) -> dict[str, Any]:
    access = secrets.token_urlsafe(32)
    common = {
        "grant_id": parent.grant_id,
        "client_id": client.client_id,
        "actor": parent.actor,
        "credential": parent.credential,
        "scope": parent.scope,
        "resource": parent.resource,
    }
    s.add(McpToken(token_sha256=sha(access), kind="access", expires_at=now + ACCESS_LIFE, **common))
    out: dict[str, Any] = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_LIFE.total_seconds()),
        "scope": parent.scope,
    }
    if "refresh_token" in client.grant_types:
        refresh = secrets.token_urlsafe(32)
        s.add(McpToken(token_sha256=sha(refresh), kind="refresh", expires_at=now + REFRESH_LIFE, **common))
        out["refresh_token"] = refresh
    return out


def pkce_ok(verifier: str, challenge: str) -> bool:
    if not _VERIFIER.match(verifier):
        return False
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return hmac.compare_digest(digest, challenge)


def _token(form: dict[str, str], authorization: str | None) -> JSONResponse:
    now = datetime.now(UTC)
    with session_scope(actor="mcp-oauth") as s:
        client = _client(s, form, authorization)
        if isinstance(client, JSONResponse):
            return client
        grant = form.get("grant_type")
        if grant not in GRANT_TYPES or grant not in client.grant_types:
            return _error("unsupported_grant_type", "grant_type: authorization_code or refresh_token")
        presented = form.get("code" if grant == "authorization_code" else "refresh_token", "")
        kind = "code" if grant == "authorization_code" else "refresh"
        row = s.get(McpToken, sha(presented), with_for_update=True) if presented else None
        if row is None or row.kind != kind or row.client_id != client.client_id:
            return _error("invalid_grant", f"the {kind} is not known for this client")
        if row.used_at is not None:
            # a spent code or refresh token again: someone may hold a copy; end the whole sign-in
            _revoke_grant(s, row.grant_id, now)
            s.commit()
            return _error("invalid_grant", f"this {kind} was used already; sign in again")
        if row.revoked_at is not None or row.expires_at <= now:
            return _error("invalid_grant", f"the {kind} has expired or was revoked")
        if grant == "authorization_code":
            if form.get("redirect_uri") and form["redirect_uri"] != row.redirect_uri:
                return _error("invalid_grant", "redirect_uri is not the one the code was issued to")
            if not pkce_ok(form.get("code_verifier", ""), row.code_challenge or ""):
                return _error("invalid_grant", "the PKCE code_verifier does not match the code_challenge")
        if form.get("resource") and form["resource"].rstrip("/") != row.resource:
            return _error("invalid_target", "this server grants access to its own MCP address only")
        if form.get("scope") and not set(form["scope"].split()) <= set(row.scope.split()):
            return _error("invalid_scope", f"the only scope is {SCOPE}")
        row.used_at = now
        if closed_reason() is not None or not _current(s, row):
            _revoke_grant(s, row.grant_id, now)
            return _error(
                "invalid_grant", closed_reason() or "the administrator's sign-in has changed; sign in again"
            )
        return JSONResponse(
            _issue(s, row, client, now), headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
        )


@router.post("/oauth/token", dependencies=[Depends(signin_limit)])
async def token(request: Request) -> JSONResponse:
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    return await run_in_threadpool(_token, form, request.headers.get("authorization"))


def _revoke(form: dict[str, str], authorization: str | None) -> JSONResponse:
    with session_scope(actor="mcp-oauth") as s:
        client = _client(s, form, authorization)
        if isinstance(client, JSONResponse):
            return client
        presented = form.get("token")
        if not presented:
            return _error("invalid_request", "token is required")
        row = s.get(McpToken, sha(presented))
        if row is not None and row.client_id == client.client_id:
            _revoke_grant(s, row.grant_id, datetime.now(UTC))
    return JSONResponse({}, headers={"Cache-Control": "no-store"})


@router.post("/oauth/revoke", dependencies=[Depends(signin_limit)])
async def revoke(request: Request) -> JSONResponse:
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    return await run_in_threadpool(_revoke, form, request.headers.get("authorization"))
