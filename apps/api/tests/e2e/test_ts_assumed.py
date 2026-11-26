"""Stage 17, Telangana: TGSPDCL HT-I(A) Industry General, 33 kV, contract demand 3,000 kVA, a
two-shift plant. Twelve bills (April 2026 to March 2027) worked by hand from TGERC's Retail Supply
Tariff Order FY 2026-27, then every step of the product through the API, asserted against those
hand figures and the APPROVED rule book (the FY 2026-27 draft imported at Stage 14 is not used).
Set TS_OUT=<folder> to also write every response there."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
import yaml

from tests.e2e import assumed_state as st
from tests.e2e.assumed_state import HandBill, Line, Run, dec, q2, q4

pytestmark = pytest.mark.slow

# TGERC Retail Supply Tariff Order FY 2026-27 (TGSPDCL, TGNPDCL), document 0c84182f-4a13-5915-9fcb-b98639b59622,
# PDF pages (printed page in brackets).
DEMAND = D(500)  # p.281 [269] 4.16.4, HT-I(A) 33 kV, Rs/kVA/month of the billing demand
ENERGY = D("7.15")  # p.281 [269] 4.16.4, 33 kV, Rs/kVAh (the normal energy charge, 22:00-06:00)
TOD_PEAK = D("1.50")  # p.281 [269] 4.16.3, 06:00-10:00 and 18:00-22:00, Rs/kVAh on the normal charge
TOD_DAY = D("-0.50")  # p.281 [269] 4.16.3, 10:00-18:00 incentive, Rs/kVAh
BILLING_MIN_PCT = D("0.80")  # p.294 [282] 4.28.1 (and 4.27.5 p.293 [281]): recorded MD or 80% of CMD
MIN_KVAH_PER_KVA = D(50)  # p.294 [282] 4.28.2: actual energy or 50 kVAh per kVA of billing demand
CUSTOMER_CHARGE = D(3500)  # p.300 [288] Customer Charges, HT consumers at 33 kV, Rs/month
# Not in the order as a figure: FSA/FCA (4.27.1 p.293 [281]: "as per Regulation No.2 of 2023") and
# electricity duty (4.27.2: "exclusive of", Telangana Electricity Duty Act 1939); the hand bills carry neither.

CD, KV, PF = D(3000), "33", D("0.98")
SLOTS = [  # the order's windows with the assumed share of a two-shift month in each, and the adjustment
    ("06:00-10:00", D("0.22"), TOD_PEAK),
    ("10:00-18:00", D("0.44"), TOD_DAY),
    ("18:00-22:00", D("0.22"), TOD_PEAK),
    ("22:00-06:00", D("0.12"), D(0)),
]
MONTHS = [  # supply month, kWh, recorded maximum demand (kVA)
    (date(2026, 4, 1), D(1092000), D(2630)), (date(2026, 5, 1), D(1138000), D(2710)),
    (date(2026, 6, 1), D(1104000), D(2650)), (date(2026, 7, 1), D(1046000), D(2480)),
    (date(2026, 8, 1), D(1021000), D(2350)), (date(2026, 9, 1), D(1038000), D(2290)),
    (date(2026, 10, 1), D(1079000), D(2560)), (date(2026, 11, 1), D(1032000), D(2310)),
    (date(2026, 12, 1), D(1015000), D(2240)), (date(2027, 1, 1), D(1011000), D(2180)),
    (date(2027, 2, 1), D(968000), D(2380)), (date(2027, 3, 1), D(1087000), D(2600)),
]  # fmt: skip


def billing_demand(md: D) -> D:
    return max(md, CD * BILLING_MIN_PCT)


def hand_bill(first: date, kwh: D, md: D) -> HandBill:
    kvah = (kwh / PF).quantize(D(1))
    tod, left = [], kwh
    for i, (name, share, _) in enumerate(SLOTS):
        k = left if i == len(SLOTS) - 1 else (kwh * share).quantize(D(1))
        tod.append((name, k))
        left -= k
    bd = billing_demand(md)
    assert kvah >= bd * MIN_KVAH_PER_KVA  # the minimum energy charge (4.28.2) does not bind
    peak = sum((kvah * sh for _, sh, adj in SLOTS if adj > 0), D(0)).quantize(D(1))
    day = sum((kvah * sh for _, sh, adj in SLOTS if adj < 0), D(0)).quantize(D(1))
    h = HandBill(first, kwh, kvah, tod, md, CD, "HT-I(A) INDUSTRY GENERAL")
    h.lines = [
        Line("demand_charge", "DEMAND CHARGES", bd, DEMAND, q2(bd * DEMAND)),
        Line("energy_charge", "ENERGY CHARGES", kvah, ENERGY, q2(kvah * ENERGY)),
        Line("tod_surcharge", "TOD CHARGES (PEAK 06-10, 18-22)", peak, TOD_PEAK, q2(peak * TOD_PEAK)),
        Line("tod_rebate", "TOD INCENTIVE (10-18)", day, TOD_DAY, q2(day * TOD_DAY)),
        Line("other", "CUSTOMER CHARGES", D(1), CUSTOMER_CHARGE, CUSTOMER_CHARGE),
    ]
    return h


BILLS = [hand_bill(m, e, md) for m, e, md in MONTHS]
KVAH_PER_KWH = sum((h.kvah / h.kwh for h in BILLS), D(0)) / len(BILLS)  # the bills' own ratio
AS_OF = "2026-11-01"
# open-access figures in the order, as the approved rules for TGSPDCL at 33 kV hold them
TD_CHARGE = D("0.09")  # p.152 [140] 3.21.22, TGSPDCL 33 kV, Rs/kWh (the approved "aggregate T&D charge")
CSS = D("1.73")  # p.152 [140] Table 3-43, HT-I Industry at 33 kV, allowable CSS
PRICE = {"solar": D("3.10"), "wind": D("3.60")}  # stated PPA tariffs (the app's benchmarks)
EQUITY = D("0.25")  # stated group-captive equity cost, Rs/kWh
KIND = {"solar": "oa_solar_intra", "wind": "oa_wind_intra"}


def hand_landed(route: str, source: str, own_cost: D | None = None) -> dict[str, D]:
    """Landed cost per kWh at the meter from the approved rules' order figures: the T&D charge and,
    for third party, CSS. No open-access loss, additional surcharge or banking rule is approved for
    Telangana (rule gaps), so none is in the sum."""
    gen = own_cost if route == "captive" else PRICE[source]
    assert gen is not None
    lines = {
        "generation": gen,
        "td_charge": TD_CHARGE,
        "css": CSS if route == "third_party" else D(0),  # captive and group captive exempt (s. 42(2))
        "equity": EQUITY if route == "group_captive" else D(0),
    }
    lines["total"] = sum(lines.values(), D(0))
    return lines


def grid_bill(month_slots: dict[str, dict[str, D]], month_peak: dict[str, D]) -> dict[str, D]:
    """The year's grid bill from a dispatch file by the APPROVED schedule (kVAh at the bills' ratio,
    Rs 7.15/kVAh with the order's ToD, Rs 500/kVA on the recorded demand: the approved row has no 80%
    floor and no customer charge, worked apart in `by_order`)."""
    energy = demand = D(0)
    for m, slots in month_slots.items():
        for name, kwh in slots.items():
            if name.startswith("__"):
                continue
            adj = TOD_PEAK if name.startswith("Peak") else TOD_DAY if name.startswith("Day") else D(0)
            energy += kwh * KVAH_PER_KWH * (ENERGY + adj)
        demand += DEMAND * month_peak[m] * KVAH_PER_KWH
    top = ((ENERGY + TOD_PEAK) * KVAH_PER_KWH, DEMAND * KVAH_PER_KWH)  # dearest, for print precision
    return {"energy": energy, "demand": demand, "max_rate": top[0], "max_demand_rate": top[1]}


def by_order(rows: list[dict[str, str]], run: dict[str, object]) -> dict[str, str]:
    """What the order adds to the approved bill on a dispatch file: the 80% floor (4.28.1) and the
    customer charge (p.300)."""
    peaks: dict[str, D] = {}
    for m, v in st.monthly(rows, "metered_kwh").items():
        peaks[m] = v["__peak_kw"] * KVAH_PER_KWH
    floor = sum((DEMAND * max(D(0), CD * BILLING_MIN_PCT - kva) for kva in peaks.values()), D(0))
    return {"floor": str(q2(floor)), "customer_charge": str(CUSTOMER_CHARGE * len(peaks))}


def test_ts_full_run() -> None:
    run = Run("TS_OUT")
    found: dict[str, object] = {}
    run.make(
        BILLS,
        "tgspdcl",
        "SOUTHERN POWER DISTRIBUTION COMPANY OF TELANGANA LIMITED",
        "Telangana",
        {"voltage_kv": KV, "industry_shape": "two_shift", "weekly_off": [6]},
    )

    # 1. bill replay: energy with the order's rupee ToD agrees to the paisa; the demand charge agrees
    # where the recorded demand is at least 80% of contract demand, and otherwise differs by the floor
    # the approved row lacks (rule gap: 4.28.1); the customer charge is not in the rule book (rule gap)
    rep = st.replay_by_group(run.get("03_replay", f"/connections/{run.conn}/replay"))
    assert sorted(rep) == [m for m, _, _ in MONTHS]
    floor_gap = {}
    for h in BILLS:
        g = rep[h.first]
        energy = h.amount("energy_charge") + h.amount("tod_surcharge") + h.amount("tod_rebate")
        assert g["energy"] == (energy, energy, "match"), (h.first, g["energy"])
        billed, computed, status = g["demand"]
        assert billed == h.amount("demand_charge") and computed == DEMAND * h.max_kva, (h.first, g["demand"])
        assert (status == "match") == (h.max_kva >= CD * BILLING_MIN_PCT), (h.first, status)
        floor_gap[str(h.first)] = billed - computed
        assert g["other:CUSTOMER CHARGES"][:2] == (CUSTOMER_CHARGE, None), g
    found["replay_demand_floor_gap"] = floor_gap

    # 2. today's cost: the approved tariff on the profile
    today = run.get("07_today", f"/connections/{run.conn}/today")
    hand_energy = sum(
        (h.amount("energy_charge") + h.amount("tod_surcharge") + h.amount("tod_rebate") for h in BILLS), D(0)
    )
    approved_demand = sum((DEMAND * h.max_kva for h in BILLS), D(0))
    # the bills print whole kVAh (and the hand bills' ToD kVAh are whole too), while today's cost turns
    # the profile's kWh into kVAh at the bills' average kVAh/kWh: each month may differ by half a kVAh
    # at the dearest rate and half a kVAh on each of the two ToD lines, nothing more
    bound = 12 * (D("0.5") * (ENERGY + TOD_PEAK) + D("0.5") * (TOD_PEAK - TOD_DAY))
    assert abs(dec(today["energy"]) - hand_energy) <= bound, (today["energy"]["value"], hand_energy, bound)
    assert abs(dec(today["demand"]) - approved_demand) <= 1, (today["demand"]["value"], approved_demand)
    assert today["fppca"] is None and today["electricity_duty"] is None
    found["today"] = {
        "energy": hand_energy,
        "energy_app": today["energy"]["value"],
        "demand_approved": approved_demand,
        "demand_floor_by_order": sum(floor_gap.values(), D(0)),
        "customer_charges": CUSTOMER_CHARGE * 12,
        "bills_total": sum((h.total for h in BILLS), D(0)),
    }

    # 3. landed cost, solar and wind on each route, against the order's figures line by line
    landed_out: list[dict[str, object]] = []
    for route in ("captive", "group_captive", "third_party"):
        sc = {
            "as_of": AS_OF,
            "month": 11,
            "sources": {
                KIND[s]: {
                    "ownership": route,
                    "price_inr_per_kwh": str(PRICE[s]),
                    **({"equity_cost_inr_per_kwh": str(EQUITY)} if route == "group_captive" else {}),
                }
                for s in ("solar", "wind")
            },
        }
        for s in ("solar", "wind"):
            got = st.landed_source(run, f"08_landed_{route}_{s}", sc, KIND[s])
            assert len(set(got["totals"].values())) == 1, got["totals"]  # the same in every ToD slot
            own = next((dec(v) for k, v in got["lines"].items() if k.startswith("Levelised cost")), None)
            hand = hand_landed(route, s, own)
            total = next(iter(got["totals"].values()))
            assert q4(total) == q4(hand["total"]), (
                route,
                s,
                total,
                hand,
                {k: v["value"] for k, v in got["lines"].items()},
            )
            assert {"oa_losses", "oa_additional_surcharge"} <= set(got["gaps"]), got[
                "gaps"
            ]  # said, not hidden
            landed_out.append({"route": route, "source": s, **{k: q4(v) for k, v in hand.items()}})
    found["landed"] = landed_out
    run.save("08_landed_hand", landed_out)

    # 4. settlement: no approved settlement rule for Telangana (rule gap: TGERC's open-access and
    # banking regulations are not in the rule book), so the app nets block by block and banks nothing
    load = run.profile_kw()
    inj = st.injection(D(4000), len(load))
    for own in ("captive", "third_party"):
        app = st.settle(run, f"09_settle_{own}", "oa_solar_intra", own, inj)
        assert app["rule"] is None and any("no approved settlement rule" in n for n in app["notes"])
        delivered = dec(app["sources"][0]["delivered_share"])
        assert delivered == 1  # no approved open-access loss (rule gap)
        hand = st.hand_settle(load, inj, delivered, lambda i: "normal", "block", False, None, D(0))
        bad = st.compare_settlement(app, hand)
        assert not bad, bad[:6]
    st.assert_balance(hand)
    found["settlement"] = {str(m): {k: q2(v) for k, v in h.items()} for m, h in hand.items()}

    # 5. the optimizer: five components, hourly and 15 minutes; the grid bill from the dispatch file
    found["runs"] = st.optimiser_checks(run, grid_bill, by_order)

    # 6. the rule book for November 2026: it carries the landed costs worked above
    b = run.post(
        "19_book",
        "/statebooks/generate",
        json={"discom": "tgspdcl", "month": "2026-11-01", "connection_id": run.conn},
    )
    text = run.get("21_book.md", f"/statebooks/{b['id']}/file")
    for s in ("solar", "wind"):
        assert f"**{hand_landed('third_party', s)['total']:.2f}**" in text, s
    assert "1.73" in text
    run.save("99_found", found)

    # 7. the hand-worked landed costs Stage 19's calculator test reads are these
    data = yaml.safe_load((Path(__file__).parent / "landed_hand" / "ts.yaml").read_text())
    for row in landed_out:
        saved = next(x for x in data["routes"] if x["route"] == row["route"] and x["source"] == row["source"])
        assert D(str(saved["total"]["value"])) == row["total"], (row, saved["total"])
