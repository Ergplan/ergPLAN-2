"""Generation profiles supplied by the user (Stage 15): what is accepted, what is refused and why,
the CUF a file implies, and how the profile is laid on a run's year."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from ergplan.core.units import kw_from_kwh_block, kwh_from_kw_block
from ergplan.intake import generation
from ergplan.intake import generation_profiles as gp
from ergplan.intake.profile_synthesis import encode
from tests.intake.gen_files import csv_of, cuf_pct, shares

APR = date(2025, 4, 1)
D = Decimal


def test_kw_from_kwh_block_is_the_inverse_of_kwh_from_kw_block() -> None:
    assert kw_from_kwh_block(D("250"), 15) == D("1000")
    assert kw_from_kwh_block(D("250"), 60) == D("250")
    assert kwh_from_kw_block(kw_from_kwh_block(D("37.5"), 15), 15) == D("37.5")


# ---------------------------------------------------------------- accepted


def test_solar_hourly_in_kw_per_mw_is_accepted_with_the_cuf_it_implies() -> None:
    rows = shares("solar", APR, 60)
    p = gp.parse(csv_of(rows, "Generation (kW)", D(1000)), "solar", basis="per_mw")
    assert (p.resolution_min, p.first_day, p.rows, len(p.per_kw)) == (60, APR, 8760, 35040)
    assert (p.unit_in_file, p.basis, p.capacity_kw, p.stamped) == ("kW", "per_mw", None, "start")
    # the annual CUF is the mean of the file's own values (worked here from the rows)
    assert p.annual_cuf_pct == cuf_pct(rows)
    assert D("24") < p.annual_cuf_pct < D("27")
    # by month: the mean of that month's rows
    june = [v for t, v in rows if t.month == 6]
    assert p.monthly_cuf_pct[6] == (sum(june, D(0)) / len(june) * 100).quantize(D("0.01"))
    assert sorted(p.monthly_cuf_pct) == list(range(1, 13))
    assert p.monthly_cuf_pct[5] > p.monthly_cuf_pct[12]  # more in May than in December
    # an hourly value fills its four 15-minute blocks
    assert p.per_kw[12 * 4 : 12 * 4 + 4] == [rows[12][1].quantize(D("0.000001"))] * 4
    assert p.peak_pct == (max(v for _, v in rows) * 100).quantize(D("0.01"))


def test_solar_15_minute_in_kwh_for_a_stated_capacity_is_accepted() -> None:
    rows = shares("solar", APR, 15)
    # a 5 MW plant: kWh in a 15-minute block = fraction × 5000 kW × 0.25 h
    data = csv_of(rows, "energy_kwh", D(5000) * D("0.25"))
    p = gp.parse(data, "solar", basis="capacity", capacity_mw=D(5))
    assert (p.resolution_min, p.rows, len(p.per_kw)) == (15, 35040, 35040)
    assert (p.unit_in_file, p.basis, p.capacity_kw) == ("kWh", "capacity", D(5000))
    assert p.annual_cuf_pct == cuf_pct(rows)


def test_wind_hourly_as_a_fraction_without_timestamps_needs_the_first_day() -> None:
    rows = shares("wind", APR, 60)
    data = csv_of(rows, "cuf", timestamps=False)
    with pytest.raises(gp.GenerationFileError, match="no timestamp column: state the first day"):
        gp.parse(data, "wind")
    p = gp.parse(data, "wind", first_day=APR)
    assert (p.resolution_min, p.basis, p.stamped, p.capacity_kw) == (60, "per_unit", "none", None)
    assert p.annual_cuf_pct == cuf_pct(rows)
    assert D("29") < p.annual_cuf_pct < D("31")
    assert p.monthly_cuf_pct[7] > p.monthly_cuf_pct[1]  # the monsoon


def test_wind_15_minute_in_mw_for_a_stated_capacity_and_as_a_percentage() -> None:
    rows = shares("wind", APR, 15)
    p = gp.parse(csv_of(rows, "wind MW", D("12.5")), "wind", basis="capacity", capacity_mw=D("12.5"))
    assert (p.resolution_min, p.unit_in_file, p.capacity_kw) == (15, "MW", D(12500))
    assert p.annual_cuf_pct == cuf_pct(rows)
    pct = gp.parse(csv_of(rows, "CUF %", D(100)), "wind")
    assert (pct.unit_in_file, pct.basis) == ("CUF %", "per_unit")
    assert pct.per_kw == p.per_kw


def test_mwh_hourly_and_rows_stamped_at_the_end_of_the_period() -> None:
    rows = shares("solar", APR, 60)
    # 20 MW plant, MWh in the hour; each row stamped at the hour's end (first row 01:00)
    data = csv_of(rows, "MWh", D(20), stamp_end_minutes=60)
    p = gp.parse(data, "solar", basis="capacity", capacity_mw=D(20))
    assert (p.stamped, p.first_day, p.unit_in_file) == ("end", APR, "MWh")
    assert p.annual_cuf_pct == cuf_pct(rows)


def test_a_year_with_29_february_has_8784_hours() -> None:
    rows = shares("wind", date(2024, 1, 1), 60)
    assert len(rows) == 8784
    p = gp.parse(csv_of(rows, "pu"), "wind")
    assert (p.rows, len(p.per_kw), p.first_day) == (8784, 35136, date(2024, 1, 1))
    q = gp.parse(csv_of(shares("wind", date(2024, 1, 1), 15), "pu"), "wind")
    assert (q.rows, len(q.per_kw)) == (35136, 35136)


# ---------------------------------------------------------------- refused, with the reason


def _refused(data: bytes, technology: str = "solar", **kw: object) -> str:
    with pytest.raises(gp.GenerationFileError) as e:
        gp.parse(data, technology, **kw)  # type: ignore[arg-type]
    return str(e.value)


def test_a_missing_period_is_a_gap() -> None:
    rows = shares("solar", APR, 60)
    del rows[1000:1003]
    why = _refused(csv_of(rows, "kW", D(1000)), basis="per_mw")
    assert why == (
        "the file has a gap: row 1001 is 2025-05-12 15:00 and row 1002 is 2025-05-12 19:00, "
        "3 periods are missing"
    )


def _with_cell(data: bytes, row: int, cell: str) -> bytes:
    """The file with the value of a row (1 = the header) replaced."""
    lines = data.decode().split("\n")
    lines[row - 1] = lines[row - 1].split(",")[0] + "," + cell
    return "\n".join(lines).encode()


def test_a_blank_a_word_or_a_negative_value_is_refused_with_its_row() -> None:
    data = csv_of(shares("wind", APR, 60), "cuf")
    assert _refused(_with_cell(data, 51, ""), "wind") == "row 51 has no value: the file has a gap"
    assert _refused(_with_cell(data, 51, "n/a"), "wind") == 'row 51: "n/a" is not a number'
    assert _refused(_with_cell(data, 51, "NaN"), "wind") == 'row 51: "NaN" is not a number'
    assert _refused(_with_cell(data, 51, "-0.2"), "wind") == ("row 51: generation cannot be negative (-0.2)")


def test_a_wrong_length_is_refused() -> None:
    rows = shares("wind", APR, 60)
    why = _refused(csv_of(rows[:8000], "cuf"), "wind")
    assert why.startswith(
        "the file has 8,000 hourly values from 2025-04-01 (333.3 days); the year from that day needs 8,760."
    )
    assert "8,760 hourly values (8,784 in a year with 29 February) or 35,040 15-minute values" in why
    # without timestamps the count alone must be a year
    why = _refused(csv_of(rows[:8759], "cuf", timestamps=False), "wind", first_day=APR)
    assert why.startswith("the file has 8,759 values; a year is 8,760 hourly values")
    # a year that holds 29 February needs 8,784
    leap = shares("wind", date(2024, 1, 1), 60)[:8760]
    assert "needs 8,784" in _refused(csv_of(leap, "cuf"), "wind")
    one = csv_of(rows[:1], "cuf")
    assert _refused(one, "wind").startswith("the file has one value; a year is 8,760 hourly values")
    # more than a year
    more = [*rows, (rows[-1][0] + timedelta(hours=1), D("0.3"))]
    assert "8,761 hourly values" in _refused(csv_of(more, "cuf"), "wind")


def test_timestamps_out_of_order_twice_or_not_from_midnight_are_refused() -> None:
    rows = shares("wind", APR, 60)
    twice = [*rows[:10], rows[9], *rows[10:]]
    assert _refused(csv_of(twice, "cuf"), "wind") == (
        "the rows are not in order of time: row 11 is 2025-04-01 09:00 and row 12 is 2025-04-01 09:00 "
        "(the same time twice)"
    )
    swapped = [*rows[:10], rows[11], rows[10], *rows[12:]]
    assert "the file has a gap: row 11 is 2025-04-01 09:00 and row 12 is 2025-04-01 11:00" in _refused(
        csv_of(swapped, "cuf"), "wind"
    )
    late = [(t + timedelta(hours=5), v) for t, v in rows]
    assert _refused(csv_of(late, "cuf"), "wind") == (
        "the first row is at 05:00 on 2025-04-01: a year of generation starts at midnight"
    )
    half = [(datetime(2025, 4, 1) + timedelta(minutes=30 * i), v) for i, (_, v) in enumerate(rows)]
    assert "rows must be one hour or 15 minutes apart" in _refused(csv_of(half, "cuf"), "wind")
    bad = csv_of(rows, "cuf").replace(b"2025-04-03 00:00", b"3rd of April")
    assert _refused(bad, "wind").startswith('row 50: "3rd of April" is not a date and time')
    assert _refused(csv_of(rows, "cuf"), "wind", first_day=date(2025, 1, 1)) == (
        "the file starts on 2025-04-01 but the first day stated is 2025-01-01"
    )


def test_a_block_above_the_capacity_is_refused() -> None:
    rows = shares("solar", APR, 60)
    # the file is for 5 MW but 2 MW is stated: the midday rows are above the capacity
    why = _refused(csv_of(rows, "kW", D(5000)), basis="capacity", capacity_mw=D(2))
    assert why.startswith("row 10 (2025-04-01 08:00): ")
    assert "of the capacity of 2 MW; a plant cannot generate above its capacity" in why
    # a percentage in a column named as a fraction
    why = _refused(csv_of(rows, "cuf", D(100)))
    assert "is above 1 (the whole capacity); if the column is a percentage, name it CUF %" in why
    assert "is above the whole capacity" in _refused(csv_of(rows, "cuf %", D(150)))


def test_an_impossible_capacity_factor_is_refused() -> None:
    rows = shares("wind", APR, 60)
    high = [(t, D("0.9")) for t, _ in rows]
    assert _refused(csv_of(high, "cuf"), "wind") == (
        "the file implies an annual CUF of 90.00%; a wind plant is between 10% and 55% "
        "(check the unit, the capacity, and whether the values are per MW)"
    )
    # MW written in a column named kW: a thousandth of the capacity
    low = _refused(csv_of(shares("solar", APR, 60), "kW", D(1)), basis="per_mw")
    assert low.startswith("the file implies an annual CUF of 0.03%; a solar plant is between 10% and 35%")
    # a wind file given as solar: it generates at night
    night = _refused(csv_of(rows, "cuf"), "solar")
    assert night.startswith("row 2 (2025-04-01 00:00): a solar plant is generating")
    assert "at night (between 20:00 and 04:00)" in night and "Indian Standard Time" in night
    # a solar file whose clock is eleven hours out: the sun is up at midnight
    solar = shares("solar", APR, 15)
    vals = [v for _, v in solar]
    moved = [(t, vals[(i + 44) % len(vals)]) for i, (t, _) in enumerate(solar)]
    assert "at night" in _refused(csv_of(moved, "cuf"), "solar")


def test_a_unit_that_is_not_stated_or_is_ambiguous_is_refused() -> None:
    rows = shares("solar", APR, 60)
    assert _refused(csv_of(rows, "generation", D(1000)), basis="per_mw").startswith(
        'no column says its unit (the header has "generation"); name the generation column in kW or MW'
    )
    assert _refused(csv_of(rows, "kWh per kW", D(1)), basis="per_mw").startswith(
        'the column "kWh per kW" names more than one unit'
    )
    two = b"\n".join(
        line + (b",wind_kw" if i == 0 else b",1") if line else line
        for i, line in enumerate(csv_of(rows, "solar_kw", D(1000)).split(b"\n"))
    )
    assert _refused(two, basis="per_mw") == (
        'the file has more than one generation column ("solar_kw", "wind_kw"); give one profile in a file'
    )
    # power or energy without saying what capacity it is for
    assert _refused(csv_of(rows, "kW", D(1000))) == (
        "the file is in kW: say whether the values are for 1 MW of capacity or for a plant of a "
        "stated capacity"
    )
    assert _refused(csv_of(rows, "kW", D(1000)), basis="capacity") == (
        "the file is in kW for a plant: state the plant's capacity in MW (AC)"
    )
    headless = csv_of(rows, "cuf", timestamps=False).split(b"\n", 1)[1]
    assert _refused(headless, first_day=APR).startswith("the first row must be a header")
    assert _refused(b"timestamp,kw\n") == "the file has no rows of data under its header"
    assert _refused(csv_of(rows, "cuf"), "hydro") == "the technology is solar or wind"


# ---------------------------------------------------------------- on a run's year


def test_the_profile_is_laid_on_the_run_year_by_the_day_of_the_month() -> None:
    # a file from 1 January 2025 (no 29 February), a run year from 1 April 2027 (holds 29 Feb 2028)
    p = gp.parse(csv_of(shares("wind", date(2025, 1, 1), 60), "cuf"), "wind")
    out = gp.year_series(p.per_kw, p.first_day, date(2027, 4, 1))
    assert len(out) == 366 * 96
    assert out[:96] == p.per_kw[90 * 96 : 91 * 96]  # 1 April is the file's day 91
    jan1 = (date(2028, 1, 1) - date(2027, 4, 1)).days
    assert out[jan1 * 96 : (jan1 + 1) * 96] == p.per_kw[:96]
    feb29 = (date(2028, 2, 29) - date(2027, 4, 1)).days
    assert out[feb29 * 96 : (feb29 + 1) * 96] == p.per_kw[58 * 96 : 59 * 96]  # 28 February's
    # a file that holds 29 February on a year without it: that day is left out
    leap = gp.parse(csv_of(shares("wind", date(2024, 1, 1), 60), "cuf"), "wind")
    assert len(gp.year_series(leap.per_kw, leap.first_day, APR)) == 365 * 96


class _Row:
    """A stored profile, as much of it as `source_series` reads."""

    def __init__(self, p: gp.Parsed, provenance: str, name: str) -> None:
        self.id, self.sha256, self.filename, self.provenance = f"id-{name}", f"sha-{name}", name, provenance
        self.technology, self.first_day = p.technology, p.first_day
        self.series_per_kw, self.annual_cuf_pct = encode(p.per_kw), p.annual_cuf_pct


def test_source_series_takes_the_supplied_profile_and_the_library_curve_for_the_rest() -> None:
    solar = gp.parse(csv_of(shares("solar", APR, 60), "cuf"), "solar")
    row = _Row(solar, "measured", "plant.csv")
    supplied: dict[str, object] = {"solar:open_access": row}
    assert gp.source_series("wind", "open_access", "UP", APR, supplied) is None  # type: ignore[arg-type]
    assert gp.source_series("solar", "rooftop", "UP", APR, supplied) is None  # type: ignore[arg-type]
    use = gp.source_series("solar", "open_access", "UP", APR, supplied)  # type: ignore[arg-type]
    assert use is not None and use.kw == solar.per_kw
    assert use.cuf.value == solar.annual_cuf_pct and use.cuf.unit == "percent"
    [ref] = use.cuf.source_refs
    assert ref.kind == "measured"
    assert "plant.csv (measured at the plant), sha256 sha-plant.csv" in str(ref.note)
    assert use.pins == {
        "solar:open_access": {
            "id": "id-plant.csv",
            "sha256": "sha-plant.csv",
            "file": "plant.csv",
            "provenance": "measured",
        }
    }
    # a hybrid is half solar, half wind: the supplied solar and the library wind curve
    hy = gp.source_series("hybrid", "open_access", "UP", APR, supplied)  # type: ignore[arg-type]
    lib, lib_cuf = generation.profile(generation.curve_for("wind", "UP"), D(1), APR)
    assert hy is not None
    assert hy.kw[50] == solar.per_kw[50] / 2 + lib[50] / 2
    assert hy.cuf.value == ((solar.annual_cuf_pct + lib_cuf) / 2).quantize(D("0.0001"))
    assert [r.kind for r in hy.cuf.source_refs] == ["measured", "assumption"]
    assert list(hy.pins) == ["solar:open_access"]
    assert gp.supplied_cuf("hybrid", "open_access", "UP", supplied) == hy.cuf  # type: ignore[arg-type]
    assert gp.supplied_cuf("wind", "open_access", "UP", supplied) is None  # type: ignore[arg-type]
    # a stated profile is the developer's word, not a measurement
    row.provenance = "stated"
    st = gp.source_series("solar", "open_access", "UP", APR, supplied)  # type: ignore[arg-type]
    assert st is not None and st.cuf.source_refs[0].kind == "client_stated"
