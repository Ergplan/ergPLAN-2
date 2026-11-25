"""Stage 13: the one administrator account. Username and password from the server's environment
(here a fixture's throwaway values, set on the settings object); only a salted hash is stored; the
lockout by address; every attempt in the table and the log without the password; the cookie; the
weak-password warning in the log and on /auth/me; switched off when either value is missing."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from ergplan.core.settings import get_settings
from ergplan.db.base import get_engine, session_scope
from ergplan.db.models import AuthSession, SignInAttempt, User
from ergplan.platform import admin
from ergplan.platform.app import app, create_app
from ergplan.platform.passwords import verify_password

USER = "PlantAdmin"  # a fixture's values, not anyone's
PASSWORD = "fixture-only-Zq7-violet-harbour"
WRONG = "fixture-only-wrong-guess"


def configure(monkeypatch: pytest.MonkeyPatch, user: str = USER, password: str = PASSWORD) -> None:
    st = get_settings()
    monkeypatch.setattr(st, "ergplan_admin_user", user)
    monkeypatch.setattr(st, "ergplan_admin_password", SecretStr(password))


@pytest.fixture
def prod(monkeypatch: pytest.MonkeyPatch) -> None:
    st = get_settings()
    monkeypatch.setattr(st, "ergplan_env", "prod")
    monkeypatch.setattr(st, "ergplan_public_url", "https://plan.example.in")
    # the test client connects as "testclient": believe its X-Forwarded-For, as Caddy's is believed
    monkeypatch.setattr(st, "ergplan_trusted_proxies", "testclient")
    configure(monkeypatch)


@pytest.fixture
def signin_log(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    # alembic's fileConfig (conftest) disables loggers that existed before it ran
    monkeypatch.setattr(logging.getLogger("ergplan.signin"), "disabled", False)
    caplog.set_level(logging.DEBUG)  # everything any logger says
    return caplog


def client() -> TestClient:
    return TestClient(app, base_url="https://testserver")


def login(c: TestClient, password: str, username: str = USER, address: str = "203.0.113.7") -> int:
    r = c.post(
        "/auth/login",
        json={"username": username, "password": password},
        headers={"X-Forwarded-For": address},
    )
    return r.status_code


def test_the_administrator_signs_in_and_reaches_the_analyst_screens(prod: None) -> None:
    c = client()
    assert c.get("/auth/me").json() == {
        "email": None,
        "role": "client",
        "signed_in": False,
        "required": True,
        "password_sign_in": True,
        "warnings": [],
    }
    assert c.get("/rulebooks/versions").status_code == 401
    r = c.post("/auth/login", json={"username": "plantadmin", "password": PASSWORD})  # any case
    assert r.status_code == 200, r.text
    assert r.json() == {
        "email": "plantadmin",
        "role": "analyst",
        "signed_in": True,
        "required": True,
        "password_sign_in": True,
        "warnings": [],
    }
    # the cookie: Secure, HttpOnly, SameSite=Lax (https)
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("ergplan_session=")
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    # the analyst screens' routes
    assert c.get("/rulebooks/versions").status_code == 200
    assert c.get("/statebooks").status_code == 200
    assert c.get("/auth/me").json()["role"] == "analyst"
    # the same session as a bearer token (a client that is not a browser)
    bearer = client().get("/auth/me", headers={"Authorization": f"Bearer {c.cookies['ergplan_session']}"})
    assert bearer.json()["email"] == "plantadmin" and bearer.json()["role"] == "analyst"


def test_only_a_salted_hash_is_stored_and_no_password_reaches_the_database_or_the_log(
    prod: None, signin_log: pytest.LogCaptureFixture
) -> None:
    c = client()
    assert login(c, WRONG) == 401
    assert login(c, PASSWORD, username=PASSWORD) == 401  # the password typed into the username box
    assert login(c, PASSWORD) == 200
    with session_scope() as s:
        u = s.get(User, "plantadmin")
        assert u is not None and u.role == "analyst"
        stored = u.password_hash
    assert stored is not None and stored.startswith("scrypt$") and verify_password(PASSWORD, stored)
    assert PASSWORD not in stored and not verify_password(WRONG, stored)
    # salted: the same password hashed again is a different string
    with session_scope(actor="auth") as s:
        u = s.get(User, "plantadmin")
        assert u is not None
        u.password_hash = None
    assert login(client(), PASSWORD) == 200
    with session_scope() as s:
        again = s.get(User, "plantadmin")
        assert again is not None and again.password_hash and again.password_hash != stored
    # every text value of every table: neither password, anywhere
    with get_engine().connect() as conn:
        tables = [
            r[0] for r in conn.execute(text("select tablename from pg_tables where schemaname='public'"))
        ]
        for table in tables:
            for needle in (PASSWORD, WRONG):
                hit = conn.execute(
                    text(f'select count(*) from "{table}" t where t::text like :p'), {"p": f"%{needle}%"}
                ).scalar_one()
                assert hit == 0, table
    # the log: the attempts are there, the passwords are not (in any logger's lines)
    assert "sign-in attempt" in signin_log.text
    for record in signin_log.records:
        line = record.getMessage()
        assert PASSWORD not in line and WRONG not in line, record.name
    assert PASSWORD not in repr(get_settings()) and PASSWORD not in str(get_settings().model_dump())


def test_every_attempt_is_logged_without_the_password(
    prod: None, signin_log: pytest.LogCaptureFixture
) -> None:
    c = client()
    assert login(c, WRONG, address="198.51.100.4") == 401
    assert login(c, WRONG, username="someone-else", address="198.51.100.4") == 401
    assert login(c, PASSWORD, address="198.51.100.4") == 200
    with session_scope() as s:
        rows = s.query(SignInAttempt).order_by(SignInAttempt.at).all()
        got = [(r.address, r.username, r.outcome) for r in rows]
    assert got == [
        ("198.51.100.4", "plantadmin", "wrong"),
        ("198.51.100.4", "(other)", "wrong"),  # what was typed is not kept unless it is the username
        ("198.51.100.4", "plantadmin", "ok"),
    ]
    lines = [r.getMessage() for r in signin_log.records if r.name == "ergplan.signin"]
    attempts = [x for x in lines if x.startswith("sign-in attempt")]
    assert len(attempts) == 3
    assert "address=198.51.100.4" in attempts[0] and "outcome=wrong" in attempts[0]
    assert "username=(other)" in attempts[1] and "someone-else" not in attempts[1]
    assert "username=plantadmin" in attempts[2] and "outcome=ok" in attempts[2]
    assert not any(PASSWORD in x or WRONG in x for x in lines)


def test_the_sixth_attempt_after_five_wrong_is_refused_for_fifteen_minutes(
    prod: None, signin_log: pytest.LogCaptureFixture
) -> None:
    c = client()
    assert [login(c, f"{WRONG}-{i}") for i in range(5)] == [401] * 5
    sixth = c.post(
        "/auth/login",
        json={"username": USER, "password": PASSWORD},  # the right password
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    assert sixth.status_code == 429 and "fifteen minutes" in sixth.json()["detail"]
    assert 14 * 60 < int(sixth.headers["retry-after"]) <= 15 * 60 + 1
    assert "ergplan_session" not in c.cookies
    # another address is not locked; the locked one stays locked
    assert login(client(), PASSWORD, address="203.0.113.8") == 200
    assert login(c, PASSWORD) == 429
    with session_scope() as s:
        rows = s.query(SignInAttempt).filter(SignInAttempt.address == "203.0.113.7").all()
        assert sorted(r.outcome for r in rows) == ["locked", "locked", *["wrong"] * 5]
        [fifth] = [r for r in rows if r.locked_until is not None]
        assert fifth.outcome == "wrong" and fifth.locked_until - fifth.at == timedelta(minutes=15)
    assert sum("outcome=locked" in r.getMessage() for r in signin_log.records) == 2
    # the lock is in the database, not in the process: fourteen minutes on it still holds
    shift = "update signin_attempts set at = at - :d, locked_until = locked_until - :d where address = :a"
    with session_scope(actor="test") as s:
        s.execute(text(shift), {"d": timedelta(minutes=14), "a": "203.0.113.7"})
    assert login(c, PASSWORD) == 429
    # after fifteen it has run out, and the count starts again
    with session_scope(actor="test") as s:
        s.execute(text(shift), {"d": timedelta(minutes=1, seconds=5), "a": "203.0.113.7"})
    assert login(c, WRONG) == 401
    assert login(c, PASSWORD) == 200


def test_four_wrong_then_right_signs_in_and_starts_the_count_again(prod: None) -> None:
    c = client()
    assert [login(c, WRONG) for _ in range(4)] == [401] * 4
    assert login(c, PASSWORD) == 200
    assert [login(c, WRONG) for _ in range(4)] == [401] * 4
    assert login(c, PASSWORD) == 200
    # wrong attempts older than fifteen minutes do not count either
    assert [login(c, WRONG, address="203.0.113.9") for _ in range(4)] == [401] * 4
    with session_scope(actor="test") as s:
        s.execute(
            text("update signin_attempts set at = at - :d where address = '203.0.113.9'"),
            {"d": timedelta(minutes=16)},
        )
    assert login(c, WRONG, address="203.0.113.9") == 401
    assert login(c, PASSWORD, address="203.0.113.9") == 200


def test_a_forwarded_address_is_believed_only_from_the_proxy(
    prod: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller who connects directly cannot escape the lockout by sending a new X-Forwarded-For
    each time, and one behind the proxy cannot by adding entries of their own in front."""
    monkeypatch.setattr(get_settings(), "ergplan_trusted_proxies", "10.0.0.0/8")  # not this caller
    c = client()
    assert [login(c, WRONG, address=f"203.0.113.{i}") for i in range(5)] == [401] * 5
    assert login(c, PASSWORD, address="203.0.113.99") == 429
    with session_scope() as s:
        assert {r.address for r in s.query(SignInAttempt).all()} == {"testclient"}
    monkeypatch.setattr(get_settings(), "ergplan_trusted_proxies", "testclient")
    assert [login(c, WRONG, address=f"192.0.2.{i}, 198.51.100.77") for i in range(5)] == [401] * 5
    assert login(c, PASSWORD, address="192.0.2.200, 198.51.100.77") == 429


@pytest.mark.parametrize(("user", "password"), [("", PASSWORD), (USER, ""), ("", "")])
def test_without_both_values_password_sign_in_is_switched_off(
    prod: None, monkeypatch: pytest.MonkeyPatch, user: str, password: str
) -> None:
    configure(monkeypatch, user, password)
    assert admin.enabled() is False and admin.weak_reasons() == []
    c = client()
    assert c.get("/auth/me").json()["password_sign_in"] is False
    for guess in (password or "anything", "admin", "password"):
        r = c.post("/auth/login", json={"username": user or "admin", "password": guess})
        assert r.status_code == 403 and "switched off" in r.json()["detail"]
    assert "ergplan_session" not in c.cookies
    with session_scope(actor="auth") as s:
        assert admin.ensure(s) is None
        assert s.query(User).count() == 0  # no account is invented
        assert {r.outcome for r in s.query(SignInAttempt).all()} == {"off"}


def test_password_sign_in_is_off_over_plain_http_in_production(
    prod: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "ergplan_public_url", "http://192.168.1.73")
    c = TestClient(app, base_url="http://testserver")
    r = c.post("/auth/login", json={"username": USER, "password": PASSWORD})
    assert r.status_code == 403 and "https" in r.json()["detail"]
    assert c.get("/auth/me").json()["password_sign_in"] is False


def test_the_account_is_written_at_start_and_follows_a_changed_password(
    prod: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(create_app(), base_url="https://testserver"):  # the app's start-up runs
        pass
    with session_scope() as s:
        u = s.get(User, "plantadmin")
        assert u is not None and u.role == "analyst"
        first = u.password_hash
    assert first and verify_password(PASSWORD, first)
    with TestClient(create_app(), base_url="https://testserver"):  # a restart, nothing changed
        pass
    with session_scope() as s:
        u = s.get(User, "plantadmin")
        assert u is not None and u.password_hash == first  # not hashed again
    old = client()
    assert login(old, PASSWORD) == 200 and old.get("/rulebooks/versions").status_code == 200
    # the person types a new password into .env and restarts
    changed = "fixture-only-second-Kp3-amber-lantern"
    configure(monkeypatch, USER, changed)
    with TestClient(create_app(), base_url="https://testserver"):
        pass
    with session_scope() as s:
        u = s.get(User, "plantadmin")
        assert u is not None and u.password_hash != first and verify_password(changed, u.password_hash)
        assert all(r.revoked_at is not None for r in s.query(AuthSession).all())
    assert old.get("/rulebooks/versions").status_code == 401  # signed out with the old password
    assert login(client(), PASSWORD, address="203.0.113.20") == 401
    assert login(client(), changed, address="203.0.113.21") == 200


@pytest.mark.parametrize(
    ("user", "password", "words"),
    [
        ("plantadmin", "fixture-9ch", ["shorter than 12 characters"]),
        ("plantadmin", "PlantAdmin-fixture-2026", ["the username with a suffix"]),
        ("plantadmin", "plantadmin1", ["shorter than 12 characters", "the username with a suffix"]),
        ("plantadmin", PASSWORD, []),
    ],
)
def test_a_weak_password_is_warned_in_the_log_and_on_the_analysts_screen(
    prod: None,
    monkeypatch: pytest.MonkeyPatch,
    signin_log: pytest.LogCaptureFixture,
    user: str,
    password: str,
    words: list[str],
) -> None:
    configure(monkeypatch, user, password)
    served = create_app()  # the server still starts
    warned = [r.getMessage() for r in signin_log.records if r.levelno == logging.WARNING]
    assert len(warned) == len(words)
    for w in words:
        assert any(w in line and "weak password" in line for line in warned)
    assert not any(password in r.getMessage() for r in signin_log.records)
    c = TestClient(served, base_url="https://testserver")
    assert c.get("/auth/me").json()["warnings"] == []  # signed out: nothing about the server's set-up
    assert c.post("/auth/login", json={"username": user, "password": password}).status_code == 200
    shown = c.get("/auth/me").json()["warnings"]
    assert len(shown) == len(words) and all(any(w in x for x in shown) for w in words)
    assert not any(password in x for x in shown)


def test_a_client_never_sees_the_weak_password_warning(prod: None, monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, "plantadmin", "plantadmin1")
    with session_scope(actor="auth") as s:
        s.add(User(email="ops@acme.in", role="client"))
        s.flush()
        s.add(
            AuthSession(
                token_sha256=admin_sha("client-session-token-for-this-test"),
                email="ops@acme.in",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
    c = client()
    me = c.get("/auth/me", headers={"Authorization": "Bearer client-session-token-for-this-test"}).json()
    assert me["signed_in"] is True and me["role"] == "client" and me["warnings"] == []


def admin_sha(token: str) -> str:
    from ergplan.platform.auth import sha

    return sha(token)


def test_a_client_still_signs_in_by_emailed_link_beside_the_administrator(
    prod: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ergplan.platform import mail

    sent: list[str] = []
    monkeypatch.setattr(get_settings(), "ergplan_smtp_url", "smtp://mail.test:587")
    monkeypatch.setattr(mail, "send", lambda to, subject, body: sent.append(body) or True)
    c = client()
    assert c.post("/auth/request", json={"email": "ops@acme.in"}).json() == {"sent": True, "dev_link": None}
    token = sent[-1].split("token=")[1].split("&")[0].split()[0]
    v = c.post("/auth/verify", json={"token": token})
    assert v.status_code == 200 and v.json()["role"] == "client" and v.json()["email"] == "ops@acme.in"
    assert v.json()["warnings"] == [] and "ergplan_session" in c.cookies
    assert c.get("/rulebooks/versions").status_code == 200  # signed in
    refused = c.post(
        "/documents", files={"file": ("o.pdf", b"%PDF-1.4", "application/pdf")}, data={"title": "An order"}
    )
    assert refused.status_code == 403  # a client, not an analyst
    # a client's email has no password: the password form does not let them (or anyone as them) in
    assert login(client(), PASSWORD, username="ops@acme.in") == 401
    with session_scope() as s:
        u = s.get(User, "ops@acme.in")
        assert u is not None and u.password_hash is None
