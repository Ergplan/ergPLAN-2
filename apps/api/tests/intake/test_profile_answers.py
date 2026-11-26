"""Stage 15: each answer changes the synthesised profile in its stated way, and the profile still
matches the bill's monthly kWh, ToD split and recorded maximum demand. No database."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from ergplan.core.sourced import RefKind
from ergplan.core.units import Unit
from ergplan.intake import answers
from ergplan.intake.profile_synthesis import (
    MonthInput,
    Profile,
    TodWindow,
    future_year,
    shape,
    shutdown_days,
    synthesise_year,
    week_for_working_days,
    week_from_shifts,
)

MAY = MonthInput(
    month=date(2026, 5, 1),
    kwh=Decimal("812000"),
    max_kva=Decimal("1900"),
    power_factor=Decimal("0.98"),
    tod=(TodWindow("19:00", "02:00", Decimal("230000")), TodWindow("07:00", "16:00", Decimal("320000"))),
)
JUNE = MonthInput(month=date(2026, 6, 1), kwh=Decimal("790000"), max_kva=Decimal("1850"))
SHAPE = "two_shift"  # Monday to Saturday 06:00-22:00, Sunday off


def _may(p: Profile) -> np.ndarray:
    return p.kw[30 * 96 : 61 * 96].reshape(31, 96)  # the year starts in April


def _mask(a: int, b: int) -> np.ndarray:
    day = np.zeros(96, dtype=bool)
    if a < b:
        day[a:b] = True
    else:
        day[a:] = True
        day[:b] = True
    return day


def _weekday(d: int) -> int:
    return date(2026, 5, d).weekday()


def _matches_the_bill(p: Profile, pf: Decimal = Decimal("0.98")) -> None:
    """Monthly kWh to the watt-hour, the ToD split within 0.1 %, the maximum = recorded MD × PF."""
    may = _may(p)
    stored = p.as_decimals()[30 * 96 : 61 * 96]
    assert sum(stored, Decimal(0)) * Decimal("0.25") == MAY.kwh
    for w, (a, b) in zip(MAY.tod, ((76, 8), (28, 64)), strict=True):
        got = float(may[:, _mask(a, b)].sum()) * 0.25
        assert got == pytest.approx(float(w.kwh), rel=1e-3)
    assert float(may.max()) == pytest.approx(float(MAY.max_kva * pf), abs=0.01)


def _year(values: dict[str, Any]) -> Profile:
    si = answers.synthesis_inputs({k: answers.parse(k, v) for k, v in values.items()}, SHAPE, weekly_off=(6,))
    return synthesise_year(
        [MAY, JUNE],
        SHAPE,
        weekly_off=si.weekly_off,
        week=si.week,
        shutdowns=si.shutdowns,
        large_loads=si.large_loads,
    )


BASE = synthesise_year([MAY, JUNE], SHAPE, weekly_off=(6,))


def test_no_answers_is_the_profile_as_before() -> None:
    assert np.array_equal(_year({}).kw, BASE.kw)
    _matches_the_bill(BASE)


def test_shifts_shape_the_day() -> None:
    """A single stated shift 08:00-16:00: the plant draws its working load in those hours and its
    base load outside them, where the two-shift pattern ran 06:00-22:00."""
    p = _year({"shifts": [{"start": "08:00", "end": "16:00"}]})
    _matches_the_bill(p)
    # the bill fixes each ToD slot's energy, so the shifts show inside a slot. In the 07:00-16:00
    # slot, 07:00-08:00 is now before the shift; in the hours outside the slots, 17:00 was a working
    # hour of the two-shift pattern and is now base load like 03:00
    work = [d for d in range(31) if _weekday(d + 1) != 6]
    may, base = _may(p)[work], _may(BASE)[work]
    assert base[:, 48:52].mean() / base[:, 28:32].mean() == pytest.approx(1.0, abs=0.2)
    assert may[:, 48:52].mean() / may[:, 28:32].mean() > 2
    assert base[:, 68:72].mean() / base[:, 12:16].mean() > 2
    assert may[:, 68:72].mean() / may[:, 12:16].mean() == pytest.approx(1.0, abs=0.05)
    week = week_from_shifts(SHAPE, (("08:00", "16:00"),), (6,))
    _, d = shape(SHAPE)
    assert week[0, 40] == 100 and week[0, 20] == d["base"] and week[0, 70] == d["base"]
    assert (week[6] == d["base"]).all()  # the weekly off stays at base load


def test_a_night_shift_runs_past_midnight_into_the_next_day() -> None:
    week = week_from_shifts(SHAPE, (("22:00", "06:00"),), (6,))
    assert week[0, 92] == 100 and week[1, 8] == 100  # Monday 23:00 and Tuesday 02:00
    assert week[0, 8] == 25  # Monday 02:00 follows the Sunday off


def test_weekly_off_shapes_the_week() -> None:
    """The off day moved from Sunday to Friday: Fridays fall to base load, Sundays work."""
    p = _year({"weekly_off": [4]})
    _matches_the_bill(p)
    may = _may(p)
    fri = [d for d in range(31) if _weekday(d + 1) == 4]
    sun = [d for d in range(31) if _weekday(d + 1) == 6]
    mon = [d for d in range(31) if _weekday(d + 1) == 0]
    assert may[fri].sum() < 0.6 * may[mon].sum() / len(mon) * len(fri)
    assert may[sun].sum() == pytest.approx(may[mon].sum() / len(mon) * len(sun), rel=0.05)
    base = _may(BASE)
    assert base[sun].sum() < 0.6 * base[mon].sum() / len(mon) * len(sun)  # before: Sunday was the off
    week = week_for_working_days(SHAPE, (4,))
    assert np.array_equal(week[6], shape(SHAPE)[0][0])  # Sunday takes a working day's shape


def test_no_weekly_off_makes_every_day_a_working_day() -> None:
    p = _year({"weekly_off": []})
    _matches_the_bill(p)
    may = _may(p)
    days = may.sum(axis=1)
    assert days.max() / days.min() < 1.02


def test_seasonal_shutdown_shapes_the_months() -> None:
    """15 to 24 May shut: those days sit at base load, the month's energy (the bill's) moves onto
    the working days, and a month with no bill is estimated lower by what its shutdown removes."""
    p = _year({"seasonal_shutdowns": [{"start": "05-15", "end": "05-24", "name": "annual overhaul"}]})
    _matches_the_bill(p)
    may, base = _may(p), _may(BASE)
    closed = list(range(14, 24))
    open_days = [d for d in range(31) if d not in closed and _weekday(d + 1) != 6]
    shut_working = [d for d in closed if _weekday(d + 1) != 6]
    sunday = next(d for d in range(31) if _weekday(d + 1) == 6 and d not in closed)
    # a shutdown day runs as the weekly off does, at base load
    assert np.allclose(may[shut_working], may[sunday])
    assert may[shut_working].sum() < 0.7 * base[shut_working].sum()
    assert may[open_days].sum() > 1.1 * base[open_days].sum()
    assert next(m for m in p.method["months"] if m["month"] == "2026-05-01")["kwh"] == pytest.approx(812000)
    # a month with no bill: December shut from the 20th, over the year end to 5 January
    q = _year({"seasonal_shutdowns": [{"start": "12-20", "end": "01-05"}]})
    dec = next(m for m in q.method["months"] if m["month"] == "2026-12-01")
    dec0 = next(m for m in BASE.method["months"] if m["month"] == "2026-12-01")
    assert dec["extrapolated"] and float(dec["kwh_target"]) < 0.8 * float(dec0["kwh_target"])
    assert shutdown_days(date(2026, 12, 1), (("12-20", "01-05"),)) == tuple(range(20, 32))
    assert shutdown_days(date(2027, 1, 1), (("12-20", "01-05"),)) == (1, 2, 3, 4, 5)
    assert shutdown_days(date(2026, 6, 1), (("12-20", "01-05"),)) == ()


def test_large_loads_shape_the_peaks() -> None:
    """A 600 kW furnace from 09:00 to 12:00: demand rises in those hours (the other hours of the same
    ToD slot give way, the slot's energy being the bill's), and in a month whose bill has no ToD
    split the month's highest demand, still the recorded one, falls inside them."""
    p = _year({"large_loads": [{"name": "Induction furnace", "kw": "600", "start": "09:00", "end": "12:00"}]})
    _matches_the_bill(p)
    may, base = _may(p), _may(BASE)
    work = [d for d in range(31) if _weekday(d + 1) != 6]
    assert may[work][:, 36:48].mean() > base[work][:, 36:48].mean() + 250
    assert may[work][:, 48:64].mean() < base[work][:, 48:64].mean()  # 12:00-16:00, the same slot
    june, june0 = p.kw[61 * 96 : 91 * 96].reshape(30, 96), BASE.kw[61 * 96 : 91 * 96].reshape(30, 96)
    d, block = np.unravel_index(int(np.argmax(june)), june.shape)
    assert 36 <= block < 48 and date(2026, 6, int(d) + 1).weekday() != 6
    assert float(june.max()) == pytest.approx(1850, abs=0.01)  # the recorded demand, as before
    assert float(june.sum()) * 0.25 == pytest.approx(790000, abs=0.01)
    assert not 36 <= int(np.argmax(june0.max(axis=0))) < 40  # before: the peak was not at 09:00
    # a load named for Mondays only runs on Mondays
    q = _year(
        {"large_loads": [{"name": "Test bed", "kw": "600", "start": "09:00", "end": "12:00", "days": [0]}]}
    )
    _matches_the_bill(q)
    mq = _may(q)
    mon = [d for d in range(31) if _weekday(d + 1) == 0]
    tue = [d for d in range(31) if _weekday(d + 1) == 1]
    assert mq[mon][:, 36:48].mean() > mq[tue][:, 36:48].mean() + 250


def test_power_factor_sets_the_peak_where_the_bill_prints_none() -> None:
    """June's bill prints no power factor: its peak is recorded kVA × the stated 0.9 (kVA × 1 before)."""
    si = answers.synthesis_inputs({"power_factor": answers.parse("power_factor", "0.9")}, SHAPE, (6,))
    assert si.power_factor == Decimal("0.9")
    june = MonthInput(month=JUNE.month, kwh=JUNE.kwh, max_kva=JUNE.max_kva, power_factor=si.power_factor)
    p = synthesise_year([MAY, june], SHAPE, weekly_off=(6,))
    seg = p.kw[61 * 96 : 91 * 96]
    assert float(seg.max()) == pytest.approx(1850 * 0.9, abs=0.01)
    assert float(BASE.kw[61 * 96 : 91 * 96].max()) == pytest.approx(1850, abs=0.01)
    _matches_the_bill(p)  # May prints 0.98 and keeps it


def test_expansion_scales_the_future_year() -> None:
    kw = BASE.as_decimals()
    grown = future_year(kw, Decimal("20"))
    assert len(grown) == len(kw) and grown[5000] == (kw[5000] * Decimal("1.2")).quantize(Decimal("0.001"))
    total, was = sum(grown, Decimal(0)), sum(kw, Decimal(0))
    assert abs(total / was - Decimal("1.2")) < Decimal("0.000001")
    assert max(grown) == (max(kw) * Decimal("1.2")).quantize(Decimal("0.001"))
    assert future_year(kw, Decimal("-25"))[5000] == (kw[5000] * Decimal("0.75")).quantize(Decimal("0.001"))
    method = {"answers": {"expansion": {"value": answers.parse("expansion", 20)}}}
    assert answers.growth_pct(method) == Decimal("20")
    assert answers.growth_pct({"answers": {"expansion": {"value": {"growth_pct": "0"}}}}) is None
    assert answers.growth_pct({}) is None and answers.growth_pct(None) is None


def test_everything_at_once_still_matches_the_bill() -> None:
    p = _year(
        {
            "shifts": [{"start": "06:00", "end": "14:00"}, {"start": "14:00", "end": "22:00"}],
            "weekly_off": [6],
            "seasonal_shutdowns": [{"start": "05-01", "end": "05-03"}],
            "large_loads": [{"name": "Compressors", "kw": "300", "start": "10:00", "end": "15:00"}],
        }
    )
    _matches_the_bill(p)
    assert len(p.kw) == 365 * 96 and float(p.kw.min()) >= 0
    for i, m in enumerate(p.method["months"]):
        n = calendar.monthrange(*map(int, m["month"].split("-")[:2]))[1]
        assert m["kwh"] == pytest.approx(float(m["kwh_target"]), abs=0.01), (i, n)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("shifts", [], "at least 1"),
        ("shifts", [{"start": "6", "end": "14:00"}], "HH:MM"),
        ("shifts", [{"start": "06:10", "end": "14:00"}], "quarter hour"),
        ("shifts", [{"start": "24:00", "end": "06:00"}], "00:00"),
        ("weekly_off", [7], "0 (Monday) to 6 (Sunday)"),
        ("weekly_off", [0, 1, 2, 3, 4, 5, 6], "at most 6"),
        ("seasonal_shutdowns", [{"start": "02-30", "end": "03-05"}], "MM-DD"),
        ("seasonal_shutdowns", [{"start": "May", "end": "June"}], "MM-DD"),
        ("large_loads", [{"name": "x", "kw": "0", "start": "09:00", "end": "12:00"}], "greater than 0"),
        ("large_loads", [{"name": "", "kw": "5", "start": "09:00", "end": "12:00"}], "at least 1"),
        ("power_factor", "1.2", "less than or equal to 1"),
        ("power_factor", "0.1", "greater than or equal to 0.3"),
        ("expansion", {"growth_pct": "900"}, "less than or equal to 500"),
        ("expansion", "a lot", "decimal"),
        ("roof_sqm", "-5", "greater than or equal to 0"),
        ("land_acres", "x", "decimal"),
        ("contract_demand_kva", "0", "greater than 0"),
    ],
)
def test_an_answer_that_cannot_be_is_refused_with_the_reason(key: str, value: Any, message: str) -> None:
    with pytest.raises(answers.InvalidAnswer) as e:
        answers.parse(key, value)
    assert message.lower() in str(e.value).lower()


def test_unknown_question() -> None:
    with pytest.raises(answers.UnknownQuestion):
        answers.parse("colour_of_the_gate", "blue")


def test_stored_values_are_strings_never_floats() -> None:
    assert answers.parse("power_factor", 0.92) == "0.92"
    assert answers.parse("roof_sqm", "8000") == "8000"
    assert answers.parse("expansion", "12.5") == {"growth_pct": "12.5", "note": ""}
    assert answers.parse("weekly_off", [6, 6, 5]) == [5, 6]
    [load] = answers.parse(
        "large_loads", [{"name": "Kiln", "kw": 450, "start": "00:00", "end": "24:00", "days": [2, 1]}]
    )
    assert load == {"start": "00:00", "end": "24:00", "name": "Kiln", "kw": "450", "days": [1, 2]}


def test_words_and_figures_are_client_stated() -> None:
    row = {"stated_by": "client@example.test", "stated_at": "2026-11-26T10:00:00+00:00"}
    cases: list[tuple[str, Any, str, list[tuple[str, Unit]]]] = [
        ("shifts", [{"start": "06:00", "end": "14:00"}], "06:00 to 14:00", []),
        ("weekly_off", [6], "Sunday", []),
        ("weekly_off", [], "No weekly off", []),
        ("seasonal_shutdowns", [{"start": "05-15", "end": "05-24", "name": "overhaul"}],
         "15 May to 24 May (overhaul)", []),
        ("seasonal_shutdowns", [], "None", []),
        ("large_loads", [{"name": "Furnace", "kw": "600", "start": "09:00", "end": "12:00", "days": [0]}],
         "Furnace: 600 kW, 09:00 to 12:00, Mon", [("600", Unit.KW)]),
        ("power_factor", "0.92", "0.92", [("0.92", Unit.RATIO)]),
        ("expansion", {"growth_pct": "20"}, "20% more consumption than today", [("20", Unit.PERCENT)]),
        ("expansion", {"growth_pct": "-10"}, "10% less consumption than today", [("-10", Unit.PERCENT)]),
        ("expansion", {"growth_pct": "0"}, "None planned", [("0", Unit.PERCENT)]),
        ("roof_sqm", "8000", "8000 m²", [("8000", Unit.COUNT)]),
        ("land_acres", "12.5", "12.5 acres", [("12.5", Unit.COUNT)]),
        ("contract_demand_kva", "6000", "6000 kVA", [("6000", Unit.KVA)]),
    ]  # fmt: skip
    for key, value, text, figs in cases:
        stored = answers.parse(key, value)
        assert answers.words(key, stored) == text
        got = answers.figures(key, {**row, "value": stored})
        assert [(format(f.value, "f"), f.unit) for f in got] == figs
        for f in got:
            assert [r.kind for r in f.source_refs] == [RefKind.CLIENT_STATED] and not f.is_sourced
            assert "client@example.test" in (f.source_refs[0].note or "")


def test_pinned_answers_are_read_from_the_profile() -> None:
    method = {
        "answers": {
            "roof_sqm": {"value": "8000"},
            "contract_demand_kva": {"value": "6000"},
        }
    }
    assert answers.roof_sqm(method, Decimal("100")) == Decimal("8000")
    assert answers.roof_sqm({}, Decimal("100")) == Decimal("100") and answers.roof_sqm(None, None) is None
    assert answers.contract_demand_kva(method) == Decimal("6000")
    assert answers.contract_demand_kva({}) is None
    assert answers.pinned(method) == {"roof_sqm": "8000", "contract_demand_kva": "6000"}
