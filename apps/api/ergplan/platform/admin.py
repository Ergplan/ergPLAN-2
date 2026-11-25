"""The one administrator account (Stage 13): a username and a password the person types into the
server's `.env` (`ERGPLAN_ADMIN_USER`, `ERGPLAN_ADMIN_PASSWORD`). Nothing here invents, defaults,
logs or returns a password.

- Switched off unless both are set, and, in production, unless the site is served over https
  (`ERGPLAN_PUBLIC_URL`): a password is not sent over plain http. The emailed link is unaffected.
- The account is a row of `users` keyed by the lowercased username, role analyst, holding only a
  salted scrypt hash. `ensure()` writes it when the API starts and again, lazily, at a sign-in whose
  stored hash no longer matches what is configured (the person changed the password and another
  process has not restarted); a changed password signs the old sessions out.
- Every attempt is a row of `signin_attempts` and one log line, without the password. Five wrong
  attempts from one address lock that address out for fifteen minutes: the fifth wrong row carries
  `locked_until`, and until then every attempt from the address is refused, the right password too.
  A success, or a lock that has run out, starts the count again."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..core.settings import get_settings
from ..db.base import session_scope
from ..db.models import AuthSession, SignInAttempt, User
from .passwords import hash_password, verify_password

log = logging.getLogger("ergplan.signin")

WRONG_LIMIT = 5
LOCK = timedelta(minutes=15)
MIN_LENGTH = 12
OTHER = "(other)"  # what is recorded when the username typed is not the administrator's


def username() -> str:
    return get_settings().ergplan_admin_user.strip().lower()


def _password() -> str:
    return get_settings().ergplan_admin_password.get_secret_value()


def off_reason() -> str | None:
    """Why password sign-in is switched off on this server, in words; None when it is on."""
    st = get_settings()
    if not username() or not _password():
        return "password sign-in is switched off on this server; use the emailed sign-in link"
    if st.ergplan_env != "dev" and not st.ergplan_public_url.startswith("https://"):
        return "password sign-in needs https, and this server is not served over https; use the sign-in link"
    return None


def enabled() -> bool:
    return off_reason() is None


def weak_reasons() -> list[str]:
    """Why the configured password is weak, in words that never contain it. Empty: not weak, or no
    administrator account is configured."""
    user, password = username(), _password()
    if not user or not password:
        return []
    out = []
    if len(password) < MIN_LENGTH:
        out.append(f"the administrator password is shorter than {MIN_LENGTH} characters")
    if password.lower().startswith(user):
        out.append("the administrator password is the username with a suffix")
    return out


def warn_if_weak() -> None:
    """At start-up. The server still starts: the choice is the person's."""
    for reason in weak_reasons():
        log.warning("weak password: %s; change ERGPLAN_ADMIN_PASSWORD in .env and restart the api", reason)


def ensure(s: Session) -> User | None:
    """The administrator's row, written or brought up to date with what is configured; None when
    password sign-in is off. The hash is replaced only when it no longer verifies the password."""
    if not enabled():
        return None
    name, password = username(), _password()
    user = s.get(User, name)
    if user is None:
        user = User(email=name, role="analyst", password_hash=hash_password(password))
        s.add(user)
        log.info("administrator account created: %s", name)
    elif not verify_password(password, user.password_hash):
        changed = user.password_hash is not None
        user.password_hash = hash_password(password)
        if changed:  # a new password: whoever holds a session under the old one is signed out
            now = datetime.now(UTC)
            for row in s.query(AuthSession).filter(
                AuthSession.email == name, AuthSession.revoked_at.is_(None)
            ):
                row.revoked_at = now
        log.info("administrator password hash %s: %s", "replaced" if changed else "stored", name)
    if user.role != "analyst":
        user.role = "analyst"
    s.flush()
    return user


def ensure_at_start() -> None:
    with session_scope(actor="auth") as s:
        ensure(s)


@lru_cache
def _decoy() -> str:
    return hash_password("no such account")


def check_password(s: Session, given_username: str, password: str) -> User | None:
    """The administrator's row when the username and password are right, else None. A wrong
    username costs the same hashing as a wrong password."""
    name = username()
    if given_username.strip().lower() != name:
        verify_password(password, _decoy())
        return None
    user = s.get(User, name)
    if user is not None and verify_password(password, user.password_hash):
        # right for the stored hash; it must also still be the configured one
        return user if verify_password(_password(), user.password_hash) else _recheck(s, password)
    return _recheck(s, password)


def _recheck(s: Session, password: str) -> User | None:
    user = ensure(s)
    return user if user is not None and verify_password(password, user.password_hash) else None


# ---------------------------------------------------------------- attempts and the lockout


def serialise(s: Session, address: str) -> None:
    """One attempt at a time per address (until this transaction ends), so five wrong attempts sent
    at once cannot become more than five."""
    s.execute(text("select pg_advisory_xact_lock(hashtext(:a))"), {"a": "signin:" + address})


def locked_until(s: Session, address: str, now: datetime) -> datetime | None:
    until = s.execute(
        select(func.max(SignInAttempt.locked_until)).where(SignInAttempt.address == address)
    ).scalar_one_or_none()
    return until if until is not None and until > now else None


def record(s: Session, address: str, given_username: str, outcome: str, now: datetime) -> SignInAttempt:
    """Append one attempt and write its log line. A wrong attempt that is the fifth since the last
    success or lock, within fifteen minutes, starts the lock."""
    name = username()
    shown = name if name and given_username.strip().lower() == name else OTHER
    row = SignInAttempt(at=now, address=address[:64], username=shown, outcome=outcome)
    if outcome == "wrong":
        since = now - LOCK
        for marker in (
            select(func.max(SignInAttempt.at)).where(
                SignInAttempt.address == address, SignInAttempt.outcome == "ok"
            ),
            select(func.max(SignInAttempt.locked_until)).where(SignInAttempt.address == address),
        ):
            at = s.execute(marker).scalar_one_or_none()
            if at is not None and at > since:
                since = at
        earlier = s.execute(
            select(func.count())
            .select_from(SignInAttempt)
            .where(
                SignInAttempt.address == address,
                SignInAttempt.outcome == "wrong",
                SignInAttempt.at > since,
            )
        ).scalar_one()
        if earlier + 1 >= WRONG_LIMIT:
            row.locked_until = now + LOCK
    s.add(row)
    s.flush()
    log.info(
        "sign-in attempt: address=%s username=%s outcome=%s%s",
        row.address,
        shown,
        outcome,
        f" locked_until={row.locked_until.isoformat()}" if row.locked_until else "",
    )
    return row
