"""Section tables → draft rule rows, on trimmed copies of the real UPERC pages (orders 64–67)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from ergplan.core.ids import document_id
from ergplan.regreader import arr_map
from ergplan.regreader.arr_network import Order, Row, SectionTable
from ergplan.rulebook import service

from .arr_helpers import UP_ORDERS, mapped, read


def _row(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    [hit] = [r for r in rows if r["label"].startswith(label)]
    return hit


def _why(m: Any, needle: str) -> list[str]:
    return [u.reason for u in m.unmapped if needle in u.fact_id]


@pytest.mark.parametrize("order_id", UP_ORDERS)
def test_every_mapped_row_is_valid_for_its_family_and_cites_the_order(order_id: int) -> None:
    order, _ = read(order_id)
    m = mapped(order_id)
    assert set(m.by_family) == {"tariff_schedule", "tod_slots", "oa_charge"}
    for family, rows in m.by_family.items():
        assert rows and service.validate_rows(family, rows) == {}
        for r in rows:
            for ref in [r["source_ref"], *r.get("field_refs", {}).values()]:
                assert ref["kind"] == "order" and ref["document_id"] == document_id(order.sha256)
                assert 1 <= ref["page"] <= order.pages and ref["quote"] and len(ref["quote"]) <= 400
                assert f"ARR Network order {order_id}" in ref["note"] and order.url in ref["note"]
            assert r["review"] == {
                "reader": "arr_network",
                "decision": "proposed",
                "order_id": order_id,
                "table_id": r["review"]["table_id"],
                "reader_version": "section-reader 3",
                "fetched_on": r["review"]["fetched_on"],
            }


def test_hv2_components_down_the_side_one_row_per_voltage_column() -> None:
    rows = mapped(67).by_family["tariff_schedule"]
    r = _row(rows, "HV-2 · Urban Schedule · For supply above 11 kV and up to 66 kV")
    assert r["scope"] == {
        "category": ["HV-2"],
        "rate_block": "Urban Schedule",
        "voltage_kv": {"min": 11, "min_exclusive": True, "max": 66},
    }
    assert r["body"] == {
        "category": "HV-2",
        "billing_basis": "kvah",
        "demand_charge": {"value": "290.00", "basis": "inr_per_kva_month"},
        "energy_charge": {"base": "6.80"},
    }
    assert r["field_refs"]["/demand_charge/value"]["quote"] == (
        "For supply above 11 kV and up to 66 kV BASE RATE | Demand Charges | Rs. 290.00 / kVA / month"
    )
    assert r["field_refs"]["/energy_charge/base"]["quote"].endswith("Energy Charges | Rs. 6.80 / kVAh")
    assert r["source_ref"]["page"] == 445 and r["source_ref"]["effective_from"] == "2026-07-02"
    assert _row(rows, "HV-2 · Urban Schedule · For supply up to 11 kV")["scope"]["voltage_kv"] == {"max": 11}
    top = _row(rows, "HV-2 · Urban Schedule · For supply above 132 kV")
    assert top["body"]["energy_charge"] == {"base": "6.10"}
    assert top["scope"]["voltage_kv"] == {"min": 132, "min_exclusive": True}


def test_a_heading_the_text_layer_broke_is_not_guessed() -> None:
    m = mapped(67)
    assert _why(m, "column 'For supply above 66 V kV and up to 132 k") == [
        "voltage not read with confidence: 'For supply above 66 V kV and up to 132 k BASE RATE'"
    ]
    assert not [r for r in m.by_family["tariff_schedule"] if "66 V" in r["label"]]
    assert len([r for r in m.by_family["tariff_schedule"] if r["body"]["category"] == "HV-2"]) == 3


def test_a_row_per_voltage_with_a_fixed_and_an_energy_column() -> None:
    rows = mapped(66).by_family["tariff_schedule"]
    r = _row(
        rows,
        "HV-1 · Commercial Loads / Private Institutions / Non - domestic bulk power consumer · For supply at 11kV",
    )
    assert r["scope"]["voltage_kv"] == {"min": 11, "max": 11}
    assert r["scope"]["rate_block"].startswith("Commercial Loads")
    assert (r["body"]["demand_charge"]["value"], r["body"]["energy_charge"]["base"]) == ("430.00", "8.32")
    assert r["source_ref"]["quote"] == "For supply at 11kV | Rs. 430.00 / kVA / month | Rs. 8.32 / kVAh"
    assert "field_refs" not in r  # one printed row holds both charges
    above = _row(
        rows,
        "HV-1 · Residential Multi-Storied Buildings including Residential Multi-Storied Buildings · For supply above",
    )
    assert above["scope"]["voltage_kv"] == {"min": 11, "min_exclusive": True}
    assert above["body"]["energy_charge"]["base"] == "7.50"  # printed 'Rs. 7.50/ kVAh'


def test_a_demand_table_and_an_energy_table_are_joined_on_the_voltage() -> None:
    rows = [r for r in mapped(65).by_family["tariff_schedule"] if r["body"]["category"] == "HV-4"]
    assert [(r["body"]["demand_charge"]["value"], r["body"]["energy_charge"]["base"]) for r in rows] == [
        ("350.00", "8.50"),
        ("340.00", "8.40"),
        ("330.00", "8.25"),
    ]
    assert rows[1]["scope"] == {
        "category": ["HV-4"],
        "voltage_kv": {"min": 11, "min_exclusive": True, "max": 66},
    }
    assert rows[1]["field_refs"]["/demand_charge/value"]["quote"].startswith("(a) Demand Charges")
    assert rows[1]["source_ref"]["page"] == 532


def test_low_tension_rate_in_kw_and_kwh() -> None:
    r = _row(mapped(64).by_family["tariff_schedule"], "LMV-6")
    assert r["body"]["billing_basis"] == "kwh"
    assert r["body"]["demand_charge"] == {"value": "290.00", "basis": "inr_per_kw_month"}
    assert r["scope"] == {
        "category": ["LMV-6"],
        "rate_block": "(A) Consumers getting supply other than Rural Schedule",
    }


def test_what_is_not_a_plain_approved_rate_is_listed_with_its_reason() -> None:
    m = mapped(67)
    assert "claimed, existing or proposed" in _why(m, "Table 9-2 ABR BASED ON EXISTING TARIFF")[0] or (
        "not printed under a tariff category" in _why(m, "Table 9-2 ABR BASED ON EXISTING TARIFF")[0]
    )
    assert all("subsidy columns" in w for w in _why(m, "tariff_schedule · LMV-1 1. Lifeline"))
    assert "consumption slabs" in _why(m, "LMV-2 RATE SCHEDULE")[0]
    assert "running text" in _why(m, "LMV-8 RATE SCHEDULE")[0]
    assert all("subsidy" in w or "BHP" in w for w in _why(m, "tariff_schedule · LMV-5"))
    assert all("no fixed or no energy charge" in w for w in _why(m, "tariff_schedule · LMV-11"))
    # two HV-3 tables (railway, metro) under headings that do not say which is which: neither is kept
    hv3 = [u.reason for u in m.unmapped if u.fact_id.startswith("order 67 · HV-3")]
    assert len(hv3) == 2 and all(
        "2 tables print a rate for the same scope (pages 449, 450)" in w for w in hv3
    )
    assert not [
        r for r in m.by_family["tariff_schedule"] if r["body"]["category"] in ("HV-3", "LMV-5", "LMV-11")
    ]


def test_tod_one_row_per_season_with_signed_slots() -> None:
    rows = mapped(67).by_family["tod_slots"]
    summer = _row(rows, "HV-2 · ToD · Summer")
    assert summer["scope"] == {"category": ["HV-2"], "months": [4, 5, 6, 7, 8, 9]}
    assert summer["body"]["slots"] == [
        {"name": "05:00 hrs – 10:00 hrs", "start": "05:00", "end": "10:00", "rebate_pct": "15"},
        {"name": "10:00 hrs – 19:00 hrs", "start": "10:00", "end": "19:00"},
        {"name": "19:00 hrs – 03:00 hrs", "start": "19:00", "end": "03:00", "surcharge_pct": "15"},
        {"name": "03:00 hrs – 05:00 hrs", "start": "03:00", "end": "05:00"},
    ]
    assert summer["body"]["season_note"] == "Summer Months (April to September)"
    assert (
        summer["source_ref"]["page"] == 446
        and "19:00 hrs – 03:00 hrs (+) 15%" in summer["source_ref"]["quote"]
    )
    assert _row(rows, "HV-2 · ToD · Winter")["scope"]["months"] == [1, 2, 3, 10, 11, 12]
    ev = _row(rows, "LMV-11 · ToD")
    assert ev["scope"] == {"category": ["LMV-11"]} and ev["body"]["slots"][1]["rebate_pct"] == "20"
    assert ev["body"]["slots"][2]["end"] == "24:00"


def test_a_tod_table_carried_over_a_page_is_joined_only_when_the_day_is_complete() -> None:
    m = mapped(64)  # NPCL FY 2025-26: LMV-6 winter is three rows on page 433 and three on page 434
    winter = _row(m.by_family["tod_slots"], "LMV-6 · ToD · Winter")
    assert [s["start"] for s in winter["body"]["slots"]] == [
        "22:00",
        "04:00",
        "06:00",
        "10:00",
        "17:00",
        "19:00",
    ]
    assert winter["body"]["slots"][4] == {
        "name": "17:00 hrs – 19:00 hrs", "start": "17:00", "end": "19:00", "surcharge_pct": "15",
    }  # fmt: skip
    assert winter["source_ref"]["page"] == 433
    assert {k: v["page"] for k, v in winter["field_refs"].items()} == {
        "/slots/3": 434,
        "/slots/4": 434,
        "/slots/5": 434,
    }
    assert not [u for u in m.unmapped if u.family == "tod_slots"]


def _tod_table(
    rows: list[tuple[str, str]], title: str = "Summer Months (April to September)"
) -> SectionTable:
    return SectionTable(
        order_id=1, section="tod", table_id="t1", label="HV-2", category="HV-2", title=title,
        fiscal_year=None, page=10, columns=("Hours", "% of Energy Charges"),
        rows=tuple(Row(r, 10) for r in rows), reader_version="section-reader 3", as_text=False,
        fetched_on=date(2026, 11, 25),
    )  # fmt: skip


ORDER = Order(1, "XERC", "Tariff order", "https://x.example/o.pdf", "ab" * 32, 100, "FY2026-27", ("X",), None, date(2026, 11, 25))  # fmt: skip
SRC = arr_map.Source(ORDER, document_id("ab" * 32), date(2026, 4, 1))


def test_tod_slots_that_do_not_cover_the_day_or_lack_a_sign_are_not_mapped() -> None:
    part = arr_map.map_order(SRC, {"tod": [_tod_table([("18:00 hrs – 22:00 hrs", "(+) 20%")])]})
    assert not part.by_family["tod_slots"] and "cover 4 of 24 hours" in part.unmapped[0].reason
    unsigned = arr_map.map_order(
        SRC, {"tod": [_tod_table([("00:00 hrs – 18:00 hrs", "0"), ("18:00 hrs – 24:00 hrs", "20%")])]}
    )
    assert not unsigned.by_family["tod_slots"] and "adjustment not read" in unsigned.unmapped[0].reason


def _table(
    section: str, label: str, title: str, columns: tuple[str, ...], rows: list[tuple[str, ...]], **kw: Any
) -> SectionTable:
    return SectionTable(
        order_id=1, section=section, table_id=kw.get("table_id", "t9"), label=label,
        category=kw.get("category"), title=title, fiscal_year=kw.get("fiscal_year", "FY2026-27"),
        page=20, columns=columns, rows=tuple(Row(r, 20) for r in rows), reader_version="section-reader 3",
        as_text=False, fetched_on=date(2026, 11, 25),
    )  # fmt: skip


def test_paise_are_stored_as_rupees_and_the_quote_keeps_the_print() -> None:
    t = _table(
        "tariff_schedule", "HTP-I", "HTP-I", ("Particulars", "Demand Charges", "Energy Charges"),
        [("For supply at 11 kV", "Rs. 150/- per kVA per month", "435 Paise per Unit")],
        category="HTP-I", fiscal_year=None,
    )  # fmt: skip
    [row] = arr_map.map_order(SRC, {"tariff_schedule": [t]}).by_family["tariff_schedule"]
    assert row["body"]["energy_charge"] == {"base": "4.35"} and row["body"]["billing_basis"] == "kwh"
    assert row["body"]["demand_charge"] == {"value": "150", "basis": "inr_per_kva_month"}
    assert "435 Paise per Unit" in row["source_ref"]["quote"]


def test_existing_proposed_and_approved_columns_side_by_side() -> None:
    cols = ("Voltage", "Existing Demand Charges", "Proposed Demand Charges", "Approved Demand Charges",
            "Existing Energy Charges", "Proposed Energy Charges", "Approved Energy Charges")  # fmt: skip
    cells = ("At 33 kV", "Rs. 300 / kVA / month", "Rs. 350 / kVA / month", "Rs. 310 / kVA / month",
             "Rs. 6.00 / kWh", "Rs. 6.90 / kWh", "Rs. 6.20 / kWh")  # fmt: skip
    [row] = arr_map.map_order(
        SRC, {"tariff_schedule": [_table("tariff_schedule", "HT-1", "HT-1", cols, [cells], category="HT-1")]}
    ).by_family["tariff_schedule"]
    assert (row["body"]["demand_charge"]["value"], row["body"]["energy_charge"]["base"]) == ("310", "6.20")
    no_approved = arr_map.map_order(
        SRC,
        {"tariff_schedule": [_table("tariff_schedule", "HT-1", "HT-1", (cols[0], cols[1], cols[2], cols[4], cols[5]),
                                    [(cells[0], cells[1], cells[2], cells[4], cells[5])], category="HT-1")]},
    )  # fmt: skip
    assert not no_approved.by_family["tariff_schedule"]
    assert "not exactly one headed approved" in no_approved.unmapped[0].reason


def test_wheeling_losses_and_the_approved_css_column() -> None:
    oa = mapped(67).by_family["oa_charge"]
    wheeling = _row(oa, "Average wheeling charge")
    assert wheeling["body"] == {"component": "wheeling", "basis": "inr_per_kwh", "value": "0.9318"}
    assert wheeling["scope"] == {"connection": ["intra_state", "ists"]}
    assert wheeling["source_ref"]["page"] == 379 and wheeling["source_ref"]["quote"] == (
        "Table 10-3 WHEELING CHARGE APPROVED BY THE COMMISSION FOR FY 2026-27 | "
        "Average Wheeling charge | Rs. /kWh | 0.9318"
    )
    losses = {
        r["label"]: (r["body"]["value"], r["scope"]["voltage_kv"])
        for r in oa
        if r["body"]["component"] == "wheeling_losses"
    }
    assert losses == {
        "Distribution Loss above 33 kV (132 kV, 66 kV)": ("1.00", {"min": 33, "min_exclusive": True}),
        "Distribution Loss at 33 kV": ("3.00", {"min": 33, "max": 33}),
        "Distribution Loss at 11 kV": ("8.00", {"min": 11, "max": 11}),
        "Distribution Loss below 11 kV": ("13.07", {"max": 11, "max_exclusive": True}),
    }
    assert all(
        r["body"]["basis"] == "percent_in_kind" for r in oa if r["body"]["component"] == "wheeling_losses"
    )
    # column D 'Approved (Lower of A & C)', not last year's, the claim or the computed figure
    css = _row(oa, "CSS · HV-2 (Supply at 11 kV)")
    assert css["body"] == {"component": "cross_subsidy_surcharge", "basis": "inr_per_kwh", "value": "0.38"}
    assert css["scope"] == {
        "connection": ["intra_state", "ists"],
        "category": ["HV-2"],
        "voltage_kv": {"min": 11, "max": 11},
    }
    assert css["source_ref"]["quote"].endswith("HV-2 (Supply at 11 kV) | 0.38 | 0.96 | 0.61 | 0.38")
    assert _row(oa, "CSS · HV-2 (Supply above 132 kV)")["body"]["value"] == "0.14"
    assert _row(oa, "CSS · HV-3 (For Railway Traction)")["scope"]["rate_block"] == "For Railway Traction"
    assert _row(oa, "CSS · Supply below 11 kV")["scope"] == {
        "connection": ["intra_state", "ists"], "voltage_kv": {"max": 11, "max_exclusive": True},
    }  # fmt: skip


def test_claimed_computed_other_year_and_input_tables_are_not_charges() -> None:
    m = mapped(67)
    assert _why(m, "Table 10-5 CROSS SUBSIDY SURCHARGE AS SUBMITTED") == [
        "claimed, existing or proposed figures, not the commission's approved rate"
    ]
    assert "not headed as approved" in _why(m, "Table 10-13 CSS COMPUTED BY THE COMMISSION")[0]
    assert "FY2024-25" in _why(m, "Table 4-37 APPROVED INTRA-STATE TRANSMISSION CHARGES")[0]
    assert "input to the surcharge computation" in _why(m, "Table 10-6 INTRA-STATE TRANSMISSION CHARGES")[0]
    assert "no single per-unit rate" in _why(m, "Table 6-36 APPROVED INTRA-STATE TRANSMISSION")[0]
    assert not [
        r
        for r in m.by_family["oa_charge"]
        if r["body"]["component"] in ("stu_transmission", "additional_surcharge")
    ]
    # NPCL's loss table is approved 'for computation of CSS' only: not stated for open-access consumers
    npcl = mapped(66)
    assert "not stated as applying to open access" in _why(npcl, "Table 9-11 VOLTAGE-WISE LOSSES")[0]
    assert not [r for r in npcl.by_family["oa_charge"] if r["body"]["component"] == "wheeling_losses"]


def test_bands_that_share_an_edge_are_made_disjoint_within_one_table() -> None:
    oa = mapped(66).by_family[
        "oa_charge"
    ]  # NPCL prints 'at 11 kV', 'at 11 kV up to 66 kV', 'at 66 kV up to 132 kV', 'at 132 kV#'
    bands = [r["scope"]["voltage_kv"] for r in oa if r["scope"].get("category") == ["HV-2"]]
    assert bands == [
        {"min": 11, "max": 11},
        {"min": 11, "max": 66, "min_exclusive": True},
        {"min": 66, "max": 132, "min_exclusive": True, "max_exclusive": True},
        {"min": 132, "max": 132},
    ]
    assert [r["body"]["value"] for r in oa if r["scope"].get("category") == ["HV-2"]] == [
        "0.57",
        "0.51",
        "0.36",
        "0.36",
    ]


def test_a_green_tariff_is_taken_when_approved_and_not_when_only_computed() -> None:
    fy26 = mapped(65).by_family[
        "oa_charge"
    ]  # Table 8-8 '… APPROVED FOR FY 2025-26': Green Tariff Approved 0.34
    green = _row(fy26, "Green tariff")
    assert green["body"] == {"component": "green_tariff_premium", "basis": "inr_per_kwh", "value": "0.34"}
    assert green["scope"] == {} and green["source_ref"]["page"] == 430
    # FY 2026-27: Table 8-8 '… COMPUTED FOR FY 2026-27' gives 0.48, which the order's text then caps at 0.34
    m = mapped(67)
    assert not [r for r in m.by_family["oa_charge"] if r["body"]["component"] == "green_tariff_premium"]
    assert "not headed as approved" in _why(m, "Table 8-8 GREEN TARIFF")[0]


def test_a_voltage_stated_both_ways_or_a_kva_figure_is_not_a_band() -> None:
    with pytest.raises(arr_map._Skip):
        arr_map._band("HV-3 (Supply at below & above 132 kV)")
    assert arr_map._band("Contracted load above 100 kVA") is None
    assert arr_map._band("For supply at 11kV") == {"min": 11, "max": 11}
    assert arr_map._band("Metered") is None
