"""Multi-year tariff orders (MERC, 28 March 2025: TPC-D, AEML-D, BEST) from the ARR Network: one
table per tariff year under one category heading, a wheeling charge beside the energy charge,
supply levels instead of voltages, and time-of-day tables with a column per year and the season
inside the cell. On trimmed copies of the real pages; the figures asserted here were read from the
orders' PDF pages."""

from __future__ import annotations

import calendar
from dataclasses import replace
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
from ergplan.regreader import arr_import, arr_map, arr_rates
from ergplan.regreader.arr_network import Row, SectionTable
from ergplan.rulebook import sandbox, service

from .arr_helpers import read
from .arr_mh import MH_EFFECTIVE, MH_JURISDICTIONS, MH_ORDERS, MH_YEARS, import_mh, mapped_year

CASES = yaml.safe_load((get_settings().rulebooks_dir / "golden" / "mh" / "tariff-months.yaml").read_text())


def _row(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    [hit] = [r for r in rows if r["label"] == label]
    return hit


def _why(m: Any, needle: str) -> list[str]:
    return [u.reason for u in m.unmapped if needle in u.fact_id]


# ------------------------------------------------------------------ printed strings


def test_a_cell_that_repeats_part_of_its_columns_unit() -> None:
    heading = "Fixed Charge (Rs./kVA/ Month)"
    got = arr_rates.rate_under("Rs. 400 per kVA", heading)
    assert got == arr_rates.Rate(D("400"), Unit.INR_PER_KVA_MONTH)
    assert arr_rates.rate("Rs. 400 per kVA", heading) is None  # the cell alone names no known unit
    assert arr_rates.rate_under("Rs. 475 per month", "Fixed/ Demand Charge") is None  # no unit in the heading
    assert arr_rates.rate_under("Rs. 45 per HP", heading) is None  # another basis than the heading's
    assert arr_rates.rate_under("-", heading) is None


def test_an_adjustment_that_changes_with_the_season_in_one_cell() -> None:
    got = arr_rates.seasonal_adjustments("-15% (April to September) -25% (October to March)")
    assert got == [(D("-15"), [4, 5, 6, 7, 8, 9]), (D("-25"), [10, 11, 12, 1, 2, 3])]
    assert arr_rates.seasonal_adjustments("-15% (Apr to Sept) -25% (Oct to Mar)") == got
    # half a cell (the table ran over the page), no sign, months missing or counted twice: not read
    assert arr_rates.seasonal_adjustments("-15% (April to September)") is None
    assert arr_rates.seasonal_adjustments("-15% (April to September) -25% (October") is None
    assert arr_rates.seasonal_adjustments("15% (April to September) 25% (October to March)") is None
    assert arr_rates.seasonal_adjustments("-15% (April to September) -25% (September to March)") is None
    assert arr_rates.seasonal_adjustments("+20%") is None
    assert arr_rates.month_span("Oct", "Mar") == [10, 11, 12, 1, 2, 3]
    assert arr_rates.month_span("Solar", "Hours") is None


def test_tariff_years_in_titles_and_headings() -> None:
    assert arr_map._title_year("Tariff w.e.f. 1 April, 2026 to 31 March, 2027") == "FY2026-27"
    assert arr_map._title_year("Tariff w.e.f. 1 April, 2026 to 31 March, 2028") is None
    assert arr_map._title_year("Rate Schedule") is None
    assert arr_map._years_named("ToD Charge / (Rebate) for categories FY 2025-26") == {"FY2025-26"}
    assert arr_map._years_named("FY 25-26") == {"FY2025-26"}
    assert arr_map._years_named("ToD (Rebate) for LT-Residential BPL FY 2026- 27") == {"FY2026-27"}
    assert arr_map._years_named("ToD Charge / (Rebate) for categories FY 2027-28 to FY 2029-30") == {
        "FY2027-28",
        "FY2028-29",
        "FY2029-30",
    }
    assert arr_map._years_named("Particulars") == set()


def test_category_codes_of_a_heading() -> None:
    assert arr_map._category_codes("EHT I and HT I: HT – Industry") == ["EHT I", "HT I"]
    assert arr_map._category_codes("HT II- HT Commercial") == ["HT II"]
    assert arr_map._category_codes("HT - IV Railways, Metro, Monorail") == ["HT IV"]
    assert arr_map._category_codes("LT III(B): Above 20kW") == ["LT III (B)"]
    assert arr_map._category_codes("LT III (B): LT - Industry, above 20 kW load") == ["LT III (B)"]
    assert arr_map._category_codes("HV-2") == [] and arr_map._category_codes("LT Industry") == []


def test_order_spec_names_one_year_of_a_multi_year_order() -> None:
    assert arr_import.order_spec(104) == (104, None)
    assert arr_import.order_spec("104@FY2026-27") == (104, "FY2026-27")
    for bad in ("104@2026-27", "x", "104@FY26-27"):
        with pytest.raises(arr_import.ImportRefused):
            arr_import.order_spec(bad)
    assert arr_import._keyed({"104@FY2026-27": "a", 104: "b"}, "104@FY2026-27") == "a"
    assert arr_import._keyed({104: "b"}, "104@FY2026-27") == "b"
    assert arr_import._keyed({100: "b"}, "104@FY2026-27") is None


# ------------------------------------------------------------------ the rate tables


@pytest.mark.parametrize("order_id", [100, 104, 108])
@pytest.mark.parametrize("year", MH_YEARS)
def test_every_mapped_row_is_valid_and_cites_its_page(order_id: int, year: str) -> None:
    order, _ = read(order_id)
    m = mapped_year(order_id, year)
    assert {"tariff_schedule", "tod_slots"} <= set(m.by_family)
    for family, rows in m.by_family.items():
        assert rows and service.validate_rows(family, rows) == {}
        for r in rows:
            for ref in [r["source_ref"], *r.get("field_refs", {}).values()]:
                assert ref["kind"] == "order" and 1 <= ref["page"] <= order.pages
                assert ref["quote"] and len(ref["quote"]) <= 400
                assert f"ARR Network order {order_id}" in ref["note"]


def test_the_later_years_tables_take_the_category_of_the_first_years_heading() -> None:
    """AEML-D prints 'EHT I and HT I: HT – Industry' once, above the FY 2025-26 table (p. 469); the
    ARR Network files the four later tables under the row label 'HT'."""
    _, sections = read(104)
    filed = {t.category for t in sections["tariff_schedule"] if "2026" in t.title[:30]}
    assert "HT" in filed
    for year, energy, wheeling in (("FY2025-26", "6.13", "0.74"), ("FY2026-27", "5.73", "0.57")):
        rows = mapped_year(104, year).by_family["tariff_schedule"]
        ht = _row(rows, "EHT I and HT I: HT – Industry · HT")
        assert ht["scope"] == {
            "category": ["EHT I and HT I: HT – Industry", "EHT I", "HT I"],
            "rate_block": "HT",
        }
        assert ht["body"] == {
            "category": "EHT I and HT I: HT – Industry",
            "billing_basis": "kvah",
            "demand_charge": {"value": "400", "basis": "inr_per_kva_month"},
            "energy_charge": {"base": energy},
            "retail_wheeling_charge": wheeling,
        }
        assert ht["source_ref"]["page"] == 469 and "Rs. 400 per kVA" in ht["source_ref"]["quote"]
        assert ht["source_ref"]["quote"].startswith(f"Tariff w.e.f. 1 April, {year[2:6]} to 31 March")
        # no wheeling charge is printed for EHV ('-'): the row carries none
        ehv = _row(rows, "EHT I and HT I: HT – Industry · EHV")
        assert ehv["scope"]["rate_block"] == "EHV" and "retail_wheeling_charge" not in ehv["body"]
        assert ehv["body"]["energy_charge"] == {"base": energy}
        assert {r["scope"]["category"][-1] for r in rows} == {"HT I", "HT II", "HT III", "HT IV", "HT V"}
        assert len(rows) == 10
    # no table of another year is read into the year asked for
    m = mapped_year(104, "FY2026-27")
    assert sum("the table is for FY" in u.reason for u in m.unmapped) >= 30
    energies = {r["body"]["energy_charge"]["base"] for r in m.by_family["tariff_schedule"]}
    assert energies == {"5.73", "5.97", "5.10", "5.01", "5.85"}


def test_tpcd_prints_a_wheeling_charge_at_ehv_too() -> None:
    rows = mapped_year(100, "FY2026-27").by_family["tariff_schedule"]
    assert _row(rows, "EHT I and HT I: HT – Industry · EHV")["body"]["retail_wheeling_charge"] == "0.04"
    ht = _row(rows, "EHT II and HT II: HT- Commercial · HT")
    assert ht["body"]["demand_charge"]["value"] == "300" and ht["body"]["energy_charge"]["base"] == "5.89"
    assert ht["body"]["retail_wheeling_charge"] == "0.69" and ht["source_ref"]["page"] == 319


def test_a_single_row_that_repeats_the_category_adds_no_rate_block() -> None:
    """BEST prints one row per HT category, labelled with the category ('HT - I Industry')."""
    rows = mapped_year(108, "FY2025-26").by_family["tariff_schedule"]
    assert len(rows) == 5
    r = _row(rows, "HT I: Industry")
    assert r["scope"] == {"category": ["HT I: Industry", "HT I"]}
    assert r["body"]["demand_charge"] == {"value": "400.00", "basis": "inr_per_kva_month"}
    assert r["body"]["energy_charge"] == {"base": "5.70"} and r["body"]["retail_wheeling_charge"] == "1.01"
    later = _row(mapped_year(108, "FY2026-27").by_family["tariff_schedule"], "HT IV- Railway/ Metro/Monorail")
    assert later["body"]["energy_charge"] == {"base": "4.86"} and later["source_ref"]["page"] == 410


def test_what_the_rate_tables_do_not_give_is_listed_not_guessed() -> None:
    m = mapped_year(104, "FY2025-26")
    # consumption slabs stay out; a fixed charge per connection or per HP is not a demand charge
    [slabs] = _why(m, "LT I (B): LT – Residential Tariff w.e.f. 1 April, 2025")
    assert slabs.startswith("energy charge in consumption slabs")
    [lt2] = _why(m, "LT II: LT – Non-Residential or Commercial Tariff w.e.f. 1 April, 2025")
    assert "rate not read: 'Rs. 475 per month'" in lt2
    # a row under the heading of another sub-category is not given that heading
    [lt3] = _why(m, "LT III (B): LT - Industry, above 20 kW load Tariff w.e.f. 1 April, 2025")
    assert "the row names another category ('LT III(A): 0-20 kW') than its heading" in lt3
    # HT VI (EV charging) prints no demand charge: the family needs one, the row is left out
    [ev] = _why(m, "EHT VI and HT VI: HT – Electric Vehicle (EV) Tariff w.e.f. 1 April, 2025")
    assert "no fixed or no energy charge is printed for the row" in ev
    # a table that does not say its year (the tail of a table split over a page) is never read as this year's
    assert arr_map._UNDATED in _why(m, "LT III(A): 0-20 kW MERC MYT Order for AEML-D")
    assert all(r["body"]["billing_basis"] == "kvah" for r in m.by_family["tariff_schedule"])


def _table(title: str, category: str | None, page: int, rows: list[tuple[str, ...]]) -> SectionTable:
    columns = ("Consumption Slab (kVAh)", "Fixed Charge (Rs./kVA/ Month)", "Energy Charge (Rs./kVAh)")
    return SectionTable(
        order_id=1, section="tariff_schedule", table_id=f"t{page}{title[-4:]}", label="", category=category,
        title=title, fiscal_year=None, page=page, columns=columns,
        rows=tuple(Row(r, page) for r in rows), reader_version="section-reader 3", as_text=False,
        fetched_on=date(2026, 11, 25),
    )  # fmt: skip


def test_year_runs_end_at_a_new_heading_and_never_start_under_a_row_label() -> None:
    wef = "Tariff w.e.f. 1 April, {} to 31 March, {}"
    first = _table(
        wef.format(2025, 2026), "HT I: Industry", 10, [("EHV", "400", "6.00"), ("HT", "400", "6.00")]
    )
    second = _table(wef.format(2026, 2027), "HT", 10, [("EHV", "400", "5.50"), ("HT", "400", "5.50")])
    other = _table(wef.format(2025, 2026), "HT II: Commercial", 11, [("HT", "300", "7.00")])
    orphan = _table(wef.format(2025, 2026), "HT", 12, [("HT", "200", "5.00")])  # its heading was lost
    undated = _table("HT", "HT II: Commercial", 13, [("HT", "300", "6.50")])
    tables, why, runs = arr_map._year_runs([first, second, other, orphan, undated])
    assert [(t.category, t.fiscal_year) for t in tables] == [
        ("HT I: Industry", "FY2025-26"),
        ("HT I: Industry", "FY2026-27"),
        ("HT II: Commercial", "FY2025-26"),
        (None, "FY2025-26"),
        ("HT II: Commercial", None),
    ]
    assert why == {undated.table_id: arr_map._UNDATED}
    assert [(r.category, r.first_page, r.last_page) for r in runs] == [
        ("HT I: Industry", 10, 10),
        ("HT II: Commercial", 11, 11),
        (None, 12, 12),
    ]
    # an order whose tables carry no year in their titles comes back as it was
    plain = [undated]
    assert arr_map._year_runs(plain) == (plain, {}, [])


# ------------------------------------------------------------------ time of day


def test_tod_of_the_year_asked_for_one_row_per_season() -> None:
    for year, night in (("FY2025-26", "10"), ("FY2026-27", "15")):
        rows = mapped_year(100, year).by_family["tod_slots"]
        summer = _row(rows, f"EHT I and HT I: HT – Industry · ToD · {year} · April to September")
        winter = _row(rows, f"EHT I and HT I: HT – Industry · ToD · {year} · October to March")
        assert summer["scope"] == {
            "category": ["EHT I and HT I: HT – Industry", "EHT I", "HT I"],
            "months": [4, 5, 6, 7, 8, 9],
        }
        assert winter["scope"]["months"] == [1, 2, 3, 10, 11, 12]
        assert summer["body"] == {
            "slots": [
                {"name": "00:00 hrs to 06:00 hrs", "start": "00:00", "end": "06:00", "rebate_pct": night},
                {"name": "06:00 hrs to 09:00 hrs", "start": "06:00", "end": "09:00"},
                {"name": "09:00 hrs to 17:00 hrs", "start": "09:00", "end": "17:00", "rebate_pct": "15"},
                {"name": "17:00 hrs to 24:00 hrs", "start": "17:00", "end": "24:00", "surcharge_pct": "20"},
            ],
            "season_note": "April to September",
        }
        assert [s.get("rebate_pct") for s in winter["body"]["slots"]] == [night, None, "25", None]
        assert summer["source_ref"]["page"] == 317
        assert summer["source_ref"]["quote"].startswith(f"ToD Charge / (Rebate) for categories FY {year[2:]}")
    third = mapped_year(100, "FY2027-28").by_family["tod_slots"]  # the column 'FY 2027-28 to FY 2029-30'
    row = _row(third, "EHT I and HT I: HT – Industry · ToD · FY2027-28 · October to March")
    assert [s.get("rebate_pct") for s in row["body"]["slots"]] == ["20", None, "30", None]


def test_a_tod_table_filed_under_a_row_label_belongs_to_the_category_whose_tables_it_follows() -> None:
    """The ARR Network files these tables under 'HT' (the last row above them). Read from the PDFs:
    TPC-D p. 317 follows HT I, p. 320 HT II, p. 326 HT V, p. 328 HT VI; AEML-D p. 469 follows HT I,
    p. 472 HT II, p. 477 HT V and p. 479 HT VI; BEST p. 402 follows HT I, p. 406 HT II, p. 414 HT V,
    p. 416 HT VI. The LT tables are filed under their own headings."""
    expect = {
        100: {"LT II": 304, "LT III (B)": 308, "LT IV": 312, "LT V": 314,
              "HT I": 317, "HT II": 320, "HT V": 326, "HT VI": 328},
        104: {"LT II": 456, "LT III (B)": 459, "LT IV": 462, "LT VI": 466,
              "HT I": 469, "HT II": 472, "HT V": 477, "HT VI": 479},
        108: {"LT II": 385, "LT III (B)": 389, "LT IV": 392, "LT VI": 399,
              "HT I": 402, "HT II": 406, "HT V": 414, "HT VI": 416},
    }  # fmt: skip
    for order_id, pages in expect.items():
        rows = mapped_year(order_id, "FY2025-26").by_family["tod_slots"]
        got = {r["scope"]["category"][-1]: r["source_ref"]["page"] for r in rows}
        assert got == pages, order_id
        assert len(rows) == 2 * len(pages)
    # the heading is the one the rate rows carry, so one context finds both
    best = mapped_year(108, "FY2025-26")
    tod_heads = {
        r["scope"]["category"][0] for r in best.by_family["tod_slots"] if "HT V" in r["scope"]["category"]
    }
    assert tod_heads == {"HT V- Public Services"}
    assert (
        _row(best.by_family["tariff_schedule"], "HT V- Public Services")["scope"]["category"][0] in tod_heads
    )


def test_a_tod_table_that_runs_over_the_page_is_read_with_its_continuation() -> None:
    """Three ways the page break falls (read from the PDFs). TPC-D LT II: three slots on p. 304, the
    peak slot on p. 305. AEML-D HT I: the solar-hours cell is cut after '(April to September)' on
    p. 469 and '-25% (October to March)' heads p. 470. BEST HT I: the period '06:00 hrs to' ends
    p. 402 and '09:00 hrs' heads p. 403."""
    for order_id, label, first, on_next in (
        (100, "LT II: LT – Non-Residential or Commercial", 304, {"/slots/3"}),
        (104, "EHT I and HT I: HT – Industry", 469, {"/slots/3"}),
        (108, "HT I: Industry", 402, {"/slots/2", "/slots/3"}),
    ):
        m = mapped_year(order_id, "FY2025-26")
        r = _row(m.by_family["tod_slots"], f"{label} · ToD · FY2025-26 · October to March")
        assert [(s["start"], s["end"]) for s in r["body"]["slots"]] == [
            ("00:00", "06:00"),
            ("06:00", "09:00"),
            ("09:00", "17:00"),
            ("17:00", "24:00"),
        ]
        assert [s.get("rebate_pct") for s in r["body"]["slots"]] == ["10", None, "25", None]
        assert r["body"]["slots"][3]["surcharge_pct"] == "20"
        assert r["source_ref"]["page"] == first
        # the slots printed on the next page cite that page (a cut row cites the page it starts on)
        assert {k: v["page"] for k, v in r["field_refs"].items()} == {k: first + 1 for k in on_next}
        # the continuation the ARR Network filed with the rate tables is not left as an unread table
        assert not [
            u for u in m.unmapped if u.family == "tariff_schedule" and u.fact_id.endswith(f"· p. {first + 1}")
        ]
    _, sections = read(104)
    [head] = [t for t in sections["tod"] if t.page == 469]
    [tail] = [t for t in sections["tariff_schedule"] if t.page == 470]
    assert head.rows[-1].cells[3] == "-15% (April to September)"
    assert tail.columns[3] == "ToD Charge / (Rebate) for categories FY 2025-26 -25% (October to March)"
    joined = arr_map._over_the_page(head, tail)
    assert joined is not None and len(joined.rows) == 4
    assert joined.rows[2].cells[3] == "-15% (April to September) -25% (October to March)"
    assert joined.rows[2].page == 469 and joined.rows[3].page == 470
    # only the next page, under the same heading, with the same columns
    assert arr_map._over_the_page(head, replace(tail, page=471)) is None
    assert arr_map._over_the_page(head, replace(tail, category="LT IV: Public Services")) is None
    assert arr_map._over_the_page(head, replace(tail, columns=tail.columns[:-1])) is None
    assert arr_map._over_the_page(head, replace(tail, columns=("Slabs", *tail.columns[1:]))) is None


def test_tod_tables_that_cannot_be_read_are_listed() -> None:
    m = mapped_year(104, "FY2025-26")
    # a rebate in rupees per unit (LT residential, HT group housing) is not a percentage of the energy charge
    [rupees] = _why(m, "tod ·  HT · p. 474")
    assert rupees.startswith("adjustment not read") and "'- Rs. 0.50/kWh (Rebate)'" in rupees
    # the general table of section 6 is under no category; the proposal tables are not the commission's
    assert _why(m, "Table 6-50 ToD slots approved by the Commission") == [
        "not printed under a tariff category"
    ]
    assert not any("proposal" in r["label"].lower() for r in m.by_family["tod_slots"])
    # half a table whose other half is not there stays unread (the head without its continuation)
    order, sections = read(104)
    alone = {**sections, "tariff_schedule": [t for t in sections["tariff_schedule"] if t.page != 470]}
    src = arr_map.Source(replace(order, arr_year="FY2025-26"), "d0c", date(2025, 4, 1))
    cut = arr_map.map_order(src, alone)
    assert _why(cut, "tod ·  HT · p. 469") == [
        "adjustment not read (no sign, not a percentage, or part of the cell missing): '-15% (April to September)'"
    ]
    assert not [r for r in cut.by_family["tod_slots"] if "HT I" in r["scope"]["category"]]


# ------------------------------------------------------------------ losses by supply level


def test_wheeling_losses_by_supply_level_from_the_years_column() -> None:
    for order_id, table, page, ht, lt in (
        (104, "Table 6-25", 375, "1.44", "5.36"),
        (100, "Table 6-35", 238, "0.37", "2.29"),
    ):
        for year in MH_YEARS:
            m = mapped_year(order_id, year)
            rows = m.by_family["oa_charge"]
            assert [(r["label"], r["body"]["value"]) for r in rows] == [("HT Loss", ht), ("LT Loss", lt)]
            for r, level in zip(rows, ("HT", "LT"), strict=True):
                assert r["scope"] == {"connection": ["intra_state", "ists"], "rate_block": level}
                assert r["body"]["component"] == "wheeling_losses" and r["body"]["basis"] == "percent_in_kind"
                assert r["source_ref"]["page"] == page and table in r["source_ref"]["quote"]
    # TPC-D's EHV row is read twice into its cell by the ARR Network ('0.00% 0.00%'): listed, not guessed
    assert _why(mapped_year(100, "FY2025-26"), "row 'EHV Loss") == [
        "loss not read as a percentage: '0.00% 0.00%'"
    ]
    # BEST: wheeling charges per level are printed per kWh and per kVAh; neither is taken for the other
    best = mapped_year(108, "FY2025-26")
    assert "oa_charge" not in best.by_family or not best.by_family["oa_charge"]
    assert _why(best, "Table 6-22 Wheeling Charges for FY 2025-26 to FY 2029-30, as approved") == [
        "no single per-unit rate in the table (0 rows state one)"
    ]


def test_year_view_takes_the_approved_column_of_the_year() -> None:
    _, sections = read(104)
    [wheeling] = [t for t in sections["wheeling"] if t.label == "Table 6-23"]
    assert (
        "FY 2025-26 AEML-D Petition" in wheeling.columns
        and "FY 2025-26 Approved in this Order" in wheeling.columns
    )
    view = arr_map._year_view(wheeling, "FY2026-27")
    assert view is not None and view.columns == ("Particulars", "FY 2026-27 Approved in this Order")
    assert [r.cells for r in view.rows][:2] == [
        ("HT Wheeling Charge (Rs/kWh)", "0.58"),
        ("HT Wheeling Charge (Rs/kVAh)", "0.57"),
    ]
    assert arr_map._year_view(wheeling, "FY2031-32") is None
    [css] = [t for t in read(100)[1]["wheeling"] if t.label == "Table 6-39"]
    assert arr_map._year_view(css, "FY2025-26") is None  # the year has a Rs./kWh and a Rs./kVAh column


# ------------------------------------------------------------------ the import


@pytest.mark.usefixtures("jurisdictions")
def test_two_years_of_three_orders_become_drafts_with_their_windows() -> None:
    out = import_mh()
    assert [(o.order.order_id, o.order.arr_year, o.jurisdiction_id) for o in out] == [
        (order_id, year, jid)
        for order_id, jid in ((100, "tpcd"), (104, "aeml"), (108, "best"))
        for year in MH_YEARS
    ]
    for o in out:
        first = o.order.arr_year == "FY2025-26"
        assert o.effective_from == (date(2025, 4, 1) if first else date(2026, 4, 1))
        assert o.effective_to == (date(2026, 3, 31) if first else None)
        assert o.effective_basis == "given on the command line"
    by = {(o.order.order_id, o.order.arr_year): o for o in out}
    assert by[(100, "FY2025-26")].mapped == {"oa_charge": 2, "tariff_schedule": 10, "tod_slots": 16}
    assert by[(104, "FY2026-27")].mapped == {"oa_charge": 2, "tariff_schedule": 10, "tod_slots": 16}
    assert by[(108, "FY2026-27")].mapped == {"tariff_schedule": 5, "tod_slots": 16}
    with session_scope() as s:
        versions = list(s.scalars(select(RulebookVersion).where(RulebookVersion.origin == "arr_network")))
        assert len(versions) == 16 and {v.status for v in versions} == {"draft"}
        assert all(v.approved_by is None for v in versions)
        titles = {v.title for v in versions if v.jurisdiction_id == "aeml" and v.family == "tariff_schedule"}
        assert titles == {
            "MERC tariff order FY2025-26 (AEML-D) · ARR Network order 104",
            "MERC tariff order FY2026-27 (AEML-D) · ARR Network order 104",
        }
    again = import_mh()
    assert {v.action for o in again for v in o.versions} == {"unchanged"}
    # every golden case written from the orders' pages passes on the version of its year
    for o in out:
        for v in o.versions:
            passed, _, total = v.golden.partition("/")
            assert v.golden_failing == [] and total and passed == total, (
                o.order.order_id,
                v.family,
                v.golden,
            )


@pytest.mark.usefixtures("jurisdictions")
def test_hand_worked_months_on_the_imported_drafts() -> None:
    import_mh()
    assert {c["discom"] for c in CASES["cases"]} == {"tpcd", "aeml", "best"}
    for c in CASES["cases"]:
        y, m = (int(x) for x in str(c["month"]).split("-"))
        when = date(y, m, calendar.monthrange(y, m)[1])
        facts = escope.ConnectionFacts(
            discom=c["discom"], state="MH", category=c["category"], voltage_kv=D(str(c["voltage_kv"])),
            connection_kind="ht", rate_block=c.get("rate_block"),
        )  # fmt: skip
        with session_scope() as s:
            chain = service.jurisdiction_chain(s, c["discom"])
            ctx = escope.context(facts, when)
            schedule = escope.first(
                sandbox.drafts_in_force(s, "tariff_schedule", chain, when, CASES["origin"]), ctx, chain
            )
            tod = escope.first(
                sandbox.drafts_in_force(s, "tod_slots", chain, when, CASES["origin"]), ctx, chain
            )
            assert schedule is not None and tod is not None, c["name"]
            v = s.get(RulebookVersion, schedule.version_id)
            assert v is not None and v.status == "draft" and c["expect"]["tariff_year"] in (v.title or "")

        def sv(value: Any, unit: Unit) -> SourcedValue:
            ref = SourceRef(kind=RefKind.ASSUMPTION, note="worked case")
            return SourcedValue(value=D(str(value)), unit=unit, source_refs=(ref,))

        assert schedule.body["billing_basis"] == "kvah"
        q = MonthQuantities(
            month=when.replace(day=1),
            units=sv(c["units"], Unit.KVAH),
            by_slot={k: sv(u, Unit.KVAH) for k, u in c["by_slot"].items()},
            max_kva=sv(c["max_kva"], Unit.KVA),
            contract_kva=sv(c["contract_kva"], Unit.KVA),
        )
        bill = evaluate(schedule, tod, q)
        exp = c["expect"]
        for code in ("demand_charge", "energy_charge", "tod_adjustment", "wheeling_charge"):
            assert bill.sum(code) == D(exp[code]), (c["name"], code)
        assert bill.total.value == D(exp["total"]), c["name"]
        refs = [r for ln in bill.lines for r in ln.amount.source_refs if r.kind == RefKind.ORDER]
        assert refs and all(r.page and r.quote and r.document_id for r in refs)


def test_the_helper_constants_name_every_spec() -> None:
    assert set(MH_EFFECTIVE) == set(MH_ORDERS) and set(MH_JURISDICTIONS) == {100, 104}
