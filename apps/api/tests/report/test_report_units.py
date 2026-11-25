"""Small pieces of report/: what-if wording, Chromium lookup, explanation input (refs only)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ergplan.report import pdf
from ergplan.report.explain import _lines
from ergplan.report.whatif import WhatIf


def test_what_if_describes_itself_in_words() -> None:
    w = WhatIf(
        prices={"oa_solar_intra": Decimal("3.4")},
        capacities_mw={"btm_bess": Decimal("1")},
        contract_demand_kva=Decimal("4500"),
        off=["rooftop_pv"],
        as_of=date(2026, 9, 1),
    )
    assert w.describe() == (
        "open-access solar, intra-state at ₹3.4/kWh, battery behind the meter at 1 MW, contract demand 4500 kVA, "
        "no rooftop solar (behind the meter), the rules in force on 01 Sep 2026"
    )
    assert WhatIf().describe() == "no change"


def test_the_pdf_prints_the_plan_page_in_print_mode() -> None:
    assert pdf.page_url("abc").endswith("/p/abc?print=1")


def test_explanation_input_keeps_values_and_sources_only() -> None:
    v = {
        "label": "CSS",
        "value": "1.25",
        "unit": "INR/kWh",
        "source_refs": [
            {
                "kind": "order",
                "document_id": "d1",
                "page": 106,
                "quote": "x" * 500,
                "url": "/documents/d1#page=106",
            }
        ],
    }
    [line] = _lines([v], {"d1": "MERC MYT order"})
    assert line == {
        "label": "CSS",
        "value": "1.25",
        "unit": "INR/kWh",
        "sources": [{"kind": "order", "document": "MERC MYT order", "page": 106, "quote": "x" * 240}],
    }


def test_quantities_print_without_trailing_zeros_and_with_indian_grouping() -> None:
    """Finding 12 of the Uttar Pradesh full test: the rule book printed "33.000 kV" and "5000.000 kVA"."""
    from ergplan.report.statebook import quantity

    assert quantity("33.000") == "33" and quantity("5000.000") == "5,000"
    assert quantity("0.400") == "0.4" and quantity("132") == "132" and quantity("11.50") == "11.5"
    assert quantity("150000.000") == "1,50,000" and quantity("12345678.25") == "1,23,45,678.25"
    assert quantity("100") == "100" and quantity("1000") == "1,000" and quantity("0") == "0"
    assert quantity("-2500.0") == "-2,500"
    assert quantity("n/a") == "n/a"  # not a number: printed as it stands
