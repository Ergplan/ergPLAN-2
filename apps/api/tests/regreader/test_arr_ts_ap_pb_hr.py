"""Telangana, Andhra Pradesh, Punjab and Haryana from the ARR Network, on trimmed copies of the real
pages (TGERC orders 187 and 188, APERC 185 and 186, PSERC 178 and 179, HERC 180; fetched 25 Nov
2026): schedules with group headings, a fixed-charge unit in its own column, energy 'per unit',
bands of contract demand, a schedule set side by side for two years, and headings that are not
categories. Expected values in the mapping tests are read from the order pages, not from the mapper."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest
import yaml
from sqlalchemy import select

from ergplan.core.ids import document_id
from ergplan.core.settings import get_settings
from ergplan.core.sourced import RefKind, SourcedValue, SourceRef
from ergplan.core.units import Unit
from ergplan.db.base import session_scope
from ergplan.db.models import RulebookVersion
from ergplan.engine import scope as escope
from ergplan.engine.tariff_eval import MonthQuantities, evaluate
from ergplan.regreader import arr_import, arr_map, arr_rates
from ergplan.regreader.tariff_oder import Mapped
from ergplan.rulebook import sandbox, service

from .arr_helpers import offline_client, read

# what each state's import is run with: the dates the orders themselves print (TGERC Appendix A:
# 'applicable with effect from 01.04.2026 to 31.03.2027' and '01.05.2025 to 31.03.2026'; PSERC para
# 6.11 'applicable from April 1st, 2025 … operative till March 31st, 2026' and, for FY 2023-24, 'from
# May 16th, 2023 … till March 31, 2024'; HERC Annexure-A 'Tariff for 2026-27 (W.E.F. 01.04.2026)'),
# and the reading that a 'unit' on TGERC's HT rows is a kVAh (its Chapter 4 tables: 'Rs. /kVAh')
IMPORTS: dict[str, dict[str, Any]] = {
    "ts": {
        "orders": [187, 188],
        "effective": {187: date(2026, 4, 1), 188: date(2025, 5, 1)},
        "until": {187: date(2027, 3, 31)},
        "per_unit": {187: "kvah", 188: "kvah"},
    },
    "pb": {
        "orders": [178, 179],
        "effective": {178: date(2025, 4, 1), 179: date(2023, 5, 16)},
        "until": {178: date(2026, 3, 31), 179: date(2024, 3, 31)},
    },
    "hr": {"orders": [180], "effective": {180: date(2026, 4, 1)}},
    "ap": {"orders": [186, 185], "effective": {}},
}


def _import(state: str, dry_run: bool = False) -> list[arr_import.OrderOutcome]:
    a = IMPORTS[state]
    return arr_import.run(
        offline_client(),
        state,
        a["orders"],
        dry_run=dry_run,
        effective=a["effective"],
        until=a.get("until"),
        per_unit=a.get("per_unit"),
    )


def _mapped(order_id: int, per_unit: str | None = None) -> Mapped:
    order, sections = read(order_id)
    src = arr_map.Source(order, document_id(order.sha256), date(2026, 4, 1), per_unit=per_unit)
    return arr_map.map_order(src, sections)


def _find(m: Mapped, **scope: Any) -> dict[str, Any]:
    [hit] = [
        r for r in m.by_family["tariff_schedule"] if all(r["scope"].get(k) == v for k, v in scope.items())
    ]
    return hit


def _rates(row: dict[str, Any]) -> tuple[str, str, str, str]:
    b = row["body"]
    return (
        b["demand_charge"]["value"],
        b["demand_charge"]["basis"],
        b["energy_charge"]["base"],
        b["billing_basis"],
    )


def _why(m: Mapped, needle: str) -> list[str]:
    return [u.reason for u in m.unmapped if needle in u.fact_id]


# ------------------------------------------------------------------ printed strings


def test_bands_of_contract_demand_and_load() -> None:
    assert arr_rates.load_band("i) Above 100 kVA and upto1000 kVA") == (
        "contract_demand_kva",
        {"min": 100, "min_exclusive": True, "max": 1000},
    )
    assert arr_rates.load_band("ii) Above 1000 KVA andupto 2500 kVA") == (
        "contract_demand_kva",
        {"min": 1000, "min_exclusive": True, "max": 2500},
    )
    assert arr_rates.load_band("iii) Above 2500 KVA") == (
        "contract_demand_kva",
        {"min": 2500, "min_exclusive": True},
    )
    assert arr_rates.load_band("Upto 10 KW") == ("load_kw", {"max": 10})
    assert arr_rates.load_band("Above 10 KW & upto 20 kW") == (
        "load_kw",
        {"min": 10, "min_exclusive": True, "max": 20},
    )
    # not a band of one unit, or more than a band: not read
    assert arr_rates.load_band("Load/Demand exceeding 20 KW/kVA & upto 100 kVA (All units)") is None
    assert arr_rates.load_band("Above 20 kW and upto 100 kVA") is None
    assert arr_rates.load_band("General Industry") is None
    assert arr_rates.load_band("132 kV and above") is None


def test_consumption_slabs_are_told_from_rate_rows() -> None:
    for slab in ("0-50", "101-200", "Above 500", "Above 800 units", "i) Upto 500 kWh", "First 2000 kWh"):
        assert arr_rates.is_slab(slab), slab
    for other in (
        "11 kV",
        "Above 100 kVA and upto 1000 kVA",
        "Industries",
        "Supply at 220 kV",
        "Above 2500 KVA",
    ):
        assert not arr_rates.is_slab(other), other


def test_an_energy_and_a_fixed_charge_in_one_cell() -> None:
    heading = (
        "Tariff for 2026-27 (W.E.F. 01.04.2026) Category of consumers Energy Charges (Paisa / kWh or/ kVAh) "
        "* Fixed Charge (Rs. per kW per month of the connected load / per kVA of sanctioned contract demand"
    )
    got = arr_rates.joined("695/kVAh Rs. 290/kVA /Month Nil", heading)
    assert got == (
        arr_rates.Rate(D("695"), Unit.PAISE_PER_KVAH),
        arr_rates.Rate(D("290"), Unit.INR_PER_KVA_MONTH),
    )
    assert got[0].in_rupees() == arr_rates.Rate(D("6.95"), Unit.INR_PER_KVAH)
    kw = arr_rates.joined("645/kVAh Rs. 100/kW/Month Nil", heading)
    assert kw is not None and kw[1] == arr_rates.Rate(D("100"), Unit.INR_PER_KW_MONTH)
    # one charge only, a per-BHP charge, words, or a heading that does not say rupees or paise: not read
    assert arr_rates.joined("680/kVAh Nil", heading) is None
    assert arr_rates.joined("748/unit - Rs. 180 BHP/ Year", heading) is None
    assert arr_rates.joined("Merged with HT Supply", heading) is None
    assert (
        arr_rates.joined("695/kVAh Rs. 290/kVA /Month Nil", "Tariff for 2026-27 Energy Charges, Fixed Charge")
        is None
    )


# ------------------------------------------------------------------ Telangana: one table, group headings


@pytest.mark.parametrize(("order_id", "page"), [(187, 302), (188, 306)])
def test_telangana_appendix_rows_take_their_category_from_the_heading_row(order_id: int, page: int) -> None:
    m = _mapped(order_id, per_unit="kvah")
    assert set(m.by_family) == {"tariff_schedule"} and len(m.by_family["tariff_schedule"]) == 35
    assert service.validate_rows("tariff_schedule", m.by_family["tariff_schedule"]) == {}
    # 'HT-I (A): Industry General' then '11 kV | kVA | 500 | 7.65', '33 kV …', '132 kV and above …'
    general = {"category": ["HT-I(A)"], "rate_block": "Industry General"}
    r11 = _find(m, **general, voltage_kv={"min": 11, "max": 11})
    assert _rates(r11) == ("500", "inr_per_kva_month", "7.65", "kvah")
    assert r11["source_ref"]["page"] == page and "HT-I (A): Industry General" in r11["source_ref"]["quote"]
    assert _rates(_find(m, **general, voltage_kv={"min": 33, "max": 33}))[2] == "7.15"
    assert _rates(_find(m, **general, voltage_kv={"min": 132}))[2] == "6.65"
    # the other sub-schedules of the same code stay apart by their rate block
    assert (
        _rates(_find(m, category=["HT-I(A)"], rate_block="Seasonal Industries", voltage_kv={"min": 132}))[2]
        == "7.70"
    )
    assert _rates(_find(m, category=["HT-I(A)"], rate_block="Optional Category with CMD Up to 150 kVA"))[
        :3
    ] == (
        "100",
        "inr_per_kva_month",
        "8.00",
    )
    # a rate row that carries its own code is a category by itself
    assert _rates(_find(m, category=["HT-V(A)"], rate_block="Railway Traction"))[::2] == ("500", "5.05")
    # a row at the top of the next page belongs to the heading the last page ended under
    last = _find(m, category=["HT-VI"], voltage_kv={"min": 132})
    assert _rates(last)[::2] == ("285", "7.30") and last["source_ref"]["page"] == page + 2
    assert "HT-VI" not in last["source_ref"]["quote"]  # the heading is on the page before: not quoted
    # no demand charge printed ('-'), a charge per HP, and LT rows 'per unit' are listed, not mapped
    assert any("no fixed or no energy charge" in w for w in _why(m, "11 kV | - | 7.65"))
    assert any("rate not read" in w for w in _why(m, "Horticulture Nurseries"))
    assert any("does not say kWh or kVAh" in w for w in _why(m, "LT-VII (A): General Purpose"))
    # the time-of-day table names no category and the tables of claims are not the commission's
    assert _why(m, "· tod ·") and all("not printed under a tariff category" in w for w in _why(m, "· tod ·"))


def test_energy_per_unit_is_not_mapped_without_the_reading() -> None:
    m = _mapped(187)
    assert not m.by_family.get("tariff_schedule")
    assert any("--per-unit" in w for w in _why(m, "Appendix A"))
    # with the reading 'kwh' the same rows are billed in kWh: the reading decides, nothing is inferred
    kwh = _mapped(187, per_unit="kwh")
    assert _rates(_find(kwh, category=["HT-II(A)"], voltage_kv={"min": 11, "max": 11}))[3] == "kwh"


# ------------------------------------------------------------------ Punjab: groups and demand bands


def test_punjab_large_supply_groups_and_bands_of_contract_demand() -> None:
    m = _mapped(178)
    ls = "SCHEDULE OF TARIFF FOR LARGE SUPPLY INDUSTRIAL POWER(LS)"
    low = {"min": 100, "min_exclusive": True, "max": 1000}
    # 'General Industry' is joined to the column headings by the site; 'Power Intensive Units …' is a row
    assert _rates(_find(m, category=[ls], rate_block="General Industry", contract_demand_kva=low)) == (
        "210",
        "inr_per_kva_month",
        "6.60",
        "kvah",
    )
    above = {"min": 1000, "min_exclusive": True}
    assert _rates(_find(m, category=[ls], rate_block="General Industry", contract_demand_kva=above))[::2] == (
        "280",
        "6.70",
    )
    piu = "Power Intensive Units"
    assert _rates(_find(m, category=[ls], rate_block=piu, contract_demand_kva=low))[::2] == ("225", "6.64")
    mid = {"min": 1000, "min_exclusive": True, "max": 2500}
    assert _rates(_find(m, category=[ls], rate_block=piu, contract_demand_kva=mid))[::2] == ("310", "6.95")
    top = {"min": 2500, "min_exclusive": True}
    assert _rates(_find(m, category=[ls], rate_block=piu, contract_demand_kva=top))[::2] == ("350", "7.04")
    # seasonal and part-of-the-year rates are not tariffs for every month
    ms = "SCHEDULE OF TARIFF FOR MEDIUM SUPPLY INDUSTRIAL POWER (MS)"
    assert _rates(_find(m, category=[ms]))[::2] == ("145", "6.25")
    assert any("part of the year" in w for w in _why(m, "(i) April to July"))
    assert any("rate not read" in w for w in _why(m, "(i) Seasonal Rate"))
    # a heading that is the tail of a longer one ('DS/BS;') is not a category
    assert any("not a tariff category" in w for w in _why(m, "DS/BS;"))
    # no approved time-of-day, wheeling or green tariff table: nothing of those families
    assert set(m.by_family) == {"tariff_schedule"}


def test_punjab_fy2023_24_general_industry_is_lost_in_the_source_heading() -> None:
    m = _mapped(179)
    ls = "SCHEDULE OF TARIFF FOR LARGE SUPPLY INDUSTRIAL POWER(LS)"
    rows = [r for r in m.by_family["tariff_schedule"] if r["scope"]["category"] == [ls]]
    # the three General Industry lines (6.45/215, 6.55/275, 6.67/315) sit in the site's column headings;
    # the rows it gives are the Power Intensive Units' and are mapped as such
    assert {r["scope"]["rate_block"] for r in rows} == {"Power Intensive Units"}
    assert sorted(_rates(r)[::2] for r in rows) == [("220", "6.49"), ("305", "6.80"), ("345", "6.89")]


# ------------------------------------------------------------------ Haryana: two years side by side


def test_haryana_reads_only_the_columns_of_the_orders_own_year() -> None:
    m = _mapped(180)
    ht = "HT Supply (above 50 kW) including Traction and DMRC"
    assert set(m.by_family) == {"tariff_schedule"} and len(m.by_family["tariff_schedule"]) == 9
    assert service.validate_rows("tariff_schedule", m.by_family["tariff_schedule"]) == {}
    r11 = [
        r
        for r in m.by_family["tariff_schedule"]
        if r["scope"] == {"category": [ht], "voltage_kv": {"min": 11, "max": 11}}
    ]
    assert len(r11) == 1 and _rates(r11[0]) == ("290", "inr_per_kva_month", "6.95", "kvah")
    assert "695/kVAh" in r11[0]["source_ref"]["quote"] and r11[0]["source_ref"]["page"] == 356
    assert _rates(_find(m, category=[ht], voltage_kv={"min": 66, "max": 132}))[2] == "6.75"
    assert _rates(_find(m, category=[ht], voltage_kv={"min": 400, "max": 400}))[2] == "6.55"
    arc = _find(m, category=[ht], rate_block="Arc furnaces/ Steel Rolling Mills")
    assert _rates(arc)[2] == "7.25" and arc["scope"]["voltage_kv"] == {"min": 11, "max": 11}
    lt = _find(m, category=["LT Supply - up to 50 kW"], load_kw={"min": 20, "min_exclusive": True, "max": 50})
    assert _rates(lt) == ("250", "inr_per_kw_month", "6.60", "kvah")
    # domestic slabs, and the pages where the year's columns are not a name and a rate, are listed
    assert any("no numbered heading" in w or "consumption slab" in w for w in _why(m, "0-150"))
    assert any("side by side" in w for w in _why(m, "p. 357"))


# ------------------------------------------------------------------ Andhra Pradesh: headings that are not categories


def test_andhra_tables_under_running_text_headings_are_not_mapped() -> None:
    m = _mapped(186)
    assert not any(m.by_family.values())
    # the rates are real (475 / 6.65 … at 220 kV is Category III(B) seasonal HT on page 194), the heading
    # the site puts them under is a row label of the table before
    assert any("time of day" in w for w in _why(m, "Industry, ToD (LGD)"))
    assert any("not a tariff category" in w for w in _why(m, "CompositeProtectedWaterSupply"))
    assert any("not a tariff category" in w for w in _why(m, "TERMS AND CONDITIONS"))
    assert any("carries no category code" in w for w in _why(m, "INDUSTRY (GENERAL)"))
    # FY 2026-27 (order 185): the site holds no table of any section
    assert _mapped(185).facts_read == 0


# ------------------------------------------------------------------ the imports, their golden cases, a month


# the cases of packages/rulebooks/golden/ts/tg_discoms-tariff_schedule.yaml (the approved version's)
_APPROVED_CASES = [
    "HT-I(A) 11 kV energy",
    "HT-I(A) 132 kV energy",
    "HT-I(A) 220 kV energy",
    "HT-I(A) 33 kV energy",
    "HT-I(A) billed in kVAh",
    "HT-I(A) demand",
]


@pytest.mark.usefixtures("jurisdictions")
def test_imports_are_drafts_with_green_golden_cases_and_rerun_unchanged() -> None:
    windows = {}
    for state in ("ts", "pb", "hr", "ap"):
        out = _import(state)
        for o in out:
            windows[o.order.order_id] = (o.jurisdiction_id, o.effective_from, o.effective_to)
            for v in o.versions:
                assert v.action == "proposed" and v.family == "tariff_schedule"
                total = int(v.golden.split("/")[1])
                if o.order.order_id == 187:
                    # the approved FY 2026-27 rulebook's own cases run against this draft and fail: they
                    # ask for HT-I(A) without a rate block, which the draft's rows carry ('Industry
                    # General', as the appendix prints it), so the approval gate refuses the draft as it is
                    assert v.golden == "34/40" and sorted(v.golden_failing) == _APPROVED_CASES, v
                    continue
                assert v.golden == f"{total}/{total}" and total >= 10 and not v.golden_failing, (state, v)
        again = _import(state)
        assert [v.action for o in again for v in o.versions] == ["unchanged"] * sum(
            len(o.versions) for o in out
        )
    assert windows[187] == ("tg_discoms", date(2026, 4, 1), date(2027, 3, 31))
    assert windows[188] == ("tg_discoms", date(2025, 5, 1), date(2026, 3, 31))
    assert windows[178] == ("pspcl", date(2025, 4, 1), date(2026, 3, 31))
    assert windows[179] == ("pspcl", date(2023, 5, 16), date(2024, 3, 31))
    assert windows[180] == ("hr_discoms", date(2026, 4, 1), None)
    with session_scope() as s:
        got = list(s.scalars(select(RulebookVersion).where(RulebookVersion.origin == "arr_network")))
        assert len(got) == 5 and all(v.status == "draft" and v.approved_by is None for v in got)
        assert not [v for v in got if v.jurisdiction_id == "ap_discoms"]  # nothing sound to import


def _sv(value: Any, unit: Unit) -> SourcedValue:
    return SourcedValue(
        value=D(str(value)), unit=unit, source_refs=(SourceRef(kind=RefKind.ASSUMPTION, note="worked case"),)
    )


@pytest.mark.usefixtures("jurisdictions")
@pytest.mark.parametrize("state", ["ts", "pb", "hr"])
def test_hand_worked_months_on_the_imported_drafts(state: str) -> None:
    worked = yaml.safe_load(
        (get_settings().rulebooks_dir / "golden" / state / "tariff-months.yaml").read_text()
    )
    _import(state)
    assert worked["cases"]
    for c in worked["cases"]:
        y, m = (int(x) for x in str(c["month"]).split("-"))
        when = date(y, m, calendar.monthrange(y, m)[1])
        facts = escope.ConnectionFacts(
            discom=c["discom"], state=worked["state"], category=c["category"], voltage_kv=D(str(c["voltage_kv"])),
            connection_kind="ht", rate_block=c.get("rate_block"), contract_demand_kva=D(str(c["contract_kva"])),
        )  # fmt: skip
        with session_scope() as s:
            chain = service.jurisdiction_chain(s, c["discom"])
            ctx = escope.context(facts, when)
            drafts = sandbox.drafts_in_force(s, "tariff_schedule", chain, when, worked["origin"])
            schedule = escope.first(drafts, ctx, chain)
            assert schedule is not None, c["name"]
            v = s.get(RulebookVersion, schedule.version_id)
            assert v is not None and v.status == "draft" and c["expect"]["tariff_year"] in (v.title or "")
            assert not sandbox.drafts_in_force(s, "tod_slots", chain, when, worked["origin"])
        exp = c["expect"]
        assert schedule.body["billing_basis"] == exp["billing_basis"], c["name"]
        unit = Unit.KVAH if schedule.body["billing_basis"] == "kvah" else Unit.KWH
        q = MonthQuantities(
            month=when.replace(day=1),
            units=_sv(c["units"], unit),
            by_slot={},
            max_kva=_sv(c["max_kva"], Unit.KVA),
            contract_kva=_sv(c["contract_kva"], Unit.KVA),
        )
        bill = evaluate(schedule, None, q)
        assert bill.sum("demand_charge") == D(exp["demand_charge"]), c["name"]
        assert bill.sum("energy_charge") == D(exp["energy_charge"]), c["name"]
        assert bill.sum("tod_adjustment") == D(exp["tod_adjustment"]), c["name"]
        assert bill.total.value == D(exp["total"]), c["name"]
        refs = [r for ln in bill.lines for r in ln.amount.source_refs if r.kind == RefKind.ORDER]
        assert refs and all(r.page and r.quote and r.document_id for r in refs)
