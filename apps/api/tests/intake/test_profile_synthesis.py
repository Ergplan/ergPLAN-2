"""Profile synthesis properties (Stage 2): monthly kWh exact, ToD split within 0.1 %, maximum equals
MD × PF, weekly offs and shutdowns honoured, every shape usable."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

import numpy as np
import pytest

from ergplan.intake.profile_synthesis import (
    MonthInput,
    ShapeNotFound,
    TodWindow,
    decode,
    encode,
    shape,
    shape_ids,
    synthesise_year,
    working_days,
)


def _month_slice(kw: np.ndarray, year_start: date, month: date) -> np.ndarray:
    i = 0
    y, m = year_start.year, year_start.month
    while (y, m) != (month.year, month.month):
        i += calendar.monthrange(y, m)[1] * 96
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return kw[i : i + calendar.monthrange(month.year, month.month)[1] * 96]


def _window(n_days: int, a: int, b: int) -> np.ndarray:
    day = np.zeros(96, dtype=bool)
    if a < b:
        day[a:b] = True
    else:
        day[a:] = True
        day[:b] = True
    return np.tile(day, n_days)


MAY = MonthInput(
    month=date(2026, 5, 1),
    kwh=Decimal("812000"),
    max_kva=Decimal("1900"),
    power_factor=Decimal("0.98"),
    tod=(TodWindow("19:00", "02:00", Decimal("230000")), TodWindow("07:00", "16:00", Decimal("320000"))),
)
JUNE = MonthInput(
    month=date(2026, 6, 1), kwh=Decimal("790000"), max_kva=Decimal("1850"), power_factor=Decimal("0.97")
)


def test_eight_shapes_ship_and_build_a_week() -> None:
    assert shape_ids() == sorted(
        [
            "continuous_process",
            "two_shift",
            "single_shift",
            "cold_storage",
            "textile_spinning",
            "steel_induction",
            "pharma_formulation",
            "food_processing",
        ]
    )
    for sid in shape_ids():
        week, d = shape(sid)
        assert week.shape == (7, 96)
        assert week.max() <= 100 and week.min() >= 0
        assert d["note"]


def test_unknown_shape() -> None:
    with pytest.raises(ShapeNotFound):
        shape("nope")


@pytest.mark.parametrize("sid", ["two_shift", "steel_induction", "single_shift", "food_processing"])
def test_month_energy_tod_and_peak(sid: str) -> None:
    p = synthesise_year([MAY, JUNE], sid)
    assert len(p.kw) == 365 * 96
    may = _month_slice(p.kw, p.year_start, MAY.month)
    assert abs(may.sum() * 0.25 - 812000) < 0.01
    for w, (a, b) in zip(MAY.tod, [(76, 8), (28, 64)], strict=True):
        got = may[_window(31, a, b)].sum() * 0.25
        assert abs(got - float(w.kwh)) / float(w.kwh) < 0.001
    assert abs(may.max() - 1900 * 0.98) < 0.01
    june = _month_slice(p.kw, p.year_start, JUNE.month)
    assert abs(june.sum() * 0.25 - 790000) < 0.01
    assert abs(june.max() - 1850 * 0.97) < 0.01
    assert p.kw.min() >= 0


def test_months_without_bills_are_extrapolated_and_flagged() -> None:
    p = synthesise_year([MAY, JUNE], "two_shift")
    reports = p.method["months"]
    assert len(reports) == 12
    assert [r["month"] for r in reports][:3] == ["2026-04-01", "2026-05-01", "2026-06-01"]
    assert sum(1 for r in reports if r.get("extrapolated")) == 10
    assert reports[1]["from_bill_month"] == "2026-05-01"


def test_same_calendar_month_of_another_year_is_used() -> None:
    p = synthesise_year(
        [MonthInput(month=date(2025, 5, 1), kwh=Decimal("500000"))], "two_shift", year_start=date(2026, 4, 1)
    )
    assert p.method["months"][1]["from_bill_month"] == "2025-05-01"


def test_weekly_off_and_shutdown_days_sit_at_base() -> None:
    m = MonthInput(month=date(2026, 5, 1), kwh=Decimal("300000"), shutdown_days=(5,))
    p = synthesise_year([m], "continuous_process", weekly_off=(6,))
    may = _month_slice(p.kw, p.year_start, m.month).reshape(31, 96)
    sundays = [d - 1 for d in range(1, 32) if date(2026, 5, d).weekday() == 6]
    for d in [*sundays, 4]:
        assert np.allclose(may[d], may[d][0])  # a flat base day
    assert may[0].max() > may[sundays[0]].max()  # working day above the off day


def test_shape_too_flat_for_the_peak_is_flagged_not_negative() -> None:
    p = synthesise_year(
        [MonthInput(month=date(2026, 5, 1), kwh=Decimal("800000"), max_kva=Decimal("2600"))],
        "continuous_process",
    )
    rep = p.method["months"][1]
    assert p.kw.min() >= 0
    assert rep.get("peak_shortfall_kw", 0) > 0 or abs(rep["max_kw"] - 2600) < 0.01


def test_kwh_without_md_keeps_the_shape() -> None:
    p = synthesise_year([MonthInput(month=date(2026, 5, 1), kwh=Decimal("400000"))], "two_shift")
    assert "max_kw" not in p.method["months"][1]


def test_working_days() -> None:
    assert working_days("two_shift") == [0, 1, 2, 3, 4, 5]
    assert working_days("continuous_process") == [0, 1, 2, 3, 4, 5, 6]


def test_encode_round_trip() -> None:
    vals = [Decimal("1.250"), Decimal("0.000"), Decimal("1234.567")]
    assert decode(encode(vals)) == vals


def test_needs_a_month() -> None:
    with pytest.raises(ValueError):
        synthesise_year([], "two_shift")


def test_stored_decimals_keep_monthly_energy_exact_and_the_peak() -> None:
    p = synthesise_year(
        [MAY, JUNE, MonthInput(month=date(2026, 7, 1), kwh=Decimal("1972620.37"), max_kva=Decimal("3250"))],
        "continuous_process",
    )
    vals = p.as_decimals()
    for a, b, kwh in p.month_targets:
        assert sum(vals[a:b], Decimal(0)) * Decimal("0.25") == kwh
    may = vals[p.month_targets[1][0] : p.month_targets[1][1]]
    assert max(may) == Decimal("1862.000")


def _year(first: date) -> list[MonthInput]:
    return [
        MonthInput(
            month=date(first.year + (first.month - 1 + i) // 12, (first.month - 1 + i) % 12 + 1, 1),
            kwh=Decimal(400000 + 1000 * i),
        )
        for i in range(12)
    ]


def test_the_year_is_the_twelve_months_the_bills_cover() -> None:
    p = synthesise_year(_year(date(2025, 11, 1)), "two_shift")
    months = [r["month"] for r in p.method["months"]]
    assert p.year_start == date(2025, 11, 1)
    assert months[0] == "2025-11-01" and months[-1] == "2026-10-01"
    assert not any(r.get("extrapolated") for r in p.method["months"])
    assert [r["from_bill_month"] for r in p.method["months"]] == months
    assert len(p.kw) == 365 * 96
    # bills of a whole April-to-March year keep that year
    assert synthesise_year(_year(date(2025, 4, 1)), "two_shift").year_start == date(2025, 4, 1)
    # a few bills across a year end: the twelve months ending with the last of them
    few = [MonthInput(month=date(2026, m, 1), kwh=Decimal("400000")) for m in (2, 5)]
    assert synthesise_year(few, "two_shift").year_start == date(2025, 6, 1)


SUMMER_SPLIT = (("05:00", "10:00", "0.22"), ("10:00", "19:00", "0.42"), ("19:00", "03:00", "0.30"))


def _june(md: str) -> MonthInput:
    kwh = Decimal("2169882")
    tod = tuple(TodWindow(a, b, (kwh * Decimal(share)).quantize(Decimal(1))) for a, b, share in SUMMER_SPLIT)
    return MonthInput(month=date(2026, 6, 1), kwh=kwh, max_kva=Decimal(md), tod=tod)


def test_tod_energy_above_the_recorded_demand_is_flagged_and_energy_kept() -> None:
    # 42% of June's energy in the 270 hours of 10:00–19:00 averages 3,375 kW: a 3,250 kVA peak cannot hold
    m = _june("3250")
    p = synthesise_year([m], "two_shift", weekly_off=(6,))
    rep = next(r for r in p.method["months"] if r["month"] == "2026-06-01")
    june = _month_slice(p.kw, p.year_start, m.month)
    mean = float(m.tod[1].kwh) / 270
    assert rep["peak_method"] == "flat_above_md" and rep["peak_excess_window"] == "10:00-19:00"
    assert rep["max_kw_target"] == 3250.0
    assert abs(rep["max_kw"] - mean) < 0.01 and abs(rep["peak_excess_kw"] - (mean - 3250)) < 0.01
    assert abs(june.max() - mean) < 0.01  # the lowest peak that energy allows, and no higher
    assert abs(june.sum() * 0.25 - float(m.kwh)) < 0.01
    for w, (a, b) in zip(m.tod, [(20, 40), (40, 76), (76, 12)], strict=True):
        assert abs(june[_window(30, a, b)].sum() * 0.25 - float(w.kwh)) < 0.01
    assert june[~_window(30, 40, 76)].max() <= 3250 + 1e-6  # the other windows stay within the demand
    assert june.min() >= 0


def test_the_peak_is_the_recorded_demand_when_the_tod_energy_allows_it() -> None:
    m = _june("3600")
    p = synthesise_year([m], "two_shift", weekly_off=(6,))
    rep = next(r for r in p.method["months"] if r["month"] == "2026-06-01")
    june = _month_slice(p.kw, p.year_start, m.month)
    assert "peak_excess_kw" not in rep and abs(june.max() - 3600) < 0.01
    assert abs(june.sum() * 0.25 - float(m.kwh)) < 0.01
