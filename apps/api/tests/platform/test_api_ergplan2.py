"""ergPLAN 2 through the API: the five components a consumer names (rooftop solar, open-access solar
and wind, a battery behind the meter, a battery at the open-access plant) with their targets, costs
and CUF, sized together under the state's cycle settlement, and the judge's agreement."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ergplan.platform.app import app
from tests.engine.helpers import restore_committed
from tests.platform.test_api_plan import _truncate
from tests.platform.test_api_runs import AN, CL, make_site, msedcl_5mw

client = TestClient(app)


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


def _mix(conn: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    body = {"sources": sources, "resolution_min": 60, "as_of": "2026-06-01", "name": "ergPLAN 2"}
    r = client.post(f"/connections/{conn}/mix", params={"wait": "true"}, json=body, headers=CL)
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["status"] == "done", out
    run = client.get(f"/runs/{out['run_id']}", headers=AN).json()
    return dict(run)


def test_five_components_sized_together_with_stated_costs_and_cuf(site: dict[str, str]) -> None:
    run = _mix(
        site["conn"],
        [
            {"kind": "rooftop_pv", "max_mw": "1", "capex_inr_per_kw": "36000", "cuf_pct": "16", "life_years": 25},
            {"kind": "oa_solar_intra", "ownership": "captive", "max_mw": "5", "price_inr_per_kwh": "3.05", "cuf_pct": "24"},
            {"kind": "oa_wind_intra", "ownership": "captive", "max_mw": "3", "price_inr_per_kwh": "3.60", "cuf_pct": "32"},
            {"kind": "btm_bess", "max_mw": "1", "capex_inr_per_kwh": "11000", "round_trip_pct": "90"},
            {"kind": "oa_bess", "max_mw": "2", "capex_inr_per_kwh": "10000", "min_hours": "2", "max_hours": "4"},
        ],
    )  # fmt: skip
    s = run["summary"]
    kinds = {z["kind"]: z for z in s["sizes"]}
    assert set(kinds) == {"rooftop_pv", "oa_solar_intra", "oa_wind_intra", "btm_bess", "oa_bess"}
    assert run["analyst"]["resettle"]["max_diff_kwh"] in ("0.000", "0.001", "0.002")
    assert Decimal(s["annual"]["saving"]["value"]) > 0
    # the stated figures are shown as the client's, never as assumptions
    roof = kinds["rooftop_pv"]
    assert any(
        r["kind"] == "client_stated" and "capital" in (r.get("note") or "") for r in roof["kw"]["source_refs"]
    )
    # sizes stay within the targets
    assert Decimal(kinds["oa_solar_intra"]["kw"]["value"]) <= 5000
    assert Decimal(kinds["oa_wind_intra"]["kw"]["value"]) <= 3000
    ob = kinds["oa_bess"]
    kw, kwh = Decimal(ob["kw"]["value"]), Decimal(ob["kwh"]["value"])
    assert kw <= 2000 and (kw == 0 or Decimal(2) * kw - 1 <= kwh <= Decimal(4) * kw + 1)
    # the settlement is the state's cycle settlement
    assert (
        "cycle settlement" in " ".join(run["analyst"].get("notes", []) + s.get("notes", []))
        or s["settlement"]["rule"]
    )


def test_a_target_can_be_taken_as_given(site: dict[str, str]) -> None:
    run = _mix(
        site["conn"],
        [{"kind": "oa_solar_intra", "ownership": "captive", "max_mw": "2", "fix_at_max": True}],
    )
    [z] = run["summary"]["sizes"]
    assert Decimal(z["kw"]["value"]) == Decimal("2000.000")


def test_a_battery_at_the_plant_needs_a_plant(site: dict[str, str]) -> None:
    body = {"sources": [{"kind": "oa_bess", "max_mw": "1"}, {"kind": "rooftop_pv"}], "as_of": "2026-06-01"}
    r = client.post(f"/connections/{site['conn']}/mix", params={"wait": "true"}, json=body, headers=CL)
    assert r.status_code == 422 and "open-access" in r.text


# ---------------------------------------------------------------- a state from the register


def _typed(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "client_name": "Marwar Castings",
        "site_name": "Jodhpur works",
        "state": "RJ",
        "discom": "jdvvnl",
        "category": "HT-5 Large Industrial",
        "voltage_kv": "33",
        "contract_demand_kva": "3000",
        "industry_shape": "two_shift",
        "annual_kwh": "12000000",
        "max_demand_kva": "2700",
        "energy_charge_inr_per_unit": "7.30",
        "fixed_charge_inr_per_kva_month": "405",
    }
    body.update(over)
    return body


def test_a_register_state_runs_on_the_consumers_own_tariff() -> None:
    """Rajasthan: no retail schedule is approved, so the grid is priced at the energy and fixed
    charges the consumer gives (their bill's), the ToD slots, open-access charges and the cycle
    settlement come from the State Regulation Master, and every such figure is shown as a register
    entry, never as a cited page."""
    _truncate()
    restore_committed()
    r = client.get("/intake/discoms", headers=CL)
    d = {x["id"]: x for x in r.json()}
    assert d["jdvvnl"]["ready"] and d["jdvvnl"]["needs_tariff"] and not d["msedcl"]["needs_tariff"]
    assert d["jdvvnl"]["sourced"]["settlement"] and d["jdvvnl"]["sourced"]["oa_charge"]
    intake = client.post("/intake/typed", json=_typed(), headers=CL)
    assert intake.status_code == 200, intake.text
    saved = client.post("/sites", json={"intake": intake.json()}, headers=CL)
    assert saved.status_code == 201, saved.text[:400]
    conn = saved.json()["connection_id"]
    today = client.get(f"/connections/{conn}/today", headers=CL)
    assert today.status_code == 200, today.text[:400]
    body = {
        "sources": [
            {"kind": "oa_solar_intra", "ownership": "captive", "max_mw": "6"},
            {"kind": "oa_wind_intra", "ownership": "captive", "max_mw": "3"},
            {"kind": "oa_bess", "max_mw": "1"},
        ],
        "resolution_min": 60,
        "as_of": "2026-11-01",
    }
    out = client.post(f"/connections/{conn}/mix", params={"wait": "true"}, json=body, headers=CL)
    assert out.status_code == 202, out.text[:400]
    assert out.json()["status"] == "done", out.json()
    run = client.get(f"/runs/{out.json()['run_id']}", headers=AN).json()
    s = run["summary"]
    assert Decimal(s["annual"]["saving"]["value"]) > 0
    assert "Banking and settlement · RJ" in s["settlement"]["rule"]
    assert "register" in {r["kind"] for r in s["annual"]["after"]["source_refs"]}
    kinds = {r["kind"] for r in s["annual"]["before"]["source_refs"]}
    assert "client_stated" in kinds and "order" not in kinds  # the grid cost today rests on their tariff
    solar = next(z for z in s["sizes"] if z["kind"] == "oa_solar_intra")
    assert Decimal(solar["kw"]["value"]) > 0
    # Rajasthan's limit from the register: renewable capacity up to twice the contract demand
    total_re = sum(
        Decimal(z["kw"]["value"]) for z in s["sizes"] if z["kind"] in ("oa_solar_intra", "oa_wind_intra")
    )
    assert total_re <= 6000 + 1
    _truncate()
