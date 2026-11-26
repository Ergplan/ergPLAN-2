"""Stage 19: the landed-cost calculator through the API. One consumer (PVVNL HV-2, 33 kV, 5,000 kVA)
priced on approved rules: three routes for solar and for wind, line by line per ToD slot, beside the
grid tariff and the saving, every figure a SourcedValue and every unstated line "not yet sourced";
the exchange route follows Stage 18's verdict (withheld from a visitor while it is not yet sourced,
the analyst's figure for an analyst, shown once an approved rule offers it, barred where not
practical); a state's minimum contract demand bars open access with the rule; a draft changes
nothing; every state at once, as JSON and as CSV; an approved state rule book as a file; the
setting ERGPLAN_PUBLIC_CALCULATOR (off: sign-in like every screen; on: a visitor may use it); the
rate limit; and a visitor's use stores nothing."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ergplan.core.settings import get_settings
from ergplan.db.base import get_engine, session_scope
from ergplan.db.models import RuleTest
from ergplan.intake import iex_loader
from ergplan.platform import ratelimit
from ergplan.platform.app import app
from ergplan.rulebook import service
from tests.engine import exchange_fixture as fx
from tests.engine.helpers import restore_committed
from tests.intake.test_iex_vintage import _month_csv
from tests.platform.test_api_plan import _truncate
from tests.platform.test_auth import prod, sign_in  # noqa: F401  (prod is a fixture)

client = TestClient(app)
AN = {"X-Ergplan-Actor": "analyst@joulewise.in", "X-Ergplan-Role": "analyst"}
CL = {"X-Ergplan-Actor": "client@acme.in", "X-Ergplan-Role": "client"}
UP = {
    "discom": "pvvnl",
    "category": "HV-2",
    "voltage_kv": "33",
    "contract_demand_kva": "5000",
    "month": "2026-11-01",
    "as_of": "2026-11-01",
}
ROUTES = ["captive", "group_captive", "third_party"]


@pytest.fixture(scope="module", autouse=True)
def rules() -> Iterator[None]:
    _truncate()
    restore_committed()
    with session_scope(actor="analyst@jw") as s:  # a month of DAM prices: ₹5,000/MWh in every block
        iex_loader.store(s, iex_loader.parse(_month_csv(date(2026, 11, 1), 30, "5000"), "DAM_nov26.csv"),
                         "DAM_nov26.csv", "analyst@jw")  # fmt: skip
    yield
    _truncate()


@pytest.fixture(autouse=True)
def clean_db() -> None:  # the module's rules and prices are shared by its tests, in order
    ratelimit.reset()  # as conftest's clean_db does


def calc(body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    r = client.post("/calculator", json={**UP, **(body or {})}, headers=headers or CL)
    assert r.status_code == 200, r.text[:500]
    return dict(r.json())


def sourced_values(node: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(node, dict):
        if {"value", "unit", "source_refs"} <= set(node):
            out.append(node)
            return
        for v in node.values():
            sourced_values(v, out)
    elif isinstance(node, list):
        for v in node:
            sourced_values(v, out)


def test_three_routes_for_solar_and_wind_line_by_line_beside_the_grid() -> None:
    out = calc()
    assert out["state"] == "UP" and out["category"] == "HV-2" and out["rate_block"] == "Urban Schedule"
    assert out["stored"] is False and len(out["slots"]) == 4
    assert all(s["rate"] and D(s["rate"]["value"]) > 0 for s in out["slots"])
    assert [t["tech"] for t in out["technologies"]] == ["solar", "wind"]
    for t in out["technologies"]:
        assert [r["route"] for r in t["routes"]] == ROUTES
        for r in t["routes"]:
            assert r["allowed"] and r["shown"] and len(r["slots"]) == 4
            for sl in r["slots"]:
                assert sum(D(x["value"]) for x in sl["lines"]) == D(sl["landed"]["value"])
                assert D(sl["saving"]["value"]) == D(sl["grid"]["value"]) - D(sl["landed"]["value"])
                assert not any((x["label"] or "").startswith("Banking") for x in sl["lines"])
            # what the approved rules do not state is listed, in words, never estimated
            assert {"oa_additional_surcharge", "electricity_duty"} <= {
                n["component"] for n in r["not_sourced"]
            }
            assert all(n["words"] for n in r["not_sourced"])
    solar = {r["route"]: r for r in out["technologies"][0]["routes"]}
    # third party pays the CSS; captive and 26% equity are exempt, a line of its own
    labels = lambda r: [x["label"] for x in r["slots"][0]["lines"]]  # noqa: E731
    assert not any("exemption" in x for x in labels(solar["third_party"]))
    assert any("exemption" in x for x in labels(solar["captive"]))
    assert any(x.startswith("Equity cost") for x in labels(solar["group_captive"]))
    assert solar["captive"]["slots"][0]["banking"] and not solar["third_party"]["slots"][0]["banking"]
    assert "banking" in {n["component"] for n in solar["third_party"]["not_sourced"]}
    # the captive price rests on the plant's figures, each an assumption until stated
    assert {x["source_refs"][0]["kind"] for x in solar["captive"]["inputs"]} == {"assumption"}
    stated = calc({"solar": {"capex_inr_per_kw": "40000", "ppa_tariff_inr_per_kwh": "2.95"}})
    s2 = {r["route"]: r for r in stated["technologies"][0]["routes"]}
    assert s2["third_party"]["slots"][0]["lines"][0] == {
        **s2["third_party"]["slots"][0]["lines"][0], "value": "2.95", "label": "PPA tariff"
    }  # fmt: skip
    assert s2["third_party"]["slots"][0]["lines"][0]["source_refs"][0]["kind"] == "client_stated"
    assert D(s2["captive"]["landed_avg"]["value"]) < D(solar["captive"]["landed_avg"]["value"])


def test_every_figure_carries_a_source_ref_or_is_assumed_or_not_sourced() -> None:
    out = calc()
    found: list[dict[str, Any]] = []
    sourced_values(out, found)
    assert len(found) > 100
    for v in found:
        assert v["source_refs"], v
        for r in v["source_refs"]:
            assert r["kind"] in {"order", "regulation", "register", "client_stated", "assumption", "derived",
                                 "market", "bill"}  # fmt: skip
            if r["kind"] in ("order", "regulation") and r.get("document_id") and r.get("page"):
                assert r["url"] == f"/documents/{r['document_id']}#page={r['page']}"
    # an order page opens from a line: the wheeling charge cites p. 379 of the FY 2026-27 order
    wheel = next(
        x for x in out["technologies"][0]["routes"][2]["slots"][0]["lines"] if "wheeling" in x["label"]
    )
    assert any(r.get("page") == 379 for r in wheel["source_refs"])
    # a route not shown says why; nothing is shown without a reason
    for r in [x for t in out["technologies"] for x in t["routes"]] + [out["exchange"]["route"]]:
        assert r["shown"] or r["reason"]


def test_the_exchange_follows_the_states_verdict() -> None:
    # no approved exchange rule for Uttar Pradesh: "not yet sourced", the figures withheld from a client
    out = calc()
    x = out["exchange"]
    assert x["verdict"]["verdict"] == "not_yet_sourced" and x["market"] == "dam"
    assert not x["route"]["shown"] and x["route"]["slots"] == [] and x["route"]["landed_avg"] is None
    assert x["route"]["reason"].startswith("Not yet sourced")  # DAM prices are loaded
    # an analyst sees the figure, marked as theirs; the price is the month's mean clearing price
    an = calc(headers=AN)["exchange"]["route"]
    assert an["shown"] and an["withheld"] and "does not see" in an["withheld"]
    price = an["slots"][0]["lines"][0]
    assert price["value"] == "5.0000" and price["source_refs"][0]["kind"] == "market"
    # a market without prices loaded cannot be priced, and says so
    rtm = calc({"iex_market": "rtm"}, headers=AN)["exchange"]["route"]
    assert not rtm["shown"] and "no RTM prices" in rtm["reason"]
    # an approved rule that offers the purchase (a test fixture): a client sees the figures
    fx.approve("UP", fx.BY_BLOCK)
    ok = calc()["exchange"]
    assert ok["verdict"]["offered"] is True and ok["route"]["shown"] and not ok["route"]["withheld"]
    assert ok["route"]["slots"][0]["lines"][0]["value"] == "5.0000"
    # where the rule makes it not practical, the route is barred, with the rule's reasons
    fx.approve("RJ", fx.UNIFORM_WITH_BARRIER)
    rj = calc({"discom": "jvvnl", "category": None})["exchange"]
    assert rj["verdict"]["verdict"] == "not_practical"
    assert not rj["route"]["allowed"] and rj["route"]["reason"].startswith("Not practical")
    assert "page" in rj["route"]["reason"]


def test_a_minimum_contract_demand_bars_open_access_with_the_rule() -> None:
    row = {
        "label": "fixture · minimum contract demand for open access",
        "scope": {},
        "body": {"limit": "min_contract_demand_for_oa_kva", "value": "1000", "unit": "kva"},
        "source_ref": fx.ref(7, "the fixture regulation's minimum contract demand"),
    }
    with session_scope(actor="analyst@jw") as s:
        s.add(RuleTest(jurisdiction_id="UP", family="regulatory_limit", name="fixture: min CD",
                       given={"context": {}, "field": "/value", "origin": "manual"}, expect={"value": "1000"}))  # fmt: skip
        v = service.propose(s, family="regulatory_limit", jurisdiction_id="UP", rows=[row], actor="analyst@jw",
                            effective_from=date(2025, 4, 1), title="fixture limit")  # fmt: skip
        vid = v.id
    with session_scope(actor="analyst@jw", role="analyst") as s:
        service.approve(s, vid, actor="analyst@jw", role="analyst")
    small = calc({"contract_demand_kva": "800"})
    for t in small["technologies"]:
        for r in t["routes"]:
            assert not r["allowed"] and not r["slots"] and "1000 kVA" in r["reason"]
            assert r["reason_values"][0]["source_refs"][0]["page"] == 7
    assert all(r["allowed"] for t in calc()["technologies"] for r in t["routes"])


def test_a_draft_changes_nothing() -> None:
    before = calc()
    absurd = {
        "label": "absurd draft wheeling",
        "scope": {"connection": ["intra_state", "ists"]},
        "body": {"basis": "inr_per_kwh", "value": "99.99", "component": "wheeling"},
        "source_ref": fx.ref(3, "a draft"),
    }
    with session_scope(actor="analyst@jw") as s:
        service.propose(s, family="oa_charge", jurisdiction_id="uppcl", rows=[absurd], actor="analyst@jw",
                        effective_from=date(2025, 4, 1), title="absurd draft")  # fmt: skip
    assert calc() == before
    assert "99.99" not in client.post("/calculator/states.csv", json=UP, headers=CL).text


def test_every_state_at_once_and_as_csv() -> None:
    r = client.post("/calculator/states", json=UP, headers=CL)
    assert r.status_code == 200, r.text[:300]
    t = r.json()
    states = {x["state"] for x in t["rows"]}
    assert {"UP", "MH", "KA", "GJ", "PB", "TS"} <= states
    up = next(x for x in t["rows"] if x["discom"] == "pvvnl")
    one = calc()
    cell = next(c for c in up["cells"] if c["tech"] == "solar" and c["route"] == "third_party")
    want = next(r for r in one["technologies"][0]["routes"] if r["route"] == "third_party")
    assert cell["landed_avg"] == want["landed_avg"] and cell["saving_avg"] == want["saving_avg"]
    assert len(up["cells"]) == 7  # three routes for solar, three for wind, the exchange
    f = client.post("/calculator/states.csv", json=UP, headers=CL)
    assert f.status_code == 200 and f.headers["content-type"].startswith("text/csv")
    assert f.headers["content-disposition"] == 'attachment; filename="landed-cost-all-states-2026-11.csv"'
    rows = list(csv.DictReader(io.StringIO(f.text)))
    assert len(rows) == sum(len(x["cells"]) or 1 for x in t["rows"])
    line = next(
        x
        for x in rows
        if x["discom"] == "pvvnl" and x["technology"] == "solar" and x["route"] == "third party"
    )
    assert line["landed_inr_per_kwh"] == want["landed_avg"]["value"]
    assert "e071ac95-048e-58dc-a9da-b2e59365f106#page=" in line["landed_sources"]
    assert "oa_additional_surcharge" in line["not_yet_sourced"]


def test_an_approved_state_rule_book_downloads_a_draft_does_not() -> None:
    assert calc()["rulebook"] is None
    g = client.post("/statebooks/generate", json={"discom": "pvvnl", "month": "2026-11-01"}, headers=AN)
    assert g.status_code == 201, g.text[:300]
    assert calc()["rulebook"] is None  # a draft is never linked
    book = g.json()["id"]
    assert client.get(f"/calculator/statebooks/{book}/file", headers=CL).status_code == 404
    assert client.post(f"/statebooks/{book}/approve", json={"note": "checked"}, headers=AN).status_code == 200
    link = calc()["rulebook"]
    assert link["book_id"] == book and link["file"] == f"/calculator/statebooks/{book}/file"
    f = client.get(link["file"], headers=CL)
    assert f.status_code == 200 and f.text.startswith("# State rule book")
    assert "attachment" in f.headers["content-disposition"]


def _counts() -> dict[str, int]:
    with get_engine().connect() as c:
        tables = [r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))]
        return {t: int(c.execute(text(f'select count(*) from "{t}"')).scalar_one()) for t in tables}


def test_off_by_default_the_calculator_needs_sign_in(prod: list[tuple[str, str]]) -> None:  # noqa: F811
    assert get_settings().ergplan_public_calculator is False
    visitor = TestClient(app, base_url="https://testserver")
    assert visitor.get("/calculator/options").status_code == 401
    assert visitor.post("/calculator", json=UP).status_code == 401
    assert visitor.post("/calculator/states.csv", json=UP).status_code == 401
    signed = sign_in("ops@acme.in", prod)
    assert signed.post("/calculator", json=UP).status_code == 200


def test_switched_on_a_visitor_may_use_it_and_it_stores_nothing(
    prod: list[tuple[str, str]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "ergplan_public_calculator", True)
    visitor = TestClient(app, base_url="https://testserver")
    before = _counts()
    opts = visitor.get("/calculator/options")
    assert opts.status_code == 200
    up = next(s for s in opts.json()["states"] if s["state"] == "UP")
    pv = next(d for d in up["discoms"] if d["discom"] == "pvvnl")
    assert "HV-2" in {c["category"] for c in pv["categories"]}
    r = visitor.post("/calculator", json=UP)
    assert r.status_code == 200 and r.json()["technologies"][0]["routes"][0]["shown"]
    assert not r.json()["exchange"]["route"]["withheld"]  # a visitor never gets the analyst's figure
    assert visitor.post("/calculator", json={**UP, "discom": "nowhere"}).status_code == 422
    assert visitor.post("/calculator/states", json=UP).status_code == 200
    assert visitor.post("/calculator/states.csv", json=UP).status_code == 200
    assert _counts() == before  # every table, row for row
    # the rest of the app stays closed to the visitor
    assert visitor.get("/statebooks").status_code == 401


def test_the_calculator_is_rate_limited_per_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "ergplan_calculator_rate_limit", 2)
    c = TestClient(app)
    assert [c.get("/calculator/options").status_code for _ in range(2)] == [200, 200]
    r = c.post("/calculator", json=UP)
    assert r.status_code == 429 and "too many requests" in r.json()["detail"]


def test_bad_input_is_refused_in_words() -> None:
    r = client.post("/calculator", json={**UP, "category": "HV-9"}, headers=CL)
    assert r.status_code == 422 and "not a category" in r.json()["detail"]
    r = client.post("/calculator", json={**UP, "state": "MH"}, headers=CL)
    assert r.status_code == 422 and "is in UP" in r.json()["detail"]
    assert client.post("/calculator", json={**UP, "voltage_kv": "-1"}, headers=CL).status_code == 422
