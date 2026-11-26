"""Stage 17, Gujarat: MGVCL HTP-I, 33 kV, contract demand 4,000 kVA, a continuous-process plant.
Twelve bills (April 2026 to March 2027) worked by hand from GERC's FY 2026-27 order for MGVCL, then
every step of the product through the API, asserted against those hand figures.
Set GJ_OUT=<folder> to also write every response there."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
import yaml

from tests.e2e import assumed_state as st
from tests.e2e.assumed_state import HandBill, Line, Run, dec, q2, q4

pytestmark = pytest.mark.slow

# GERC Tariff Order FY 2026-27 for MGVCL (Case 2582 of 2025), document 17db86c4-2914-591e-a92b-03372554e3a6,
# PDF pages (printed page in brackets). Typed from the page, not from the app.
DEMAND_SLABS = [
    (D(500), D(150)),
    (D(500), D(260)),
    (None, D(475)),
]  # p.173 [157] 11.1.1 (a)-(c), Rs/kVA/month
EXCESS_RATE = D(555)  # p.173 [157] 11.1.2, Rs/kVA/month on billing demand above contract demand
BILLING_MIN_PCT, BILLING_MIN_KVA = D("0.85"), D(100)  # p.174 [158] 11.4 (b), (c)
ENERGY_BY_BD = [(D(500), D("4.00")), (D(2500), D("4.20")), (None, D("4.30"))]  # p.173 [157] 11.2, Rs/kWh
PEAK_ADDER = {False: D("0.45"), True: D("0.85")}  # p.173 [157] 11.3, billing demand above 500 kVA: True
DAY_DISCOUNT = D("0.60")  # p.175 [159] 11.10, 11:00-17:00 (as modified at p.155 [139] Table 10-1)
EHV_REBATE_PCT = D("1.5")  # p.175 [159] 11.9 (b), 33 kV / 66 kV, on energy charges
FPPAS = D("2.52")  # p.137 [121] Table 6.6, base FPPAS for FY 2026-27, Rs/kWh
# open-access figures in the same order
WHEELING_HT = D("0.2352")  # p.150 [134] Table 9-2 row 5, 23.52 paise/kWh at 11, 22 and 33 kV
WHEELING_LOSS_PCT = D("6.50")  # p.151 [135] 9.3, injection and drawal at 11/22/33 kV
CSS = D("1.33")  # p.152 [136] Table 9-3 and text, HT category, capped at 20% of tariff

CD, KV = D(4000), "33"
PF = D("0.95")  # assumed: PF of exactly 95% earns neither penalty (below 90%) nor rebate (above 95%), 11.6
SLOTS = [  # the order's ToD windows, with the assumed share of a continuous-process month in each
    ("07:00-11:00", D("0.165"), "peak"),
    ("11:00-17:00", D("0.248"), "day"),
    ("17:00-18:00", D("0.042"), "normal"),
    ("18:00-22:00", D("0.167"), "peak"),
    ("22:00-07:00", D("0.378"), "normal"),
]
# supply month, energy per day (kWh), recorded maximum demand (kVA)
MONTHS = [
    (date(2026, 4, 1), D(70500), D(3620)), (date(2026, 5, 1), D(71200), D(3680)),
    (date(2026, 6, 1), D(69800), D(3590)), (date(2026, 7, 1), D(67900), D(3480)),
    (date(2026, 8, 1), D(67400), D(3390)), (date(2026, 9, 1), D(68600), D(3450)),
    (date(2026, 10, 1), D(69900), D(3560)), (date(2026, 11, 1), D(68800), D(3510)),
    (date(2026, 12, 1), D(67700), D(3350)), (date(2027, 1, 1), D(67500), D(3340)),
    (date(2027, 2, 1), D(68400), D(3420)), (date(2027, 3, 1), D(70200), D(3600)),
]  # fmt: skip


def billing_demand(md: D) -> D:
    return max(md, CD * BILLING_MIN_PCT, BILLING_MIN_KVA)


def demand_charge(bd: D) -> D:
    within, amt, left = min(bd, CD), D(0), min(bd, CD)
    for width, rate in DEMAND_SLABS:
        k = left if width is None else min(left, width)
        amt += k * rate
        left -= k
    return amt + max(D(0), bd - within) * EXCESS_RATE


def demand_lines(bd: D) -> list[Line]:
    """The demand charge as the bill prints it: one line per slab of 11.1.1, then any excess (11.1.2)."""
    out, left = [], min(bd, CD)
    for width, rate in DEMAND_SLABS:
        k = left if width is None else min(left, width)
        if k > 0:
            out.append(Line("demand_charge", f"DEMAND CHARGES @ RS {rate}/KVA", k, rate, q2(k * rate)))
        left -= k
    if bd > CD:
        out.append(
            Line(
                "excess_demand_penalty",
                "EXCESS DEMAND CHARGES",
                bd - CD,
                EXCESS_RATE,
                q2((bd - CD) * EXCESS_RATE),
            )
        )
    return out


def energy_rate(bd: D) -> D:
    return next(r for top, r in ENERGY_BY_BD if top is None or bd <= top)


def hand_bill(first: date, per_day: D, md: D) -> HandBill:
    import calendar

    days = calendar.monthrange(first.year, first.month)[1]
    kwh = per_day * days
    kvah = (kwh / PF).quantize(D(1))
    tod, left = [], kwh
    for i, (name, share, _) in enumerate(SLOTS):
        k = left if i == len(SLOTS) - 1 else (kwh * share).quantize(D(1))
        tod.append((name, k))
        left -= k
    by = {name: k for name, k in tod}
    bd = billing_demand(md)
    rate = energy_rate(bd)
    peak = sum((by[n] for n, _, c in SLOTS if c == "peak"), D(0))
    day = sum((by[n] for n, _, c in SLOTS if c == "day"), D(0))
    energy = q2(kwh * rate)
    h = HandBill(first, kwh, kvah, tod, md, CD, "HTP-I")
    h.lines = [
        *demand_lines(bd),
        Line("energy_charge", "ENERGY CHARGES", kwh, rate, energy),
        Line("tod_surcharge", "TOD CHARGES (PEAK)", peak, PEAK_ADDER[bd > 500], q2(peak * PEAK_ADDER[bd > 500])),
        Line("tod_rebate", "TOD DISCOUNT (11-17 HRS)", day, -DAY_DISCOUNT, -q2(day * DAY_DISCOUNT)),
        Line("energy_charge", "EHV REBATE ON ENERGY CHARGES @1.5%", energy, -EHV_REBATE_PCT, -q2(energy * EHV_REBATE_PCT / 100)),
        Line("fppca", "FPPPA CHARGES", kwh, FPPAS, q2(kwh * FPPAS)),
    ]  # fmt: skip
    return h


BILLS = [hand_bill(m, e, md) for m, e, md in MONTHS]
KVAH_PER_KWH = sum((h.kvah / h.kwh for h in BILLS), D(0)) / len(BILLS)  # the bills' own ratio
AS_OF = "2026-11-01"
STU_CHARGE = D(
    "3858.96"
)  # register (GERC GETCO MYT order, Table 6-4 p.120): Rs/MW/day; that order is not stored
STU_LOSS_PCT = D("3.37")  # register, no citation
SLDC = D("0.05")  # register, no citation, Rs/kWh
ADDITIONAL_SURCHARGE = D("0.99")  # register (GERC AS Order 05/2026, paras 5-6); that order is not stored
PRICE = {"solar": D("3.10"), "wind": D("3.60")}  # stated PPA tariffs (the app's benchmarks)
CUF = {"solar": D(23), "wind": D(30)}  # stated, to spread the per-MW transmission charge
EQUITY = D("0.25")  # stated group-captive equity cost, Rs/kWh
KIND = {"solar": "oa_solar_intra", "wind": "oa_wind_intra"}


def hand_landed(route: str, source: str, own_cost: D | None = None) -> dict[str, D]:
    """Landed cost per kWh at the meter from the order's (and the register's) figures. A captive plant
    is priced at its levelised cost (`own_cost`, ergPLAN's plant assumptions), the others at the stated
    tariff; losses gross up the price and the wheeling charge (energy is paid for at injection)."""
    gen = own_cost if route == "captive" else PRICE[source]
    assert gen is not None
    keep = (1 - STU_LOSS_PCT / 100) * (1 - WHEELING_LOSS_PCT / 100)
    lines = {
        "generation": gen,
        "wheeling": WHEELING_HT,
        "losses": (gen + WHEELING_HT) / keep - (gen + WHEELING_HT),
        "stu_transmission": STU_CHARGE / (24 * 1000 * CUF[source] / 100),
        "sldc": SLDC,
        "css": CSS if route == "third_party" else D(0),  # captive and group captive exempt (s. 42(2))
        "additional_surcharge": ADDITIONAL_SURCHARGE if route == "third_party" else D(0),
        "equity": EQUITY if route == "group_captive" else D(0),
    }
    lines["total"] = sum(lines.values(), D(0))
    return lines


def gj_category(i: int) -> str:
    """The order's ToD windows as settlement categories: peak 07-11 and 18-22 (11.3), the 11-17
    discount (11.10) off-peak, the rest normal."""
    h = (i % 96) // 4
    return "peak" if 7 <= h < 11 or 18 <= h < 22 else "off_peak" if 11 <= h < 17 else "normal"


def grid_bill(month_slots: dict[str, dict[str, D]], month_peak: dict[str, D]) -> dict[str, D]:
    """The year's grid bill worked from a dispatch file's energy by slot and each month's peak kW."""
    energy = demand = D(0)
    for m, slots in month_slots.items():
        kwh = sum((v for k, v in slots.items() if not k.startswith("__")), D(0))
        bd = billing_demand(month_peak[m] * KVAH_PER_KWH)  # kVA at the bills' kVAh per kWh
        rate = energy_rate(bd)
        peak = sum((v for k, v in slots.items() if k.startswith("Peak")), D(0))
        day = sum((v for k, v in slots.items() if k.startswith("Day")), D(0))
        energy += kwh * rate * (1 - EHV_REBATE_PCT / 100) + peak * PEAK_ADDER[bd > 500] - day * DAY_DISCOUNT
        energy += kwh * FPPAS  # at the bills' rate, as the app charges it (the approved tariff has none)
        demand += demand_charge(bd)
    top = (
        ENERGY_BY_BD[-1][1] + PEAK_ADDER[True] + FPPAS,
        EXCESS_RATE * KVAH_PER_KWH,
    )  # dearest, for print precision
    return {"energy": energy, "demand": demand, "max_rate": top[0], "max_demand_rate": top[1]}


def test_gj_full_run() -> None:
    run = Run("GJ_OUT")
    found: dict[str, object] = {}
    run.make(
        BILLS,
        "mgvcl",
        "MADHYA GUJARAT VIJ COMPANY LIMITED",
        "Gujarat",
        {"voltage_kv": KV, "industry_shape": "continuous_process", "weekly_off": []},
    )

    # 1. bill replay: demand and energy (with ToD and the EHV rebate) agree with every hand bill to the paisa
    rep = st.replay_by_group(run.get("03_replay", f"/connections/{run.conn}/replay"))
    assert sorted(rep) == [m for m, _, _ in MONTHS]
    for h in BILLS:
        g = rep[h.first]
        demand = h.amount("demand_charge")
        energy = h.amount("energy_charge") + h.amount("tod_surcharge") + h.amount("tod_rebate")
        assert g["demand"] == (demand, demand, "match"), (h.first, g["demand"])
        assert g["energy"] == (energy, energy, "match"), (h.first, g["energy"])
        # rule gap: the order's base FPPAS (p.137, Rs 2.52/kWh) is not in the approved schedule, so
        # the bill's FPPPA line is not rebuilt; it is explained by the named gap, not computed
        assert g["fppca"] == (h.amount("fppca"), None, "explained"), g["fppca"]

    # 2. today's cost: the approved tariff on the profile equals the twelve hand bills
    today = run.get("07_today", f"/connections/{run.conn}/today")
    hand_energy = sum(
        (h.amount("energy_charge") + h.amount("tod_surcharge") + h.amount("tod_rebate") for h in BILLS), D(0)
    )
    hand_demand = sum((h.amount("demand_charge") for h in BILLS), D(0))
    hand_fppas = sum((h.amount("fppca") for h in BILLS), D(0))
    assert abs(dec(today["energy"]) - hand_energy) <= 1, (today["energy"]["value"], hand_energy)
    assert abs(dec(today["demand"]) - hand_demand) <= 1, (today["demand"]["value"], hand_demand)
    assert abs(dec(today["fppca"]) - hand_fppas) <= 1  # at the bills' own rate, cited to the bills
    assert today["electricity_duty"] is None  # duty is not in the order (listed)
    found["today"] = {"energy": hand_energy, "demand": hand_demand, "fppas": hand_fppas}

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
                    "capacity_mw": "10",
                    "cuf_pct": str(CUF[s]),
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
            assert q4(total) == q4(hand["total"]), (route, s, total, hand)
            by = got["lines"]
            assert dec(by["Wheeling charge · GJ · FY 2026-27"]) == WHEELING_HT
            assert (
                dec(by["SLDC scheduling and operating charge · GJ · FY 2026-27"]) == SLDC
            )  # fixed in Stage 17
            assert q4(dec(next(v for k, v in by.items() if k.startswith("Losses")))) == q4(hand["losses"])
            cap = next(v for k, v in by.items() if "over scheduled energy" in k)
            assert q4(dec(cap)) == q4(hand["stu_transmission"])
            landed_out.append({"route": route, "source": s, **{k: q4(v) for k, v in hand.items()}})
    found["landed"] = landed_out
    data = yaml.safe_load((Path(__file__).parent / "landed_hand" / "gj.yaml").read_text())
    for row in landed_out:
        saved = next(x for x in data["routes"] if x["route"] == row["route"] and x["source"] == row["source"])
        assert D(str(saved["total"]["value"])) == row["total"], (row, saved["total"])

    # 4. settlement after banking (15-minute set-off, a monthly bank up to 30% of the drawal, no charge,
    # lapse at the month's end, a higher-value slot's bank serving lower ones), closed by hand
    load = run.profile_kw()
    inj = st.injection(D(9000), len(load))
    delivered = (1 - STU_LOSS_PCT / 100) * (1 - WHEELING_LOSS_PCT / 100)
    hand = st.hand_settle(load, inj, delivered, gj_category, "block", True, D(30), D(0))
    for own in ("captive", "third_party"):
        app = st.settle(run, f"09_settle_{own}", "oa_solar_intra", own, inj)
        assert app["rule"] == "Banking and settlement · GJ · solar, wind and hybrid, intra-state"
        assert app["granularity"] == "block_15min"
        assert dec(app["sources"][0]["delivered_share"]) == delivered
        bad = st.compare_settlement(app, hand)
        assert not bad, bad[:6]
    st.assert_balance(hand)
    found["settlement"] = {str(m): {k: q2(v) for k, v in h.items()} for m, h in hand.items()}

    # 5. the optimizer: five components, hourly and 15 minutes; the grid bill from the dispatch file
    found["runs"] = st.optimiser_checks(run, grid_bill)

    # 6. the rule book for November 2026: it carries the landed costs worked above
    b = run.post(
        "19_book",
        "/statebooks/generate",
        json={"discom": "mgvcl", "month": "2026-11-01", "connection_id": run.conn},
    )
    text = run.get("21_book.md", f"/statebooks/{b['id']}/file")
    # the book states no plant capacity, so it carries no per-MW transmission line ("not yet sourced")
    for s in ("solar", "wind"):
        tp = hand_landed("third_party", s)
        assert f"**{tp['total'] - tp['stu_transmission']:.2f}**" in text, s
    assert "1.33" in text and "0.05" in text
    run.save("99_found", found)
