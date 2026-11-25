"""Sign-in. Clients and analysts by a one-time emailed link (Stage 8: POST /auth/request sends it,
15 minutes; POST /auth/verify turns it into a session). The one administrator by username and
password (Stage 13: POST /auth/login; the account, its lockout and its log are platform/admin.py).
Either way the session is a cookie (30 days, HttpOnly, SameSite=Lax, Secure on https). The three
sign-in routes are rate-limited per address (platform/ratelimit.py). Nobody else has a password:
the older per-person passwords (POST /auth/password, POST /auth/users) were retired in Stage 13."""

from __future__ import annotations

import secrets
import smtplib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ...core.settings import get_settings
from ...db.base import session_scope
from ...db.models import AuthSession, LoginToken, User
from .. import admin, mail
from ..auth import COOKIE, Caller, caller, production, role_for, sha
from ..ratelimit import client_address, signin_limit

router = APIRouter(prefix="/auth", tags=["sign-in"])
LINK_MINUTES = 15
SESSION_DAYS = 30


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(pattern=r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,}$")
    next: str = Field(default="/plan/intake", pattern=r"^/[^\s]{0,200}$")


class RequestOut(BaseModel):
    sent: bool
    dev_link: str | None = None  # development only, when no SMTP is configured


class VerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=200)


class MeOut(BaseModel):
    email: str | None  # the administrator: the username
    role: str
    signed_in: bool
    required: bool = False  # production: sign-in needed outside the shared plan
    password_sign_in: bool = False  # the server has an administrator account: show the password form
    # for a signed-in analyst only: what is wrong with the server's sign-in set-up (a weak password)
    warnings: list[str] = Field(default_factory=list)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=200)


def _me(email: str | None, role: str, signed_in: bool) -> MeOut:
    return MeOut(
        email=email,
        role=role,
        signed_in=signed_in,
        required=production(),
        password_sign_in=admin.enabled(),
        warnings=admin.weak_reasons() if signed_in and role == "analyst" else [],
    )


def _start_session(s: Any, user: User, response: Response, now: datetime) -> MeOut:
    user.last_login_at = now
    s.flush()
    cookie = secrets.token_urlsafe(32)
    s.add(
        AuthSession(token_sha256=sha(cookie), email=user.email, expires_at=now + timedelta(days=SESSION_DAYS))
    )
    response.set_cookie(
        COOKIE,
        cookie,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        secure=secure_cookie(),
        samesite="lax",
        path="/",
    )
    return _me(user.email, user.role, True)


def secure_cookie() -> bool:
    """Secure whenever the site is served over https (a LAN server on plain http cannot set one)."""
    return get_settings().ergplan_public_url.startswith("https://")


def issue_link(email: str, next_path: str = "/plan/intake", actor: str | None = None) -> str:
    """A one-time sign-in link for an email (15 minutes); only its SHA-256 is stored. Used by
    /auth/request and by the server operator's `python -m ergplan.platform.signin`."""
    email = email.strip().lower()
    token = secrets.token_urlsafe(32)
    with session_scope(actor=actor or email) as s:
        s.add(
            LoginToken(
                token_sha256=sha(token),
                email=email,
                expires_at=datetime.now(UTC) + timedelta(minutes=LINK_MINUTES),
            )
        )
    return f"{get_settings().ergplan_public_url.rstrip('/')}/login/verify?token={token}&next={next_path}"


@router.post("/request", response_model=RequestOut, dependencies=[Depends(signin_limit)])
def request_link(body: RequestIn) -> RequestOut:
    email = body.email.strip().lower()
    if production() and not get_settings().ergplan_smtp_url:
        # no way to deliver the link: say so rather than claim it was sent
        raise HTTPException(
            503, "sign-in mail is not set up on this server (ERGPLAN_SMTP_URL); ask jouleWise for a link"
        )
    link = issue_link(email, body.next)
    try:
        sent = mail.send(
            email,
            "Your ergPLAN sign-in link",
            f"Open this link to sign in to ergPLAN (it works once, for {LINK_MINUTES} minutes):\n\n{link}\n\n"
            "If you did not ask for it, ignore this message.\n\njouleWise",
        )
    except (OSError, smtplib.SMTPException) as e:  # the mail server refused or could not be reached
        raise HTTPException(502, "the sign-in mail could not be sent; try again shortly") from e
    return RequestOut(sent=sent, dev_link=None if sent or production() else link)


@router.post("/verify", response_model=MeOut, dependencies=[Depends(signin_limit)])
def verify(body: VerifyIn, response: Response) -> MeOut:
    now = datetime.now(UTC)
    with session_scope(actor="auth") as s:
        t = s.get(LoginToken, sha(body.token))
        if t is None or t.used_at is not None or t.expires_at < now:
            raise HTTPException(401, "this link has expired or was used; ask for a new one")
        t.used_at = now
        user = s.get(User, t.email)
        if user is None:
            user = User(email=t.email, role=role_for(t.email))
            s.add(user)
        return _start_session(s, user, response, now)


@router.post("/login", response_model=MeOut, dependencies=[Depends(signin_limit)])
def login(body: LoginIn, request: Request, response: Response) -> MeOut:
    """The administrator signs in. Every attempt is recorded (never the password); the sixth after
    five wrong ones from an address is refused for fifteen minutes, whatever it carries."""
    now = datetime.now(UTC)
    address = client_address(request)
    refusal: HTTPException | None = None
    out: MeOut | None = None
    with session_scope(actor="auth") as s:
        off = admin.off_reason()
        if off is not None:
            admin.record(s, address, body.username, "off", now)
            refusal = HTTPException(403, off)
        else:
            admin.serialise(s, address)
            until = admin.locked_until(s, address, now)
            if until is not None:
                admin.record(s, address, body.username, "locked", now)
                wait = max(1, int((until - now).total_seconds()) + 1)
                refusal = HTTPException(
                    429,
                    "too many wrong attempts from this address; sign-in is locked for fifteen minutes "
                    f"from the fifth (about {max(1, round(wait / 60))} more)",
                    headers={"Retry-After": str(wait)},
                )
            else:
                user = admin.check_password(s, body.username, body.password)
                if user is None:
                    admin.record(s, address, body.username, "wrong", now)
                    refusal = HTTPException(401, "that username and password do not match")
                else:
                    admin.record(s, address, body.username, "ok", now)
                    out = _start_session(s, user, response, now)
    if refusal is not None:  # raised after the attempt is committed
        raise refusal
    assert out is not None
    return out


@router.get("/me", response_model=MeOut)
def me(who: Annotated[Caller, Depends(caller)]) -> MeOut:
    return _me(who.actor if who.signed_in else None, who.role, who.signed_in)


@router.post("/logout", response_model=MeOut)
def logout(response: Response, who: Annotated[Caller, Depends(caller)]) -> MeOut:
    if who.signed_in:
        with session_scope(actor=who.actor) as s:
            for row in s.query(AuthSession).filter(
                AuthSession.email == who.actor, AuthSession.revoked_at.is_(None)
            ):
                row.revoked_at = datetime.now(UTC)
    response.delete_cookie(COOKIE, path="/")
    return _me(None, "client", False)
