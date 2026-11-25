"""Finding 2 of the Uttar Pradesh full test (docs/stage-reports/up-full-test-2026-11-25.md), on the
same twelve hand-worked PVVNL HV-2 bills: a captive open-access plant is the consumer's own and
carries its capital (an assumption, never a sourced fact) into the year's cost, payback and IRR; a
plant taken with 26% equity carries the equity outlay and pays the tariff, with no equity charged
again per kWh; a third-party plant carries no capital, and capital beside it is not credited with
its saving; a plan with no capital of its own has no payback, and says why; runs replay to the
paisa, and a run made before this treatment replays the old way."""

from __future__ import annotations

import json
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ergplan.db.base import session_scope
from ergplan.db.models import Run
from ergplan.engine import lcoe
from ergplan.platform.app import app
from ergplan.platform.routers.landed import EQUITY_LABEL, OWN_LCOE
from ergplan.report import whatif
from tests.platform.test_api_up_levies import AN, AS_OF, site  # noqa: F401

client = TestClient(app)
SOLAR = lcoe.default_utility("solar")
CRF = lcoe.crf(SOLAR.discount_pct, SOLAR.life_years).value
UP_SOLAR_CUF = D(20)  # packages/profiles/cuf/solar_up.yaml


def _kinds(sv: dict[str, Any]) -> set[str]:
    return {r["kind"] for r in sv["source_refs"]}


def _run(site: dict[str, str], name: str, cands: list[dict[str, Any]]) -> dict[str, Any]:  # noqa: F811
    spec = {"candidates": cands, "as_of": AS_OF, "resolution_min": 60}
    sc = client.post(
        f"/sites/{site['site']}/scenarios",
        json={"connection_id": site["conn"], "name": name, "spec": spec},
        headers=AN,
    )
    assert sc.status_code == 201, sc.text
    run: dict[str, Any] = client.post(
        f"/scenarios/{sc.json()['id']}/run", params={"wait": "true"}, headers=AN
    ).json()
    assert run["status"] == "done", run["error"]
    assert run["analyst"]["resettle"]["agrees"] is True  # invariant 6: the judge and the solver agree
    return run


def _line(z: dict[str, Any], label: str) -> list[dict[str, Any]]:
    return [ln for ln in next(iter(z["waterfall"].values())) if str(ln["label"]).startswith(label)]


def test_a_captive_plant_carries_its_capital_into_cost_payback_and_irr(site: dict[str, str]) -> None:  # noqa: F811
    run = _run(
        site,
        "captive",
        [
            {"kind": "rooftop_pv", "max_mw": "1"},
            {"kind": "oa_solar_intra", "ownership": "captive", "max_mw": "6", "price_inr_per_kwh": "2.5"},
        ],
    )
    s, lines = run["summary"], run["analyst"]["lines_after"]
    z = {x["kind"]: x for x in s["sizes"]}
    kw = D(z["oa_solar_intra"]["kw"]["value"])
    assert kw > 1000
    # the plant's capital: the benchmark per kW × the capacity sized, an assumption (never sourced)
    oc = s["owned_capital"]
    assets = {a["id"]: a for a in oc["assets"]}
    assert set(assets) == {"rooftop_pv", "oa_solar_intra"}
    plant = assets["oa_solar_intra"]
    assert plant["kind"] == "plant" and D(plant["capex"]["value"]) == (kw * 42000).quantize(D("0.01"))
    assert _kinds(plant["capex"]) == {"assumption"}  # an editable assumption, never a sourced fact
    assert any(
        "to be replaced by your own figure" in str(r.get("note")) for r in plant["capex"]["source_refs"]
    )
    total = sum((D(a["capex"]["value"]) for a in oc["assets"]), D(0))
    assert D(oc["total"]["value"]) == total == D(s["annual"]["capex"]["value"])
    assert D(s["lifecycle"]["capex"]["value"]) == total
    assert s["annual"]["capex"]["label"] == "Capital of your own (plant, batteries and equity outlay)"
    # its yearly cost in the optimizer and the re-settled lines: capital × CRF + O&M per kW
    yearly = lcoe.annual_cost_per_kw(SOLAR).value
    assert abs(D(lines["fixed:oa_solar_intra"]) - kw * yearly) < D("0.01")
    # no tariff per kWh: what is paid per kWh delivered is the open-access charges and the assumed duty
    delivered = D(z["oa_solar_intra"]["delivered_kwh"]["value"])
    per_kwh = D(lines["energy:oa_solar_intra"]) / delivered
    assert D("1.5") < per_kwh < D("2.0")  # 0.3075 + 0.9318 grossed up by the losses, + duty 0.52
    assert not _line(z["oa_solar_intra"], "PPA tariff")
    [own] = _line(z["oa_solar_intra"], OWN_LCOE)
    assert _kinds(own) == {"assumption"}  # the levelised cost shown for it is built on assumptions
    assert D(z["oa_solar_intra"]["energy_cost"]["value"]) == D(lines["fixed:oa_solar_intra"]) + D(
        lines["energy:oa_solar_intra"]
    )
    # the price typed for a plant of one's own is not used, and the run says so
    assert any("the price you stated (₹2.5/kWh) is not used" in n for n in s["notes"])
    assert any("your own plant" in n and "capital at year 0" in n for n in s["notes"])
    # payback and IRR are on that capital: no longer about a year on the rooftop's ₹4 crore alone
    assert D(s["payback_years"]["value"]) > 3 and D(s["irr_pct"]["value"]) < 40
    assert s["payback_years"]["label"] == "Payback on the owned capital (simple)"
    assert s["irr_pct"]["label"] == "IRR on the owned capital"
    assert oc["bought"] == [] and oc["bought_saving"] is None and oc["payback_absent"] is None
    assert (
        "against the whole plan's saving: every source in the plan carries capital of your own" in oc["basis"]
    )
    y1 = s["lifecycle"]["years"][0]
    assert y1["owned_saving"]["value"] == y1["saving"]["value"] == oc["saving"]["value"]
    assert D(oc["saving"]["value"]) > D(s["annual"]["saving"]["value"])  # before the capital's yearly cost
    assert not any("read them as the plan's return" in n for n in s["notes"])  # the old caveat is gone
    # a what-if holds the same treatment: a plant of one's own has no price to change, and a smaller
    # plant carries less capital
    with session_scope() as db:
        row = db.get(Run, run["id"])
        assert row is not None
        with pytest.raises(whatif.WhatIfFailed, match="pays no tariff"):
            whatif.evaluate(db, row, whatif.WhatIf(prices={"oa_solar_intra": D("2.9")}))
        smaller = whatif.evaluate(db, row, whatif.WhatIf(capacities_mw={"oa_solar_intra": D(3)}))
    roof_kw = D(z["rooftop_pv"]["kw"]["value"])
    assert D(smaller["annual"]["capex"]["value"]) == roof_kw * 40000 + D(3000) * 42000
    # invariant 5: the treatment is pinned and the run replays to the paisa
    assert run["pins"]["capital"] == {"treatment": 2}
    rp = client.post(f"/runs/{run['id']}/replay", params={"wait": "true"}, headers=AN).json()
    assert rp["status"] == "done" and rp["identical_to_original"] is True
    assert rp["summary"] == run["summary"] and rp["pins"]["capital"] == {"treatment": 2}
    # a run made before the treatment has no such pin, and replays the way it was solved: the plant
    # at the PPA tariff with no capital, the summary in its old shape
    with session_scope(actor="test@ergplan") as db:
        row = db.get(Run, run["id"])
        assert row is not None
        row.pins = {k: v for k, v in row.pins.items() if k != "capital"}
    old = client.post(f"/runs/{run['id']}/replay", params={"wait": "true"}, headers=AN).json()
    assert old["status"] == "done" and "capital" not in old["pins"]
    so, lo = old["summary"], old["analyst"]["lines_after"]
    assert so["owned_capital"] is None and "fixed:oa_solar_intra" not in lo
    zo = {x["kind"]: x for x in so["sizes"]}
    [tariff] = _line(zo["oa_solar_intra"], "PPA tariff")
    assert D(tariff["value"]) == D("2.5")
    assert so["annual"]["capex"]["label"] == "Capital cost (rooftop and batteries)"
    assert D(so["annual"]["capex"]["value"]) == D(zo["rooftop_pv"]["kw"]["value"]) * 40000
    assert so["payback_years"]["label"] == "Simple payback"
    assert so["lifecycle"]["years"][0]["owned_saving"] is None
    again = client.post(f"/runs/{old['id']}/replay", params={"wait": "true"}, headers=AN).json()
    assert again["identical_to_original"] is True and again["summary"] == old["summary"]


def test_a_plant_with_26_percent_equity_carries_the_outlay_once(site: dict[str, str]) -> None:  # noqa: F811
    run = _run(site, "equity", [{"kind": "oa_solar_intra", "ownership": "group_captive", "max_mw": "6"}])
    s, lines = run["summary"], run["analyst"]["lines_after"]
    [z] = s["sizes"]
    kw, delivered = D(z["kw"]["value"]), D(z["delivered_kwh"]["value"])
    [eq] = s["owned_capital"]["assets"]
    # the outlay: equity share × the plant's capital cost per kW × the capacity taken
    assert eq["kind"] == "equity" and D(eq["capex"]["value"]) == (kw * 42000 * D("0.26")).quantize(D("0.01"))
    assert _kinds(eq["capex"]) == {"assumption"}
    assert "debt and equity split is not modelled" in eq["words"]
    assert D(s["annual"]["capex"]["value"]) == D(eq["capex"]["value"]) == D(s["lifecycle"]["capex"]["value"])
    # in the year's cost as its carrying cost (outlay × CRF), and in the cash flow as the outlay at
    # year 0: the lifecycle has no O&M and no yearly equity line
    assert abs(D(lines["fixed:oa_solar_intra"]) - kw * 42000 * D("0.26") * CRF) < D("0.01")
    assert all(D(y["om"]["value"]) == 0 for y in s["lifecycle"]["years"])
    # the tariff is paid per kWh as before; the equity is not charged again per kWh
    [tariff] = _line(z, "PPA tariff")
    assert D(tariff["value"]) == D("3.10") and not _line(z, "Equity cost")
    landed = sum((D(ln["value"]) for ln in next(iter(z["waterfall"].values()))), D(0))
    assert abs(D(lines["energy:oa_solar_intra"]) - delivered * landed) < 5
    y1 = s["lifecycle"]["years"][0]
    assert D(y1["contracted"]["value"]) == D(lines["energy:oa_solar_intra"])
    assert D(s["payback_years"]["value"]) > 0 and s["owned_capital"]["payback_absent"] is None
    assert any("The equity is not charged again per kWh" in n for n in s["notes"])
    assert run["pins"]["capital"] == {"treatment": 2}
    rp = client.post(f"/runs/{run['id']}/replay", params={"wait": "true"}, headers=AN).json()
    assert rp["identical_to_original"] is True and rp["summary"] == run["summary"]
    # the client's own share and capital cost are theirs, shown as stated
    mine = _run(
        site,
        "equity stated",
        [
            {
                "kind": "oa_solar_intra",
                "ownership": "group_captive",
                "max_mw": "6",
                "fix_at_max": True,
                "equity_share_pct": "10",
                "capex_inr_per_kw": "45000",
            }
        ],
    )
    [eq2] = mine["summary"]["owned_capital"]["assets"]
    assert D(eq2["capex"]["value"]) == D(6000) * 45000 * D("0.10")
    assert _kinds(eq2["capex"]) == {"client_stated"}  # their capital cost and their share


def test_third_party_carries_no_capital_and_capital_beside_it_is_not_credited_with_its_saving(
    site: dict[str, str],  # noqa: F811
) -> None:
    ppa = _run(site, "third party", [{"kind": "oa_solar_intra", "ownership": "third_party", "max_mw": "6"}])
    s = ppa["summary"]
    assert D(s["annual"]["saving"]["value"]) > 0
    oc = s["owned_capital"]
    assert oc["assets"] == [] and D(oc["total"]["value"]) == 0 and D(s["annual"]["capex"]["value"]) == 0
    assert not any(k.startswith("fixed:") for k in ppa["analyst"]["lines_after"])
    # absent, with the reason in words; never a zero
    assert s["payback_years"] is None and s["irr_pct"] is None
    assert oc["payback_absent"] == oc["irr_absent"] and oc["payback_absent"].startswith("No payback or IRR")
    assert "no capital of your own" in oc["payback_absent"] and "Open-access solar" in oc["payback_absent"]
    # a rooftop beside the third-party plant: payback is on the rooftop's own saving
    both = _run(
        site,
        "rooftop and third party",
        [
            {"kind": "rooftop_pv", "max_mw": "1", "fix_at_max": True},
            {"kind": "oa_solar_intra", "ownership": "third_party", "max_mw": "6"},
        ],
    )
    sb = both["summary"]
    ob = sb["owned_capital"]
    assert [a["id"] for a in ob["assets"]] == ["rooftop_pv"] and D(ob["total"]["value"]) == 40_000_000
    assert ob["bought"] == ["Open-access solar, intra-state"]
    y1 = sb["lifecycle"]["years"][0]
    bought, owned, plan = D(ob["bought_saving"]["value"]), D(ob["saving"]["value"]), D(y1["saving"]["value"])
    assert bought > 0 and owned > 0 and abs(owned + bought - plan) <= D("0.02")
    assert D(y1["owned_saving"]["value"]) == owned
    assert "less the saving of the sources bought by the unit" in ob["basis"]
    # ₹4 crore of rooftop against the whole plan's saving read as under two years (the finding);
    # against its own saving it is what a rooftop is
    whole_plan_years = D(ob["total"]["value"]) / plan
    assert whole_plan_years < 2 < D(sb["payback_years"]["value"])
    assert abs(D(sb["payback_years"]["value"]) - D(ob["total"]["value"]) / owned) < 1


def test_the_landed_cost_card_of_a_captive_plant_is_its_levelised_cost(site: dict[str, str]) -> None:  # noqa: F811
    def landed(sources: dict[str, Any]) -> dict[str, Any]:
        r = client.get(
            f"/connections/{site['conn']}/landed-cost",
            headers=AN,
            params={"scenario": json.dumps({"as_of": AS_OF, "sources": sources})},
        )
        assert r.status_code == 200, r.text
        return {x["id"]: x for x in r.json()["sources"]}

    at_up = lcoe.GenerationCost(
        SOLAR.capex_inr_per_kw,
        SOLAR.om_pct_of_capex,
        SOLAR.cuf_pct.model_copy(update={"value": UP_SOLAR_CUF}),
        SOLAR.degradation_pct,
        SOLAR.life_years,
        SOLAR.discount_pct,
    )
    own = landed(
        {"oa_solar_intra": {"ownership": "captive"}, "oa_wind_intra": {"ownership": "group_captive"}}
    )
    card = own["oa_solar_intra"]
    first = card["by_slot"][0]["lines"][0]
    assert first["label"] == OWN_LCOE and _kinds(first) == {"assumption"}
    assert abs(D(first["value"]) - lcoe.lcoe(at_up).value) < D("0.0001")  # ₹3.18, not the ₹3.10 PPA
    assert not any(ln["label"] == "PPA tariff" for ln in card["by_slot"][0]["lines"])
    by = {x["label"]: x for x in card["inputs"]}
    assert set(by) == {"Capital cost per kW", "O&M", "CUF", "Degradation", "Life", "Discount rate"}
    assert all(_kinds(x) == {"assumption"} for x in by.values())  # each one a chip, none a sourced fact
    assert D(by["Capital cost per kW"]["value"]) == 42000 and D(by["CUF"]["value"]) == UP_SOLAR_CUF
    assert any("no tariff is paid" in n for n in card["notes"])
    # 26% equity: the tariff, and the outlay carried per kWh at the meter, from the same inputs
    wind = own["oa_wind_intra"]
    w_lines = {ln["label"]: ln for ln in wind["by_slot"][0]["lines"]}
    assert D(w_lines["PPA tariff"]["value"]) == D("3.60")
    eq = w_lines[EQUITY_LABEL]
    g = lcoe.default_utility("wind")
    g30 = lcoe.GenerationCost(
        g.capex_inr_per_kw,
        g.om_pct_of_capex,
        g.cuf_pct.model_copy(update={"value": D(30)}),  # packages/profiles/cuf/wind_up.yaml
        g.degradation_pct,
        g.life_years,
        g.discount_pct,
    )
    at_bus = lcoe.equity_cost_per_kwh(g30, lcoe.default_equity_share()).value
    kept = (1 - D("0.0318")) * (1 - D("0.03"))  # the approved transmission and wheeling losses at 33 kV
    assert abs(D(eq["value"]) - at_bus / kept) < D("0.0001")
    assert "assumption" in _kinds(eq) and {x["label"] for x in wind["inputs"]} >= {"Equity share", "Life"}
    assert any("does not charge it again per kWh" in n for n in wind["notes"])
    # third party: the PPA tariff alone, no capital inputs
    third = landed({"oa_solar_intra": {"ownership": "third_party"}})["oa_solar_intra"]
    assert third["by_slot"][0]["lines"][0]["label"] == "PPA tariff" and third["inputs"] == []
    # the client's own capital cost is theirs
    mine = landed({"oa_solar_intra": {"ownership": "captive", "capex_inr_per_kw": "38000"}})["oa_solar_intra"]
    cap = next(x for x in mine["inputs"] if x["label"] == "Capital cost per kW")
    assert _kinds(cap) == {"client_stated"} and D(cap["value"]) == 38000
    assert D(mine["by_slot"][0]["lines"][0]["value"]) < D(first["value"])
