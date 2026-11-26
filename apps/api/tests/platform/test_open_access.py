"""ERGPLAN_OPEN_ACCESS (the person's decision of 26 Nov 2026): no sign-in for the web app or the MCP
connector; every visitor is the analyst `OPEN_ACTOR`. Approving a rule or a state rule book still
needs an analyst's own sign-in (invariant 3)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ergplan.core.settings import get_settings
from ergplan.platform.app import app
from ergplan.platform.auth import OPEN_ACTOR
from tests.mcp import helpers as h


@pytest.fixture
def prod(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(get_settings(), "ergplan_env", "prod")
    return monkeypatch


def test_off_by_default_a_visitor_must_sign_in(prod: pytest.MonkeyPatch) -> None:
    assert get_settings().ergplan_open_access is False
    c = TestClient(app, base_url="https://testserver")
    assert c.get("/auth/me").json()["signed_in"] is False
    assert c.get("/rulebooks/versions").status_code == 401


def test_switched_on_a_visitor_is_the_open_analyst(prod: pytest.MonkeyPatch) -> None:
    prod.setattr(get_settings(), "ergplan_open_access", True)
    c = TestClient(app, base_url="https://testserver")
    me = c.get("/auth/me").json()
    assert me["signed_in"] is True and me["role"] == "analyst" and me["email"] == OPEN_ACTOR
    assert c.get("/rulebooks/versions").status_code == 200
    # headers still carry no weight in production: the visitor is the open analyst, nobody else
    fake = {"X-Ergplan-Actor": "intruder@x.in", "X-Ergplan-Role": "analyst"}
    assert c.get("/auth/me", headers=fake).json()["email"] == OPEN_ACTOR


def test_approving_still_needs_an_analysts_own_sign_in(prod: pytest.MonkeyPatch) -> None:
    prod.setattr(get_settings(), "ergplan_open_access", True)
    c = TestClient(app, base_url="https://testserver")
    for path, body in (
        ("/rulebooks/versions/any-version/approve", None),
        ("/statebooks/any-book/approve", {"note": "looks right"}),
    ):
        r = c.post(path, json=body)
        assert r.status_code == 403 and "own sign-in" in r.json()["detail"], path


def test_the_mcp_connector_needs_no_token_when_open(monkeypatch: pytest.MonkeyPatch) -> None:
    h.configure(monkeypatch)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    c = h.client()
    assert c.post("/mcp", json=body, headers=headers).status_code == 401
    monkeypatch.setattr(get_settings(), "ergplan_open_access", True)
    r = c.post("/mcp", json=body, headers=headers)
    assert r.status_code == 200 and "read_bill" in r.text
    # a token that is offered is still checked
    bad = c.post("/mcp", json=body, headers=headers | {"Authorization": "Bearer made-up"})
    assert bad.status_code == 401
