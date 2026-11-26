"""What the client states about a site that its bills do not say (Stage 15): the store of answers.

Each answer is a `client_stated` value in `site_answers` with who stated it and when. This module is
the only writer of that table. It validates an answer's value (`parse`), keeps the answer in force
per key (`put`, `withdraw`, `in_force`), says it in plain words (`words`), gives its numbers as
`SourcedValue`s of kind `client_stated` (`figures`), and turns the answers into what the profile
synthesis reads (`synthesis_inputs`).

A load profile records the answers it was made with under `method["answers"]` (`snapshot`), so a run,
which pins its load profile, pins the answers too: `pinned`, `growth_pct`, `contract_demand_kva` and
`roof_sqm` read them back from a profile, never from the live table, and an old run replays as it
was solved after an answer changes.

`intake.questions` holds the questions themselves and rebuilds the profile after an answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.sourced import RefKind, SourcedValue, SourceRef
from ..core.units import Unit
from ..db.models import Connection, SiteAnswer
from . import profile_synthesis as ps

KEYS = (
    "shifts",
    "weekly_off",
    "seasonal_shutdowns",
    "large_loads",
    "power_factor",
    "expansion",
    "roof_sqm",
    "land_acres",
    "contract_demand_kva",
)
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_HHMM = re.compile(r"^([01]\d|2[0-3]):(00|15|30|45)$|^24:00$")
_MMDD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_DAYS_IN = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


class UnknownQuestion(LookupError):
    pass


class InvalidAnswer(ValueError):
    pass


class _Hours(BaseModel):
    """From a time to a time, on quarter hours; an end at or before the start runs past midnight."""

    model_config = ConfigDict(extra="forbid")

    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("a time is HH:MM on a quarter hour, such as 06:00 or 22:30")
        return v

    @field_validator("start")
    @classmethod
    def _starts_in_the_day(cls, v: str) -> str:
        if v == "24:00":
            raise ValueError("a start of 24:00 is written 00:00")
        return v


def _weekdays(v: list[int]) -> list[int]:
    if any(not 0 <= d <= 6 for d in v):
        raise ValueError("days are 0 (Monday) to 6 (Sunday)")
    return sorted(set(v))


class ShiftHours(_Hours):
    pass


class Shutdown(BaseModel):
    """A shutdown that comes every year: from a day to a day, both included, as MM-DD."""

    model_config = ConfigDict(extra="forbid")

    start: str
    end: str
    name: str = Field(default="", max_length=80)

    @field_validator("start", "end")
    @classmethod
    def _mmdd(cls, v: str) -> str:
        if not _MMDD.match(v) or int(v[3:]) > _DAYS_IN[int(v[:2]) - 1]:
            raise ValueError("a day of the year is MM-DD, such as 05-15")
        return v


class LargeLoadIn(_Hours):
    name: str = Field(min_length=1, max_length=80)
    kw: Decimal = Field(gt=0, le=Decimal("500000"))
    days: list[int] | None = None  # None: every working day

    @field_validator("days")
    @classmethod
    def _d(cls, v: list[int] | None) -> list[int] | None:
        return None if v is None else _weekdays(v)


class Expansion(BaseModel):
    """Planned growth of the plant's consumption, in percent of today's (0: none planned)."""

    model_config = ConfigDict(extra="forbid")

    growth_pct: Decimal = Field(ge=Decimal("-90"), le=Decimal("500"))
    note: str = Field(default="", max_length=200)


class _Shifts(BaseModel):
    v: list[ShiftHours] = Field(min_length=1, max_length=4)


class _WeeklyOff(BaseModel):
    v: list[int] = Field(max_length=6)

    @field_validator("v")
    @classmethod
    def _d(cls, v: list[int]) -> list[int]:
        return _weekdays(v)


class _Shutdowns(BaseModel):
    v: list[Shutdown] = Field(max_length=12)


class _Loads(BaseModel):
    v: list[LargeLoadIn] = Field(max_length=12)


class _PowerFactor(BaseModel):
    v: Decimal = Field(ge=Decimal("0.3"), le=Decimal("1"))


class _Area(BaseModel):
    v: Decimal = Field(ge=0, le=Decimal("100000000"))


class _Demand(BaseModel):
    v: Decimal = Field(gt=0, le=Decimal("10000000"))


_MODELS: dict[str, type[BaseModel]] = {
    "shifts": _Shifts,
    "weekly_off": _WeeklyOff,
    "seasonal_shutdowns": _Shutdowns,
    "large_loads": _Loads,
    "power_factor": _PowerFactor,
    "roof_sqm": _Area,
    "land_acres": _Area,
    "contract_demand_kva": _Demand,
}
_EXPANSION = TypeAdapter(Expansion)


def parse(key: str, value: Any) -> Any:
    """The answer's value, validated, as the JSON that is stored (decimals as strings)."""
    if key not in KEYS:
        raise UnknownQuestion(key)
    try:
        if key == "expansion":
            if not isinstance(value, dict):  # a bare number is the growth in percent
                value = {"growth_pct": value}
            return _EXPANSION.validate_python(value).model_dump(mode="json")
        return _MODELS[key].model_validate({"v": value}).model_dump(mode="json")["v"]
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(x) for x in first["loc"] if x != "v")
        raise InvalidAnswer(f"{where + ': ' if where else ''}{first['msg']}") from e


# ---------------------------------------------------------------- the store


def in_force(session: Session, site_id: str) -> dict[str, SiteAnswer]:
    rows = session.scalars(
        select(SiteAnswer).where(SiteAnswer.site_id == site_id, SiteAnswer.superseded_at.is_(None))
    )
    return {r.key: r for r in rows}


def put(session: Session, site_id: str, key: str, value: Any, actor: str) -> tuple[SiteAnswer, bool]:
    """State or change an answer. Returns the answer in force and whether anything changed (the
    same value stated again changes nothing)."""
    stored = parse(key, value)
    now = datetime.now(UTC)
    old = in_force(session, site_id).get(key)
    if old is not None:
        if old.value == stored:
            return old, False
        old.superseded_at = now
        session.flush()
    row = SiteAnswer(site_id=site_id, key=key, value=stored, stated_by=actor, stated_at=now)
    session.add(row)
    session.flush()
    return row, True


def withdraw(session: Session, site_id: str, key: str, actor: str) -> bool:
    """Withdraw the answer in force (its row is kept, closed). False when there was none."""
    if key not in KEYS:
        raise UnknownQuestion(key)
    old = in_force(session, site_id).get(key)
    if old is None:
        return False
    old.superseded_at = datetime.now(UTC)
    old.withdrawn_by = actor
    session.flush()
    return True


def snapshot(session: Session, site_id: str) -> dict[str, Any]:
    """The answers in force as a load profile records them (`method["answers"]`)."""
    return {
        k: {"value": r.value, "stated_by": r.stated_by, "stated_at": r.stated_at.isoformat()}
        for k, r in sorted(in_force(session, site_id).items())
    }


def snapshot_for_connection(session: Session, connection_id: str) -> dict[str, Any]:
    conn = session.get(Connection, connection_id)
    return snapshot(session, conn.site_id) if conn is not None else {}


# ---------------------------------------------------------------- read back from a load profile


def pinned(method: dict[str, Any] | None) -> dict[str, Any]:
    """key → value of the answers a load profile was made with."""
    return {k: v["value"] for k, v in ((method or {}).get("answers") or {}).items()}


def growth_pct(method: dict[str, Any] | None) -> Decimal | None:
    """The planned expansion the profile carries, in percent; None when none (or zero) is stated."""
    e = pinned(method).get("expansion")
    g = Decimal(str(e["growth_pct"])) if e else Decimal(0)
    return g if g != 0 else None


def contract_demand_kva(method: dict[str, Any] | None) -> Decimal | None:
    v = pinned(method).get("contract_demand_kva")
    return Decimal(str(v)) if v is not None else None


def roof_sqm(method: dict[str, Any] | None, otherwise: Decimal | None) -> Decimal | None:
    """The roof the profile's answers state; `otherwise` (the site's typed value) when they state none."""
    v = pinned(method).get("roof_sqm")
    return Decimal(str(v)) if v is not None else otherwise


def stated_ref(key: str, row: dict[str, Any] | SiteAnswer) -> SourceRef:
    by = row.stated_by if isinstance(row, SiteAnswer) else row.get("stated_by")
    at = row.stated_at.isoformat() if isinstance(row, SiteAnswer) else str(row.get("stated_at") or "")
    return SourceRef(
        kind=RefKind.CLIENT_STATED,
        note=f"{LABELS[key]}: the client's answer, stated by {by} on {at[:10]}",
    )


def power_factor(session: Session, site_id: str) -> SourcedValue | None:
    """The power factor the client states, for where kVAh billing needs one and no bill prints it."""
    row = in_force(session, site_id).get("power_factor")
    if row is None:
        return None
    return SourcedValue(
        value=Decimal(str(row.value)),
        unit=Unit.RATIO,
        source_refs=(stated_ref("power_factor", row),),
        label="Power factor (stated)",
    )


SHAPING = ("shifts", "weekly_off", "seasonal_shutdowns", "large_loads", "power_factor")


def shaping_refs(method: dict[str, Any] | None) -> tuple[SourceRef, ...]:
    """The `client_stated` refs of the answers that shaped a synthesised profile's series."""
    rows = (method or {}).get("answers") or {}
    return tuple(stated_ref(k, rows[k]) for k in SHAPING if k in rows)


# ---------------------------------------------------------------- words and figures

LABELS = {
    "shifts": "Shifts",
    "weekly_off": "Weekly off",
    "seasonal_shutdowns": "Seasonal shutdowns",
    "large_loads": "Large loads",
    "power_factor": "Power factor",
    "expansion": "Planned expansion",
    "roof_sqm": "Roof available",
    "land_acres": "Land available",
    "contract_demand_kva": "Contract demand intended",
}


def plain(v: Any) -> str:
    d = Decimal(str(v))
    return format(d.quantize(Decimal(1)) if d == d.to_integral_value() else d.normalize(), "f")


def _day(mmdd: str) -> str:
    return f"{int(mmdd[3:])} {_MONTHS[int(mmdd[:2]) - 1]}"


def words(key: str, value: Any) -> str:
    """An answer in plain words, for the questions screen, the report and the MCP tools."""
    if key == "shifts":
        return ", ".join(f"{s['start']} to {s['end']}" for s in value)
    if key == "weekly_off":
        return ", ".join(DAY_NAMES[d] for d in value) or "No weekly off"
    if key == "seasonal_shutdowns":
        return (
            "; ".join(
                f"{_day(s['start'])} to {_day(s['end'])}" + (f" ({s['name']})" if s.get("name") else "")
                for s in value
            )
            or "None"
        )
    if key == "large_loads":
        return (
            "; ".join(
                f"{x['name']}: {plain(x['kw'])} kW, {x['start']} to {x['end']}"
                + (f", {', '.join(DAY_NAMES[d][:3] for d in x['days'])}" if x.get("days") else "")
                for x in value
            )
            or "None"
        )
    if key == "power_factor":
        return plain(value)
    if key == "expansion":
        g = Decimal(str(value["growth_pct"]))
        if g == 0:
            return "None planned"
        text = f"{plain(abs(g))}% {'more' if g > 0 else 'less'} consumption than today"
        return text + (f" ({value['note']})" if value.get("note") else "")
    if key == "roof_sqm":
        return f"{plain(value)} m²"
    if key == "land_acres":
        return f"{plain(value)} acres"
    if key == "contract_demand_kva":
        return f"{plain(value)} kVA"
    raise UnknownQuestion(key)


def figures(key: str, row: dict[str, Any] | SiteAnswer) -> list[SourcedValue]:
    """The numbers of an answer, each a `client_stated` SourcedValue (invariant 1)."""
    value = row.value if isinstance(row, SiteAnswer) else row["value"]
    ref = (stated_ref(key, row),)

    def sv(v: Any, unit: Unit, label: str) -> SourcedValue:
        return SourcedValue(value=Decimal(str(v)), unit=unit, source_refs=ref, label=label)

    if key == "power_factor":
        return [sv(value, Unit.RATIO, "Power factor")]
    if key == "expansion":
        return [sv(value["growth_pct"], Unit.PERCENT, "Planned growth in consumption")]
    if key == "roof_sqm":
        return [sv(value, Unit.COUNT, "Roof available, m²")]
    if key == "land_acres":
        return [sv(value, Unit.COUNT, "Land available, acres")]
    if key == "contract_demand_kva":
        return [sv(value, Unit.KVA, "Contract demand intended")]
    if key == "large_loads":
        return [sv(x["kw"], Unit.KW, x["name"]) for x in value]
    return []


# ---------------------------------------------------------------- into the synthesis


@dataclass(frozen=True)
class SynthesisInputs:
    """What the answers change in `profile_synthesis.synthesise_year`."""

    weekly_off: tuple[int, ...]
    week: np.ndarray | None
    shutdowns: tuple[tuple[str, str], ...]
    large_loads: tuple[ps.LargeLoad, ...]
    power_factor: Decimal | None


def synthesis_inputs(values: dict[str, Any], shape_id: str, weekly_off: tuple[int, ...]) -> SynthesisInputs:
    """`values`: key → value of the answers in force; `weekly_off`: the intake's (the answer wins)."""
    off = tuple(values["weekly_off"]) if "weekly_off" in values else weekly_off
    week = None
    if "shifts" in values:
        week = ps.week_from_shifts(shape_id, tuple((s["start"], s["end"]) for s in values["shifts"]), off)
    elif "weekly_off" in values:
        week = ps.week_for_working_days(shape_id, off)
    return SynthesisInputs(
        weekly_off=off,
        week=week,
        shutdowns=tuple((s["start"], s["end"]) for s in values.get("seasonal_shutdowns") or []),
        large_loads=tuple(
            ps.LargeLoad(
                kw=Decimal(str(x["kw"])),
                start=x["start"],
                end=x["end"],
                days=tuple(x["days"]) if x.get("days") else None,
                name=x["name"],
            )
            for x in values.get("large_loads") or []
        ),
        power_factor=Decimal(str(values["power_factor"])) if "power_factor" in values else None,
    )
