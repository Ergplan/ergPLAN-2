"""The connector's OAuth rows are pruned at every registration and every token issued: a sign-in whose
tokens all ended (expired or revoked) more than a day ago loses its rows, then a client registered more
than thirty days ago with no token left. Nothing a good sign-in needs is deleted: only expired or
revoked, unusable rows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from ergplan.db.base import session_scope
from ergplan.db.models import McpClient, McpToken
from ergplan.mcp import oauth
from tests.mcp import helpers as h


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> h.Admin:
    return h.configure(monkeypatch)


def _age(client_id: str, registered: timedelta, tokens_end: timedelta | None) -> None:
    """Move a client's registration back, and every token of it to end `tokens_end` ago."""
    now = datetime.now(UTC)
    with session_scope(actor="mcp-oauth") as s:
        s.execute(
            update(McpClient).where(McpClient.client_id == client_id).values(created_at=now - registered)
        )
        if tokens_end is not None:
            s.execute(
                update(McpToken).where(McpToken.client_id == client_id).values(expires_at=now - tokens_end)
            )


def _rows(client_id: str) -> tuple[bool, int]:
    with session_scope() as s:
        there = s.get(McpClient, client_id) is not None
        n = len(s.scalars(select(McpToken).where(McpToken.client_id == client_id)).all())
    return there, n


def test_only_unusable_rows_are_pruned(admin: h.Admin) -> None:
    c = h.client()
    dead: dict[str, Any] = h.tokens(c, admin)
    recent: dict[str, Any] = h.tokens(c, admin)
    live: dict[str, Any] = h.tokens(c, admin)
    idle = h.register(c)
    fresh = h.register(c)
    # (aged after every registration, each of which prunes)
    _age(dead["client_id"], timedelta(days=90), timedelta(days=2))  # its sign-in ended two days ago
    _age(recent["client_id"], timedelta(days=90), timedelta(hours=1))  # within the day's grace
    _age(live["client_id"], timedelta(days=90), None)  # an old client whose sign-in is still good
    _age(idle["client_id"], timedelta(days=31), None)  # registered long ago, never signed in
    _age(fresh["client_id"], timedelta(days=3), None)  # registered this week, not yet signed in
    before = {k: _rows(v["client_id"]) for k, v in [("dead", dead), ("recent", recent), ("live", live)]}
    assert all(n >= 2 for _, n in before.values())  # the spent code, an access and a refresh token

    h.register(c)  # a registration prunes

    assert _rows(dead["client_id"]) == (False, 0)
    assert _rows(idle["client_id"]) == (False, 0)
    assert _rows(recent["client_id"]) == before["recent"]
    assert _rows(live["client_id"]) == before["live"]
    assert _rows(fresh["client_id"]) == (True, 0)
    # the good sign-in still works: its refresh token turns into new tokens (which prunes again)
    rf = {
        "grant_type": "refresh_token",
        "refresh_token": live["refresh_token"],
        "client_id": live["client_id"],
    }
    assert c.post("/oauth/token", data=rf).status_code == 200


def test_a_revoked_sign_in_is_pruned_a_day_after(admin: h.Admin) -> None:
    c = h.client()
    t = h.tokens(c, admin)
    assert (
        c.post("/oauth/revoke", data={"token": t["refresh_token"], "client_id": t["client_id"]}).status_code
        == 200
    )
    now = datetime.now(UTC)
    with session_scope(actor="mcp-oauth") as s:
        assert oauth.prune(s, now)[0] == 0  # revoked just now: kept for the day
        assert oauth.prune(s, now + timedelta(days=2))[0] >= 2  # then deleted, every token of it
    assert _rows(t["client_id"]) == (True, 0)  # the client registered today stays
