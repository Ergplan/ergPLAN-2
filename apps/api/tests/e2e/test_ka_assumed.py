"""Stage 17, Karnataka: twelve bills of a BESCOM HT-2(a) industry worked by hand from KERC's Tariff
Order 2025, then every step of the product through the API (bill replay, today's cost, landed cost on
each route, settlement after banking, the optimizer at hourly and 15-minute blocks, the grid bill
from the dispatch file, replay, the month's rule book), each checked against the hand figures.

Where the app differs from the order because the approved rule book lacks a rule (a rule gap, listed
in docs/stage-reports/multi-state-test.md), the assertion holds the app's figure with the gap named
beside it and the order's own figure is worked too. Set KA_OUT to a folder to keep every answer."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP
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
OUT = Out("KA_OUT")
Q = D("0.01")
LANDED_FILE = Path(__file__).parent / "landed_hand" / "ka.yaml"


def r2(x: D) -> D:
    return x.quantize(Q, rounding=ROUND_HALF_UP)


# ---- Constants typed from the order (document, page, table) ------------------------------------
# KERC Tariff Order 2025 for the ESCOMs, 27 March 2025 (document 8e00613e-3b46-50c8-b8c5-a475bb79196c;
# PDF pages, the printed page in brackets):
ORDER = "8e00613e-3b46-50c8-b8c5-a475bb79196c"
# p. 566 (Annexure-9 p. 550), "TARIFF SCHEDULE HT-2(a)", FY2026-27 column: demand charges Rs.350/- per
# kVA / month of billing demand; energy charges 660 paise per kWh.
DEMAND = D("350")
ENERGY = D("6.60")
# p. 568 (552), "ToD Tariff : HT-2(a) ...": from December to June 06-09 (+)100, 09-18 0, 18-22 (+)100,
# 22-06 (-)100 paise/unit; from July to November (monsoon period) 0 in every slot.
TOD = {"06:00-09:00": D("1.00"), "09:00-18:00": D("0"), "18:00-22:00": D("1.00"), "22:00-06:00": D("-1.00")}
TOD_MONTHS = {12, 1, 2, 3, 4, 5, 6}
# p. 561 (545), "CONDITIONS APPLICABLE TO BILLING OF HT INSTALLATIONS", 1.A: the billing demand is the
# maximum demand recorded in the month or 90% of the CD, whichever is higher; 1.D: rounded up to the
# next kVA. 2.(i): below 0.90 lag, 3 paise per unit per 0.01 (this plant runs at 0.96: none).
# p. 562 (546), 3: the high-voltage rebate is for supply above 13.2 kV (this plant is at 11 kV: none).
BD_FLOOR_CD_PCT = D("90")
# p. 249 (233), "For using BESCOM's network only", FY 27, injection HT, drawal HT: 29 paise per unit,
# applicable losses 2.76%.
WHEELING, WHEELING_LOSS = D("0.29"), D("2.76")
# p. 260 (244), "common CSS for all the ESCOMs", approved for FY2026-27, HT-2a at HT level: 186 paise.
CSS = D("1.86")
# p. 266 (250): "till such time BESCOM files the petition and the Commission passes orders, ASC shall
# not be levied."
ASC = D("0")
# p. 252 (236), 6.11: banking charges "as specified in the separate Regulations / Orders": not in the
# corpus. The approved rule (KA settlement v1, register): monthly, 8% in kind, drawn from a dearer ToD
# category into the same or a cheaper one, what is not drawn lapses.
BANK_CHARGE = D("8")
DRAW = {"peak": {"peak", "normal", "off_peak"}, "normal": {"normal", "off_peak"}, "off_peak": {"off_peak"}}
SINKS = ["peak", "normal", "off_peak"]
# Levies the bills carry that the order leaves to the State Government (p. 539 (523), general
# condition 5: "subject to levy of Tax and Surcharges thereon as may be decided by the State
# Government"): electricity tax as a percentage of the charges, and the ESCOM's FAC per kWh. Rates as
# a BESCOM bill would print them; the assumption of this test, not a regulation's figure.
TAX_PCT = D("9")
FAC = D("0.15")

# ---- The consumer ----------------------------------------------------------------------------------
# A continuous-process plant (round the clock, all week) on BESCOM's 11 kV network, contract demand
# 3,000 kVA. Shares of the month's kWh in the order's ToD slots from the 24 x 7 working (hours in the
# slot over 24, less the shift-change dips at 06:00, 14:00 and 22:00).
CD = D("3000")
SLOTS = [
    ("06:00-09:00", D("0.124")),
    ("09:00-18:00", D("0.375")),
    ("18:00-22:00", D("0.167")),
    ("22:00-06:00", D("0.334")),
]
PF = D("0.96")  # kWh / kVAh
# supply month → (kWh, recorded maximum demand kVA): below 90% of contract demand (2,700 kVA) in ten
# months, above it in May and March
MONTHS = [
    (date(2026, 4, 1), D("1636000"), D("2580")),
    (date(2026, 5, 1), D("1712000"), D("2760")),
    (date(2026, 6, 1), D("1648000"), D("2610")),
    (date(2026, 7, 1), D("1655000"), D("2600")),
    (date(2026, 8, 1), D("1661000"), D("2590")),
    (date(2026, 9, 1), D("1594000"), D("2570")),
    (date(2026, 10, 1), D("1650000"), D("2600")),
    (date(2026, 11, 1), D("1588000"), D("2560")),
    (date(2026, 12, 1), D("1640000"), D("2580")),
    (date(2027, 1, 1), D("1652000"), D("2600")),
    (date(2027, 2, 1), D("1498000"), D("2590")),
    (date(2027, 3, 1), D("1702000"), D("2720")),
]
AS_OF = "2026-11-01"


def hand_bills() -> list[HandBill]:
    """The twelve bills worked by hand from the order above, independent of the app."""
    out: list[HandBill] = []
    for first, kwh, md in MONTHS:
        kvah = (kwh / PF).quantize(D("1"))
        tod = [(name, (kwh * share).quantize(D("1"))) for name, share in SLOTS[:-1]]
        tod.append((SLOTS[-1][0], kwh - sum((k for _, k in tod), D(0))))
        bd = max(md, CD * BD_FLOOR_CD_PCT / 100).to_integral_value(rounding=ROUND_CEILING)
        lines = [
            Line("demand_charge", "DEMAND CHARGES", bd, DEMAND, "kVA", r2(bd * DEMAND)),
            Line("energy_charge", "ENERGY CHARGES", kwh, ENERGY, "kWh", r2(kwh * ENERGY)),
        ]
        if first.month in TOD_MONTHS:
            on = dict(tod)
            sur = on["06:00-09:00"] + on["18:00-22:00"]
            reb = on["22:00-06:00"]
            lines.append(
                Line(
                    "tod_surcharge",
                    "TOD CHARGES",
                    sur,
                    TOD["06:00-09:00"],
                    "kWh",
                    r2(sur * TOD["06:00-09:00"]),
                )
            )
            lines.append(
                Line("tod_rebate", "TOD REBATE", reb, TOD["22:00-06:00"], "kWh", r2(reb * TOD["22:00-06:00"]))
            )
        base = sum((ln.amount for ln in lines), D(0))
        lines.append(Line("fppca", "FAC", kwh, FAC, "kWh", r2(kwh * FAC)))
        lines.append(
            Line("electricity_duty", "ELECTRICITY TAX", base, TAX_PCT, "%", r2(base * TAX_PCT / 100))
        )
        out.append(HandBill(first, kwh, kvah, md, CD, tod, lines))
    return out


BILLS = hand_bills()
KWH_YEAR = sum((h.kwh for h in BILLS), D(0))
# the levies as the twelve bills average them per kWh billed, a rate held to four places by the
# project's rule for rates (core/money.py)
DUTY_PER_KWH = (sum((h.amount("electricity_duty") for h in BILLS), D(0)) / KWH_YEAR).quantize(D("0.0001"))
FAC_PER_KWH = (sum((h.amount("fppca") for h in BILLS), D(0)) / KWH_YEAR).quantize(D("0.0001"))
KVAH_PER_KWH = sum((h.kvah / h.kwh for h in BILLS), D(0)) / len(BILLS)
FLOOR = CD * BD_FLOOR_CD_PCT / 100


def floor_gap(h: HandBill) -> D:
    """What the order's 90% floor adds to the month's demand charge over the recorded demand: the
    approved schedule holds no floor (gap KA-1)."""
    return h.amount("demand_charge") - DEMAND * h.max_kva


# the plant figures given for the captive and 26%-equity routes, and the cost of capital the
# landed-cost card assumes (10%)
PLANT = {
    "solar": {"capex": D("42000"), "om": D("1.5"), "cuf": D("20"), "deg": D("0.5"), "life": 25},
    "wind": {"capex": D("68000"), "om": D("2"), "cuf": D("30"), "deg": D("0"), "life": 25},
}
PPA = {"solar": D("3.10"), "wind": D("3.60")}
DISCOUNT = D("10")
EQUITY = D("26")
KIND = {"solar": "oa_solar_intra", "wind": "oa_wind_intra"}


def hand_landed() -> dict[str, dict[str, Any]]:
    """Landed cost per kWh at the meter, by route and source, worked by hand, in November (the monsoon
    months carry no ToD, so every slot is the same). The approved rule book carries every figure the
    order states for a plant injecting at HT in BESCOM's own network, so `approved` and `order` agree."""
    out: dict[str, dict[str, Any]] = {}
    keep = 1 - WHEELING_LOSS / 100
    for source in ("solar", "wind"):
        p = PLANT[source]
        f = crf(DISCOUNT, p["life"])
        e = energy_per_kw(p["cuf"], p["deg"], p["life"])
        # a rate keeps four places (core/money.py): the levelised cost and the equity cost are stated so
        lcoe = (p["capex"] * (f + p["om"] / 100) / e).quantize(D("0.0001"))
        equity = p["capex"] * EQUITY / 100 * f / e
        for route in ("captive", "group_captive", "third_party"):
            gen = lcoe if route == "captive" else PPA[source]
            drawal = CSS + ASC if route == "third_party" else D(0)  # s. 42(2), 42(4): captive pays neither
            eq = equity if route == "group_captive" else D(0)
            lines = landed_lines(
                generation=gen,
                injection=WHEELING,
                keep=keep,
                drawal=drawal,
                bank_pct=BANK_CHARGE,
                duty=DUTY_PER_KWH,
                equity=eq,
            )
            out[f"{route}:{source}"] = {"approved": lines, "order": lines}
    return out


# ---- steps ------------------------------------------------------------------------------------------


def step_intake(ctx: dict[str, Any]) -> None:
    si = ctx["intake"]
    assert si["discom"]["value"] == "bescom" and si["category"]["value"] == "HT-2(a)"
    assert si.get("open_access") is None
    assert [m["month"] for m in si["months"]] == [m.isoformat() for m, _, _ in MONTHS]
    for m, h in zip(si["months"], BILLS, strict=True):
        assert D(m["energy"]["value"]) == h.kwh
        assert [(t["start"], t["end"]) for t in m["tod"]] == [tuple(name.split("-")) for name, _ in h.tod]
        assert D(m["power_factor"]["value"]).quantize(D("0.0001")) == (h.kwh / h.kvah).quantize(D("0.0001"))


def step_replay(ctx: dict[str, Any]) -> None:
    by_month = {r["evaluated_on"][:7]: r for r in ctx["replay"]}
    assert len(by_month) == 12
    for h in BILLS:
        r = by_month[f"{h.first:%Y-%m}"]
        lines = {ln["group"]: ln for ln in r["lines"]}
        assert sv(r["billed_total"]) == h.total, h.first
        energy = h.amount("energy_charge") + h.amount("tod_rebate") + h.amount("tod_surcharge")
        assert sv(lines["energy"]["billed"]) == energy
        assert abs(sv(lines["energy"]["computed"]) - energy) < 1, (h.first, lines["energy"])
        assert lines["energy"]["status"] == "match"
        # demand: the order's floor of 90% of contract demand (p. 561, 1.A) is not in the approved
        # schedule (gap KA-1): below 2,700 kVA the app bills the recorded demand
        if h.max_kva >= FLOOR:
            assert sv(lines["demand"]["computed"]) == h.amount("demand_charge")
        else:
            assert sv(lines["demand"]["computed"]) == DEMAND * h.max_kva  # known wrong: gap KA-1
            assert lines["demand"]["gap"] == "billing_demand_rule"
        for group, gap, amount in (
            ("fppca", "fppca_monthly", h.amount("fppca")),
            ("electricity_duty", "electricity_duty", h.amount("electricity_duty")),
        ):
            assert lines[group]["computed"] is None and lines[group]["gap"] == gap
            assert sv(lines[group]["billed"]) == amount
        outside = h.amount("fppca") + h.amount("electricity_duty")
        assert abs(sv(r["total_difference"]) + outside + floor_gap(h)) < 1, (h.first, r["total_difference"])


def step_today(ctx: dict[str, Any]) -> None:
    t = ctx["today"]
    hand = {
        c: sum((h.amount(c) for h in BILLS), D(0))
        for c in (
            "demand_charge",
            "energy_charge",
            "tod_rebate",
            "tod_surcharge",
            "fppca",
            "electricity_duty",
        )
    }
    energy = hand["energy_charge"] + hand["tod_rebate"] + hand["tod_surcharge"]
    assert abs(sv(t["energy"]) - energy) <= 12, (t["energy"]["value"], energy)
    gap = sum((floor_gap(h) for h in BILLS), D(0))
    assert abs(sv(t["demand"]) - (hand["demand_charge"] - gap)) <= 1, (
        t["demand"]["value"],
        hand["demand_charge"],
        gap,
    )
    # FAC and tax at the bills' average per kWh, a rate held to four places (core/money.py): within a
    # rupee of that rate on the year's kWh
    for code in ("fppca", "electricity_duty"):
        rate4 = (hand[code] / KWH_YEAR).quantize(D("0.0001"))
        assert abs(sv(t[code]) - rate4 * KWH_YEAR) <= 1, (code, t[code]["value"], rate4 * KWH_YEAR)
        ctx["figures"][f"today_{code}_vs_bills"] = str(sv(t[code]) - hand[code])
    parts = sum((sv(t[k]) for k in ("energy", "demand", "fppca", "electricity_duty")), D(0))
    assert abs(sv(t["annual"]) - parts) <= 2, (t["annual"]["value"], parts)
    assert sv(t["kwh"]) == KWH_YEAR
    ctx["figures"]["today_vs_bills"] = {
        "bills": str(sum(hand.values(), D(0))),
        "app": t["annual"]["value"],
        "floor_left_out": str(gap),
    }


def step_landed(ctx: dict[str, Any]) -> None:
    hand = hand_landed()
    for route in ("captive", "group_captive", "third_party"):
        sources: dict[str, Any] = {}
        for source in ("solar", "wind"):
            p = PLANT[source]
            s: dict[str, Any] = {"ownership": route}
            if route == "captive":
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
                s.update(
                    capex_inr_per_kw=str(p["capex"]),
                    cuf_pct=str(p["cuf"]),
                    degradation_pct=str(p["deg"]),
                    life_years=p["life"],
                    equity_share_pct=str(EQUITY),
                )
            sources[KIND[source]] = s
        sc = {"as_of": AS_OF, "month": 11, "sources": sources}
        out = OUT.get(
            f"08_landed_{route}",
            f"/connections/{ctx['conn']}/landed-cost",
            params={"scenario": json.dumps(sc)},
        )
        for source in ("solar", "wind"):
            card = next(x for x in out["sources"] if x["id"] == KIND[source])
            h = hand[f"{route}:{source}"]["approved"]
            assert card["available"], card
            # BESCOM's wheeling and HT loss are in; the KPTCL transmission loss is not (gap KA-5: it
            # applies only to a plant injecting into the transmission network, p. 250, 6.10.7)
            assert "oa_wheeling" not in card["gaps"] and "oa_losses" in card["gaps"], card["gaps"]
            for slot in card["by_slot"]:
                total = sv(slot["total"])
                assert abs(total - h["total"]) < D("0.0001"), (route, source, slot["slot"], total, h)
                assert abs(sum((sv(x) for x in slot["lines"]), D(0)) - total) < D("0.000001")
            ctx["figures"][f"landed:{route}:{source}"] = str(total)


def slot_of_hour(month: int, h: int) -> str:
    """The ToD category of an hour (p. 568): December to June 06-09 and 18-22 peak (+100 paise),
    22-06 off-peak (-100), 09-18 normal; July to November one slot (no ToD)."""
    if month not in TOD_MONTHS:
        return "normal"
    return "peak" if 6 <= h < 9 or 18 <= h < 22 else "normal" if 9 <= h < 18 else "off_peak"


def _cons_by_category(h: HandBill) -> dict[str, D]:
    out: dict[str, D] = {}
    for name, kwh in h.tod:
        c = slot_of_hour(h.first.month, int(name[:2]))
        out[c] = out.get(c, D(0)) + kwh
    return out


def hand_year(curve: str, kw: D) -> dict[date, Any]:
    """The year's settlement by hand: the bills' ToD kWh against the normative curve's energy in each
    ToD category, delivered after BESCOM's HT loss of 2.76% (p. 249), banked without a limit, 8% in
    kind, drawn from a dearer category into the same or a cheaper one, lapsing at the month's end."""
    keep = 1 - WHEELING_LOSS / 100
    gen = curve_energy(curve, kw, date(2026, 4, 1), slot_of_hour)
    out = {}
    for h in BILLS:
        cons = _cons_by_category(h)
        g = {c: gen.get((h.first, c), D(0)) * keep for c in cons}
        out[h.first] = hand_settle(
            cons, g, charge_pct=BANK_CHARGE, limit=None, may_draw=DRAW, sink_order=SINKS
        )
    return out


KEYS = ("setoff", "banked_before", "charge", "withdrawn", "lapsed", "unbanked", "grid")


def _year(hand: dict[date, Any]) -> dict[str, str]:
    return {k: str(sum((getattr(h, k) for h in hand.values()), D(0)).quantize(D("1"))) for k in KEYS}


def _settle(conn: str, name: str, kind: str, mw: str, own: str) -> dict[str, Any]:
    body = {"sources": [{"id": "s", "kind": kind, "capacity_mw": mw, "ownership": own}], "as_of": AS_OF}
    return dict(OUT.post(name, f"/connections/{conn}/settle", json=body))


def step_settle(ctx: dict[str, Any]) -> None:
    conn = ctx["conn"]
    solar = _settle(conn, "09_settle_solar_captive", "oa_solar_intra", "4", "captive")
    assert D(solar["sources"][0]["delivered_share"]) == 1 - WHEELING_LOSS / 100
    assert solar["granularity"] == "tod_slot_monthly"
    hs = hand_year("solar_ka", D(4000))
    check_settle(solar, hs)
    third = _settle(conn, "09_settle_solar_third_party", "oa_solar_intra", "4", "third_party")
    assert third["months"] == solar["months"]
    wind = _settle(conn, "09_settle_wind_captive", "oa_wind_intra", "3", "captive")
    hw = hand_year("wind_ka", D(3000))
    check_settle(wind, hw)
    ctx["figures"]["settle_solar_4MW"] = _year(hs)
    ctx["figures"]["settle_wind_3MW"] = _year(hw)


FIVE = [
    {"kind": "rooftop_pv", "max_mw": "0.8"},
    {"kind": "oa_solar_intra", "ownership": "captive", "max_mw": "5"},
    {"kind": "oa_wind_intra", "ownership": "captive", "max_mw": "4"},
    {"kind": "btm_bess", "max_mw": "1"},
    {"kind": "oa_bess", "max_mw": "1"},
]


def grid_rate(month: int, slot: str) -> D:
    """INR per kWh bought from BESCOM in a month and slot (pp. 566, 568): energy charge with the ToD
    adjustment of the slot's hours, FAC and electricity tax at the bills' rates per kWh."""
    hours = slot[-11:]
    delta = TOD.get(hours, D(0)) if month in TOD_MONTHS else D(0)
    return ENERGY + delta + FAC_PER_KWH + DUTY_PER_KWH


def _grid_bill(ctx: dict[str, Any], run: dict[str, Any], minutes: int, name: str) -> None:
    d = client.get(f"/runs/{run['id']}/dispatch.csv", headers=AN)
    assert d.status_code == 200
    OUT.text(f"14_dispatch_{name}.csv", d.text)
    rows = dispatch_rows(d.text)
    balance_by_hand(rows, lambda month, slot: slot_of_hour(month, int(slot[-11:-9])))
    energy, demand = grid_bill_by_hand(rows, minutes, grid_rate, KVAH_PER_KWH, DEMAND)
    after = {k: D(v) for k, v in run["analyst"]["lines_after"].items()}
    # to the rupee (each line of the run is in whole paise)
    assert abs(energy - after["grid_energy"]) < 1, (energy, after["grid_energy"])
    # demand on the metered peak; the order's 90% floor is not applied (gap KA-1)
    assert abs(demand - after["demand"]) < 1, (demand, after["demand"])
    _, with_floor = grid_bill_by_hand(rows, minutes, grid_rate, KVAH_PER_KWH, DEMAND, FLOOR)
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
        json={"discom": "bescom", "month": "2026-11-01", "connection_id": ctx["conn"]},
    )
    f = client.get(f"/statebooks/{b['id']}/file", headers=AN)
    assert f.status_code == 200
    OUT.text("21_book.md", f.text)
    import re

    # November: no ToD, the grid tariff the same in every slot
    rows = list(re.finditer(r"^\| (Jul-Nov [0-9:\-]+) \| [0-9:–]+ \| ([0-9.]+) ", f.text, re.M))
    assert len(rows) == 4, rows
    for m in rows:
        assert D(m.group(2)) == r2(grid_rate(11, m.group(1))), m.group(0)
    # third party: the book uses the PPA tariffs this test states (its captive routes use the state's
    # normative CUF, not this test's plant figures); its landed cost leaves the banking charge out
    hand = hand_landed()
    for source, title in (
        ("solar", "Open-access solar, intra-state"),
        ("wind", "Open-access wind, intra-state"),
    ):
        section = f.text.split(f"### {title}", 1)[1].split("### ", 1)[0]
        got = re.search(r"^\| Third party \| ([0-9.]+) \|", section, re.M)
        want = hand[f"third_party:{source}"]["approved"]["total_excluding_banking_upper_bound"]
        assert got and D(got.group(1)) == r2(want), (source, got and got.group(1), want)


def test_ka_assumed() -> None:
    assert yaml.safe_load(LANDED_FILE.read_text()) == landed_yaml(), (
        "landed_hand/ka.yaml is not the hand figures"
    )
    OUT.save("00_hand_bills", [{"month": h.first, "total": h.total, "lines": h.lines} for h in BILLS])
    site = make_site(
        OUT,
        BILLS,
        "BANGALORE ELECTRICITY SUPPLY COMPANY LIMITED",
        "HT-2(a) INDUSTRIES",
        {"industry_shape": "continuous_process"},
    )
    conn = site["conn"]
    ctx: dict[str, Any] = {"conn": conn, "site": site["site"], "figures": {}, "intake": site["intake"]}
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
    """landed_hand/ka.yaml as the hand figures make it (the file a later test reads)."""
    doc: dict[str, Any] = {
        "state": "KA",
        "discom": "bescom",
        "category": "HT-2(a)",
        "voltage_kv": "11",
        "contract_demand_kva": "3000",
        "as_of": AS_OF,
        "month": "2026-11",
        "slot": "all",
        # what the landed-cost calculator is given (Stage 19 test_calculator_hand)
        "calculator": calculator_input(PLANT, PPA, EQUITY),
        "tod_slot": "all (July to November carry no ToD; December to June the grid tariff moves by +1/0/+1/-1 Rs, the landed cost does not)",
        "sources": {
            "generation": "PPA tariff stated (solar 3.10, wind 3.60); for a captive plant its levelised cost from the plant figures stated (capital, O&M, CUF, degradation, 25 years) at a 10% cost of capital",
            "injection_charges": f"BESCOM wheeling, injection and drawal at HT, FY 27: 29 paise per unit, KERC Tariff Order 2025 ({ORDER}) p. 249",
            "losses": "BESCOM HT loss 2.76% (the figure in brackets beside it, p. 249), applied to the generation cost and the wheeling charge",
            "drawal_charges": "cross-subsidy surcharge 186 paise, HT-2a at HT, FY2026-27 (p. 260); additional surcharge not levied (p. 266); nil for captive and 26%-equity users (Electricity Act s. 42(2), 42(4))",
            "banking_upper_bound": "8% in kind on energy banked (approved rule KA settlement v1, a register entry; the order leaves banking charges to separate regulations, p. 252, 6.11), as if every unit were banked: 8/92 of the generation, injection charges and losses (the drawal charges fall on units drawn, so are not grossed up)",
            "duty_assumed": "electricity tax at the twelve bills' average per kWh (9% of the bill's charges); in no order; an assumption",
            "equity_cost": "26% of the plant's capital x capital recovery factor (10%, 25 years) / average yearly kWh per kW",
            "not_stated_in_the_corpus": "KPTCL transmission charges and losses, which apply only where the plant injects into the transmission network (p. 250, 6.10.7); banking regulations",
        },
        "routes": [],
    }
    for key, v in hand_landed().items():
        route, source = key.split(":")
        doc["routes"].append(route_entry(route, source, v["approved"], v["order"]))
    return doc
