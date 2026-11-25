"""The rate, hours and adjustment strings of real orders (cells of UPERC orders 64–67 as the ARR
Network serves them; paise forms as GERC, BERC and MPERC print them)."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from ergplan.core.units import Unit
from ergplan.regreader import arr_rates as r


@pytest.mark.parametrize(
    ("cell", "heading", "value", "unit"),
    [
        ("Rs. 300.00 / kVA / month", None, "300.00", Unit.INR_PER_KVA_MONTH),
        ("Rs. 290.00 / kVA / month", None, "290.00", Unit.INR_PER_KVA_MONTH),
        ("Rs. 7.10 / kVAh", None, "7.10", Unit.INR_PER_KVAH),
        ("Rs. 7.50/ kVAh", None, "7.50", Unit.INR_PER_KVAH),
        ("Rs. 6.75/ kWh", None, "6.75", Unit.INR_PER_KWH),
        ("Rs. 110.00 / kW / Month", None, "110.00", Unit.INR_PER_KW_MONTH),
        ("Rs. 2,100.00 / kW or part thereof per month", None, "2100.00", Unit.INR_PER_KW_MONTH),
        ("0.9318", "Rs. /kWh", "0.9318", Unit.INR_PER_KWH),  # order 67, Table 10-3: units in their own cell
        ("1.03", "Rs./kWh", "1.03", Unit.INR_PER_KWH),  # order 66, Table 9-5
        ("0.38", "CSS APPROVED BY THE COMMISSION FOR FY 2026-27 (Rs. /kWh)", "0.38", Unit.INR_PER_KWH),
        ("6.63#", "Rs/kWh", "6.63", Unit.INR_PER_KWH),  # a footnote mark is not part of the number
        ("650 paise/kWh", None, "650", Unit.PAISE_PER_KWH),
        ("Paise 650 per unit", None, "650", Unit.PAISE_PER_KWH),
        ("435", "Paise/kVAh", "435", Unit.PAISE_PER_KVAH),
        ("1.00%", None, "1.00", Unit.PERCENT),
        ("13.07%", "Loss Levels", "13.07", Unit.PERCENT),
        ("₹ 4.50 per unit", None, "4.50", Unit.INR_PER_KWH),
        ("1,25,000", "Rs./MW/month", "125000", Unit.INR_PER_MW_MONTH),
    ],
)
def test_rates_as_printed(cell: str, heading: str | None, value: str, unit: Unit) -> None:
    got = r.rate(cell, heading)
    assert got == r.Rate(D(value), unit)


@pytest.mark.parametrize(
    ("cell", "heading"),
    [
        ("Rs. 1005.00 / BHP / month", None),  # per horsepower: not a unit of the families
        ("Rs. 4,750.00 / day", None),
        ("Rs. 560.00 / day / shop", None),
        ("load up to 20 kW per connection plus Rs. 100 per kW per day", None),  # two numbers
        ("13,325.63", "Rs. Crore"),  # an amount, not a rate
        ("0.38", "Approved D (Lower of A & C)"),  # no unit anywhere
        ("-", None),
        ("", "Rs. /kWh"),
        ("NIL", "Rs. /kWh"),
        ("From 3rd year onwards: Base Tariff applicable for current year", None),
    ],
)
def test_what_is_not_a_rate_is_not_read(cell: str, heading: str | None) -> None:
    assert r.rate(cell, heading) is None


def test_paise_become_rupees_through_units_only() -> None:
    assert r.Rate(D("650"), Unit.PAISE_PER_KWH).in_rupees() == r.Rate(D("6.50"), Unit.INR_PER_KWH)
    assert r.Rate(D("435"), Unit.PAISE_PER_KVAH).in_rupees() == r.Rate(D("4.35"), Unit.INR_PER_KVAH)
    same = r.Rate(D("300.00"), Unit.INR_PER_KVA_MONTH)
    assert same.in_rupees() is same and same.text() == "300.00"


def test_numbers() -> None:
    assert r.number("1,43,013.60") == D("143013.60")
    assert r.number("(12.29%)") == D("-12.29")
    assert r.number("23.96%") == D("23.96")
    assert r.number("0.23 0.44") is None and r.number("Rs. Crore") is None


@pytest.mark.parametrize(
    ("cell", "pct"),
    [("(-) 15%", "-15"), ("(+) 15%", "15"), ("(+)15%", "15"), ("(-)15%", "-15"), ("0%", "0"), ("0", "0"),
     ("(-) 20%", "-20"), ("+ 10 %", "10"), ("-7.5%", "-7.5")],
)  # fmt: skip
def test_tod_adjustments(cell: str, pct: str) -> None:
    assert r.adjustment_pct(cell) == D(pct)


def test_an_adjustment_without_its_sign_is_not_read() -> None:
    assert r.adjustment_pct("15%") is None
    assert r.adjustment_pct("120% of normal rate") is None
    assert r.adjustment_pct("(+) 15% (-) 15%") is None


@pytest.mark.parametrize(
    ("cell", "band"),
    [
        ("05:00 hrs – 10:00 hrs", ("05:00", "10:00")),
        ("19:00 hrs – 03:00 hrs", ("19:00", "03:00")),
        ("16:00 hrs – 24:00 hrs", ("16:00", "24:00")),
        ("00:00 hrs – 09:00 hrs", ("00:00", "09:00")),
        ("0600 hrs to 1000 hrs", ("06:00", "10:00")),
        ("6 AM to 10 PM", ("06:00", "22:00")),
        ("10 p.m. to 6 a.m.", ("22:00", "06:00")),
        ("6.00 to 10.00 hrs", ("06:00", "10:00")),
        ("22:00 to 06:00", ("22:00", "06:00")),
    ],
)
def test_hours(cell: str, band: tuple[str, str]) -> None:
    assert r.hours(cell) == band


def test_what_is_not_one_time_band_is_not_read() -> None:
    assert r.hours("06:00 to 10:00 & 18:00 to 22:00") is None  # two bands in a cell
    assert r.hours("1st April to 15th June") is None  # dates, not hours (PSERC prints such ranges)
    assert r.hours("10 to 18") is None  # no clock marker
    assert r.hours("Rest of the day") is None
