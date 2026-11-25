"""Chhattisgarh, Odisha, Uttarakhand and Tamil Nadu from the ARR Network, on trimmed copies of the
real pages (CSERC orders 73 and 76, OERC 49 and 50, UERC 55 and 54, TNERC 184): the shapes these
orders print (categories with their names, a condition in brackets beside the voltage, one row per
licensee, paise as 'P/U', Rs./MW/day, a category column), the drafts they become, their golden cases
and the months worked by hand (packages/rulebooks/golden/{cg,uk,tn}/tariff-months.yaml)."""

from __future__ import annotations

import calendar
import dataclasses
from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest
import yaml
from sqlalchemy import select

from ergplan.core.settings import get_settings
from ergplan.core.sourced import RefKind, SourcedValue, SourceRef
from ergplan.core.units import Unit
from ergplan.db.base import session_scope
from ergplan.db.models import RulebookVersion
from ergplan.engine import scope as escope
from ergplan.engine.tariff_eval import MonthQuantities, evaluate
from ergplan.regreader import arr_import, arr_map, arr_rates, grounding
from ergplan.rulebook import repository, sandbox, service

from .arr_helpers import mapped, offline_client

# where each date comes from: CSERC 'applicable from July 1, 2026' / 'July 1, 2025' (the head of each
# tariff schedule); OERC and UERC 'effective from 1st April'; TNERC 'on and from 01st July, 2025'
STATES: dict[str, tuple[list[int], dict[int, date]]] = {
    "cg": ([73, 76], {73: date(2026, 7, 1), 76: date(2025, 7, 1)}),
    "od": ([49, 50], {49: date(2026, 4, 1), 50: date(2025, 4, 1)}),
    "uk": ([55, 54], {55: date(2026, 4, 1), 54: date(2025, 4, 1)}),
    "tn": ([184], {184: date(2025, 7, 1)}),
}


def _import(state: str, dry_run: bool = False) -> list[arr_import.OrderOutcome]:
    orders, effective = STATES[state]
    return arr_import.run(offline_client(), state, orders, dry_run=dry_run, effective=effective)


def _rows(order_id: int, family: str) -> list[dict[str, Any]]:
    return mapped(order_id).by_family[family]


def _one(rows: list[dict[str, Any]], **scope: Any) -> dict[str, Any]:
    [hit] = [r for r in rows if all(r["scope"].get(k) == v for k, v in scope.items())]
    return hit


def _why(order_id: int, needle: str) -> list[str]:
    return [u.reason for u in mapped(order_id).unmapped if needle in u.fact_id]


# ------------------------------------------------------------------ printed units


def test_units_these_orders_print() -> None:
    assert arr_rates.rate("18867/-", heading="Rs./MW/day") == arr_rates.Rate(D("18867"), Unit.INR_PER_MW_DAY)
    assert arr_rates.rate("Rs. 5760/MW-day") == arr_rates.Rate(D("5760"), Unit.INR_PER_MW_DAY)
    assert arr_rates.rate("Rs. 50 per day") is None  # a day alone is still a basis this module does not know
    css = arr_rates.rate("151.83", heading="Cross Subsidy Surcharge (P/U) EHT")
    assert css == arr_rates.Rate(D("151.83"), Unit.PAISE_PER_KWH) and css.in_rupees().text() == "1.5183"
    assert arr_rates.unit_of("Energy Charge (P/kWh)") == Unit.PAISE_PER_KWH
    assert arr_rates.unit_of("(P/kVAh)") == Unit.PAISE_PER_KVAH
    assert arr_rates.unit_of("Rs./kVA/month") == Unit.INR_PER_KVA_MONTH
    assert arr_rates.unit_of("PF surcharge") is None  # letters around the P or the U: not paise per unit


def test_a_table_number_is_not_a_figure_to_ground() -> None:
    # the ARR Network labels 'Table 5-20' what UERC prints as 'Table 5.20:'
    page = "Table 5.20: Wheeling Charges approved for FY 2026-27\nDescription Rs./MW/day\nWheeling Charges 18867/-"
    quote = "Table 5-20 Wheeling Charges approved for FY 2026-27 | Wheeling Charges | 18867/-"
    assert grounding.quote_numbers_missing(quote, page) == ["20", "5"]
    assert grounding.quote_numbers_missing(arr_import._TABLE_NUMBER.sub(" ", quote), page) == []
    wrong = arr_import._TABLE_NUMBER.sub(" ", quote.replace("18867", "18868"))
    assert grounding.quote_numbers_missing(wrong, page) == ["18868"]


# ------------------------------------------------------------------ Chhattisgarh


@pytest.mark.parametrize("order_id", [73, 76])
def test_cg_category_by_code_and_name_and_no_rate_block_from_a_bare_heading(order_id: int) -> None:
    rows = _rows(order_id, "tariff_schedule")
    assert len(rows) == 24 and service.validate_rows("tariff_schedule", rows) == {}
    hv2 = _one(rows, category=["HV-2: Mines", "HV-2"], voltage_kv={"min": 33, "max": 33})
    # the table is headed 'Tariff': that is no rate block; the body keeps the printed name
    assert "rate_block" not in hv2["scope"] and hv2["body"]["category"] == "HV-2: Mines"
    assert (
        hv2["body"]["billing_basis"] == "kvah"
        and hv2["body"]["demand_charge"]["basis"] == "inr_per_kva_month"
    )
    # 'Residential' under 'HV-6: Residential' repeats the category: no rate block either
    hv6 = _one(rows, category=["HV-6: Residential", "HV-6"])
    assert set(hv6["scope"]) == {"category"}
    lv4 = [r for r in rows if r["scope"]["category"][-1] == "LV-4"]  # printed 'LV- 4: L.V. Agriculture …'
    assert len(lv4) == 1 and lv4[0]["scope"]["rate_block"].startswith("LV-4.2: Demand based tariff")


def test_cg_a_condition_beside_the_voltage_is_a_rate_block_in_words() -> None:
    rows = _rows(73, "tariff_schedule")
    hv3 = [r for r in rows if r["scope"]["category"][-1] == "HV-3"]
    got = {
        (r["scope"]["voltage_kv"]["min"], r["scope"].get("rate_block")): (
            r["body"]["demand_charge"]["value"],
            r["body"]["energy_charge"]["base"],
        )
        for r in hv3
    }
    assert got == {
        (220, None): ("400", "6.95"),
        (132, None): ("400", "7.10"),
        (33, "Load factor above 15%"): ("400", "7.55"),
        (33, "Load factor up to 15%"): ("200", "7.75"),
        (11, "Load Factor above 15%"): ("400", "7.85"),
        (11, "Load Factor up to 15%"): ("200", "8.15"),
    }
    # the citation keeps the row as printed, signs and all
    low = _one(hv3, voltage_kv={"min": 33, "max": 33}, rate_block="Load factor up to 15%")
    assert "33 kV supply (Load factor <=15%)* | 200 | 7.75" in low["source_ref"]["quote"]
    assert low["source_ref"]["page"] == 205
    assert arr_map._in_words("Load factor >=15%") == "Load factor at or above 15%"
    # a voltage that is still not one band is refused as before
    assert "voltage not read with confidence" in _why(73, "HV-1: Railway Traction")[0]
    assert "voltage not read with confidence" in _why(73, "HV-7: Start-Up Power")[0]


def test_cg_surcharge_by_voltage_and_what_is_left_unmapped() -> None:
    rows = _rows(73, "oa_charge")
    assert {r["body"]["component"] for r in rows} == {"cross_subsidy_surcharge"} and len(rows) == 5
    assert _one(rows, voltage_kv={"min": 220})["body"]["value"] == "1.23"
    lt = _one(rows, consumer_kind=["lt"])  # the row printed 'LT' is the supply level, not a rate block
    assert lt["body"] == {"component": "cross_subsidy_surcharge", "basis": "inr_per_kwh", "value": "1.43"}
    assert "rate_block" not in lt["scope"] and "Approved CSS" in lt["source_ref"]["quote"]
    assert mapped(76).by_family.get("oa_charge", []) == []  # the FY 2025-26 schedule prints no such table
    for order_id in (73, 76):
        m = mapped(order_id)
        assert m.by_family.get("tod_slots", []) == []  # '120% of normal rate': a wording not read
        assert [u for u in m.unmapped if u.family == "tod_slots"]
        # the tribal-area rate is printed under each slab with the same words: none of them is kept
        assert sum("print a rate for the same scope" in u.reason for u in m.unmapped) == 3
    assert "not headed as approved" in _why(73, "Table 5-6 Voltage Wise Losses")[0]


# ------------------------------------------------------------------ Odisha


def test_od_one_row_per_licensee_goes_to_that_licensee() -> None:
    rows = _rows(49, "oa_charge")
    by: dict[str | None, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r.get(arr_map.JURISDICTION_KEY), []).append(r)
    assert set(by) == {None, "tpcodl", "tpnodl", "tpsodl", "tpwodl"}
    south = by["tpsodl"]
    assert {
        (r["body"]["component"], tuple(r["scope"]["consumer_kind"])): r["body"]["value"] for r in south
    } == {
        ("cross_subsidy_surcharge", ("eht",)): "2.5053",
        ("cross_subsidy_surcharge", ("ht",)): "1.0602",
        ("cross_subsidy_surcharge", ("lt",)): "1.4034",
        # Table 47 gives the wheeling charge for HT, Table 48 the same figure for HT and LT: one row
        ("wheeling", ("ht", "lt")): "1.9732",
    }
    assert all(r["body"]["basis"] == "inr_per_kwh" for r in south)
    css = _one(south, consumer_kind=["eht"])
    assert css["source_ref"]["page"] == 145 and css["source_ref"]["quote"].endswith(
        "TPSODL | Cross Subsidy Surcharge (P/U) EHT | 250.53"
    )
    # the transmission charge is printed once, in words, for the four licensees: the order's own jurisdiction
    [stu] = by[None]
    assert stu["body"] == {"component": "stu_transmission", "basis": "inr_per_mw_day", "value": "5760"}
    assert stu["scope"]["consumer_kind"] == ["eht", "ht"] and "Rs.5760/MW-day" in stu["source_ref"]["quote"]
    clean = [{k: v for k, v in r.items() if k != arr_map.JURISDICTION_KEY} for r in rows]
    assert service.validate_rows("oa_charge", clean) == {}


def test_od_previous_year_and_the_tables_that_are_not_the_charges_payable() -> None:
    rows = _rows(50, "oa_charge")
    central = [r for r in rows if r.get(arr_map.JURISDICTION_KEY) == "tpcodl"]
    assert {
        (r["body"]["component"], tuple(r["scope"]["consumer_kind"])): r["body"]["value"] for r in central
    } == {
        ("cross_subsidy_surcharge", ("eht",)): "1.6198",
        ("cross_subsidy_surcharge", ("ht",)): "0.7436",
        ("wheeling", ("ht",)): "1.0255",
    }
    assert _one(rows, consumer_kind=["eht", "ht"])["body"]["value"] == "6120"
    assert "claimed, existing or proposed" in _why(49, "Table 18 Proposed wheeling charge")[0]
    assert "not headed as approved" in _why(49, "Table 44 Computed Surcharge")[0]
    assert (
        "not headed as approved" in _why(49, "Table 45 Applicable Surcharge")[0]
    )  # licensees across, not down
    assert (
        "FY2025-26" in _why(49, "Table 46 Wheeling Charges Approved")[0]
    )  # its heading names the wrong year
    assert mapped(49).by_family.get("tariff_schedule", []) == []


def test_a_licensee_table_of_amounts_or_of_claims_is_not_read_as_charges() -> None:
    order, sections = offline_client().order(49), offline_client().sections(49, "wheeling")
    [t47] = [t for t in sections if t.label == "Table 47"]
    assert arr_map._is_licensee_table(t47, order)
    crore = dataclasses.replace(
        t47, columns=("Name of the licensee", "Wheeling cost (Rs. Crore)"), title="Cost"
    )
    assert not arr_map._is_licensee_table(crore, order)
    src = arr_map.Source(order, "d", date(2026, 4, 1))
    claimed = dataclasses.replace(t47, title="Surcharge and Wheeling Charge proposed by the licensees")
    with pytest.raises(arr_map._Skip, match="claimed, proposed or computed"):
        arr_map._by_licensee_rows(src, claimed, [], arr_map.Mapped())
    bare = dataclasses.replace(
        t47,
        title="Surcharge and Wheeling Charge",
        columns=tuple(c.replace("applicable", "") for c in t47.columns),
    )
    with pytest.raises(arr_map._Skip, match="neither the title nor a column"):
        arr_map._by_licensee_rows(src, bare, [], arr_map.Mapped())


@pytest.mark.usefixtures("jurisdictions")
def test_od_drafts_per_licensee_twice_the_same_and_nothing_approved() -> None:
    out = _import("od")
    assert [(o.order.order_id, o.jurisdiction_id, o.effective_from, o.effective_to) for o in out] == [
        (49, "od_discoms", date(2026, 4, 1), None),
        (50, "od_discoms", date(2025, 4, 1), date(2026, 3, 31)),
    ]
    got = {(v.jurisdiction_id, v.family, v.rows, v.action, v.golden) for v in out[0].versions}
    assert got == {
        ("od_discoms", "oa_charge", 1, "proposed", "5/5"),
        ("tpcodl", "oa_charge", 4, "proposed", "7/7"),
        ("tpnodl", "oa_charge", 4, "proposed", "7/7"),
        ("tpsodl", "oa_charge", 4, "proposed", "7/7"),
        ("tpwodl", "oa_charge", 4, "proposed", "7/7"),
    }
    assert {(v.jurisdiction_id, v.rows, v.golden) for v in out[1].versions} == {
        ("od_discoms", 1, "5/5"),
        *((d, 3, "7/7") for d in ("tpcodl", "tpnodl", "tpsodl", "tpwodl")),
    }
    with session_scope() as s:
        versions = list(s.scalars(select(RulebookVersion)))
        assert len(versions) == 10 and all(
            v.status == "draft" and v.origin == "arr_network" for v in versions
        )
        assert all(v.approved_by is None for v in versions)
        west = [v for v in versions if v.jurisdiction_id == "tpwodl"]
        assert {(v.effective_from, v.effective_to) for v in west} == {
            (date(2026, 4, 1), None),
            (date(2025, 4, 1), date(2026, 3, 31)),
        }
        # no client path sees a draft (invariant 2)
        chain = service.jurisdiction_chain(s, "tpwodl")
        assert chain[:2] == ["tpwodl", "od_discoms"]
        assert repository.approved_rules(s, "oa_charge", chain, date(2026, 6, 1)) == []
        # the sandbox sees the licensee's surcharge and the group's transmission charge together
        ctx = {"connection": "intra_state", "consumer_kind": "ht", "voltage_kv": 33, "month": 6}
        rows = sandbox.drafts_in_force(s, "oa_charge", chain, date(2026, 6, 1), "arr_network")
        hits = {r.body["component"]: r.body["value"] for r in escope.pick(rows, ctx, chain)}
        assert hits == {"cross_subsidy_surcharge": "0.1726", "wheeling": "1.2090", "stu_transmission": "5760"}
    again = _import("od")
    assert {v.action for o in again for v in o.versions} == {"unchanged"}


def test_rows_split_by_jurisdiction_lose_the_key() -> None:
    rows = [
        {"label": "a", arr_map.JURISDICTION_KEY: "tpcodl"},
        {"label": "b"},
        {"label": "c", arr_map.JURISDICTION_KEY: "tpcodl"},
    ]
    assert arr_import.per_jurisdiction(rows, "od_discoms") == [
        ("od_discoms", [{"label": "b"}]),
        ("tpcodl", [{"label": "a"}, {"label": "c"}]),
    ]


# ------------------------------------------------------------------ Uttarakhand


@pytest.mark.parametrize(("order_id", "wheeling", "bulk"), [(55, "18867", "6.25"), (54, "18025", "7.50")])
def test_uk_wheeling_per_mw_per_day_and_the_schedule_rows(order_id: int, wheeling: str, bulk: str) -> None:
    [w] = _rows(order_id, "oa_charge")
    assert w["body"] == {"component": "wheeling", "basis": "inr_per_mw_day", "value": wheeling}
    rows = _rows(order_id, "tariff_schedule")
    assert len(rows) == 7 and service.validate_rows("tariff_schedule", rows) == {}
    # '2. Rate of Charge' and 'The following rates shall apply …' are headings, not rate blocks
    spbs = _one(rows, category=["RTS-1: Domestic", "RTS-1"])
    assert (
        spbs["scope"]["rate_block"] == "2) Single Point Bulk Supply"
        and spbs["body"]["energy_charge"]["base"] == bulk
    )
    mixed = _one(rows, category=["RTS 6: Mixed Load", "RTS-6"])
    assert set(mixed["scope"]) == {"category"} and mixed["body"]["demand_charge"]["value"] == "150"
    rail = _one(rows, category=["RTS 7: Railway Traction", "RTS-7"])
    assert rail["body"]["billing_basis"] == "kvah" and rail["body"]["energy_charge"]["base"] == "7.05"
    m = mapped(order_id)
    # HT industry is printed by load factor and time of day; the transmission table is in Rs. crore
    assert not [r for r in rows if "RTS-5" in r["scope"]["category"][0]]
    assert [
        u
        for u in m.unmapped
        if "RTS-5: LT and HT Industry" in u.fact_id and "no row states a rate" in u.reason
    ]
    assert any("consumption slab" in u.reason for u in m.unmapped if "RTS-1: Domestic" in u.fact_id)
    assert any(
        "no single per-unit rate" in u.reason for u in m.unmapped if "Transmission Charges" in u.fact_id
    )
    assert m.by_family.get("tod_slots", []) == []


# ------------------------------------------------------------------ Tamil Nadu


def test_tn_each_row_names_its_category_and_slabs_stay_out() -> None:
    rows = _rows(184, "tariff_schedule")
    assert service.validate_rows("tariff_schedule", rows) == {}
    got = {
        (r["scope"]["category"][0], r["scope"].get("rate_block")): (
            r["body"]["demand_charge"]["value"],
            r["body"]["demand_charge"]["basis"],
            r["body"]["energy_charge"]["base"],
        )
        for r in rows
    }
    assert got == {
        ("High Tension Tariff I", None): ("608", "inr_per_kva_month", "7.50"),
        ("High Tension Tariff II (A)", None): ("608", "inr_per_kva_month", "7.75"),
        ("High Tension Tariff II (B)", None): ("608", "inr_per_kva_month", "8.25"),
        ("High Tension Tariff III", None): ("608", "inr_per_kva_month", "9.40"),
        ("High Tension Tariff IV", None): ("608", "inr_per_kva_month", "13.25"),
        ("Low Tension Tariff I-D", None): ("110", "inr_per_kw_month", "8.80"),
        ("Low Tension Tariff I-E", None): ("110", "inr_per_kw_month", "8.80"),
        ("Low Tension Tariff II-A", None): ("110", "inr_per_kw_month", "8.80"),
        ("Low Tension Tariff III-B", "0-50kW"): ("84", "inr_per_kw_month", "8.25"),
        ("Low Tension Tariff VI", None): ("608", "inr_per_kw_month", "13.25"),
    }
    assert all(r["body"]["billing_basis"] == "kwh" for r in rows)  # 'Paise per kWh (Unit)'
    ht1 = _one(rows, category=["High Tension Tariff I"])
    assert (
        ht1["source_ref"]["page"] == 24 and "High Tension Tariff I | 608 | 750" in ht1["source_ref"]["quote"]
    )
    m = mapped(184)
    reasons = [u.reason for u in m.unmapped]
    assert sum("energy charge by time slot" in r for r in reasons) == 2  # HT Tariff V, LT Tariff VII
    assert any(
        "a rate for one consumption slab" in r for r in reasons
    )  # LT II-C, III-A printed under 'Description'
    assert not any("units" in (r["scope"].get("rate_block") or "") for r in rows)


def test_a_consumption_range_is_a_slab_whatever_its_column_is_called() -> None:
    for text in (
        "0 to 60 units per month or 0 to 120 units bimonthly",
        "From 251 units and above units per month or 501 units and above bimonthly",
        "101-200 units per month",
    ):
        with pytest.raises(arr_map._Skip, match="consumption slab"):
            arr_map._slab_guard(text)
    for text in (
        "0-50kW",
        "33 kV supply (Load factor >15%)",
        "Urban (Metered)",
        "5.2.1 Up to 25 HP or 18.7 kW",
    ):
        arr_map._slab_guard(text)


# ------------------------------------------------------------------ drafts, golden cases and worked months


@pytest.mark.usefixtures("jurisdictions")
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("cg", {(73, "cspdcl", "oa_charge", 5, "8/8"), (73, "cspdcl", "tariff_schedule", 24, "51/51"),
                (76, "cspdcl", "tariff_schedule", 24, "51/51")}),
        ("uk", {(55, "upcl", "oa_charge", 1, "3/3"), (55, "upcl", "tariff_schedule", 7, "13/13"),
                (54, "upcl", "oa_charge", 1, "3/3"), (54, "upcl", "tariff_schedule", 7, "13/13")}),
        ("tn", {(184, "tnpdcl", "tariff_schedule", 10, "27/27")}),
    ],
)  # fmt: skip
def test_drafts_and_their_golden_cases(state: str, expected: set[tuple[int, str, str, int, str]]) -> None:
    out = _import(state)
    got = {(o.order.order_id, v.jurisdiction_id, v.family, v.rows, v.golden) for o in out for v in o.versions}
    assert got == expected
    assert all(not v.golden_failing and v.action == "proposed" for o in out for v in o.versions)
    with session_scope() as s:
        versions = list(s.scalars(select(RulebookVersion)))
        assert len(versions) == len(expected)
        assert all(
            v.status == "draft" and v.origin == "arr_network" and v.approved_by is None for v in versions
        )
    assert {v.action for o in _import(state) for v in o.versions} == {"unchanged"}


@pytest.mark.usefixtures("jurisdictions")
def test_cg_the_two_tariff_years_meet_on_1_july() -> None:
    by = {o.order.order_id: o for o in _import("cg", dry_run=True)}
    assert (by[76].effective_from, by[76].effective_to) == (date(2025, 7, 1), date(2026, 6, 30))
    assert (by[73].effective_from, by[73].effective_to) == (date(2026, 7, 1), None)
    assert all(o.effective_basis == "given on the command line" for o in by.values())


def _sv(value: Any, unit: Unit) -> SourcedValue:
    return SourcedValue(
        value=D(str(value)), unit=unit, source_refs=(SourceRef(kind=RefKind.ASSUMPTION, note="worked case"),)
    )


@pytest.mark.usefixtures("jurisdictions")
@pytest.mark.parametrize(("state", "at_least"), [("cg", 4), ("uk", 4), ("tn", 2)])
def test_hand_worked_months_on_the_imported_drafts(state: str, at_least: int) -> None:
    cases = yaml.safe_load(
        (get_settings().rulebooks_dir / "golden" / state / "tariff-months.yaml").read_text()
    )
    _import(state)
    assert len(cases["cases"]) >= at_least and cases["state"] == state.upper()
    for c in cases["cases"]:
        y, m = (int(x) for x in str(c["month"]).split("-"))
        when = date(y, m, calendar.monthrange(y, m)[1])
        facts = escope.ConnectionFacts(
            discom=c["discom"], state=cases["state"], category=c["category"], voltage_kv=D(str(c["voltage_kv"])),
            connection_kind="ht", rate_block=c.get("rate_block"),
        )  # fmt: skip
        with session_scope() as s:
            chain = service.jurisdiction_chain(s, c["discom"])
            ctx = escope.context(facts, when)
            hits = escope.pick(
                sandbox.drafts_in_force(s, "tariff_schedule", chain, when, cases["origin"]), ctx, chain
            )
            assert len(hits) == 1, c["name"]  # one row answers: no second draft or row competes
            schedule = hits[0]
            tod = escope.first(
                sandbox.drafts_in_force(s, "tod_slots", chain, when, cases["origin"]), ctx, chain
            )
            assert tod is None, c["name"]  # no time-of-day table of these orders is imported
            v = s.get(RulebookVersion, schedule.version_id)
            assert v is not None and v.status == "draft" and c["expect"]["tariff_year"] in (v.title or "")
        unit = Unit.KVAH if schedule.body["billing_basis"] == "kvah" else Unit.KWH
        q = MonthQuantities(
            month=when.replace(day=1),
            units=_sv(c["units"], unit),
            by_slot={},
            max_kva=_sv(c["max_kva"], Unit.KVA),
            contract_kva=_sv(c["contract_kva"], Unit.KVA),
        )
        bill = evaluate(schedule, tod, q)
        exp = c["expect"]
        assert bill.sum("demand_charge") == D(exp["demand_charge"]), c["name"]
        assert bill.sum("energy_charge") == D(exp["energy_charge"]), c["name"]
        assert bill.sum("tod_adjustment") == D(exp["tod_adjustment"]), c["name"]
        assert bill.total.value == D(exp["total"]), c["name"]
        refs = [r for ln in bill.lines for r in ln.amount.source_refs if r.kind == RefKind.ORDER]
        assert refs and all(r.page and r.quote and r.document_id for r in refs)
