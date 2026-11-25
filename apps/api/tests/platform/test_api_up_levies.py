"""Findings 3, 4 and 8 of the Uttar Pradesh full test (docs/stage-reports/up-full-test-2026-11-25.md),
on the same twelve hand-worked PVVNL HV-2 bills (FPPCA ₹0.18/kVAh and duty ₹0.50/kVAh printed on
each): a bill dated before the only approved order is not replayed and not "explained"; today's cost
carries FPPCA and duty at the bills' own rates and reconciles with the bills; the treatment on units
not bought from the DISCOM is stated; a run pins what it read from the bills and replays to the paisa."""

from __future__ import annotations

import copy
import json
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ergplan.db.base import session_scope
from ergplan.db.models import Bill
from ergplan.engine import lcoe
from ergplan.intake import bills as bill_store
from ergplan.platform.app import app
from tests.e2e.test_up_assumed import DUTY, FPPCA, assumed_bill, hand_bill
from tests.e2e.test_up_assumed import MONTHS as UP_MONTHS
from tests.engine.helpers import restore_committed
from tests.intake.helpers import FakeBillx

client = TestClient(app)
AN = {"X-Ergplan-Actor": "analyst@joulewise.in", "X-Ergplan-Role": "analyst"}
# eight supply months before the FY 2026-27 order took effect (2 July 2026) and four under it
BEFORE = {"2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"}
UNDER = {"2026-07", "2026-08", "2026-09", "2026-10"}
AS_OF = "2026-11-01"
ASSUMED_DUTY = "Electricity duty on units not bought from the DISCOM (assumed; not sourced to a regulation)"


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    restore_committed()
    fake = FakeBillx()
    monkeypatch.setattr(bill_store, "BillxClient", fake.client)
    ids = []
    for first, factor in UP_MONTHS:
        tag = first.strftime("%Y%m")
        data = fake.register(tag, assumed_bill(first, factor))
        r = client.post("/bills", files={"file": (f"{tag}.pdf", data, "application/pdf")})
        assert r.status_code in (200, 201), r.text
        [bill] = r.json()
        for path in list(bill["outstanding"]):
            value = next(x["value"] for x in bill["fields"] if x["path"] == path)
            bill = client.post(f"/bills/{bill['id']}/confirm", json={"path": path, "value": value}).json()
        ids.append(bill["id"])
    si = client.post("/intake/from-bills", json={"bill_ids": ids, "overrides": {"voltage_kv": "33"}})
    assert si.status_code == 200, si.text
    out = client.post("/sites", json={"intake": si.json()})
    assert out.status_code == 201, out.text
    return {"site": out.json()["site_id"], "conn": out.json()["connection_id"]}


def _kinds(sv: dict[str, Any]) -> set[str]:
    return {r["kind"] for r in sv["source_refs"]}


# ---------------------------------------------------------------- finding 4: no tariff in force


def test_bills_before_the_only_approved_order_are_not_replayed_and_not_explained(
    site: dict[str, str],
) -> None:
    r0 = client.get(f"/connections/{site['conn']}/replay", headers=AN)
    assert r0.status_code == 200, r0.text
    by = {x["evaluated_on"][:7]: x for x in r0.json()}
    assert set(by) == BEFORE | UNDER
    for month in sorted(BEFORE):
        r = by[month]
        assert r["status"] == "no_tariff"
        assert r["computed_total"] is None and r["total_difference"] is None and r["schedule"] is None
        assert r["tariff_lines"] == []
        assert r["lines"] and all(ln["status"] == "no_tariff" for ln in r["lines"])
        assert all(ln["computed"] is None and ln["difference"] is None for ln in r["lines"])
        assert all(ln["billed"] is not None for ln in r["lines"])
        [why] = r["notes"]
        assert why.startswith("No tariff in force for this date")
        assert "2 Jul 2026" in why  # the earliest order in the rulebook
        assert "tariff_period" in r["gaps"]
    assert "1 Jun 2026 to 30 Jun 2026" in by["2026-06"]["notes"][0]
    for month in sorted(UNDER):
        r = by[month]
        assert r["status"] in ("match", "explained") and r["computed_total"] is not None
        assert all(ln["status"] != "no_tariff" for ln in r["lines"])
    # no tally of matched-or-explained bills may count the eight that were never rebuilt
    assert sum(1 for r in by.values() if r["status"] in ("match", "explained")) == 4


# ---------------------------------------------------------------- finding 3: FPPCA and duty


def test_todays_cost_carries_fppca_and_duty_at_the_bills_rates_and_reconciles(
    site: dict[str, str],
) -> None:
    t = client.get(f"/connections/{site['conn']}/today", headers=AN, params={"as_of": AS_OF}).json()
    hand = [hand_bill(m, f) for m, f in UP_MONTHS]
    kvah = sum((h["kvah"] for h in hand), D(0))
    billed = sum((h["total"] for h in hand), D(0))
    # each levy is the bills' own: cited to the bill lines, never to an order or a regulation
    for key, rate in (("fppca", FPPCA), ("electricity_duty", DUTY)):
        sv = t[key]
        assert sv is not None, key
        assert "bill" in _kinds(sv) and not _kinds(sv) & {"order", "regulation", "assumption"}
        paths = {r["field_path"] for r in sv["source_refs"] if r["kind"] == "bill"}
        assert any(p.startswith("elements.") and p.endswith(".amount") for p in paths)
        assert abs(D(sv["value"]) - rate * kvah) / (rate * kvah) < D("0.001")  # the year at the bill's rate
    parts = sum((D(t[k]["value"]) for k in ("energy", "demand", "fppca", "electricity_duty")), D(0))
    assert abs(parts - D(t["annual"]["value"])) <= 2
    # today's cost reconciles with the twelve bills (it was 8.4% short without the two levies)
    assert abs(D(t["annual"]["value"]) - billed) / billed < D("0.005")
    assert "your own bills" in t["note"] and "not sourced to a regulation" in t["note"]
    assert not any("FPPCA" in g or "Electricity duty" in g for g in t["gaps"])


def test_avoided_cost_cards_name_the_levies_as_the_bills_own(site: dict[str, str]) -> None:
    a = client.get(f"/connections/{site['conn']}/avoided-cost", headers=AN, params={"as_of": AS_OF}).json()
    by = {c["label"]: c for c in a["cards"][0]["components"]}
    fp = next(v for k, v in by.items() if k.startswith("Fuel and power purchase adjustment"))
    ed = next(v for k, v in by.items() if k.startswith("Electricity duty"))
    assert D(fp["value"]) == FPPCA and fp["unit"] == "INR/kVAh" and _kinds(fp) == {"bill"}
    assert D(ed["value"]) == DUTY and ed["unit"] == "INR/kVAh" and _kinds(ed) == {"bill"}
    assert "12 bills" in fp["label"]
    assert "fppca_monthly" not in a["gaps"] and "electricity_duty" not in a["gaps"]
    assert sum("not at a rate sourced to a regulation" in n for n in a["notes"]) == 2
    labels = [x["label"] for x in a["annual"]]
    assert any(x.startswith("of which FPPCA") for x in labels)
    assert any(x.startswith("of which electricity duty") for x in labels)


def _landed(site: dict[str, str]) -> dict[str, Any]:
    r = client.get(
        f"/connections/{site['conn']}/landed-cost",
        headers=AN,
        params={"scenario": json.dumps({"as_of": AS_OF})},
    )
    assert r.status_code == 200, r.text
    return {x["id"]: x for x in r.json()["sources"]} | {"_avoided": r.json()["avoided"]}


def test_landed_cost_states_the_treatment_on_units_not_bought_from_the_discom(
    site: dict[str, str],
) -> None:
    src = _landed(site)
    for sid in ("oa_solar_intra", "oa_wind_intra", "rooftop_pv"):
        slot = src[sid]["by_slot"][0]
        [duty] = [ln for ln in slot["lines"] if ln["label"] == ASSUMED_DUTY]
        # the bill's ₹0.50/kVAh on a kWh: what a kWh bought from the DISCOM pays
        assert D("0.52") < D(duty["value"]) < D("0.53")
        assert {"assumption", "bill"} <= _kinds(duty)  # never shown as a sourced fact
        assert not _kinds(duty) & {"order", "regulation"}
        assert not any("FPPCA" in str(ln["label"]) for ln in slot["lines"])  # not charged, and said so
        notes = " ".join(src[sid]["notes"])
        assert "FPPCA" in notes and "assumed not charged" in notes
        assert "Not sourced to a regulation" in notes
        assert "electricity duty on these units is an assumption" in notes
    # DISCOM supply carries both at the bills' rates, and says where they come from
    assert any("at the rates on your own bills" in n for n in src["discom_grid"]["notes"])
    rates = [D(x["rate"]["value"]) for x in src["_avoided"]]
    assert min(rates) > D("6.7")  # (6.80 − 15% + 0.18 + 0.50) per kVAh × 1.0438 kVAh a kWh
    # a battery charged from the grid pays the levies inside the grid rate: no separate duty line
    assert not any(ln["label"] == ASSUMED_DUTY for ln in src["btm_bess"]["by_slot"][0]["lines"])


# ---------------------------------------------------------------- finding 8: battery at the plant


def test_battery_at_the_plant_card_is_charged_from_the_plant_not_the_grid(site: dict[str, str]) -> None:
    src = _landed(site)
    # the plant's landed cost as the optimizer prices it: every line but the banking upper bound,
    # which the settlement decides
    plant = sum(
        (
            D(ln["value"])
            for ln in src["oa_solar_intra"]["by_slot"][0]["lines"]
            if not str(ln["label"]).startswith("Banking charge, upper bound")
        ),
        D(0),
    )
    card = src["oa_bess"]
    assert card["available"] and len(card["by_slot"]) == len(src["_avoided"])  # it delivers in any slot
    for slot in card["by_slot"]:
        labels = [str(ln["label"]) for ln in slot["lines"]]
        assert labels[0] == "LCOS" and len(labels) == 2
        assert labels[1].startswith("Energy stored from the plant (Open-access solar, intra-state)")
        assert not any("Energy charged in the" in x for x in labels)  # the grid-charged battery's line
        stored = slot["lines"][1]
        # the plant's landed cost at the meter (losses and open-access charges in) over the round trip
        assert abs(D(stored["value"]) - plant / (lcoe.default_bess().round_trip_pct.value / 100)) < D(
            "0.0001"
        )
        assert {"order", "assumption"} <= _kinds(stored)  # the plant's rule pages and the efficiency
        assert all(ln["source_refs"] for ln in slot["lines"])
        assert D(slot["total"]["value"]) == sum((D(ln["value"]) for ln in slot["lines"]), D(0))
    notes = " ".join(card["notes"])
    assert "charged from the open-access plant's own generation, not from the grid" in notes
    assert "comes from its own sizing" in notes
    assert card["rules_used"] == src["oa_solar_intra"]["rules_used"]
    # the behind-the-meter battery keeps its own card: charged from the grid in the cheapest slot
    btm = [str(ln["label"]) for ln in src["btm_bess"]["by_slot"][0]["lines"]]
    assert any(x.startswith("Energy charged in the") for x in btm)


# ---------------------------------------------------------------- finding 3: the plan and its replay


def test_a_run_charges_the_levies_alike_on_both_sides_pins_them_and_replays(site: dict[str, str]) -> None:
    spec = {
        "candidates": [
            {"kind": "rooftop_pv", "max_mw": "1"},
            {"kind": "oa_solar_intra", "ownership": "captive", "max_mw": "6"},
        ],
        "as_of": AS_OF,
        "resolution_min": 60,
    }
    sc = client.post(
        f"/sites/{site['site']}/scenarios",
        json={"connection_id": site["conn"], "name": "levies", "spec": spec},
        headers=AN,
    )
    assert sc.status_code == 201, sc.text
    run = client.post(f"/scenarios/{sc.json()['id']}/run", params={"wait": "true"}, headers=AN).json()
    assert run["status"] == "done", run["error"]
    assert run["analyst"]["resettle"]["agrees"] is True  # the judge and the solver still agree
    s = run["summary"]
    lv = {x["code"]: x for x in s["levies"]}
    assert set(lv) == {"fppca", "electricity_duty"}
    grid = D(s["settlement"]["grid_kwh"]["value"])
    load = D(s["levies"][0]["discom_kwh_before"]["value"])
    sizes = {x["kind"]: x for x in s["sizes"]}
    other = sum((D(sizes[k]["delivered_kwh"]["value"]) for k in sizes), D(0))
    assert grid < load and other > 0 and D(s["levies"][0]["discom_kwh_after"]["value"]) == grid
    for code, x in lv.items():
        per_kwh = D(x["on_discom_kwh"]["value"])
        assert x["sourced_to_regulation"] is False and x["bills"] == 12
        assert "bill" in _kinds(x["rates"][0]) and not _kinds(x["rates"][0]) & {"order", "regulation"}
        assert _kinds(x["treatment"]) == {"assumption"}
        assert "Not sourced to a regulation" in x["treatment_words"]
        # today on every unit, with the plan on the units still bought from the DISCOM, same rate
        assert abs(D(x["before"]["value"]) - load * per_kwh) < 1
        assert abs(D(x["after_discom"]["value"]) - grid * per_kwh) < 1
        assert code in ("fppca", "electricity_duty")
    fp, ed = lv["fppca"], lv["electricity_duty"]
    assert D(fp["rates"][0]["value"]) == FPPCA and D(ed["rates"][0]["value"]) == DUTY
    # FPPCA: not charged on rooftop and open-access units, stated and never silently zero
    assert fp["charged_on_other_units"] is False and fp["on_other_kwh"] is None
    assert D(fp["after_other"]["value"]) == 0 and "assumed not charged" in fp["treatment_words"]
    assert D(fp["other_kwh"]["value"]) == 0 and D(ed["other_kwh"]["value"]) == other
    assert D(fp["treatment"]["value"]) == 0
    # duty: charged on them at the bill's rate (the cautious side for the saving)
    assert ed["charged_on_other_units"] is True and D(ed["treatment"]["value"]) == 100
    assert D(ed["on_other_kwh"]["value"]) == D(ed["on_discom_kwh"]["value"])
    assert "assumption" in _kinds(ed["on_other_kwh"])
    assert abs(D(ed["after_other"]["value"]) - other * D(ed["on_other_kwh"]["value"])) < 1
    # the levies are inside the two totals: today's cost is the bills' (within 0.5%)
    billed = sum((hand_bill(m, f)["total"] for m, f in UP_MONTHS), D(0))
    assert abs(D(s["annual"]["before"]["value"]) - billed) / billed < D("0.005")
    assert D(run["analyst"]["lines_after"]["energy:rooftop_pv"]) > 0  # the rooftop's assumed duty
    assert sum("not in the approved tariff" in n for n in s["notes"]) == 2
    # each source's chain shows the assumed duty as its own link
    oa = sizes["oa_solar_intra"]
    assert any(ln["label"] == ASSUMED_DUTY for ln in next(iter(oa["waterfall"].values())))
    # invariant 5: the run pins what it read from the bills, and a replay uses the pins — also
    # after a bill changes (here: a bill's FPPCA line is rewritten after the run)
    pins = run["pins"]["bills"]
    assert [x["code"] for x in pins["levies"]] == ["fppca", "electricity_duty"]
    assert len(pins["factors"]) == 2
    with session_scope(actor="test@ergplan") as db:
        b = db.query(Bill).filter(Bill.connection_id == site["conn"]).order_by(Bill.id).first()
        assert b is not None
        res = copy.deepcopy(b.billx_result)
        el = next(e for e in res["bills"][0]["elements"] if e.get("code") == "fppca")
        el["amount"]["value"] = float(el["amount"]["value"]) * 3
        b.billx_result = res
    try:
        t = client.get(f"/connections/{site['conn']}/today", headers=AN, params={"as_of": AS_OF}).json()
        assert D(t["fppca"]["value"]) > D(fp["before"]["value"]) * D("1.05")  # the bills now say more
        rp = client.post(f"/runs/{run['id']}/replay", params={"wait": "true"}, headers=AN).json()
        assert rp["status"] == "done" and rp["identical_to_original"] is True
        assert rp["summary"] == run["summary"]
    finally:
        with session_scope(actor="test@ergplan") as db:
            b = db.query(Bill).filter(Bill.connection_id == site["conn"]).order_by(Bill.id).first()
            assert b is not None
            res = copy.deepcopy(b.billx_result)
            el = next(e for e in res["bills"][0]["elements"] if e.get("code") == "fppca")
            el["amount"]["value"] = float(el["amount"]["value"]) / 3
            b.billx_result = res
