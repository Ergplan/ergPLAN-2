"""Generation files for the tests: a year of solar or wind as CSV, built here by hand (not by the
app's code), hourly or 15-minute, in any accepted unit."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from decimal import Decimal

Q = Decimal("0.0001")


def solar_share(day: date, hour: float) -> Decimal:
    """A fraction of capacity: a sine between 06:00 and 18:00, peak 0.80, a little more in summer."""
    if not 6 <= hour < 18:
        return Decimal(0)
    season = 1 + 0.1 * math.cos(2 * math.pi * (day.timetuple().tm_yday - 135) / 365)
    return Decimal(str(0.8 * season * math.sin(math.pi * (hour - 6) / 12))).quantize(Q)


def wind_share(day: date, hour: float) -> Decimal:
    """A fraction of capacity: 0.30 on average, more in the monsoon and in the evening."""
    v = (
        0.30
        + 0.18 * math.cos(2 * math.pi * (day.timetuple().tm_yday - 195) / 365)
        + 0.08 * math.cos(2 * math.pi * (hour - 20) / 24)
    )
    return Decimal(str(v)).quantize(Q)


def shares(technology: str, first: date, minutes: int) -> list[tuple[datetime, Decimal]]:
    """(period start, fraction of capacity) for the year from `first`."""
    fn = solar_share if technology == "solar" else wind_share
    try:
        end = date(first.year + 1, first.month, first.day)
    except ValueError:
        end = date(first.year + 1, 3, 1)
    out = []
    t = datetime(first.year, first.month, first.day)
    stop = datetime(end.year, end.month, end.day)
    while t < stop:
        # the middle of the period, so an hourly file and a 15-minute file are both fair samples
        mid = t.hour + t.minute / 60 + minutes / 120
        out.append((t, fn(t.date(), mid)))
        t += timedelta(minutes=minutes)
    return out


def csv_of(
    rows: list[tuple[datetime, Decimal]],
    header: str,
    factor: Decimal = Decimal(1),
    timestamps: bool = True,
    stamp_end_minutes: int = 0,
) -> bytes:
    """`factor` turns the fraction into the column's unit (kW for 1 MW: 1000)."""
    lines = [f"timestamp,{header}" if timestamps else header]
    for t, v in rows:
        val = format(v * factor, "f")
        stamp = t + timedelta(minutes=stamp_end_minutes)
        lines.append(f"{stamp:%Y-%m-%d %H:%M},{val}" if timestamps else val)
    return ("\n".join(lines) + "\n").encode()


def cuf_pct(rows: list[tuple[datetime, Decimal]]) -> Decimal:
    return (sum((v for _, v in rows), Decimal(0)) / len(rows) * 100).quantize(Decimal("0.0001"))
