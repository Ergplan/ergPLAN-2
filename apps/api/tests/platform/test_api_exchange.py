"""Exchange purchase end to end on the real year of IEX prices (Stage 18): the verdict a client
sees comes from approved rules only; the optimizer offers no exchange source where there is no
approved rule or the rule bars it, and says why with the citation; a draft changes nothing for a
client and is costed only in the analyst's sandbox, where Rajasthan and two workable states are
worked for June at the stored prices (the same figures as the hand working in
tests/engine/test_exchange.py); a uniform rule is modelled as a flat block; a run pins the price
vintages and the rule and replays to the paisa after newer prices and a newer rule arrive.

The rules are fixtures approved in the test database (tests/engine/exchange_fixture.py)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ergplan.db.base import session_scope
from ergplan.db.models import Scenario
from ergplan.intake import iex_loader, iex_vintage
from ergplan.optimizer.highs_solve import solve
from ergplan.optimizer.resettle import resettle
from ergplan.optimizer.scenarios import ScenarioSpec
from ergplan.platform import runs as svc
from ergplan.platform.app import app
from ergplan.rulebook.repository import approved_rules
from tests.engine import exchange_fixture as fx
from tests.intake.test_iex_vintage import _month_csv
from tests.platform.test_api_plan import _truncate
from tests.platform.test_api_runs import AN, CL, _scenario, make_site, msedcl_5mw

client = TestClient(app)
SOLAR_AND_DAM = {
    "candidates": [
        {"kind": "oa_solar_intra", "ownership": "captive", "max_mw": "4"},
        {"kind": "iex_dam", "max_mw": "4"},
    ],
    "as_of": "2026-06-01",
    "resolution_min": 60,
}
DAM_ONLY = {"candidates": [{"kind": "iex_dam", "max_mw": "4"}], "as_of": "2026-06-01", "resolution_min": 60}


@pytest.fixture(scope="module")
def site() -> Iterator[dict[str, str]]:
    _truncate()
    mp = pytest.MonkeyPatch()
    try:
        site_id, conn = make_site(mp, msedcl_5mw(), {})
        with session_scope(actor=iex_loader.COPIED_BY) as s:
            vintages = {v.segment: v.id for v, _ in iex_vintage.load(s)}
        yield {"site": site_id, "conn": conn, **vintages}
    finally:
        mp.undo()
        _truncate()


@pytest.fixture(autouse=True)
def clean_db() -> None:  # the module's site, prices and rules are shared by its tests, in order
    return None


def run(site: dict[str, str], spec: dict[str, Any], name: str) -> dict[str, Any]:
    sid = _scenario(site["site"], site["conn"], spec, name)
    r = client.post(f"/scenarios/{sid}/run", params={"wait": "true"}, headers=AN)
    assert r.status_code == 202, r.text
    out: dict[str, Any] = r.json()
    assert out["status"] == "done", out["error"]
    return out


def test_with_no_approved_rule_the_verdict_is_not_yet_sourced_and_nothing_is_offered(
    site: dict[str, str],
) -> None:
    r = client.get(f"/connections/{site['conn']}/exchange", headers=CL)
    assert r.status_code == 200, r.text
    out = r.json()
    v = out["verdict"]
    assert v["verdict"] == "not_yet_sourced" and v["verdict_label"] == "Not yet sourced" and not v["offered"]
    assert "Maharashtra" in v["words"] and "no approved rule" in v["words"]
    # the provenance of the prices: every month from the year copied from Ergplan/tenderos2
    assert [p["month"] for p in out["prices"]] == list(range(1, 13))
    assert {p["vintage_id"] for p in out["prices"]} == {site["dam"]}
    assert out["prices"][0]["filename"].startswith("Ergplan/tenderos2@")
    assert out["prices"][0]["date_from"] == "2025-11-19" and out["prices"][0]["date_to"] == "2026-11-18"
    assert client.get("/connections/missing/exchange", headers=CL).status_code == 404
    # the client's choice of sources: the exchange is not available, and says why
    by = {o["kind"]: o for o in client.get(f"/connections/{site['conn']}/mix/options", headers=CL).json()}
    assert not by["iex_dam"]["available"] and "not yet sourced" in by["iex_dam"]["reason"]
    assert not by["iex_gdam"]["available"] and by["oa_solar_intra"]["available"]
    r = client.post(f"/connections/{site['conn']}/mix", json={"sources": [{"kind": "iex_dam"}]}, headers=CL)
    assert r.status_code == 422 and "not yet sourced" in r.text


def test_a_draft_reaches_only_the_analysts_sandbox(site: dict[str, str]) -> None:
    vid = fx.propose("MH", fx.BY_BLOCK)  # a DRAFT that would allow the purchase
    assert client.get(f"/connections/{site['conn']}/exchange", headers=CL).json()["verdict"]["verdict"] == (
        "not_yet_sourced"
    )
    assert client.get(f"/exchange/sandbox/{vid}", headers=CL).status_code == 403
    assert client.get("/exchange/sandbox/missing", headers=AN).status_code == 404
    sb = client.get(f"/exchange/sandbox/{vid}", params={"contract_demand_kva": "5000"}, headers=AN).json()
    assert (
        sb["verdict"] == "feasible_with_conditions" and sb["offered"] and sb["schedule"] == "block by block"
    )
    assert sb["state"] == "Maharashtra" and sb["conditions"][0]["refs"][0]["document_id"] == fx.DOC
    small = client.get(f"/exchange/sandbox/{vid}", params={"contract_demand_kva": "600"}, headers=AN).json()
    assert small["verdict"] == "not_practical" and "600 kVA is below" in small["reasons"][0]["text"]
    # the optimizer offers no exchange source: the run leaves it out and says why (invariant 2)
    out = run(site, SOLAR_AND_DAM, "no rule")
    sm = out["summary"]
    assert {z["kind"] for z in sm["sizes"]} == {"oa_solar_intra"}
    [note] = [n for n in sm["notes"] if "IEX day-ahead" in n]
    assert "not offered" in note and "not yet sourced" in note
    [x] = sm["exchange"]
    assert x["kind"] == "iex_dam" and x["verdict"] == "not_yet_sourced" and not x["offered"]
    assert out["pins"]["exchange"] == {"gate": 1, "deviation_pct": "5"}
    assert out["pins"]["price_vintages"] == []  # no price was read


def cost(vid: str, **body: Any) -> Any:
    return client.post(f"/exchange/sandbox/{vid}/month-cost", json=body, headers=AN)


def test_rajasthan_and_two_workable_states_costed_for_june_at_the_stored_prices(
    site: dict[str, str],
) -> None:
    """The hand working of tests/engine/test_exchange.py, through the sandbox at the prices in
    price_series: the same totals to the paisa."""
    rj = fx.propose("RJ", fx.UNIFORM_WITH_BARRIER)
    r = cost(rj, segment="dam", month=6, kw="1000", loss_pct="5", charges_inr_per_kwh="2.50",
             grid_inr_per_kwh="7.00", deviation_pct="5")  # fmt: skip
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["verdict"]["verdict"] == "not_practical" and out["verdict"]["state"] == "Rajasthan"
    assert out["verdict"]["schedule"] == "one flat block over the day" and out["days"] == 30
    assert out["scheduled_kwh"]["value"] == "720000.000" and out["delivered_kwh"]["value"] == "684000.000"
    assert out["price_inr"]["value"] == "3721115.93" and out["charges_inr"]["value"] == "1710000.00"
    assert out["deviation_inr"]["value"] == "331405.80" and out["total_inr"]["value"] == "5762521.72"
    assert out["landed_inr_per_kwh"]["value"] == "8.4247"
    assert "costs more than the grid's cost for the month by ₹974521.72" in out["words"]
    kinds = {ref["kind"] for ref in out["total_inr"]["source_refs"]}
    assert {"market", "client_stated", "regulation"} <= kinds
    market = next(ref for ref in out["total_inr"]["source_refs"] if ref["kind"] == "market")
    assert market["vintage_id"] == site["dam"] == out["vintage"]["vintage_id"]
    assert out["vintage"]["filename"].startswith("Ergplan/tenderos2@")
    # a schedule shaped to the day is refused where the rule requires a uniform one
    r = cost(rj, segment="dam", month=6, kw="2000", from_hour=9, to_hour=17, loss_pct="5",
             charges_inr_per_kwh="2.50", grid_inr_per_kwh="7.00")  # fmt: skip
    assert r.status_code == 422 and "The schedule is refused" in r.text and "uniform schedule" in r.text

    mh = fx.propose("MH", fx.BY_BLOCK)
    out = cost(mh, segment="dam", month=6, kw="2000", from_hour=9, to_hour=17, loss_pct="3",
               charges_inr_per_kwh="1.80", grid_inr_per_kwh="8.00", deviation_pct="5").json()  # fmt: skip
    assert out["verdict"]["verdict"] == "feasible_with_conditions"
    assert out["scheduled_kwh"]["value"] == "480000.000" and out["price_inr"]["value"] == "835038.40"
    assert out["deviation_inr"]["value"] == "83655.92" and out["total_inr"]["value"] == "1756774.32"
    assert out["landed_inr_per_kwh"]["value"] == "3.7731" and "lowers the grid's cost" in out["words"]

    gj = fx.propose("GJ", fx.BY_BLOCK_OVER_AT_MULTIPLE)
    out = cost(gj, segment="gdam", month=6, kw="1500", from_hour=22, to_hour=6, loss_pct="4.5",
               charges_inr_per_kwh="0.90", grid_inr_per_kwh="6.50", deviation_pct="5").json()  # fmt: skip
    assert out["delivered_kwh"]["value"] == "343800.000" and out["price_inr"]["value"] == "3193229.81"
    assert out["deviation_inr"]["value"] == "55867.50" and out["total_inr"]["value"] == "3558517.31"
    assert out["landed_inr_per_kwh"]["value"] == "10.3505" and "costs more than" in out["words"]
    assert out["vintage"]["vintage_id"] == site["gdam"]
    # no prices for the market, a client, and a rule that states no schedule
    assert cost(gj, segment="gtam", month=6, kw="1", loss_pct="0", charges_inr_per_kwh="0",
                grid_inr_per_kwh="0").status_code == 422  # fmt: skip
    assert client.post(f"/exchange/sandbox/{gj}/month-cost", json={}, headers=CL).status_code == 403
    bare = fx.propose("PB", fx.body(cross_subsidy_surcharge={"payable": "yes"}))
    r = cost(bare, month=6, kw="1", loss_pct="0", charges_inr_per_kwh="0", grid_inr_per_kwh="0")
    assert r.status_code == 422 and "not yet sourced" in r.text


def test_where_the_approved_rule_bars_it_the_plan_says_why_citing_the_rule(site: dict[str, str]) -> None:
    fx.approve("MH", fx.UNIFORM_WITH_BARRIER)
    v = client.get(f"/connections/{site['conn']}/exchange", headers=CL).json()["verdict"]
    assert v["verdict"] == "not_practical" and not v["offered"]
    assert v["reasons"][0]["refs"][0]["page"] == 40 and v["reasons"][0]["refs"][0]["url"]
    assert [c["field"] for c in v["apply_for"]] == ["sldc_clearance", "contract_demand"]
    out = run(site, SOLAR_AND_DAM, "barred")
    sm = out["summary"]
    assert {z["kind"] for z in sm["sizes"]} == {"oa_solar_intra"}
    [note] = [n for n in sm["notes"] if "IEX day-ahead" in n]
    assert "not practical in Maharashtra" in note and "before the exchange's results are known" in note
    assert f"(source: page 40 of document {fx.DOC})" in note
    [x] = sm["exchange"]
    assert x["verdict"] == "not_practical" and x["reasons"][0]["refs"][0]["document_id"] == fx.DOC
    r = client.post(f"/connections/{site['conn']}/mix", json={"sources": [{"kind": "iex_dam"}]}, headers=CL)
    assert r.status_code == 422 and "not practical" in r.text


def test_a_uniform_rule_is_a_flat_block_and_the_run_pins_prices_and_rule_and_replays(
    site: dict[str, str],
) -> None:
    rule = fx.approve("MH", fx.UNIFORM)
    by = {o["kind"]: o for o in client.get(f"/connections/{site['conn']}/mix/options", headers=CL).json()}
    assert by["iex_dam"]["available"] and by["iex_dam"]["reason"] is None
    out = run(site, DAM_ONLY, "uniform")
    [z] = out["summary"]["sizes"]
    ex = z["exchange"]
    assert z["kind"] == "iex_dam" and ex["verdict"]["verdict"] == "feasible_with_conditions"
    assert ex["verdict"]["schedule"] == "one flat block over the day"
    assert any("same in every 15-minute block" in c["text"] for c in ex["verdict"]["conditions"])
    assert out["summary"]["exchange"][0]["offered"] is True
    bought = Decimal(z["delivered_kwh"]["value"])
    hours = [Decimal(h["value"]) for h in ex["by_hour_kwh"]]
    assert len(hours) == 24 and sum(hours) == bought
    # one flat block over each day: every hour of the day carries the same energy over the year
    assert max(hours) - min(hours) <= Decimal("0.5")
    if bought > 0:
        assert ex["words"].startswith("The exchange lowers the cost in")
        dev = ex["deviation"]
        assert Decimal(dev["cost"]["value"]) > 0 and dev["assumed_pct"]["value"] == "5"
        assert dev["assumed_pct"]["source_refs"][0]["kind"] == "assumption"
        assert dev["refs"][0]["document_id"] == fx.DOC
        months = ex["by_month"]
        assert sum(Decimal(m["kwh"]["value"]) for m in months) == bought
        for m in months:  # bought only where it lowers the cost
            assert Decimal(m["avg_landed"]["value"]) <= Decimal(m["avg_grid"]["value"])
            assert any(r["kind"] == "market" for r in m["kwh"]["source_refs"])
    else:
        assert ex["words"].startswith("The exchange does not lower the cost in any hour")
    # pins: the vintage read (repository, commit, file and SHA-256 are on it), the rule, the deviation
    pins = out["pins"]
    assert pins["price_vintages"] == [site["dam"]]
    assert pins["price_vintages_by_segment_all"] == {"dam": [site["dam"]]}
    assert rule in pins["rulebook_versions"] and pins["exchange"] == {"gate": 1, "deviation_pct": "5"}
    vint = client.get(f"/prices/vintages/{site['dam']}", headers=AN).json()
    assert vint["filename"].startswith("Ergplan/tenderos2@") and len(vint["file_sha256"]) == 64
    # newer prices arrive (an analyst's November 2026) and the rule is replaced: the replay still
    # reads what the run pinned and is identical to the paisa
    with session_scope(actor="analyst@jw") as s:
        nov = iex_loader.parse(_month_csv(date(2026, 11, 1), 30, "100"), "DAM_nov26.csv")
        newer = iex_loader.store(s, nov, "DAM_nov26.csv", "analyst@jw")
        newer_id = newer.id
    fx.approve("MH", fx.BY_BLOCK)
    rp = client.post(f"/runs/{out['id']}/replay", params={"wait": "true"}, headers=AN).json()
    assert rp["status"] == "done" and rp["identical_to_original"] is True
    assert rp["summary"] == out["summary"]
    # a new run reads the analyst's November and the copied year for the other months
    again = run(site, DAM_ONLY, "after the upload")
    assert sorted(again["pins"]["price_vintages_by_segment_all"]["dam"]) == sorted([site["dam"], newer_id])
    again_ex = again["summary"]["sizes"][0]["exchange"]
    assert again_ex["verdict"]["schedule"] == "block by block"
    nov_row = next(m for m in again_ex["by_month"] if m["month"] == 11)
    assert Decimal(nov_row["avg_landed"]["value"]) < Decimal(nov_row["avg_grid"]["value"])


def test_a_run_made_before_exchange_sources_were_gated_is_assembled_as_it_was(site: dict[str, str]) -> None:
    """Replay of an older run (its pins carry no `exchange`): offered, block by block, no deviation
    priced, whatever the rules say now."""
    with session_scope(actor="t") as s:
        sc = Scenario(site_id=site["site"], connection_id=site["conn"], name="probe", spec={}, created_by="t")
        spec = ScenarioSpec.model_validate(DAM_ONLY)
        old = svc.assemble(s, sc, spec, approved_rules, exchange_pins={"gate": 0})
        new = svc.assemble(s, sc, spec, approved_rules)
    assert old.exchange == [] and old.cands[0].exchange is None and old.exchange_deviation is None
    assert old.problem.candidates[0].uniform_hours == 0.0 and not old.exchange_pins.get("gate")
    assert new.cands[0].exchange is not None and new.exchange_deviation is not None
    # the deviation risk is in the new run's block prices and not in the old one's
    assert new.problem.candidates[0].energy_inr_per_kwh[0] > old.problem.candidates[0].energy_inr_per_kwh[0]
    # and its result is shaped as it was: no verdict, no breakdown, nothing a stored digest lacks
    sol = solve(old.problem, 60.0, 0.001)
    summary = svc.summarise(old, sol, resettle(old.problem, sol, Decimal("1")))
    assert "exchange" not in summary
    assert set(summary["sizes"][0]["exchange"]) == {
        "segment",
        "blocks_bought",
        "hours_bought",
        "avg_landed",
        "share_of_load",
    }
