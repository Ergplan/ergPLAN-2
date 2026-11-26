"""Stage 17, Maharashtra: twelve bills of an MSEDCL HT I (A) industry worked by hand from MERC's
orders, then every step of the product through the API (bill replay, today's cost, landed cost on
each route, settlement after banking, the optimizer at hourly and 15-minute blocks, the grid bill
from the dispatch file, replay, the month's rule book), each checked against the hand figures.

Where the app differs from the order because the approved rule book lacks a rule (a rule gap, listed
in docs/stage-reports/multi-state-test.md), the assertion holds the app's figure with the gap named
beside it, and the order's own figure is worked too, so the gap stays visible and a fix shows up as
a failing assertion to update. Set MH_OUT to a folder to keep every answer for reading."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import ROUND_HALF_UP
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.e2e.assumed_site import (
    AN,
    HandBill,
    Line,
    Out,
    balance_by_hand,
    calculator_input,
    check_settle,
    client,
    crf,
    curve_energy,
    dispatch_rows,
    energy_per_kw,
    grid_bill_by_hand,
    hand_settle,
    landed_lines,
    make_site,
    route_entry,
    run_scenario,
    sv,
)

pytestmark = [pytest.mark.slow]
OUT = Out("MH_OUT")
Q = D("0.01")
LANDED_FILE = Path(__file__).parent / "landed_hand" / "mh.yaml"


def r2(x: D) -> D:
    return x.quantize(Q, rounding=ROUND_HALF_UP)


# ---- Constants typed from the orders (document, page, table) -----------------------------------
CASE75 = "5ee7dce7-acfb-5b8b-a933-4ea3865809e2"  # MERC Case 75/2025, post-remand order, 25 Mar 2026
MYT = "c1d27543-6ebe-5f8c-8e07-f2a6625b1100"  # MERC MYT order, Case 217/2024, 28 Mar 2025
# Case 75/2025 p. 100, "Summary of HT Tariff for FY 2026-27, effective from 1 April, 2026", row "HT I
# (A) HT - Industry Sub-total" under "HT": demand Rs/kVA/mth 650, energy 8.44, wheeling 0.81 Rs/kVAh.
DEMAND = D("650")
ENERGY = D("8.44")
WHEELING = D("0.81")
# Case 75/2025 p. 77, Table 8 "Revised ToD Tariff": solar hours 09:00-17:00 -15% (Apr to Sep) and
# -25% (Oct to Mar) of the energy charge in FY 27 (note ^); peak 17:00-24:00 "+20%#", the # being
# the MYT order's note (p. 565, Table 403): "For HT & LT Industrial & Commercial Categories = +25%".
# 00:00-09:00: 0%.
SOLAR_REBATE = {True: D("15"), False: D("25")}  # keyed by "April to September"
PEAK_SURCHARGE = D("25")
# MYT order p. 608, "Billing Demand - HT tariff categories": the higher of (a) the actual maximum
# demand, (b) 75% of the highest billing demand of the preceding eleven months (up to contract
# demand), (c) 70% of the contract demand.
BD_FLOOR_CD_PCT, BD_FLOOR_PRIOR_PCT = D("70"), D("75")
# Case 75/2025 p. 106, "Cross Subsidy Surcharge for FY 2026-27 - HT Category", row "HT I (A) (i): HT -
# Industry" under "HT (33kV, 22kV and 11 kV)": WL 7.50%, TL 3.26%, approved CSS 2.11 Rs./kWh.
CSS = D("2.11")
WHEELING_LOSS, TRANSMISSION_LOSS = D("7.50"), D("3.26")
# Case 75/2025 p. 83, Table 9, row E "Wheeling Charges (HT) (Rs/unit)", FY 26-27: 0.81.
OA_WHEELING = D("0.81")
# Levies the bills carry that no MERC tariff order states (rates as an MSEDCL bill prints them; the
# assumption of this test, not a regulation's figure): FAC per kVAh, electricity duty as a percentage
# of the charges above, tax on sale of electricity per kWh.
FAC = D("0.20")
DUTY_PCT = D("7.5")
TOSE = D("0.1804")
# The approved settlement rule (MSEDCL v4, Case 75/2025 pp. 77-79 with register fields): monthly
# banking, credit not carried forward, charge 8% in kind (register), limit the higher of 30% of the
# drawal after set-off and 25% of the generation (register). Drawal, Case 75/2025 p. 79, 18.21: energy
# banked in the normal slot may be drawn in the normal and solar slots; in the solar slot, in the solar
# slot only; in the peak slot, in the peak, normal and solar slots.
BANK_CHARGE = D("8")
ORDER_DRAW = {
    "normal": {"normal", "off_peak"},
    "off_peak": {"off_peak"},
    "peak": {"peak", "normal", "off_peak"},
}
# what the approved rule's register matrix allows (`banking.matrix`): normal → normal only (gap MH-4)
RULE_DRAW = {"normal": {"normal"}, "off_peak": {"off_peak"}, "peak": {"peak", "normal", "off_peak"}}
SINKS = ["peak", "normal", "off_peak"]  # dearest first: peak +25%, normal, solar hours (rebate)

# ---- The consumer ----------------------------------------------------------------------------------
# A single-shift engineering works on MSEDCL's 22 kV network, contract demand 2,000 kVA. Shares of the
# month's kWh in the order's ToD slots, from the single-shift working (08:00-17:00 on weekdays, a half
# Saturday): mostly in the solar hours.
CD = D("2000")
SLOTS = [
    ("00:00-09:00", D("0.22"), "normal"),
    ("09:00-17:00", D("0.67"), "solar"),
    ("17:00-24:00", D("0.11"), "peak"),
]
CATEGORY = {"normal": "normal", "solar": "off_peak", "peak": "peak"}
PF = D("0.97")  # kWh / kVAh
# supply month → (kWh, recorded maximum demand kVA); November carries the Diwali shutdown
MONTHS = [
    (date(2026, 4, 1), D("452000"), D("1640")),
    (date(2026, 5, 1), D("468000"), D("1680")),
    (date(2026, 6, 1), D("441000"), D("1620")),
    (date(2026, 7, 1), D("436000"), D("1600")),
    (date(2026, 8, 1), D("447000"), D("1610")),
    (date(2026, 9, 1), D("439000"), D("1600")),
    (date(2026, 10, 1), D("428000"), D("1580")),
    (date(2026, 11, 1), D("352000"), D("1300")),
    (date(2026, 12, 1), D("430000"), D("1570")),
    (date(2027, 1, 1), D("433000"), D("1580")),
    (date(2027, 2, 1), D("405000"), D("1590")),
    (date(2027, 3, 1), D("458000"), D("1650")),
]
AS_OF = "2026-11-01"
NOVEMBER = date(2026, 11, 1)  # the month whose recorded demand is below 70% of contract demand


def kvah_by_slot(kwh: D, kvah: D, tod_kwh: dict[str, D]) -> dict[str, D]:
    """kVAh in each slot as the meter's ToD registers read it, whole units: the month's kVAh in the
    proportion of the slot's kWh, the normal slot taking the remainder."""
    solar = (kvah * tod_kwh["solar"] / kwh).quantize(D("1"))
    peak = (kvah * tod_kwh["peak"] / kwh).quantize(D("1"))
    return {"solar": solar, "peak": peak, "normal": kvah - solar - peak}


def hand_bills() -> list[HandBill]:
    """The twelve bills worked by hand from the orders above, independent of the app."""
    out: list[HandBill] = []
    prior_bd: list[D] = []
    for first, kwh, md in MONTHS:
        kvah = (kwh / PF).quantize(D("1"))
        tod = [(name, (kwh * share).quantize(D("1"))) for name, share, _ in SLOTS[1:]]
        tod.insert(0, (SLOTS[0][0], kwh - sum((k for _, k in tod), D(0))))
        tod_kwh = {kind: k for (_, k), (_, _, kind) in zip(tod, SLOTS, strict=True)}
        kv = kvah_by_slot(kwh, kvah, tod_kwh)
        floors = [md, CD * BD_FLOOR_CD_PCT / 100]
        if prior_bd:
            floors.append(min(CD, max(prior_bd[-11:]) * BD_FLOOR_PRIOR_PCT / 100))
        bd = max(floors)
        prior_bd.append(bd)
        rebate = SOLAR_REBATE[4 <= first.month <= 9]
        lines = [
            Line("demand_charge", "DEMAND CHARGES", bd, DEMAND, "kVA", r2(bd * DEMAND)),
            Line("energy_charge", "ENERGY CHARGES", kvah, ENERGY, "kVAh", r2(kvah * ENERGY)),
            Line("wheeling_charge", "WHEELING CHARGES", kvah, WHEELING, "kVAh", r2(kvah * WHEELING)),
            Line(
                "tod_rebate",
                "TOD REBATE (SOLAR HOURS)",
                kv["solar"],
                -ENERGY * rebate / 100,
                "kVAh",
                -r2(kv["solar"] * ENERGY * rebate / 100),
            ),
            Line(
                "tod_surcharge",
                "TOD CHARGES (PEAK HOURS)",
                kv["peak"],
                ENERGY * PEAK_SURCHARGE / 100,
                "kVAh",
                r2(kv["peak"] * ENERGY * PEAK_SURCHARGE / 100),
            ),
            Line("fppca", "FAC", kvah, FAC, "kVAh", r2(kvah * FAC)),
        ]
        base = sum((ln.amount for ln in lines), D(0))
        lines.append(
            Line("electricity_duty", "ELECTRICITY DUTY", base, DUTY_PCT, "%", r2(base * DUTY_PCT / 100))
        )
        lines.append(Line("other", "TAX ON SALE OF ELECTRICITY", kwh, TOSE, "kWh", r2(kwh * TOSE)))
        out.append(HandBill(first, kwh, kvah, md, CD, tod, lines))
    return out


BILLS = hand_bills()
# the levies as the twelve bills average them per kVAh billed (a rate held to four places by the
# project's rule for rates, core/money.py), and per kWh at the bills' kVAh per kWh
KWH_YEAR = sum((h.kwh for h in BILLS), D(0))
KVAH_YEAR = sum((h.kvah for h in BILLS), D(0))
KVAH_PER_KWH = sum((h.kvah / h.kwh for h in BILLS), D(0)) / len(BILLS)
DUTY_PER_KWH = (sum((h.amount("electricity_duty") for h in BILLS), D(0)) / KVAH_YEAR).quantize(
    D("0.0001")
) * KVAH_PER_KWH
FAC_PER_KWH = (sum((h.amount("fppca") for h in BILLS), D(0)) / KVAH_YEAR).quantize(D("0.0001")) * KVAH_PER_KWH


# the plant figures given for the captive and 26%-equity routes (stated in the scenario: the planning
# figures of a utility-scale plant), and the cost of capital the landed-cost card assumes (10%)
PLANT = {
    "solar": {"capex": D("42000"), "om": D("1.5"), "cuf": D("21"), "deg": D("0.5"), "life": 25},
    "wind": {"capex": D("68000"), "om": D("2"), "cuf": D("28"), "deg": D("0"), "life": 25},
}
PPA = {"solar": D("3.10"), "wind": D("3.60")}
DISCOUNT = D("10")
EQUITY = D("26")
KIND = {"solar": "oa_solar_intra", "wind": "oa_wind_intra"}


def hand_landed() -> dict[str, dict[str, Any]]:
    """Landed cost per kWh at the meter, by route and source, worked by hand. `approved` is built from
    the lines the approved rule book carries (what the app can show today); `order` from the order's
    own figures, which add the wheeling charge and the losses the rule book lacks (gaps MH-2, MH-3)."""
    out: dict[str, dict[str, Any]] = {}
    keep_order = (1 - TRANSMISSION_LOSS / 100) * (1 - WHEELING_LOSS / 100)
    for source in ("solar", "wind"):
        p = PLANT[source]
        f = crf(DISCOUNT, p["life"])
        e = energy_per_kw(p["cuf"], p["deg"], p["life"])
        # a rate keeps four places (core/money.py): the levelised cost and the equity cost are stated so
        lcoe = (p["capex"] * (f + p["om"] / 100) / e).quantize(D("0.0001"))
        equity = p["capex"] * EQUITY / 100 * f / e
        for route in ("captive", "group_captive", "third_party"):
            gen = lcoe if route == "captive" else PPA[source]
            css = CSS if route == "third_party" else D(0)  # s. 42(2) proviso: captive users pay none
            eq = equity if route == "group_captive" else D(0)
            common = {
                "generation": gen,
                "drawal": css,
                "bank_pct": BANK_CHARGE,
                "duty": DUTY_PER_KWH,
                "equity": eq,
            }
            out[f"{route}:{source}"] = {
                "approved": landed_lines(injection=D(0), keep=D(1), **common),
                "order": landed_lines(injection=OA_WHEELING, keep=keep_order, **common),
            }
    return out


# ---- steps ------------------------------------------------------------------------------------------


def step_intake(ctx: dict[str, Any]) -> None:
    si = ctx["intake"]
    assert si["discom"]["value"] == "msedcl" and si["category"]["value"] == "HT I(A)"
    # Stage 17 fix: a retail wheeling charge alone is not open access (intake/recognise.py)
    assert si.get("open_access") is None
    assert [m["month"] for m in si["months"]] == [m.isoformat() for m, _, _ in MONTHS]
    for m, h in zip(si["months"], BILLS, strict=True):
        assert D(m["energy"]["value"]) == h.kwh
        # Stage 17 fix: the power factor is the bill's kWh / kVAh when no PF is printed
        assert D(m["power_factor"]["value"]).quantize(D("0.0001")) == (h.kwh / h.kvah).quantize(D("0.0001"))


def step_replay(ctx: dict[str, Any]) -> None:
    rep = ctx["replay"]
    by_month = {r["evaluated_on"][:7]: r for r in rep}
    assert len(by_month) == 12
    for h in BILLS:
        r = by_month[f"{h.first:%Y-%m}"]
        lines = {ln["group"]: ln for ln in r["lines"]}
        assert sv(r["billed_total"]) == h.total, h.first
        # energy and ToD to the rupee
        energy = h.amount("energy_charge") + h.amount("tod_rebate") + h.amount("tod_surcharge")
        assert sv(lines["energy"]["billed"]) == energy
        assert abs(sv(lines["energy"]["computed"]) - energy) < 1, (h.first, lines["energy"])
        assert lines["energy"]["status"] == "match"
        assert sv(lines["wheeling"]["computed"]) == h.amount("wheeling_charge")
        # demand: the order's floor of 70% of contract demand (MYT p. 608) is not in the approved
        # schedule (gap MH-1); November's 1,300 kVA is billed at the floor, 1,400 kVA
        bd = h.amount("demand_charge")
        if h.first == NOVEMBER:
            assert bd == DEMAND * CD * BD_FLOOR_CD_PCT / 100
            assert sv(lines["demand"]["computed"]) == DEMAND * h.max_kva  # known wrong: gap MH-1
            assert lines["demand"]["gap"] == "billing_demand_rule"
        else:
            assert sv(lines["demand"]["computed"]) == bd
        # levies outside the order: named, not computed
        for group, gap, amount in (
            ("fppca", "fppca_monthly", h.amount("fppca")),
            ("electricity_duty", "electricity_duty", h.amount("electricity_duty")),
            ("tax_on_sale", "tax_on_sale", h.amount("other")),
        ):
            assert lines[group]["computed"] is None and lines[group]["gap"] == gap
            assert sv(lines[group]["billed"]) == amount
        outside = h.amount("fppca") + h.amount("electricity_duty") + h.amount("other")
        demand_gap = bd - DEMAND * h.max_kva
        assert abs(sv(r["total_difference"]) + outside + demand_gap) < 1, (h.first, r["total_difference"])


def step_today(ctx: dict[str, Any]) -> None:
    t = ctx["today"]
    hand = {
        c: sum((h.amount(c) for h in BILLS), D(0))
        for c in (
            "demand_charge",
            "energy_charge",
            "wheeling_charge",
            "tod_rebate",
            "tod_surcharge",
            "fppca",
            "electricity_duty",
            "other",
        )
    }
    energy = hand["energy_charge"] + hand["wheeling_charge"] + hand["tod_rebate"] + hand["tod_surcharge"]
    assert abs(sv(t["energy"]) - energy) <= 12, (t["energy"]["value"], energy)  # a rupee a bill
    # demand on the recorded maximum demand every month (gap MH-1: November's floor missing)
    november_gap = DEMAND * (CD * BD_FLOOR_CD_PCT / 100 - D("1300"))
    assert abs(sv(t["demand"]) - (hand["demand_charge"] - november_gap)) <= 1, t["demand"]["value"]
    # FPPCA and duty at the bills' average per kVAh, a rate held to four places (core/money.py), on
    # the year's kVAh: within a rupee of that, and within the rounding of the rate of the bills' sum
    kvah_year = KVAH_PER_KWH * KWH_YEAR
    kvah_billed = sum((h.kvah for h in BILLS), D(0))
    for code, key in (("fppca", "fppca"), ("electricity_duty", "electricity_duty")):
        rate4 = (hand[code] / kvah_billed).quantize(D("0.0001"))
        assert abs(sv(t[key]) - rate4 * kvah_year) <= 1, (code, t[key]["value"], rate4 * kvah_year)
        ctx["figures"][f"today_{code}_vs_bills"] = str(sv(t[key]) - hand[code])
    # tax on sale is in no part of today's cost (gap MH-5)
    parts = sum((sv(t[k]) for k in ("energy", "demand", "fppca", "electricity_duty")), D(0))
    assert abs(sv(t["annual"]) - parts) <= 2, (t["annual"]["value"], parts)  # each part in whole rupees
    ctx["figures"]["today_vs_bills"] = {
        "bills": str(sum(hand.values(), D(0))),
        "app": t["annual"]["value"],
        "tax_on_sale_left_out": str(hand["other"]),
        "november_floor_left_out": str(november_gap),
    }
    assert sv(t["kwh"]) == KWH_YEAR
    ctx["figures"]["today_annual"] = t["annual"]["value"]


def _landed(conn: str, sources: dict[str, Any]) -> dict[str, Any]:
    sc = {"as_of": AS_OF, "month": 11, "sources": sources}
    import json

    return dict(
        OUT.get(
            f"08_landed_{next(iter(sources.values())).get('ownership')}",
            f"/connections/{conn}/landed-cost",
            params={"scenario": json.dumps(sc)},
        )
    )


def step_landed(ctx: dict[str, Any]) -> None:
    conn = ctx["conn"]
    hand = hand_landed()
    for route in ("captive", "group_captive", "third_party"):
        sources = {}
        for source in ("solar", "wind"):
            s: dict[str, Any] = {"ownership": route}
            if route == "captive":
                p = PLANT[source]
                s.update(
                    capex_inr_per_kw=str(p["capex"]),
                    om_pct_of_capex=str(p["om"]),
                    cuf_pct=str(p["cuf"]),
                    degradation_pct=str(p["deg"]),
                    life_years=p["life"],
                )
            else:
                s["price_inr_per_kwh"] = str(PPA[source])
            if route == "group_captive":
                p = PLANT[source]
                s.update(
                    capex_inr_per_kw=str(p["capex"]),
                    cuf_pct=str(p["cuf"]),
                    degradation_pct=str(p["deg"]),
                    life_years=p["life"],
                    equity_share_pct=str(EQUITY),
                )
            sources[KIND[source]] = s
        out = _landed(conn, sources)
        for source in ("solar", "wind"):
            card = next(x for x in out["sources"] if x["id"] == KIND[source])
            h = hand[f"{route}:{source}"]
            assert card["available"], card
            # the rule book lacks the order's wheeling charge and losses (gaps MH-2, MH-3)
            assert {"oa_wheeling", "oa_losses"} <= set(card["gaps"]), card["gaps"]
            for slot in card["by_slot"]:
                total = sv(slot["total"])
                assert abs(total - h["approved"]["total"]) < D("0.0001"), (
                    route,
                    source,
                    slot["slot"],
                    total,
                    h,
                )
                assert abs(sum((sv(x) for x in slot["lines"]), D(0)) - total) < D("0.000001")
                css = [x for x in slot["lines"] if "CSS" in (x.get("label") or "")]
                assert sum((sv(x) for x in css), D(0)) == h["approved"]["drawal_charges"]
                assert all(
                    r["kind"] == "order" and r["page"] == 106 for x in css for r in x["source_refs"][:1]
                )
            ctx["figures"][f"landed:{route}:{source}"] = str(total)


def _settle(conn: str, name: str, kind: str, mw: str, own: str) -> dict[str, Any]:
    body = {"sources": [{"id": "s", "kind": kind, "capacity_mw": mw, "ownership": own}], "as_of": AS_OF}
    return dict(OUT.post(name, f"/connections/{conn}/settle", json=body))


def slot_of_hour(month: int, h: int) -> str:
    """The ToD category of an hour (Case 75/2025 p. 77): normal 00-09, solar hours 09-17 (a rebate,
    so off-peak), peak 17-24."""
    return "normal" if h < 9 else "off_peak" if h < 17 else "peak"


def hand_year(curve: str, kw: D, draw: dict[str, set[str]]) -> dict[date, Any]:
    """The year's settlement by hand: the bills' ToD kWh against the normative curve's energy in each
    slot (delivered share 1: the approved rules hold no loss, gap MH-3), the banking limit the higher
    of 30% of the drawal after set-off and 25% of the generation (the register's limit)."""
    gen = curve_energy(curve, kw, date(2026, 4, 1), slot_of_hour)
    out = {}
    for h in BILLS:
        tod_kwh = {CATEGORY[kind]: k for (_, k), (_, _, kind) in zip(h.tod, SLOTS, strict=True)}
        g = {c: gen[(h.first, c)] for c in ("normal", "off_peak", "peak")}
        short = sum((max(D(0), tod_kwh[c] - g[c]) for c in g), D(0))
        lim = max(D("0.30") * short, D("0.25") * sum(g.values(), D(0)))
        out[h.first] = hand_settle(
            tod_kwh, g, charge_pct=BANK_CHARGE, limit=lim, may_draw=draw, sink_order=SINKS
        )
    return out


KEYS = ("setoff", "banked_before", "charge", "withdrawn", "lapsed", "unbanked", "grid")


def _year(hand: dict[date, Any]) -> dict[str, str]:
    return {k: str(sum((getattr(h, k) for h in hand.values()), D(0)).quantize(D("1"))) for k in KEYS}


def step_settle(ctx: dict[str, Any]) -> None:
    conn = ctx["conn"]
    solar = _settle(conn, "09_settle_solar_captive", "oa_solar_intra", "2", "captive")
    # no approved loss rule: the delivered share is 1, not the order's (1 - 3.26%)(1 - 7.50%) (gap MH-3)
    assert solar["sources"][0]["delivered_share"] == "1"
    assert solar["granularity"] == "tod_slot_monthly" and "Regulation 20.3" in (solar["rule"] or "")
    hand_solar = hand_year("solar_mh", D(2000), ORDER_DRAW)
    check_settle(solar, hand_solar)
    third = _settle(conn, "09_settle_solar_third_party", "oa_solar_intra", "2", "third_party")
    assert third["months"] == solar["months"]  # the rule covers every ownership alike
    wind = _settle(conn, "09_settle_wind_captive", "oa_wind_intra", "0.8", "captive")
    by_rule, by_order = hand_year("wind_mh", D(800), RULE_DRAW), hand_year("wind_mh", D(800), ORDER_DRAW)
    # the app draws by the register's matrix, normal-slot banking in the normal slot only; the order
    # (p. 79) lets it be drawn in the solar hours too (gap MH-4)
    check_settle(wind, by_rule)
    more = sum((by_order[m].withdrawn - by_rule[m].withdrawn for m in by_rule), D(0))
    assert more > 0
    f = ctx["figures"]
    f["settle_solar_2MW"] = _year(hand_solar)
    f["settle_wind_0.8MW_rule"] = _year(by_rule)
    f["settle_wind_0.8MW_order"] = _year(by_order)


FIVE = [
    {"kind": "rooftop_pv", "max_mw": "0.5"},
    {"kind": "oa_solar_intra", "ownership": "captive", "max_mw": "3"},
    {"kind": "oa_wind_intra", "ownership": "captive", "max_mw": "2"},
    {"kind": "btm_bess", "max_mw": "1"},
    {"kind": "oa_bess", "max_mw": "1"},
]


def grid_rate(month: int, slot: str) -> D:
    """INR per kWh bought from MSEDCL in a month and slot (p. 100 and p. 77): energy charge with the
    ToD adjustment, wheeling and FAC, all on kVAh at the bills' kVAh per kWh, and duty per kWh."""
    adj = {"normal": D(0), "peak": PEAK_SURCHARGE}.get(slot, -SOLAR_REBATE[4 <= month <= 9])
    return KVAH_PER_KWH * (ENERGY * (1 + adj / 100) + WHEELING + FAC) + DUTY_PER_KWH


def _grid_bill(ctx: dict[str, Any], run: dict[str, Any], minutes: int, name: str) -> None:
    d = client.get(f"/runs/{run['id']}/dispatch.csv", headers=AN)
    assert d.status_code == 200
    OUT.text(f"14_dispatch_{name}.csv", d.text)
    rows = dispatch_rows(d.text)
    balance_by_hand(rows, lambda month, slot: slot)  # each MSEDCL slot is a category of its own
    energy, demand = grid_bill_by_hand(rows, minutes, grid_rate, KVAH_PER_KWH, DEMAND)
    after = {k: D(v) for k, v in run["analyst"]["lines_after"].items()}
    # to the rupee (each line of the run is in whole paise)
    assert abs(energy - after["grid_energy"]) < 1, (energy, after["grid_energy"])
    # demand on the metered peak; the order's floor of 70% of contract demand is not applied (gap MH-1)
    assert abs(demand - after["demand"]) < 1, (demand, after["demand"])
    floor = CD * BD_FLOOR_CD_PCT / 100
    _, with_floor = grid_bill_by_hand(rows, minutes, grid_rate, KVAH_PER_KWH, DEMAND, floor)
    ctx["figures"][f"grid_bill_{name}"] = {
        "energy_by_hand": str(r2(energy)),
        "energy_app": str(after["grid_energy"]),
        "demand_by_hand": str(r2(demand)),
        "demand_app": str(after["demand"]),
        "demand_with_the_order_floor": str(r2(with_floor)),
    }


def step_optimise(ctx: dict[str, Any]) -> None:
    hourly = run_scenario(OUT, ctx, "five_hourly", FIVE, 60, AS_OF)
    _grid_bill(ctx, hourly, 60, "hourly")
    rep = OUT.post("13_replay", f"/runs/{hourly['id']}/replay", params={"wait": "true"})
    assert rep["identical_to_original"] is True, rep
    f = ctx["figures"]
    f["sizes_hourly_kw"] = {x["id"]: x["kw"]["value"] for x in hourly["summary"]["sizes"]}
    f["annual_hourly"] = {k: v["value"] for k, v in hourly["summary"]["annual"].items()}
    quarter = run_scenario(OUT, ctx, "five_15min", FIVE, 15, AS_OF)
    _grid_bill(ctx, quarter, 15, "15min")
    f["sizes_15min_kw"] = {x["id"]: x["kw"]["value"] for x in quarter["summary"]["sizes"]}
    f["annual_15min"] = {k: v["value"] for k, v in quarter["summary"]["annual"].items()}


def step_book(ctx: dict[str, Any]) -> None:
    b = OUT.post(
        "19_book",
        "/statebooks/generate",
        json={"discom": "msedcl", "month": "2026-11-01", "connection_id": ctx["conn"]},
    )
    f = client.get(f"/statebooks/{b['id']}/file", headers=AN)
    assert f.status_code == 200
    OUT.text("21_book.md", f.text)
    book_figures(
        f.text,
        {"normal": grid_rate(11, "normal"), "solar": grid_rate(11, "solar"), "peak": grid_rate(11, "peak")},
        hand_landed(),
    )


def book_figures(text: str, grid: dict[str, D], hand: dict[str, dict[str, Any]]) -> None:
    """The month's rule book prints the grid tariff of each slot and, per source, the landed cost of
    each route as the day's average, to the paisa: each the hand figure (the book's landed cost leaves
    the banking charge out, showing it apart for banked units only)."""
    import re

    for slot, rate in grid.items():
        m = re.search(rf"^\| {slot} \| [0-9:–]+ \| ([0-9.]+) ", text, re.M)
        assert m and D(m.group(1)) == r2(rate), (slot, m and m.group(1), rate)
    for source, title in (
        ("solar", "Open-access solar, intra-state"),
        ("wind", "Open-access wind, intra-state"),
    ):
        section = text.split(f"### {title}", 1)[1].split("### ", 1)[0]
        for route, label in (
            ("third_party", "Third party"),
            ("group_captive", "Captive, 26% equity (group captive)"),
            ("captive", "Captive, own plant"),
        ):
            m = re.search(rf"^\| {re.escape(label)} \| ([0-9.]+) \|", section, re.M)
            want = hand[f"{route}:{source}"]["approved"]["total_excluding_banking_upper_bound"]
            assert m and D(m.group(1)) == r2(want), (source, route, m and m.group(1), want)


def test_mh_assumed() -> None:
    assert yaml.safe_load(LANDED_FILE.read_text()) == landed_yaml(), (
        "landed_hand/mh.yaml is not the hand figures"
    )
    OUT.save("00_hand_bills", [{"month": h.first, "total": h.total, "lines": h.lines} for h in BILLS])
    site = make_site(
        OUT, BILLS, "MSEDCL", "HT I (A) - INDUSTRY", {"voltage_kv": "22", "industry_shape": "single_shift"}
    )
    conn = site["conn"]
    ctx: dict[str, Any] = {"conn": conn, "site": site["site"], "figures": {}}
    ctx["intake"] = site["intake"]
    ctx["replay"] = OUT.get("03_replay", f"/connections/{conn}/replay")
    ctx["today"] = OUT.get("07_today", f"/connections/{conn}/today", params={"as_of": AS_OF})
    failures = []
    steps: list[Callable[[dict[str, Any]], None]] = [
        step_intake,
        step_replay,
        step_today,
        step_landed,
        step_settle,
        step_optimise,
        step_book,
    ]
    for step in steps:
        try:
            step(ctx)
        except AssertionError as e:
            failures.append(f"{step.__name__}: {e}")
    OUT.save("98_figures", ctx["figures"])
    OUT.save("99_failures", failures)
    assert not failures, failures


def landed_yaml() -> dict[str, Any]:
    """landed_hand/mh.yaml as the hand figures make it (the file a later test reads)."""
    doc: dict[str, Any] = {
        "state": "MH",
        "discom": "msedcl",
        "category": "HT I (A)",
        "voltage_kv": "22",
        "contract_demand_kva": "2000",
        "as_of": AS_OF,
        "month": "2026-11",
        "slot": "all",
        # what the landed-cost calculator is given (Stage 19 test_calculator_hand)
        "calculator": calculator_input(PLANT, PPA, EQUITY),
        "tod_slot": "all (the same in every slot)",
        "sources": {
            "generation": "PPA tariff stated (solar 3.10, wind 3.60); for a captive plant its levelised cost from the plant figures stated (capital, O&M, CUF, degradation, 25 years) at a 10% cost of capital",
            "injection_charges": f"wheeling charges (HT) 0.81 Rs/unit FY 26-27: MERC Case 75/2025 ({CASE75}) p. 83, Table 9 row E; NOT in the approved rule book",
            "losses": "transmission loss 3.26% and wheeling loss 7.50% at HT: Case 75/2025 p. 106 (columns TL, WL); NOT in the approved rule book",
            "drawal_charges": "cross-subsidy surcharge 2.11 Rs./kWh, HT I (A) (i) at 33/22/11 kV: Case 75/2025 p. 106; nil for captive and 26%-equity users (Electricity Act s. 42(2), fourth proviso)",
            "banking_upper_bound": "8% in kind on energy banked (approved rule MSEDCL settlement v4, a register field; the order does not state it), as if every unit were banked: 8/92 of the generation, injection charges and losses (the drawal charges fall on units drawn, so are not grossed up)",
            "duty_assumed": "electricity duty at the twelve bills' average per kWh (7.5% of the bill's charges); in no order; an assumption",
            "equity_cost": "26% of the plant's capital x capital recovery factor (10%, 25 years) / average yearly kWh per kW",
            "not_stated_in_the_corpus": "intra-state transmission charge (MSETCL tariff order); additional surcharge (MERC's order on MSEDCL's additional surcharge)",
        },
        "routes": [],
    }
    for key, v in hand_landed().items():
        route, source = key.split(":")
        doc["routes"].append(route_entry(route, source, v["approved"], v["order"]))
    return doc
