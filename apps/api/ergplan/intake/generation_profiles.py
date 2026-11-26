"""Generation profiles supplied by the user (Stage 15): a year of solar or wind generation from the
developer or the plant's own meter, used in settlement and the optimizer in place of the library
curve (packages/profiles/cuf, `intake.generation`).

What a file must be (a file that is not is refused, with the reason in plain words; nothing is
filled in or guessed):

- CSV with a header row; one row per period, a whole year: 8,760 hourly values (8,784 when the year
  holds 29 February) or 35,040 15-minute values (35,136).
- One column of generation whose header names its unit: `kW` or `MW` (average power in the period),
  `kWh` or `MWh` (energy in the period), `CUF` or `pu` (a fraction of capacity, 0 to 1) or `CUF %`
  (0 to 100). A header with no unit, with two units, or two generation columns is refused.
- kW, MW, kWh and MWh are either for 1 MW of capacity (`per_mw`) or for a stated capacity in MW
  (`capacity`); the person says which. Capacity is AC, at the meter the profile is taken at.
- A timestamp column (Indian Standard Time), or, without one, the first day of the year the file
  covers. Rows start at midnight; a file stamped at the end of each period (first row 01:00 or
  00:15) is read as such. Every period is present, once, in order.
- Every value is a number, not negative, not above the capacity; the annual CUF is within what the
  technology can do (`CUF_BOUNDS`); solar does not generate between 20:00 and 04:00.

The profile is stored per kW of capacity at 15 minutes (an hourly value fills its four blocks), with
the file's sha256, and is never edited: a run pins its id and sha256 (invariant 5).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.sourced import RefKind, SourcedValue, SourceRef
from ..core.units import Unit, convert, kw_from_kwh_block
from ..db.models import Connection, GenerationProfile, Run, Scenario
from . import generation
from .measured_upload import _TS_FORMATS
from .profile_synthesis import decode, encode, year_days

TECHNOLOGIES = ("solar", "wind")
APPLIES_TO = ("open_access", "rooftop")
PROVENANCES = ("measured", "stated")
BASES = ("per_mw", "capacity")

# annual CUF (percent of AC capacity) a plant of the technology can have; outside it the file is
# refused. Assumptions: fixed-tilt solar in a poor site is above 10%, tracked and DC-overloaded solar
# below 35%; onshore wind in India between 10% and 55%.
CUF_BOUNDS: dict[str, tuple[Decimal, Decimal]] = {
    "solar": (Decimal(10), Decimal(35)),
    "wind": (Decimal(10), Decimal(55)),
}
# a block may read this far above capacity (rounding in the export), no further
OVER_CAPACITY = Decimal("1.0005")
# solar: no generation from 20:00 to 04:00 IST (the sun is down everywhere in India); a reading up
# to 1% of capacity in those hours is taken as meter noise
NIGHT_FROM, NIGHT_TO = 20, 4
NIGHT_NOISE = Decimal("0.01")
Q6 = Decimal("0.000001")
Q2 = Decimal("0.01")

LENGTHS = "8,760 hourly values (8,784 in a year with 29 February) or 35,040 15-minute values (35,136)"
UNITS = (
    "kW or MW (average power in the period), kWh or MWh (energy in the period), "
    "CUF or pu (0 to 1) or CUF % (0 to 100)"
)


class GenerationFileError(ValueError):
    """The file is refused; the message is the reason, in plain words."""


class ProfilePinMismatch(ValueError):
    """A replay's pinned profile is missing or is not the file the run used."""


@dataclass
class Parsed:
    technology: str
    resolution_min: int
    first_day: date
    per_kw: list[Decimal]  # average kW per kW of capacity, one per 15-minute block
    unit_in_file: str
    basis: str  # per_mw|capacity|per_unit
    capacity_kw: Decimal | None
    rows: int
    stamped: str  # start|end|none: how the rows were stamped
    annual_cuf_pct: Decimal = Decimal(0)
    monthly_cuf_pct: dict[int, Decimal] = field(default_factory=dict)
    peak_pct: Decimal = Decimal(0)


def _unit_of(header: str) -> str | None:
    """The unit a column header names, or None when it names none. Raises when it names two."""
    tokens = set(re.findall(r"[a-z]+|%", header.lower()))
    if {"per", "unit"} <= tokens:
        tokens.add("pu")
    found = [u for u in ("kwh", "mwh", "kw", "mw") if u in tokens]
    share = bool(tokens & {"cuf", "plf", "pu", "fraction"})
    percent = bool(tokens & {"percent", "pct", "%"})
    if len(found) > 1 or (found and (share or percent)):
        raise GenerationFileError(
            f'the column "{header.strip()}" names more than one unit; a generation column is in {UNITS}'
        )
    if found:
        return {"kwh": "kWh", "mwh": "MWh", "kw": "kW", "mw": "MW"}[found[0]]
    if percent:
        return "CUF %"
    return "CUF" if share else None


def _columns(header: list[str]) -> tuple[int | None, int, str]:
    c_ts = next(
        (
            i
            for i, h in enumerate(header)
            if re.sub(r"[^a-z]", "", h.lower()).startswith(("timestamp", "datetime", "time", "date"))
        ),
        None,
    )
    valued = [(i, u) for i, h in enumerate(header) if i != c_ts and (u := _unit_of(h)) is not None]
    if not valued:
        named = ", ".join(f'"{h.strip()}"' for i, h in enumerate(header) if i != c_ts and h.strip())
        raise GenerationFileError(
            f"no column says its unit (the header has {named or 'no generation column'}); "
            f"name the generation column in {UNITS}"
        )
    if len(valued) > 1:
        names = ", ".join(f'"{header[i].strip()}"' for i, _ in valued)
        raise GenerationFileError(
            f"the file has more than one generation column ({names}); give one profile in a file"
        )
    return c_ts, valued[0][0], valued[0][1]


def _stamp(s: str, hint: list[str]) -> datetime:
    s = s.strip()
    for fmt in (*hint, *_TS_FORMATS):
        try:
            t = datetime.strptime(s, fmt)
        except ValueError:
            continue
        hint[:] = [fmt]
        return t
    raise ValueError(s)


def _year_blocks(first: date, per_day: int) -> int:
    try:
        end = date(first.year + 1, first.month, first.day)
    except ValueError:  # 29 February
        end = date(first.year + 1, 3, 1)
    return (end - first).days * per_day


def parse(
    data: bytes,
    technology: str,
    basis: str | None = None,
    capacity_mw: Decimal | None = None,
    first_day: date | None = None,
) -> Parsed:
    """Read and validate a generation file. Raises GenerationFileError with the reason."""
    if technology not in TECHNOLOGIES:
        raise GenerationFileError("the technology is solar or wind")
    rows = [
        r for r in csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))) if "".join(r).strip()
    ]
    if len(rows) < 2:
        raise GenerationFileError("the file has no rows of data under its header")
    try:
        Decimal(rows[0][-1].replace(",", "").strip())
    except (InvalidOperation, IndexError):
        pass
    else:
        raise GenerationFileError(
            f"the first row must be a header that names the columns and the unit ({UNITS})"
        )
    c_ts, c_val, unit = _columns(rows[0])

    # the capacity the values are for
    cap_kw: Decimal | None
    if unit in ("CUF", "CUF %"):
        used_basis = "per_unit"
        cap_kw = convert(capacity_mw, Unit.MW, Unit.KW) if capacity_mw else None
    elif basis == "per_mw":
        used_basis, cap_kw = basis, convert(Decimal(1), Unit.MW, Unit.KW)
    elif basis == "capacity":
        if capacity_mw is None or capacity_mw <= 0:
            raise GenerationFileError(
                f"the file is in {unit} for a plant: state the plant's capacity in MW (AC)"
            )
        used_basis, cap_kw = basis, convert(capacity_mw, Unit.MW, Unit.KW)
    else:
        raise GenerationFileError(
            f"the file is in {unit}: say whether the values are for 1 MW of capacity or for a plant "
            "of a stated capacity"
        )

    # the rows: every one a timestamp (when the file has them) and a number that is not negative
    stamps: list[datetime] = []
    values: list[Decimal] = []
    hint: list[str] = []
    for n, r in enumerate(rows[1:], start=2):
        cell = r[c_val].replace(",", "").strip() if c_val < len(r) else ""
        if not cell:
            raise GenerationFileError(f"row {n} has no value: the file has a gap")
        try:
            v = Decimal(cell)
        except InvalidOperation:
            raise GenerationFileError(f'row {n}: "{cell}" is not a number') from None
        if not v.is_finite():
            raise GenerationFileError(f'row {n}: "{cell}" is not a number')
        if v < 0:
            raise GenerationFileError(f"row {n}: generation cannot be negative ({cell})")
        if c_ts is not None:
            try:
                stamps.append(_stamp(r[c_ts] if c_ts < len(r) else "", hint))
            except ValueError as e:
                raise GenerationFileError(
                    f'row {n}: "{e}" is not a date and time (for example 2025-04-01 00:00)'
                ) from None
        values.append(v)

    # the period, the first day, and every period present once and in order
    stamped = "none"
    if len(values) < 2:
        raise GenerationFileError(f"the file has one value; a year is {LENGTHS}")
    if c_ts is not None:
        step = stamps[1] - stamps[0]
        if step not in (timedelta(minutes=15), timedelta(minutes=60)):
            raise GenerationFileError(
                f"the first two rows are {stamps[0]:%Y-%m-%d %H:%M} and {stamps[1]:%Y-%m-%d %H:%M}: "
                "rows must be one hour or 15 minutes apart"
            )
        for i in range(1, len(stamps)):
            gap = stamps[i] - stamps[i - 1]
            if gap == step:
                continue
            a, b = f"{stamps[i - 1]:%Y-%m-%d %H:%M}", f"{stamps[i]:%Y-%m-%d %H:%M}"
            if gap > step:
                missing = int(gap / step) - 1
                raise GenerationFileError(
                    f"the file has a gap: row {i + 1} is {a} and row {i + 2} is {b}, "
                    f"{missing} {'period is' if missing == 1 else 'periods are'} missing"
                )
            raise GenerationFileError(
                f"the rows are not in order of time: row {i + 1} is {a} and row {i + 2} is {b}"
                + (" (the same time twice)" if gap == timedelta(0) else "")
            )
        res = int(step.total_seconds() // 60)
        t0 = stamps[0]
        if (t0.hour, t0.minute, t0.second) == (0, 0, 0):
            stamped = "start"
        elif t0 - step == datetime(t0.year, t0.month, t0.day):
            stamped = "end"  # each row stamped at the end of its period
        else:
            raise GenerationFileError(
                f"the first row is at {t0:%H:%M} on {t0:%Y-%m-%d}: a year of generation starts at midnight"
            )
        start = t0.date()
        if first_day is not None and first_day != start:
            raise GenerationFileError(f"the file starts on {start} but the first day stated is {first_day}")
    else:
        if len(values) in (8760, 8784):
            res = 60
        elif len(values) in (35040, 35136):
            res = 15
        else:
            raise GenerationFileError(f"the file has {len(values):,} values; a year is {LENGTHS}")
        if first_day is None:
            raise GenerationFileError(
                "the file has no timestamp column: state the first day of the year it covers"
            )
        start = first_day
    per_day = 24 * 60 // res
    expected = _year_blocks(start, per_day)
    if len(values) != expected:
        days = Decimal(len(values)) / per_day
        raise GenerationFileError(
            f"the file has {len(values):,} {'hourly' if res == 60 else '15-minute'} values from {start} "
            f"({days.quantize(Decimal('0.1'))} days); the year from that day needs {expected:,}. "
            f"A year is {LENGTHS}"
        )

    # to a fraction of capacity, and what is physically possible
    def at(i: int) -> str:
        t = datetime(start.year, start.month, start.day) + timedelta(minutes=res * i)
        return f"row {i + 2} ({t:%Y-%m-%d %H:%M})"

    share: list[Decimal] = []
    for i, v in enumerate(values):
        if unit == "CUF":
            f = v
        elif unit == "CUF %":
            f = convert(v, Unit.PERCENT, Unit.RATIO)
        else:
            assert cap_kw is not None
            kw = convert(v, Unit.MW, Unit.KW) if unit in ("MW", "MWh") else v
            if unit in ("kWh", "MWh"):
                kw = kw_from_kwh_block(kw, res)
            f = kw / cap_kw
        if f > OVER_CAPACITY:
            if unit == "CUF":
                why = f"{v} is above 1 (the whole capacity); if the column is a percentage, name it CUF %"
            elif unit == "CUF %":
                why = f"{v}% is above the whole capacity"
            else:
                assert cap_kw is not None
                why = (
                    f"{v} {unit} is {(f * 100).quantize(Q2)}% of the capacity of "
                    f"{convert(cap_kw, Unit.KW, Unit.MW).normalize():f} MW; a plant cannot generate above "
                    "its capacity (check the unit, the capacity, and whether the values are per MW)"
                )
            raise GenerationFileError(f"{at(i)}: {why}")
        hour = (i * res // 60) % 24
        if technology == "solar" and (hour >= NIGHT_FROM or hour < NIGHT_TO) and f > NIGHT_NOISE:
            raise GenerationFileError(
                f"{at(i)}: a solar plant is generating {(f * 100).quantize(Q2)}% of its capacity at night "
                f"(between {NIGHT_FROM}:00 and 0{NIGHT_TO}:00); the timestamps may not be Indian Standard "
                "Time, or this is not a solar profile"
            )
        share.append(f.quantize(Q6))
    per_kw = [f for f in share for _ in range(60 // 15 if res == 60 else 1)]
    days_file = [start + timedelta(days=k) for k in range(len(per_kw) // 96)]
    annual = (sum(per_kw, Decimal(0)) / len(per_kw) * 100).quantize(Decimal("0.0001"))
    lo, hi = CUF_BOUNDS[technology]
    if not lo <= annual <= hi:
        raise GenerationFileError(
            f"the file implies an annual CUF of {annual.quantize(Q2)}%; a {technology} plant is between "
            f"{lo}% and {hi}% (check the unit, the capacity, and whether the values are per MW)"
        )
    by_month: dict[int, list[Decimal]] = {}
    for k, d in enumerate(days_file):
        by_month.setdefault(d.month, []).extend(per_kw[k * 96 : (k + 1) * 96])
    monthly = {m: (sum(v, Decimal(0)) / len(v) * 100).quantize(Q2) for m, v in sorted(by_month.items())}
    return Parsed(
        technology=technology,
        resolution_min=res,
        first_day=start,
        per_kw=per_kw,
        unit_in_file=unit,
        basis=used_basis,
        capacity_kw=cap_kw if used_basis != "per_mw" else None,
        rows=len(values),
        stamped=stamped,
        annual_cuf_pct=annual,
        monthly_cuf_pct=monthly,
        peak_pct=(max(per_kw) * 100).quantize(Q2),
    )


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store(
    session: Session,
    site_id: str,
    p: Parsed,
    *,
    applies_to: str,
    provenance: str,
    filename: str,
    sha256: str,
    actor: str,
) -> GenerationProfile:
    if applies_to not in APPLIES_TO or provenance not in PROVENANCES:
        raise ValueError("applies_to or provenance")
    if applies_to == "rooftop" and p.technology != "solar":
        raise GenerationFileError("a rooftop profile is a solar profile")
    version = (
        session.scalar(
            select(func.max(GenerationProfile.version)).where(GenerationProfile.site_id == site_id)
        )
        or 0
    ) + 1
    row = GenerationProfile(
        site_id=site_id,
        version=version,
        technology=p.technology,
        applies_to=applies_to,
        provenance=provenance,
        filename=filename,
        sha256=sha256,
        resolution_min=p.resolution_min,
        first_day=p.first_day,
        blocks=len(p.per_kw),
        unit_in_file=p.unit_in_file,
        basis=p.basis,
        capacity_kw=p.capacity_kw,
        series_per_kw=encode(p.per_kw),
        annual_cuf_pct=p.annual_cuf_pct,
        monthly_cuf_pct={str(m): format(v, "f") for m, v in p.monthly_cuf_pct.items()},
        method={"rows": p.rows, "stamped": p.stamped, "peak_pct": format(p.peak_pct, "f")},
        created_by=actor,
    )
    session.add(row)
    session.flush()
    return row


def withdraw(session: Session, row: GenerationProfile, actor: str) -> None:
    """Stop using a profile in new runs. The row stays: runs that pinned it still replay."""
    if row.withdrawn_at is None:
        row.withdrawn_at = datetime.now(UTC)
        row.withdrawn_by = actor
        session.flush()


# ---------------------------------------------------------------- use in a run


def key(technology: str, applies_to: str) -> str:
    return f"{technology}:{applies_to}"


def source_ref(row: GenerationProfile) -> SourceRef:
    """Where a figure taken from the profile comes from: the plant's meter (`measured`) or the
    developer's statement (`client_stated`), never a sourced regulatory fact."""
    how = "measured at the plant" if row.provenance == "measured" else "stated by the developer"
    return SourceRef(
        kind=RefKind.MEASURED if row.provenance == "measured" else RefKind.CLIENT_STATED,
        note=f"{row.technology} generation profile {row.filename[:200]} ({how}), sha256 {row.sha256}",
    )


def in_use(session: Session, site_id: str) -> dict[str, GenerationProfile]:
    """The profile new runs take for each technology and use: the latest upload not withdrawn."""
    rows = session.scalars(
        select(GenerationProfile)
        .where(GenerationProfile.site_id == site_id, GenerationProfile.withdrawn_at.is_(None))
        .order_by(GenerationProfile.version)
    ).all()
    return {key(r.technology, r.applies_to): r for r in rows}


def for_run(session: Session, site_id: str, pins: dict[str, Any] | None) -> dict[str, GenerationProfile]:
    """The profiles a run generates with: those in use for the site, or (a replay) exactly those the
    original run pinned, checked against the pinned sha256."""
    if pins is None:
        return in_use(session, site_id)
    out: dict[str, GenerationProfile] = {}
    for k, pin in pins.items():
        row = session.get(GenerationProfile, str(pin.get("id")))
        if row is None or row.sha256 != pin.get("sha256"):
            raise ProfilePinMismatch(
                f"the generation profile this run used ({pin.get('file')}) is no longer stored as it was"
            )
        out[k] = row
    return out


def pin(row: GenerationProfile) -> dict[str, str]:
    return {"id": row.id, "sha256": row.sha256, "file": row.filename, "provenance": row.provenance}


def year_series(per_kw: list[Decimal], first_day: date, year_start: date) -> list[Decimal]:
    """The profile laid on the year a run works in: each day takes the file's values for the same
    day of the same month (29 February, when the file has none, takes 28 February's)."""
    at = {((d := first_day + timedelta(days=k)).month, d.day): k for k in range(len(per_kw) // 96)}
    out: list[Decimal] = []
    for d in year_days(year_start):
        k = at.get((d.month, d.day), at[(2, 28)])
        out += per_kw[k * 96 : (k + 1) * 96]
    return out


def _library_ref(technology: str, state: str) -> SourceRef:
    return SourceRef(
        kind=RefKind.ASSUMPTION,
        note=f"normative {technology} curve for {state} (packages/profiles/cuf)",
    )


@dataclass
class Use:
    kw: list[Decimal]  # 15-minute average kW per kW of capacity over the run's year
    cuf: SourcedValue  # the annual CUF the source generates at
    pins: dict[str, dict[str, str]]
    note: str


def source_series(
    source: str,
    applies_to: str,
    state: str,
    year_start: date,
    supplied: dict[str, GenerationProfile],
) -> Use | None:
    """Generation per kW for a source (`solar`, `wind`, or `hybrid` = half solar, half wind) from the
    supplied profiles. None when none applies: the caller keeps the library curve. A hybrid with only
    one of its halves supplied takes the library curve for the other."""
    parts = ["solar", "wind"] if source == "hybrid" else [source]
    rows = {t: supplied.get(key(t, applies_to)) for t in parts}
    if not any(rows.values()):
        return None
    share = Decimal(1) / len(parts)
    total: list[Decimal] | None = None
    cufs: list[Decimal] = []
    refs: list[SourceRef] = []
    pins: dict[str, dict[str, str]] = {}
    words: list[str] = []
    for t in parts:
        row = rows[t]
        if row is not None:
            series = year_series(decode(row.series_per_kw), row.first_day, year_start)
            cuf = Decimal(row.annual_cuf_pct)
            refs.append(source_ref(row))
            pins[key(t, applies_to)] = pin(row)
            words.append(
                f"{t}: the profile supplied ({row.filename}, "
                f"{'measured' if row.provenance == 'measured' else 'stated'}, "
                f"{cuf.quantize(Q2)}% CUF) is used in place of the library curve"
            )
        else:
            series, cuf = generation.profile(generation.curve_for(t, state), Decimal(1), year_start)
            refs.append(_library_ref(t, state))
            words.append(f"{t}: no profile supplied, the normative curve for {state} is used")
        cufs.append(cuf)
        total = (
            [v * share for v in series]
            if total is None
            else [a + v * share for a, v in zip(total, series, strict=True)]
        )
    assert total is not None
    blended = (sum(cufs, Decimal(0)) / len(cufs)).quantize(Decimal("0.0001"))
    return Use(
        kw=total,
        cuf=SourcedValue(value=blended, unit=Unit.PERCENT, source_refs=tuple(refs), label="CUF"),
        pins=pins,
        note="; ".join(words),
    )


def supplied_cuf(
    source: str, applies_to: str, state: str, supplied: dict[str, GenerationProfile]
) -> SourcedValue | None:
    """The annual CUF a run generates the source at when a profile is supplied for it (as
    `source_series` returns it, without building the year). None: the library curve applies."""
    parts = ["solar", "wind"] if source == "hybrid" else [source]
    rows = {t: supplied.get(key(t, applies_to)) for t in parts}
    if not any(rows.values()):
        return None
    cufs: list[Decimal] = []
    refs: list[SourceRef] = []
    for t in parts:
        row = rows[t]
        if row is not None:
            cufs.append(Decimal(row.annual_cuf_pct))
            refs.append(source_ref(row))
        else:
            cufs.append(generation.annual_cuf(generation.curve_for(t, state)))
            refs.append(_library_ref(t, state))
    blended = (sum(cufs, Decimal(0)) / len(cufs)).quantize(Decimal("0.0001"))
    return SourcedValue(value=blended, unit=Unit.PERCENT, source_refs=tuple(refs), label="CUF")


def runs_using(session: Session, row: GenerationProfile) -> list[tuple[Run, Scenario]]:
    """The runs of the site that pinned this profile, newest first."""
    q = (
        select(Run, Scenario)
        .join(Scenario, Scenario.id == Run.scenario_id)
        .join(Connection, Connection.id == Scenario.connection_id)
        .where(Connection.site_id == row.site_id)
        .order_by(Run.created_at.desc())
    )
    return [
        (run, sc)
        for run, sc in session.execute(q).all()
        if any(p.get("id") == row.id for p in ((run.pins or {}).get("generation_profiles") or {}).values())
    ]
