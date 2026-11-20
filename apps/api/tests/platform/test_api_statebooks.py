"""State rule books through the API: a month's issue for a DISCOM (a reference consumer, or a client's
connection) with landed cost by source and route, the saving on the grid tariff and the settlement
in words; an analyst's review (approve, reject), what changed since the last approved issue, the
file, and what a client may see."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ergplan.db.base import get_engine
from ergplan.platform.app import app
from tests.platform.test_api_plan import _truncate
from tests.platform.test_api_runs import AN, CL, make_site, msedcl_5mw

client = TestClient(app)
NOV = "2026-11-01"


@pytest.fixture(scope="module")
def site() -> Iterator[dict[str, str]]:
    _truncate()
    mp = pytest.MonkeyPatch()
    try:
        s, c = make_site(mp, msedcl_5mw(), {})
        yield {"site": s, "conn": c}
    finally:
        mp.undo()
        _truncate()


@pytest.fixture(autouse=True)
def clean_db() -> None:
    return None


def _gen(**body: Any) -> dict[str, Any]:
    r = client.post("/statebooks/generate", json={"discom": "msedcl", "month": NOV, **body}, headers=AN)
    assert r.status_code == 201, r.text[:400]
    return dict(r.json())


def _route(book: dict[str, Any], source: str, route: str) -> dict[str, Any]:
    src = next(x for x in book["content"]["renewable"] if x["source"] == source)
    return dict(next(r for r in src["routes"] if r["route"] == route))


def test_reference_book_landed_cost_saving_and_routes(site: dict[str, str]) -> None:
    b = _gen()
    c = b["content"]
    assert b["status"] == "draft" and b["issue"] == 1 and b["connection_id"] is None
    assert c["consumer"]["kind"] == "reference" and c["consumer"]["category"] == "HT I (A)"
    assert c["state"] == "MH" and c["title"].startswith("Maharashtra")
    assert len(c["grid"]) >= 3 and all(D(g["rate"]["value"]) > 0 for g in c["grid"])
    assert [x["source"] for x in c["renewable"]] == [
        "oa_solar_intra",
        "oa_wind_intra",
        "oa_solar_ists",
        "oa_wind_ists",
    ]
    third, group, own = (_route(b, "oa_solar_intra", r) for r in ("third_party", "group_captive", "captive"))
    for r in (third, group, own):
        assert r["available"] and r["slots"]
        for sl in r["slots"]:
            # the landed cost is the sum of its lines; the saving is the grid tariff less the landed cost
            assert sum(D(x["value"]) for x in sl["lines"]) == D(sl["landed"]["value"])
            assert D(sl["saving"]["value"]) == D(sl["grid"]["value"]) - D(sl["landed"]["value"])
            assert all(x["source_refs"] for x in sl["lines"])
            # the banking charge falls on banked units only: beside the landed cost, never inside it
            assert not any((x["label"] or "").startswith("Banking charge") for x in sl["lines"])
            assert D(sl["banking"]["value"]) > 0 and sl["banking"]["source_refs"]
    labels = lambda r: " | ".join(x["label"] or "" for x in r["slots"][0]["lines"])  # noqa: E731
    # third party pays the cross-subsidy surcharge; a captive user does not; the 26% route carries its equity
    assert "exemption" not in labels(third).lower()
    assert "exemption (captive 26 51)" in labels(own) and "exemption (group captive)" in labels(group)
    assert "Equity cost" in labels(group) and "Equity cost" not in labels(own)
    assert D(own["landed_avg"]["value"]) < D(group["landed_avg"]["value"]) < D(third["landed_avg"]["value"])
    assert D(group["landed_avg"]["value"]) - D(own["landed_avg"]["value"]) == D("0.25")
    # third party less group captive is exactly the surcharges exempted, less the equity cost
    for t_sl, g_sl in zip(third["slots"], group["slots"], strict=True):
        exempt = -sum(
            D(x["value"]) for x in g_sl["lines"] if "exemption (group captive)" in (x["label"] or "")
        )
        assert exempt > 0
        assert D(t_sl["landed"]["value"]) - D(g_sl["landed"]["value"]) == exempt - D("0.25")
    assert "section 42(2)" in group["route_note"] and "rule 3" in group["route_note"]
    assert D(own["saving_avg"]["value"]) > D(third["saving_avg"]["value"]) > 0
    # how far the figures are traced is counted, and a page ref names its order
    t = c["trace"]
    assert t["page"] > 0 and t["assumption"] > 0 and t["other"] == 0
    css = next(x for x in third["slots"][0]["lines"] if "CSS" in (x["label"] or ""))
    assert css["source_refs"][0]["kind"] == "order" and css["source_refs"][0]["page"]
    # the benchmark tariff is shown as an assumption until a quote replaces it
    ppa = next(x for x in third["slots"][0]["lines"] if x["label"] == "PPA tariff")
    assert ppa["source_refs"][0]["kind"] == "assumption"


def test_settlement_in_words_cites_the_rule(site: dict[str, str]) -> None:
    c = _gen()["content"]
    [block] = [x for x in c["settlement"] if x["rule"]]
    # every sentence of the book carries a source: the rule's page, or a ref saying it is ergPLAN's own
    assert all(p["ref"] and p["ref"]["kind"] for b in c["settlement"] for p in b["points"])
    assert all(p["ref"] and p["ref"]["kind"] for p in c["iex_settlement"])
    assert block["method"] == "cycle_pooled" and "solar, third party" in block["applies_to"]
    words = " ".join(p["text"] for p in block["points"])
    assert (
        "time-of-day slot is set off" in words and "banked with the DISCOM" in words and "same month" in words
    )
    assert "keeps 8% of the energy banked" in words and "92 can be drawn" in words
    assert "energy banked at peak can be drawn at peak, normal and off-peak" in words
    assert "lapses" in words
    assert all(p["ref"] and p["ref"]["kind"] in ("regulation", "order", "register") for p in block["points"])
    iex = c["iex_settlement"]
    assert any("cannot be banked" in p["text"] for p in iex) and any(
        "15-minute block" in p["text"] for p in iex
    )
    assert all(p["ref"]["kind"] in ("assumption", "regulation", "order", "register") for p in iex)
    assert {v["family"] for v in c["versions"]} >= {"oa_charge", "settlement"}


def test_review_approve_reject_and_what_a_client_sees(site: dict[str, str]) -> None:
    with get_engine().begin() as e:
        e.execute(text("truncate state_rulebooks"))
    first = _gen()
    assert client.get("/statebooks", headers=CL).status_code == 403
    assert (
        client.get(f"/statebooks/{first['id']}", headers=CL).status_code == 404
    )  # a draft is not for clients
    assert client.post(f"/statebooks/{first['id']}/approve", json={}, headers=CL).status_code == 403
    ok = client.post(
        f"/statebooks/{first['id']}/approve", json={"note": "checked against the MYT order"}, headers=AN
    )
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert ok.json()["reviewed_by"] == "analyst@joulewise.in" and ok.json()["review_note"].startswith(
        "checked"
    )
    assert client.post(f"/statebooks/{first['id']}/approve", json={}, headers=AN).status_code == 409
    assert (
        client.get(f"/statebooks/{first['id']}", headers=CL).status_code == 200
    )  # the reference book, approved
    # a second issue with a quoted tariff: what changed is listed against the approved issue
    second = _gen(prices={"oa_solar_intra": "2.95"})
    assert second["issue"] == 2 and second["content"]["compared_with"]["id"] == first["id"]
    changed = {ch["what"]: ch for ch in second["content"]["changes"]}
    assert changed and all("solar, intra-state" in k for k in changed)
    tariff = next(v for k, v in changed.items() if k.endswith("PPA tariff") and "Third party" in k)
    assert (D(tariff["before"]), D(tariff["after"])) == (D("3.10"), D("2.95"))
    assert not any("wind" in k.lower() for k in changed)
    ppa = next(
        x
        for x in _route(second, "oa_solar_intra", "third_party")["slots"][0]["lines"]
        if x["label"] == "PPA tariff"
    )
    assert ppa["source_refs"][0]["kind"] == "client_stated"
    # rejecting needs a reason
    third = _gen(prices={"oa_solar_intra": "9.99"})
    assert client.post(f"/statebooks/{third['id']}/reject", json={}, headers=AN).status_code == 422
    no = client.post(f"/statebooks/{third['id']}/reject", json={"note": "tariff mistyped"}, headers=AN)
    assert no.status_code == 200 and no.json()["status"] == "rejected"
    # approving the second issue supersedes the first
    assert client.post(f"/statebooks/{second['id']}/approve", json={}, headers=AN).status_code == 200
    rows = {
        b["issue"]: b["status"] for b in client.get("/statebooks", params={"state": "MH"}, headers=AN).json()
    }
    assert rows == {1: "superseded", 2: "approved", 3: "rejected"}
    # every write is audited with its actor
    with get_engine().connect() as e:
        n = e.execute(
            text(
                "select count(*) from audit_log where table_name = 'state_rulebooks' and actor = 'analyst@joulewise.in'"
            )
        ).scalar()
    assert n is not None and n >= 6


def test_the_file(site: dict[str, str]) -> None:
    b = _gen()
    # a draft's file is not served to a client; the list filters combine
    assert client.get(f"/statebooks/{b['id']}/file", headers=CL).status_code == 404
    listed = client.get(f"/statebooks?state=MH&month={NOV}&status=draft", headers=AN).json()
    assert b["id"] in [x["id"] for x in listed] and all(x["status"] == "draft" for x in listed)
    approved = client.get(f"/statebooks?state=MH&month={NOV}&status=approved", headers=AN).json()
    assert all(x["status"] == "approved" and x["id"] != b["id"] for x in approved)
    r = client.get(f"/statebooks/{b['id']}/file", headers=AN)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert f"issue-{b['issue']}.md" in r.headers["content-disposition"]
    md = r.text
    for heading in (
        "# State rule book — Maharashtra",
        "## 1. What the grid costs",
        "## 2. Landed cost and saving, by source and route",
        "### Open-access solar, intra-state",
        "| Captive, 26% equity (group captive) |",
        "### Exchange purchases (IEX)",
        "## 3. How open-access solar and wind are settled, after banking",
        "## 4. How an exchange (IEX) purchase is settled",
        "## Rule versions this issue rests on",
        "## Sources",
    ):
        assert heading in md, heading
    assert "| **Saving** |" in md and "[1" in md and "Register:" in md


def test_a_clients_own_book(site: dict[str, str]) -> None:
    b = _gen(connection_id=site["conn"])
    c = b["content"]["consumer"]
    assert c["kind"] == "client" and c["connection_id"] == site["conn"] and c["site"]
    assert client.get(f"/connections/{site['conn']}/statebook", headers=CL).status_code in (200, 404)
    assert client.post(f"/statebooks/{b['id']}/approve", json={}, headers=AN).status_code == 200
    got = client.get(f"/connections/{site['conn']}/statebook", headers=CL)
    assert got.status_code == 200 and got.json()["id"] == b["id"]
    bad = client.post(
        "/statebooks/generate",
        json={"discom": "npcl", "month": NOV, "connection_id": site["conn"]},
        headers=AN,
    )
    assert bad.status_code == 422 and "not on this DISCOM" in bad.text


def test_the_months_drafts_for_every_discom(site: dict[str, str]) -> None:
    with get_engine().begin() as e:
        e.execute(text("truncate state_rulebooks"))
    r = client.post("/statebooks/generate-month", json={"month": NOV}, headers=AN)
    assert r.status_code == 201
    made = r.json()
    states = {b["state"] for b in made}
    assert {"MH", "UP", "RJ", "TN", "KA", "GJ", "AP", "MP"} <= states
    assert sum(1 for b in made if b["connection_id"] == site["conn"]) == 1  # the client's own
    # Rajasthan's three DISCOMs carry the same figures: one book, naming the others it covers
    rj = next(b for b in made if b["state"] == "RJ")
    assert sum(1 for b in made if b["state"] == "RJ") == 1
    covers = client.get(f"/statebooks/{rj['id']}", headers=AN).json()["content"]["covers"]
    assert {rj["discom_id"], *(x["discom"] for x in covers)} == {"avvnl", "jdvvnl", "jvvnl"}
    assert "grid_tariff" in rj["gaps"]  # no approved schedule: the saving is not stated, and the book says so
    # drafts awaiting review are not generated twice
    assert client.post("/statebooks/generate-month", json={"month": NOV}, headers=AN).json() == []
    # an approved issue is regenerated only when something changed
    assert client.post(f"/statebooks/{rj['id']}/approve", json={}, headers=AN).status_code == 200
    again = client.post("/statebooks/generate-month", json={"month": NOV}, headers=AN).json()
    assert [b for b in again if b["state"] == "RJ"] == []


def test_bad_requests(site: dict[str, str]) -> None:
    r = client.post("/statebooks/generate", json={"discom": "nowhere", "month": NOV}, headers=AN)
    assert r.status_code == 422
    r = client.post(
        "/statebooks/generate",
        json={"discom": "msedcl", "month": NOV, "prices": {"iex_dam": "3"}},
        headers=AN,
    )
    assert r.status_code == 422
    assert client.get("/statebooks/none", headers=AN).status_code == 404
