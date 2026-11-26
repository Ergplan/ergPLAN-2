"""Stage 17, Punjab: PSPCL Large Supply General Industry above 1000 kVA, 11 kV, contract demand
2,000 kVA, a single-shift plant. Twelve bills (April 2026 to March 2027) worked by hand from PSERC's
FY 2026-27 tariff order for PSPCL, then every step of the product through the API, asserted against
those hand figures. Set PB_OUT=<folder> to also write every response there."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import ROUND_HALF_UP
from decimal import Decimal as D
from pathlib import Path

import pytest
import yaml

from tests.e2e import assumed_state as st
from tests.e2e.assumed_state import HandBill, Line, Run, dec, q2, q4

pytestmark = pytest.mark.slow

# PSERC Tariff Order FY 2026-27 for PSPCL (order of 06.03.2026). The stored order is document
# 8fc3d382-3fde-5496-9246-411738181d7e (PDF pages as the approved rules cite them); the figures were read
# from the Commission's own chapter files of the same order (pserc.punjab.gov.in/pages/, Chapter 6 and
# Annexures I and II), whose printed page is two more than the stored PDF's page. Printed pages in brackets.
FIXED = D(280)  # p.225 [227] Table 6.2 3(c) and Annexure II SI.3.1 (ii) [253]: above 1000 kVA, Rs/kVA/month
ENERGY = D("6.00")  # same rows, Rs/kVAh
FIXED_ON_PCT = D("0.80")  # p.226 [228] note * and Annexure I 9(a) [244]: 80% of CD or actual demand (≤ CD)
TOD_NIGHT_APR_JUN15 = D("-0.50")  # Annexure I condition 15 [246]: 1 Apr-15 Jun, 22:00-06:00
TOD_PEAK_JUN16_OCT15 = D("2.00")  # condition 15 [246]: 16 Jun-15 Oct, 18:00-22:00
TOD_NIGHT_OCT16_MAR = D("-1.00")  # condition 15 [246]: 16 Oct-31 Mar, 22:00-06:00
TOD_FLOOR = D("5.50")  # condition 15 [246]: cumulative ToD rebate limited to an energy charge of Rs 5.50/kVAh
CSS = D("0.86")  # p.239 [241] 6.10.3, Large Supply, paise held in rupees
WHEEL_IN_KIND_PCT = D(2)  # p.238 [240] 6.10.1, NRSE power within the State: 2% of the energy injected
TRANS_LOSS_PCT = D("2.19")  # p.239 [241] 6.10.2 (i)-(iii)
DIST_LOSS_11KV_PCT = D("4.00")  # p.239 [241] 6.10.2 (iii), 40% of 10.00%, at 11 kV
# Not in the order as a figure: FPPAS (condition 8 [244]: "as per" the 2025 Regulations), electricity
# duty and cesses (condition 2 [243]: "exclusive of"); the hand bills carry neither.

CD, KV, PF = D(2000), "11", D("0.97")
SLOTS = [("06:00-18:00", D("0.81")), ("18:00-22:00", D("0.07")), ("22:00-06:00", D("0.12"))]
MONTHS = [  # supply month, kWh, recorded maximum demand (kVA)
    (date(2026, 4, 1), D(472000), D(1720)), (date(2026, 5, 1), D(495000), D(1810)),
    (date(2026, 6, 1), D(488000), D(1790)), (date(2026, 7, 1), D(461000), D(1650)),
    (date(2026, 8, 1), D(452000), D(1580)), (date(2026, 9, 1), D(458000), D(1610)),
    (date(2026, 10, 1), D(476000), D(1700)), (date(2026, 11, 1), D(455000), D(1560)),
    (date(2026, 12, 1), D(463000), D(1540)), (date(2027, 1, 1), D(449000), D(1520)),
    (date(2027, 2, 1), D(431000), D(1590)), (date(2027, 3, 1), D(470000), D(1690)),
]  # fmt: skip


def rupee(x: D) -> D:
    """Condition 20 [249]: every charge rounded to the nearest rupee, 50 paise and above up."""
    return x.quantize(D(1), rounding=ROUND_HALF_UP)


def tod_lines(first: date, by: dict[str, D]) -> list[Line]:
    """Condition 15 by the days of the month in each of its periods (energy spread evenly over the
    month's days: an assumption, the bill's meter splits it by date)."""
    days = calendar.monthrange(first.year, first.month)[1]
    night, evening = by["22:00-06:00"], by["18:00-22:00"]

    def floor(delta: D) -> D:  # the rebate is limited so the energy charge stays at Rs 5.50
        return max(delta, TOD_FLOOR - ENERGY)

    m = first.month
    if m in (4, 5):
        parts = [("night", night, floor(TOD_NIGHT_APR_JUN15))]
    elif m == 6:
        parts = [
            ("night", night * 15 / days, floor(TOD_NIGHT_APR_JUN15)),
            ("peak", evening * 15 / days, TOD_PEAK_JUN16_OCT15),
        ]
    elif m in (7, 8, 9):
        parts = [("peak", evening, TOD_PEAK_JUN16_OCT15)]
    elif m == 10:
        parts = [
            ("peak", evening * 15 / days, TOD_PEAK_JUN16_OCT15),
            ("night", night * 16 / days, floor(TOD_NIGHT_OCT16_MAR)),
        ]
    else:
        parts = [("night", night, floor(TOD_NIGHT_OCT16_MAR))]
    out = []
    for kind, units, rate in parts:
        u = units.quantize(D(1))
        if kind == "peak":
            out.append(Line("tod_surcharge", "TOD SURCHARGE 18-22 HRS", u, rate, rupee(u * rate)))
        else:
            out.append(Line("tod_rebate", "TOD REBATE 22-06 HRS", u, rate, rupee(u * rate)))
    return out


def hand_bill(first: date, kwh: D, md: D) -> HandBill:
    kvah = (kwh / PF).quantize(D(1))
    tod, left = [], kwh
    for i, (name, share) in enumerate(SLOTS):
        k = left if i == len(SLOTS) - 1 else (kwh * share).quantize(D(1))
        tod.append((name, k))
        left -= k
    by_kvah = {name: kvah * share for name, share in SLOTS}  # the kVAh in each window, at the month's PF
    billed_kva = max(CD * FIXED_ON_PCT, min(md, CD))
    h = HandBill(first, kwh, kvah, tod, md, CD, "LS GENERAL INDUSTRY")
    h.lines = [
        Line("demand_charge", "FIXED CHARGES", billed_kva, FIXED, rupee(billed_kva * FIXED)),
        Line("energy_charge", "ENERGY CHARGES", kvah, ENERGY, rupee(kvah * ENERGY)),
        *tod_lines(first, by_kvah),
    ]
    return h


BILLS = [hand_bill(m, e, md) for m, e, md in MONTHS]
KVAH_PER_KWH = sum((h.kvah / h.kwh for h in BILLS), D(0)) / len(BILLS)  # the bills' own ratio
AS_OF = "2026-11-01"
BANK_CHARGE_PCT = D(10)  # register (State Regulation Master, Punjab, version 12): in kind, no citation
PRICE = {"solar": D("3.10"), "wind": D("3.60")}  # stated PPA tariffs (the app's benchmarks)
EQUITY = D("0.25")  # stated group-captive equity cost, Rs/kWh
KIND = {"solar": "oa_solar_intra", "wind": "oa_wind_intra"}
KEEP = (1 - WHEEL_IN_KIND_PCT / 100) * (1 - TRANS_LOSS_PCT / 100) * (1 - DIST_LOSS_11KV_PCT / 100)


def hand_landed(route: str, source: str, own_cost: D | None = None) -> dict[str, D]:
    """Landed cost per kWh at the meter: the energy lost in kind (2% wheeling, 2.19% transmission,
    4.00% distribution at 11 kV, one after the other) grosses up the price; the banking charge in kind
    (10%) as its upper bound, every unit banked, on the price and the losses (the energy it takes is
    generated and carried, never drawn); CSS on the units drawn, third party only."""
    gen = own_cost if route == "captive" else PRICE[source]
    assert gen is not None
    carried = gen / KEEP
    lines = {
        "generation": gen,
        "losses_in_kind": carried - gen,
        "banking_upper_bound": carried * (1 / (1 - BANK_CHARGE_PCT / 100) - 1),
        "css": CSS if route == "third_party" else D(0),  # captive and group captive exempt (s. 42(2))
        "equity": EQUITY if route == "group_captive" else D(0),
    }
    lines["total"] = sum(lines.values(), D(0))
    return lines


def pb_category(i: int) -> str:
    """The register's ToD categories for Punjab: off-peak 22:00-06:00, normal 06:00-22:00, every month."""
    h = (i % 96) // 4
    return "off_peak" if h < 6 or h >= 22 else "normal"


def grid_bill(month_slots: dict[str, dict[str, D]], month_peak: dict[str, D]) -> dict[str, D]:
    """The year's grid bill from a dispatch file by the APPROVED schedule (kVAh at the bills' ratio,
    Rs 6.00/kVAh, Rs 280/kVA on 80% of contract demand or the recorded demand); the order's ToD is
    worked apart (`tod_by_order`), the approved rule book has no ToD rates (rule gap)."""
    energy = demand = D(0)
    for m, slots in month_slots.items():
        kvah = sum((v for k, v in slots.items() if not k.startswith("__")), D(0)) * KVAH_PER_KWH
        energy += kvah * ENERGY
        demand += FIXED * max(CD * FIXED_ON_PCT, min(month_peak[m] * KVAH_PER_KWH, CD))
    top = (
        (ENERGY + TOD_PEAK_JUN16_OCT15) * KVAH_PER_KWH,
        FIXED * KVAH_PER_KWH,
    )  # dearest, for print precision
    return {"energy": energy, "demand": demand, "max_rate": top[0], "max_demand_rate": top[1]}


def tod_adder(day: date, hour: int) -> D:
    """Condition 15 [246], Rs/kVAh, with the Rs 5.50 floor on rebates."""
    after_jun15, after_oct15 = (day.month, day.day) >= (6, 16), (day.month, day.day) >= (10, 16)
    night, evening = hour >= 22 or hour < 6, 18 <= hour < 22
    if day.month in (1, 2, 3) or after_oct15:
        return max(TOD_NIGHT_OCT16_MAR, TOD_FLOOR - ENERGY) if night else D(0)
    if after_jun15:
        return TOD_PEAK_JUN16_OCT15 if evening else D(0)
    return max(TOD_NIGHT_APR_JUN15, TOD_FLOOR - ENERGY) if night else D(0)


def tod_by_order(rows: list[dict[str, str]], run: dict[str, object]) -> str:
    """The order's ToD on the grid energy of a dispatch file: what the approved rule book leaves out."""
    total = sum(
        (
            D(r["grid_kwh"])
            * KVAH_PER_KWH
            * tod_adder(date.fromisoformat(r["time"][:10]), int(r["time"][11:13]))
            for r in rows
        ),
        D(0),
    )
    return str(q2(total))


def test_pb_full_run() -> None:
    run = Run("PB_OUT")
    found: dict[str, object] = {}
    run.make(
        BILLS,
        "pspcl",
        "PUNJAB STATE POWER CORPORATION LIMITED",
        "Punjab",
        {"voltage_kv": KV, "industry_shape": "single_shift", "weekly_off": [6]},
    )

    # 1. bill replay: fixed charges agree with every hand bill; energy agrees on the base rate, and the
    # whole ToD of condition 15 is the difference (rule gap: the approved ToD rule has no rates)
    rep = st.replay_by_group(run.get("03_replay", f"/connections/{run.conn}/replay"))
    assert sorted(rep) == [m for m, _, _ in MONTHS]
    tod_gap = {}
    for h in BILLS:
        g = rep[h.first]
        fixed = h.amount("demand_charge")
        assert g["demand"] == (fixed, fixed, "match"), (h.first, g["demand"])
        tod = h.amount("tod_surcharge") + h.amount("tod_rebate")
        billed, computed, status = g["energy"]
        assert billed == h.amount("energy_charge") + tod, (h.first, billed)
        assert computed == h.kvah * ENERGY, (h.first, computed)  # the approved Rs 6.00/kVAh, unrounded
        assert abs(billed - computed - tod) < 1, (h.first, billed, computed, tod)  # condition 20 rounding
        assert status == ("match" if abs(tod) < 1 else "unexplained"), (h.first, status)
        tod_gap[str(h.first)] = tod
    found["replay_tod_gap"] = tod_gap

    # 2. today's cost: the approved tariff on the profile; the order's ToD is not in it (rule gap)
    today = run.get("07_today", f"/connections/{run.conn}/today")
    base_energy = sum((h.kvah * ENERGY for h in BILLS), D(0))
    hand_fixed = sum((h.amount("demand_charge") for h in BILLS), D(0))
    assert abs(dec(today["energy"]) - base_energy) <= 1, (today["energy"]["value"], base_energy)
    assert abs(dec(today["demand"]) - hand_fixed) <= 1, (today["demand"]["value"], hand_fixed)
    assert today["fppca"] is None and today["electricity_duty"] is None  # neither is on the bills
    found["today"] = {
        "energy_approved": base_energy,
        "fixed": hand_fixed,
        "tod_by_order": sum(tod_gap.values(), D(0)),
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
            by = got["lines"]
            loss = next(v for k, v in by.items() if k.startswith("Losses and charges in kind"))
            assert q4(dec(loss)) == q4(hand["losses_in_kind"])
            bank = next(v for k, v in by.items() if k.startswith("Banking charge"))
            assert q4(dec(bank)) == q4(hand["banking_upper_bound"])  # fixed in Stage 17 (CSS not grossed up)
            landed_out.append({"route": route, "source": s, **{k: q4(v) for k, v in hand.items()}})
    found["landed"] = landed_out
    run.save("08_landed_hand", landed_out)

    # 4. settlement after banking: set-off on each month's ToD-category totals, a monthly bank with a
    # 10% charge in kind, no cap, a normal-slot bank serving the off-peak, the rest lapsing; by hand
    load = run.profile_kw()
    inj = st.injection(D(3000), len(load))
    hand = st.hand_settle(load, inj, KEEP, pb_category, "tod", True, None, BANK_CHARGE_PCT)
    for own in ("captive", "third_party"):
        app = st.settle(run, f"09_settle_{own}", "oa_solar_intra", own, inj)
        assert app["rule"] == "Banking and settlement · PB · solar, wind and hybrid, intra-state"
        assert app["granularity"] == "tod_slot_monthly"
        assert q4(dec(app["sources"][0]["delivered_share"])) == q4(KEEP)
        bad = st.compare_settlement(app, hand)
        assert not bad, bad[:6]
    st.assert_balance(hand)
    found["settlement"] = {str(m): {k: q2(v) for k, v in h.items()} for m, h in hand.items()}

    # 5. the optimizer: five components, hourly and 15 minutes; the grid bill from the dispatch file
    # ToD-slot set-off: the balance nets each month's off-peak and normal totals, as the rule does
    cell = lambda r: (r["time"][:7], r["slot"].startswith("Off-Peak"))  # noqa: E731
    found["runs"] = st.optimiser_checks(run, grid_bill, tod_by_order, cell)

    # 6. the rule book for November 2026: it carries the landed costs worked above
    b = run.post(
        "19_book",
        "/statebooks/generate",
        json={"discom": "pspcl", "month": "2026-11-01", "connection_id": run.conn},
    )
    text = run.get("21_book.md", f"/statebooks/{b['id']}/file")
    for s in ("solar", "wind"):  # the book shows the banking charge apart, on banked units only
        tp = hand_landed("third_party", s)
        assert f"**{tp['total'] - tp['banking_upper_bound']:.2f}**" in text, s
        assert f"on banked units only | {tp['banking_upper_bound']:.2f} " in text, s
    assert "0.86" in text
    run.save("99_found", found)

    # 7. the hand-worked landed costs Stage 19's calculator test reads are these
    data = yaml.safe_load((Path(__file__).parent / "landed_hand" / "pb.yaml").read_text())
    for row in landed_out:
        saved = next(x for x in data["routes"] if x["route"] == row["route"] and x["source"] == row["source"])
        assert D(str(saved["total"]["value"])) == row["total"], (row, saved["total"])
