"""GET /tariffs/{discom_id}: a DISCOM's tariff as the approved rule books hold it. Approved versions
only by default (invariant 2); drafts only for an analyst, each labelled a draft; a draft with an
absurd value never shows in the default view; every figure is a SourcedValue citing its rule."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ergplan.core.settings import get_settings
from ergplan.db.base import session_scope
from ergplan.platform.app import app
from ergplan.rulebook import service
from tests.engine.helpers import restore_committed

client = TestClient(app)
AN = {"X-Ergplan-Actor": "analyst@joulewise.in", "X-Ergplan-Role": "analyst"}
CL = {"X-Ergplan-Actor": "client@acme.in", "X-Ergplan-Role": "client"}
ON = {"on": "2026-11-26"}
MSEDCL_ORDER = "5ee7dce7-acfb-5b8b-a933-4ea3865809e2"


def get(path: str = "/tariffs/msedcl", headers: dict[str, str] | None = None, **params: Any) -> Any:
    r = client.get(path, params={**ON, **params}, headers=headers or CL)
    assert r.status_code == 200, r.text[:300]
    return r.json()


def figures(book: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for row in book["rows"]:
        out += row["figures"]
        for s in row["slots"]:
            out += s["figures"]
    return out


def absurd_draft() -> str:
    row = {
        "label": "absurd draft HT I",
        "scope": {"category": ["HT I (A)", "HT I (A) (i)"], "voltage_kv": {"min": 66}},
        "body": {
            "category": "HT I (A)",
            "billing_basis": "kvah",
            "demand_charge": {"value": "650", "basis": "inr_per_kva_month"},
            "energy_charge": {"base": "999.99"},
        },
        "source_ref": {"kind": "order", "document_id": MSEDCL_ORDER, "page": 100, "quote": "a draft"},
    }
    with session_scope(actor="analyst@jw") as s:
        v = service.propose(
            s,
            family="tariff_schedule",
            jurisdiction_id="msedcl",
            rows=[row],
            actor="analyst@jw",
            effective_from=date(2026, 4, 1),
            title="absurd draft",
            origin="arr_network",
        )
        return v.id


def test_the_approved_tariff_with_every_figure_sourced() -> None:
    restore_committed()
    b = get()
    assert b["discom_name"].startswith("Maharashtra State") and b["drafts"] is False
    assert [c["id"] for c in b["chain"]] == ["msedcl", "MH", "CERC"]
    fams = {r["family"] for r in b["rows"]}
    assert fams == {"tariff_schedule", "tod_slots", "oa_charge", "settlement"} and b["not_sourced"] == []
    assert {r["status"] for r in b["rows"]} == {"approved"}
    assert {v["status"] for v in b["versions"]} == {"approved"}
    assert {r["version_id"] for r in b["rows"]} == {v["id"] for v in b["versions"]}
    for v in b["versions"]:
        assert v["effective_from"] <= ON["on"] and v["golden_total"] >= v["golden_passed"] >= 0
    for f in figures(b):
        assert isinstance(f["value"]["value"], str) and f["value"]["source_refs"], f
        assert all(
            r["kind"] in ("order", "regulation", "register", "assumption") for r in f["value"]["source_refs"]
        )
    ht = next(r for r in b["rows"] if r["family"] == "tariff_schedule" and r["heading"] == "HT I (A)")
    energy = next(f for f in ht["figures"] if f["key"] == "/energy_charge/base")
    assert energy["value"]["unit"] == "INR/kVAh" and energy["value"]["value"] == "8.44"
    ref = energy["value"]["source_refs"][0]
    assert ref["url"] == f"/documents/{MSEDCL_ORDER}#page=100" and ref["rule_id"] == ht["rule_id"]
    assert b["documents"][MSEDCL_ORDER]["stored"] is False  # the PDF is not in the test store
    tod = next(r for r in b["rows"] if r["family"] == "tod_slots")
    assert next((s["name"], s["start"], s["end"]) for s in tod["slots"]) == ("normal", "00:00", "09:00")
    bank = next(r for r in b["rows"] if r["family"] == "settlement")
    assert bank["points"] and any(f["label"] == "Banking charge (in kind)" for f in bank["figures"])
    # a group DISCOM reads its group's schedule and the state's charges
    up = get("/tariffs/pvvnl")
    assert {r["jurisdiction_id"] for r in up["rows"]} >= {"uppcl", "UP"}


def test_an_absurd_draft_never_shows_by_default_and_only_an_analyst_sees_it() -> None:
    restore_committed()
    before = get()
    vid = absurd_draft()
    assert get() == before
    assert "999.99" not in client.get("/tariffs/msedcl", params=ON, headers=AN).text  # analyst, default view
    assert client.get("/tariffs/msedcl", params={**ON, "drafts": "true"}, headers=CL).status_code == 403
    assert client.get("/tariffs/msedcl", params={**ON, "drafts": "true"}).status_code == 403  # nobody
    d = get(headers=AN, drafts="true")
    assert d["drafts"] is True
    (draft,) = [r for r in d["rows"] if r["status"] == "draft"]
    assert draft["version_id"] == vid and draft["label"] == "absurd draft HT I"
    assert (
        next(f for f in draft["figures"] if f["key"] == "/energy_charge/base")["value"]["value"] == "999.99"
    )
    card = next(v for v in d["versions"] if v["id"] == vid)
    assert (card["status"], card["origin"], card["version"]) == ("draft", "arr_network", 3)
    assert card["golden_passed"] == 0 and card["golden_total"] >= 1
    # the approved row it would replace is still there, with the same match key
    twin = [r for r in d["rows"] if r["match_key"] == draft["match_key"] and r["status"] == "approved"]
    assert len(twin) == 1
    assert len(d["rows"]) == len(before["rows"]) + 1


def test_dates_and_unknowns() -> None:
    restore_committed()
    early = get(on="2000-01-01")
    assert early["rows"] == [] and set(early["not_sourced"]) == {
        "tariff_schedule",
        "tod_slots",
        "oa_charge",
        "settlement",
    }
    assert client.get("/tariffs/MH", headers=CL).status_code == 404  # a state is not a DISCOM
    assert client.get("/tariffs/nowhere", headers=CL).status_code == 404
    assert client.get("/tariffs/msedcl", params={"on": "not-a-date"}, headers=CL).status_code == 422


@pytest.fixture
def prod(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(get_settings(), "ergplan_env", "prod")
    return monkeypatch


def test_production_sign_in_and_open_access(prod: pytest.MonkeyPatch) -> None:
    restore_committed()
    absurd_draft()
    c = TestClient(app, base_url="https://testserver")
    assert c.get("/tariffs/msedcl", params=ON).status_code == 401  # signed out
    assert c.get("/tariffs/msedcl", params=ON, headers=AN).status_code == 401  # headers carry no weight
    prod.setattr(get_settings(), "ergplan_open_access", True)
    plain = c.get("/tariffs/msedcl", params=ON)
    assert plain.status_code == 200 and "999.99" not in plain.text
    # the open-access visitor is an analyst: may read drafts (never approve them)
    d = c.get("/tariffs/msedcl", params={**ON, "drafts": "true"})
    assert d.status_code == 200 and any(r["status"] == "draft" for r in d.json()["rows"])


def test_the_rule_book_screens_show_drafts_to_analysts_only() -> None:
    """Invariant 2: a draft reaches only the analysts, on the rule-book screens as on this tab."""
    restore_committed()
    vid = absurd_draft()
    listed = {v["id"] for v in client.get("/rulebooks/versions", headers=AN).json()}
    assert vid in listed
    for who in (CL, {}):
        assert vid not in {v["id"] for v in client.get("/rulebooks/versions", headers=who).json()}
        assert client.get(f"/rulebooks/versions/{vid}", headers=who).status_code == 404
    assert client.get(f"/rulebooks/versions/{vid}", headers=AN).json()["version"]["status"] == "draft"
