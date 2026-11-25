"""Exploratory full run for Uttar Pradesh on twelve assumed bills (PVVNL HV-2, 5,000 kVA, 33 kV):
every step of the product through the API, each result written to UP_OUT for checking by hand."""

from __future__ import annotations

import copy
import json
import os
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ergplan.intake import bills as bill_store
from ergplan.platform.app import app
from tests.engine.helpers import restore_committed
from tests.intake.helpers import FakeBillx, result

OUT = os.environ.get("UP_OUT")
pytestmark = [pytest.mark.slow, pytest.mark.skipif(not OUT, reason="exploratory: set UP_OUT")]
client = TestClient(app)
AN = {"X-Ergplan-Actor": "analyst@joulewise.in", "X-Ergplan-Role": "analyst"}

# consumption month (first day), energy against the fixture's August
MONTHS = [
    (date(2025, 11, 1), "0.88"), (date(2025, 12, 1), "0.84"), (date(2026, 1, 1), "0.82"),
    (date(2026, 2, 1), "0.85"), (date(2026, 3, 1), "0.93"), (date(2026, 4, 1), "1.00"),
    (date(2026, 5, 1), "1.08"), (date(2026, 6, 1), "1.10"), (date(2026, 7, 1), "1.04"),
    (date(2026, 8, 1), "1.00"), (date(2026, 9, 1), "0.97"), (date(2026, 10, 1), "0.95"),
]  # fmt: skip
# the order's HV-2 ToD windows with an assumed two-shift split of the month's energy, and the
# rebate (-) or surcharge (+) the order puts on each
SUMMER = [
    ("05:00-10:00", "0.22", -1),
    ("10:00-19:00", "0.42", 0),
    ("19:00-03:00", "0.30", 1),
    ("03:00-05:00", "0.06", 0),
]
WINTER = [
    ("05:00-11:00", "0.26", 0),
    ("11:00-17:00", "0.30", 0),
    ("17:00-23:00", "0.26", 1),
    ("23:00-05:00", "0.18", -1),
]
RATE, DEMAND, FPPCA, DUTY = D("6.80"), D("290"), D("0.18"), D("0.50")  # HV-2 urban, above 11 kV up to 66 kV
BASE_KWH, BASE_KVAH, CD, MD = D("1972620"), D("2059020"), D("5000"), D("3250")
Q = D("0.01")


def hand_bill(first: date, factor: str) -> dict[str, D]:
    """The month's bill worked by hand from the FY 2026-27 order, independent of the app."""
    f = D(factor)
    kwh, kvah = (BASE_KWH * f).quantize(D("1")), (BASE_KVAH * f).quantize(D("1"))
    slots = SUMMER if 4 <= first.month <= 9 else WINTER
    billable = max(MD, CD * D("0.75"))
    out = {
        "kwh": kwh,
        "kvah": kvah,
        "demand_charge": (DEMAND * billable).quantize(Q),
        "energy_charge": (RATE * kvah).quantize(Q),
        "tod_surcharge": sum(
            ((RATE * D("0.15") * kvah * D(sh)).quantize(Q) for _, sh, sign in slots if sign > 0), D(0)
        ),
        "tod_rebate": -sum(
            ((RATE * D("0.15") * kvah * D(sh)).quantize(Q) for _, sh, sign in slots if sign < 0), D(0)
        ),
        "fppca": (FPPCA * kvah).quantize(Q),
        "electricity_duty": (DUTY * kvah).quantize(Q),
    }
    out["total"] = sum(
        (
            out[k]
            for k in (
                "demand_charge",
                "energy_charge",
                "tod_surcharge",
                "tod_rebate",
                "fppca",
                "electricity_duty",
            )
        ),
        D(0),
    )
    return out


def assumed_bill(first: date, factor: str) -> dict[str, Any]:
    """billx's reading of that hand-worked bill, in the shape of the synthetic PVVNL fixture."""
    res = copy.deepcopy(result("pvvnl_passed"))
    h = hand_bill(first, factor)
    slots = SUMMER if 4 <= first.month <= 9 else WINTER
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    nxt = last + timedelta(days=3)
    b = res["bills"][0]
    fl = b["fields"]
    p = fl["period"]
    p["from"]["value"], p["to"]["value"] = first.isoformat(), last.isoformat()
    p["bill_date"]["value"], p["bill_month"]["value"] = nxt.isoformat(), nxt.strftime("%Y-%m")
    p["due_date"]["value"] = (nxt + timedelta(days=15)).isoformat()
    fl["energy"]["kwh"]["value"], fl["energy"]["kvah"]["value"] = float(h["kwh"]), float(h["kvah"])
    proto = fl["energy"]["tod"][0]
    fl["energy"]["tod"] = []
    for name, share, _ in slots:
        row = copy.deepcopy(proto)
        row["slot"] = name
        row["kwh"]["value"] = float((h["kwh"] * D(share)).quantize(D("1")))
        fl["energy"]["tod"].append(row)
    codes = ["demand_charge", "energy_charge", "tod_surcharge", "tod_rebate", "fppca", "electricity_duty"]
    fl["charges"]["energy"]["value"], fl["charges"]["demand"]["value"] = (
        float(h["energy_charge"]),
        float(h["demand_charge"]),
    )
    qty = {
        "demand_charge": (D("3750"), DEMAND),
        "energy_charge": (h["kvah"], RATE),
        "tod_surcharge": (sum((h["kvah"] * D(sh) for _, sh, sg in slots if sg > 0), D(0)), RATE * D("0.15")),
        "tod_rebate": (sum((h["kvah"] * D(sh) for _, sh, sg in slots if sg < 0), D(0)), -RATE * D("0.15")),
        "fppca": (h["kvah"], FPPCA),
        "electricity_duty": (h["kvah"], DUTY),
    }
    for i, code in enumerate(codes):
        fl["charges"]["by_code"][i]["amount"]["value"] = float(h[code])
        el = b["elements"][i]
        el["amount"]["value"] = float(h[code])
        el["quantity"], el["rate"] = str(qty[code][0].quantize(D("1"))), str(qty[code][1].normalize())
        el["formula_recheck"].update(computed=str(abs(h[code])), billed=str(abs(h[code])), difference="0")
    b["elements"][6]["amount"]["value"] = float(h["total"])
    b["elements"][7]["amount"]["value"] = 0
    b["elements"][8]["amount"]["value"] = float(h["total"])
    t = fl["totals"]
    t["current_charges"]["value"], t["arrears"]["value"], t["net_payable"]["value"] = (
        float(h["total"]),
        0,
        float(h["total"]),
    )
    res["file"]["sha256"] = f"assumed-{first.isoformat()}"
    return res


def save(name: str, data: Any) -> None:
    Path(OUT or ".", f"{name}.json").write_text(json.dumps(data, indent=1, default=str))


def make() -> dict[str, str]:
    restore_committed()
    mp = pytest.MonkeyPatch()
    fake = FakeBillx()
    mp.setattr(bill_store, "BillxClient", fake.client)
    ids, notes = [], []
    for first, factor in MONTHS:
        tag = first.strftime("%Y%m")
        r = client.post(
            "/bills",
            files={
                "file": (f"{tag}.pdf", fake.register(tag, assumed_bill(first, factor)), "application/pdf")
            },
        )
        assert r.status_code in (200, 201), r.text
        [bill] = r.json()
        notes.append({"month": tag, "accepted": bill.get("accepted"), "outstanding": bill["outstanding"]})
        for path in list(bill["outstanding"]):
            value = next(x["value"] for x in bill["fields"] if x["path"] == path)
            bill = client.post(f"/bills/{bill['id']}/confirm", json={"path": path, "value": value}).json()
        ids.append(bill["id"])
    si = client.post("/intake/from-bills", json={"bill_ids": ids, "overrides": {"voltage_kv": "33"}})
    assert si.status_code == 200, si.text
    save("01_bills", notes)
    save("00_hand_bills", {m.isoformat(): hand_bill(m, f) for m, f in MONTHS})
    save("02_intake", si.json())
    out = client.post("/sites", json={"intake": si.json()})
    assert out.status_code == 201, out.text
    return {"site": out.json()["site_id"], "conn": out.json()["connection_id"]}


def _get(name: str, path: str, **kw: Any) -> Any:
    r = client.get(path, headers=AN, **kw)
    save(
        name,
        r.json()
        if r.headers.get("content-type", "").startswith("application/json")
        else {"status": r.status_code, "text": r.text[:4000]},
    )
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:300]}"
    return r.json()


def _post(name: str, path: str, ok: tuple[int, ...] = (200, 201, 202), **kw: Any) -> Any:
    r = client.post(path, headers=AN, **kw)
    save(
        name,
        r.json()
        if r.headers.get("content-type", "").startswith("application/json")
        else {"status": r.status_code, "text": r.text[:4000]},
    )
    assert r.status_code in ok, f"{path}: {r.status_code} {r.text[:300]}"
    return r.json()


AS_OF = "2026-11-01"


def step_01_replay(site: dict[str, str]) -> None:
    _get("03_replay", f"/connections/{site['conn']}/replay")


def step_02_connection_profile_avoided(site: dict[str, str]) -> None:
    _get("04_connection", f"/connections/{site['conn']}")
    _get("05_profile", f"/connections/{site['conn']}/profile")
    _get("06_avoided", f"/connections/{site['conn']}/avoided-cost", params={"as_of": AS_OF})


def step_03_landed(site: dict[str, str]) -> None:
    _get("08_landed", f"/connections/{site['conn']}/landed-cost", params={"as_of": AS_OF})


def step_04_settle(site: dict[str, str]) -> None:
    for own in ("captive", "third_party"):
        body = {
            "sources": [{"id": "pv", "kind": "oa_solar_intra", "capacity_mw": "6", "ownership": own}],
            "as_of": AS_OF,
        }
        _post(f"09_settle_solar_{own}", f"/connections/{site['conn']}/settle", json=body)
    body = {
        "sources": [{"id": "w", "kind": "oa_wind_intra", "capacity_mw": "4", "ownership": "captive"}],
        "as_of": AS_OF,
    }
    _post("09_settle_wind_captive", f"/connections/{site['conn']}/settle", json=body)


FIVE = [
    {"kind": "rooftop_pv", "max_mw": "1"},
    {"kind": "oa_solar_intra", "ownership": "captive", "max_mw": "8"},
    {"kind": "oa_wind_intra", "ownership": "captive", "max_mw": "5"},
    {"kind": "btm_bess", "max_mw": "2"},
    {"kind": "oa_bess", "max_mw": "2"},
]


def step_05_optimise_five_components(site: dict[str, str]) -> None:
    for name, cands in (
        ("captive", FIVE),
        ("third_party", [dict(c, ownership="third_party") if "ownership" in c else c for c in FIVE]),
    ):
        spec = {"candidates": cands, "as_of": AS_OF, "resolution_min": 60}
        sc = _post(
            f"10_scenario_{name}",
            f"/sites/{site['site']}/scenarios",
            json={"connection_id": site["conn"], "name": f"five {name}", "spec": spec},
        )
        run = _post(f"11_run_{name}", f"/scenarios/{sc['id']}/run", params={"wait": "true"})
        _get(f"12_run_detail_{name}", f"/runs/{run['id']}")
        if name == "captive":
            _post("13_replay_captive", f"/runs/{run['id']}/replay", params={"wait": "true"})
            d = client.get(f"/runs/{run['id']}/dispatch.csv", headers=AN)
            Path(OUT or ".", "14_dispatch_captive.csv").write_text(d.text)


def step_06_plan_and_share(site: dict[str, str]) -> None:
    _post(
        "15_plan_start",
        f"/connections/{site['conn']}/plan",
        params={"wait": "true", "resolution_min": 60, "as_of": AS_OF},
    )
    _get("16_plan", f"/connections/{site['conn']}/plan")
    tok = _post("17_share", f"/connections/{site['conn']}/share")
    r = client.get(tok["path"])  # as a visitor with the link only
    save(
        "18_shared_plan",
        r.json() if r.status_code == 200 else {"status": r.status_code, "text": r.text[:2000]},
    )
    assert r.status_code == 200
    _get("07_chain", f"/connections/{site['conn']}/chain")
    _get("22_today", f"/connections/{site['conn']}/today")


def step_07_rule_book(site: dict[str, str]) -> None:
    b = _post(
        "19_book",
        "/statebooks/generate",
        json={"discom": "pvvnl", "month": "2026-11-01", "connection_id": site["conn"]},
    )
    _post("20_book_approved", f"/statebooks/{b['id']}/approve", json={"note": "full test"})
    f = client.get(f"/statebooks/{b['id']}/file", headers=AN)
    Path(OUT or ".", "21_book.md").write_text(f.text)
    assert f.status_code == 200


def test_up_full_run() -> None:
    site = make()
    failures = []
    for step in (
        step_01_replay,
        step_02_connection_profile_avoided,
        step_03_landed,
        step_04_settle,
        step_05_optimise_five_components,
        step_06_plan_and_share,
        step_07_rule_book,
    ):
        try:
            step(site)
        except AssertionError as e:
            failures.append(f"{step.__name__}: {e}")
    save("99_failures", failures)
    assert not failures, failures
