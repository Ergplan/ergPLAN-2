"""POST /regreader/arr-network: an analyst imports ARR Network orders as drafts; nobody else can."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ergplan.platform.app import app
from ergplan.platform.routers import regreader
from tests.regreader.arr_helpers import offline_client

client = TestClient(app)
AN = {"X-Ergplan-Actor": "analyst@joulewise.in", "X-Ergplan-Role": "analyst"}
BODY = {
    "state": "up",
    "orders": [67, 65],
    "effective_from": {"65": "2025-11-22"},
}

pytestmark = pytest.mark.usefixtures("jurisdictions")


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(regreader, "arr_client", offline_client)


def test_dry_run_by_default_then_drafts() -> None:
    r = client.post("/regreader/arr-network", headers=AN, json=BODY)
    assert r.status_code == 201, r.text
    dry = r.json()
    assert [(o["order_id"], o["jurisdiction"], o["tariff_year"]) for o in dry] == [
        (67, "uppcl", "FY2026-27"),
        (65, "uppcl", "FY2025-26"),
    ]
    assert {v["action"] for o in dry for v in o["versions"]} == {"would propose"}
    assert dry[1]["effective_from"] == "2025-11-22" and dry[1]["effective_to"] == "2026-07-01"
    assert any("voltage not read with confidence" in u["reason"] for u in dry[0]["unmapped"])
    assert client.get("/rulebooks/versions", headers=AN).json() == []

    r = client.post("/regreader/arr-network", headers=AN, json={**BODY, "dry_run": False})
    assert r.status_code == 201, r.text
    done = r.json()
    assert {v["action"] for o in done for v in o["versions"]} == {"proposed"}
    assert done[0]["pdf"].startswith("NOT stored")
    versions = client.get("/rulebooks/versions", headers=AN).json()
    assert len(versions) == 6 and {v["status"] for v in versions} == {"draft"}
    assert {v["origin"] for v in versions} == {"arr_network"}
    assert all(v["created_by"] == "analyst@joulewise.in" for v in versions)


def test_only_an_analyst_and_only_what_can_be_imported() -> None:
    assert client.post("/regreader/arr-network", json=BODY).status_code in (401, 403)
    as_client = {"X-Ergplan-Actor": "buyer@client.example", "X-Ergplan-Role": "client"}
    assert client.post("/regreader/arr-network", headers=as_client, json=BODY).status_code == 403
    assert client.post("/regreader/arr-network", headers=AN, json={**BODY, "orders": []}).status_code == 422
    r = client.post("/regreader/arr-network", headers=AN, json={**BODY, "state": "mh"})
    assert r.status_code == 422 and "not all in MH" in r.json()["detail"]
    r = client.post("/regreader/arr-network", headers=AN, json={"state": "up", "orders": [9999]})
    assert r.status_code == 502 and "HTTP 503" in r.json()["detail"]  # the site cannot be reached
