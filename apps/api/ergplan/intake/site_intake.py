"""SiteIntake: everything the engines will read about a client's site, filled by any door — bills,
the typed form, or (Stage 7) the voice tools, which reuse these schemas.

Every field is a `Fact`: a value, its provenance (`bill`, `client_stated`, `assumption`) and a
SourceRef. Defaults come from `defaults.py` and are recorded as assumptions; values from bills
carry `bill` refs naming the bill and the billx path. `build()` persists client, site, connection,
assets and the synthesised load profile.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.sourced import RefKind, SourceRef
from ..db.models import Asset, Bill, Client, Connection, LoadProfile, Site
from ..rulebook.models import RuleView
from ..rulebook.scope import norm_code
from . import answers as site_answers
from . import bills as bill_store
from . import defaults, measured_upload, recognise
from .bill_fields import parse_month
from .profile_synthesis import MonthInput, ShapeNotFound, TodWindow, encode, shape, shape_ids, synthesise_year

T = TypeVar("T")
Provenance = Literal["bill", "client_stated", "assumption"]
_KIND_OF = {"bill": RefKind.BILL, "client_stated": RefKind.CLIENT_STATED, "assumption": RefKind.ASSUMPTION}


class Fact(BaseModel, Generic[T]):  # noqa: UP046 (pydantic generic model)
    """One input value with where it came from. A typed value needs no ref (client_stated is made);
    an assumption needs a note; a bill value needs a bill ref."""

    model_config = ConfigDict(extra="forbid")

    value: T
    provenance: Provenance = "client_stated"
    ref: SourceRef | None = None

    @model_validator(mode="after")
    def _ref_matches_provenance(self) -> Fact[T]:
        if self.ref is None:
            if self.provenance != "client_stated":
                raise ValueError(f"a {self.provenance} value needs its source_ref")
            self.ref = SourceRef(kind=RefKind.CLIENT_STATED)
        if self.ref.kind != _KIND_OF[self.provenance]:
            raise ValueError(f"provenance {self.provenance} with a {self.ref.kind} ref")
        return self


def stated(v: Any) -> Fact[Any]:
    return Fact(value=v)


def assumed(a: defaults.Assumed) -> Fact[Any]:
    return Fact(value=a.value, provenance="assumption", ref=a.ref)


def from_bill(v: bill_store.BillValue, value: Any) -> Fact[Any]:
    return Fact(value=value, provenance="bill", ref=v.ref)


_HHMM = re.compile(r"^([01]\d|2[0-4]):[0-5]\d$")


class TodEnergy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str
    end: str
    energy: Fact[Decimal]
    slot: str | None = None  # the bill's slot name, when the window came from a bill
    # the approved rule the hours were taken from, when the bill's slot prints a name and no hours
    hours_ref: SourceRef | None = None

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v) or (v.startswith("24") and v != "24:00") or int(v[3:]) % 15:
            raise ValueError("a time is HH:MM on a quarter hour, 00:00–24:00")
        return v


class MonthFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    month: date
    energy: Fact[Decimal]
    energy_unit: Literal["kWh", "kVAh"] = "kWh"
    tod: list[TodEnergy] = Field(default_factory=list)
    max_kva: Fact[Decimal] | None = None
    power_factor: Fact[Decimal] | None = None
    bill_inr: Fact[Decimal] | None = None
    bill_id: str | None = None
    shutdown_days: list[int] = Field(default_factory=list)
    unmapped_tod_slots: list[str] = Field(default_factory=list)
    # why the hours of those slots could not be taken from the rulebook either
    unmapped_tod_reason: str | None = None
    # how a bill's month was chosen: from its supply period, or (the bill states none) its bill month,
    # which is usually the month after the supply; None for a typed month
    month_basis: Literal["supply_period", "bill_month"] | None = None

    @field_validator("month")
    @classmethod
    def _first_of_month(cls, v: date) -> date:
        return v.replace(day=1)

    @model_validator(mode="after")
    def _checks(self) -> MonthFacts:
        if self.energy.value <= 0:
            raise ValueError("monthly energy must be positive")
        if self.max_kva is not None and self.max_kva.value <= 0:
            raise ValueError("maximum demand must be positive")
        if self.power_factor is not None and not (Decimal("0.3") <= self.power_factor.value <= Decimal("1")):
            raise ValueError("power factor must be between 0.3 and 1")
        n = calendar.monthrange(self.month.year, self.month.month)[1]
        if any(not 1 <= d <= n for d in self.shutdown_days):
            raise ValueError(f"shutdown days must be within 1–{n}")
        if self.tod:
            tod_sum = sum((t.energy.value for t in self.tod), Decimal(0))
            if tod_sum > self.energy.value * Decimal("1.001"):
                raise ValueError("the ToD split adds up to more than the month's energy")
        return self


ASSET_KINDS = ("rooftop_pv", "captive_thermal", "waste_heat", "bess", "dg")


class CaptiveThermal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fuel: str
    gcv_kcal_per_kg: Decimal | None = None
    heat_rate_kcal_per_kwh: Decimal | None = None
    capacity_mw: Decimal = Field(gt=0)
    technical_minimum_pct: Decimal = Field(ge=0, le=100)
    ramp_up_mw_per_block: Decimal | None = Field(default=None, gt=0)
    ramp_down_mw_per_block: Decimal | None = Field(default=None, gt=0)
    min_up_blocks: int | None = Field(default=None, ge=0)
    min_down_blocks: int | None = Field(default=None, ge=0)
    variable_cost_inr_per_kwh: Decimal | None = Field(default=None, ge=0)
    fuel_price_inr_per_tonne: Decimal | None = Field(default=None, ge=0)
    aux_consumption_pct: Decimal = Field(default=Decimal("8"), ge=0, le=30)
    must_run: bool = False

    @model_validator(mode="after")
    def _cost(self) -> CaptiveThermal:
        if self.variable_cost_inr_per_kwh is None and not (
            self.fuel_price_inr_per_tonne is not None and self.gcv_kcal_per_kg and self.heat_rate_kcal_per_kwh
        ):
            raise ValueError("give variable_cost_inr_per_kwh, or fuel_price with gcv and heat_rate")
        return self


class RooftopPv(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity_kwp: Decimal = Field(gt=0)
    metering: Literal["net", "gross", "behind_the_meter"] = "behind_the_meter"
    commissioned: date | None = None


class WasteHeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity_mw: Decimal = Field(gt=0)
    availability_pct: Decimal = Field(default=Decimal("90"), ge=0, le=100)


class Bess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    power_mw: Decimal = Field(gt=0)
    energy_mwh: Decimal = Field(gt=0)
    round_trip_efficiency_pct: Decimal = Field(default=Decimal("88"), gt=0, le=100)


class DieselGenerator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity_kva: Decimal = Field(gt=0)
    cost_inr_per_kwh: Decimal | None = Field(default=None, ge=0)


ASSET_MODELS: dict[str, type[BaseModel]] = {
    "rooftop_pv": RooftopPv,
    "captive_thermal": CaptiveThermal,
    "waste_heat": WasteHeat,
    "bess": Bess,
    "dg": DieselGenerator,
}


class AssetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["rooftop_pv", "captive_thermal", "waste_heat", "bess", "dg"]
    params: dict[str, Any]
    provenance: Provenance = "client_stated"

    @model_validator(mode="after")
    def _params_valid(self) -> AssetIn:
        self.params = (
            ASSET_MODELS[self.kind].model_validate(self.params).model_dump(mode="json", exclude_none=True)
        )
        return self


class Shift(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str
    end: str
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5])

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("a time is HH:MM")
        return v

    @field_validator("days")
    @classmethod
    def _weekdays(cls, v: list[int]) -> list[int]:
        if any(not 0 <= d <= 6 for d in v):
            raise ValueError("days are 0 (Mon) to 6 (Sun)")
        return v


OPEN_ACCESS_VALUES = (
    "captive",
    "group_captive",
    "third_party",
    "exchange",
    "geoa",
    "open_access",
    "green_tariff",
    "none",
)


class SiteIntake(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_name: Fact[str]
    site_name: Fact[str]
    state: Fact[str]
    discom: Fact[str]
    category: Fact[str]
    voltage_kv: Fact[Decimal]
    contract_demand_kva: Fact[Decimal]
    consumer_number: Fact[str] | None = None
    rate_block: Fact[str] | None = None
    # the tariff as the bill shows it (or as typed): what the grid costs where ergPLAN holds no
    # approved retail schedule for the DISCOM (elsewhere the approved schedule is used)
    energy_charge_inr_per_unit: Fact[Decimal] | None = None
    fixed_charge_inr_per_kva_month: Fact[Decimal] | None = None
    billing_basis: Fact[str] | None = None  # kwh | kvah
    industry_shape: Fact[str]
    weekly_off: Fact[list[int]]
    shifts: Fact[list[Shift]] | None = None
    land_acres: Fact[Decimal] | None = None
    roof_sqm: Fact[Decimal] | None = None
    # the open access the consumer already takes, as the bill shows it (recognise.py) or the client
    # states it: captive | group_captive | third_party | exchange | geoa | open_access (kind not
    # printed) | green_tariff | none
    open_access: Fact[str] | None = None
    months: list[MonthFacts] = Field(min_length=1, max_length=12)
    assets: list[AssetIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> SiteIntake:
        st = defaults.state_of(self.discom.value)
        if st is None:
            raise ValueError(f"unknown DISCOM {self.discom.value!r}")
        if st != self.state.value:
            raise ValueError(f"{self.discom.value} is in {st}, not {self.state.value}")
        if self.voltage_kv.value <= 0 or self.contract_demand_kva.value <= 0:
            raise ValueError("voltage and contract demand must be positive")
        if self.industry_shape.value not in shape_ids():
            raise ValueError(
                f"unknown industry shape {self.industry_shape.value!r}; one of {', '.join(shape_ids())}"
            )
        if self.open_access is not None and self.open_access.value not in OPEN_ACCESS_VALUES:
            raise ValueError(
                f"open access {self.open_access.value!r} is not one of {', '.join(OPEN_ACCESS_VALUES)}"
            )
        if any(not 0 <= d <= 6 for d in self.weekly_off.value):
            raise ValueError("weekly off days are 0 (Mon) to 6 (Sun)")
        months = [m.month for m in self.months]
        if len(set(months)) != len(months):
            raise ValueError("each month may appear once")
        return self

    def warnings(self) -> list[str]:
        out = []
        cd = self.contract_demand_kva.value
        for m in self.months:
            if m.max_kva is not None and m.max_kva.value > cd * Decimal("1.2"):
                out.append(
                    f"{m.month:%b %Y}: recorded demand {m.max_kva.value} kVA exceeds "
                    f"contract demand {cd} kVA by more than 20%"
                )
            if m.month_basis == "bill_month":
                out.append(
                    f"{m.month:%b %Y}: the bill states no supply period, so its energy is filed under its "
                    "bill month, which is usually the month after the supply; check the month"
                )
            if m.unmapped_tod_slots:
                why = f", and {m.unmapped_tod_reason}" if m.unmapped_tod_reason else ""
                out.append(
                    f"{m.month:%b %Y}: the bill's ToD slots {', '.join(m.unmapped_tod_slots)} "
                    f"do not name their hours{why}; "
                    "the month's total is used and the day's shape comes from the operating pattern "
                    "until the hours are typed"
                )
        if len(self.months) < 12:
            out.append(f"{len(self.months)} of 12 months given; the other months are extrapolated from them")
        return out


# ---------------------------------------------------------------- filling from bills

_WINDOW = re.compile(r"(\d{1,2})[:.](\d{2})\s*(?:hrs?)?\s*(?:-|–|to)\s*(\d{1,2})[:.](\d{2})")


def window_of(slot: str | None) -> tuple[str, str] | None:
    """A bill ToD slot that prints its hours ('19:00-03:00', '05.00 to 10.00 hrs') → (start, end)."""
    if not slot:
        return None
    m = _WINDOW.search(slot)
    if not m:
        from .bill_reading import slot_hours  # hours without minutes: 'TOD-1 (05 to 11)'

        return slot_hours(slot)
    a, b = f"{int(m.group(1)):02d}:{m.group(2)}", f"{int(m.group(3)):02d}:{m.group(4)}"
    return (a, b) if _HHMM.match(a) and _HHMM.match(b) else None


def _hours(t: TodEnergy) -> int:
    """A window's length in quarter hours (23:00–05:00 wraps midnight)."""
    a = int(t.start[:2]) * 4 + int(t.start[3:]) // 15
    b = int(t.end[:2]) * 4 + int(t.end[3:]) // 15
    return (b - a) % 96 or 96


def one_scheme(
    tod: list[TodEnergy], month_energy: Decimal, approved: set[tuple[str, str]]
) -> list[TodEnergy]:
    """A bill that prints two ToD schemes for the same month (UPPCL meters register the old four
    slots and the new four, each adding up to the month) gives one: the printed slots are split,
    into runs of consecutive windows that cover the day exactly once; of the complete runs, the one whose
    windows the approved ToD rules name, else the one nearest the month's energy, else the last
    printed. Empty when no run covers the day (the month's total is then used)."""
    schemes: list[list[TodEnergy]] = []
    for i in range(len(tod)):
        quarters = 0
        for j in range(i, len(tod)):
            if j > i and tod[j].start != tod[j - 1].end:
                break  # the windows must follow on from each other
            quarters += _hours(tod[j])
            if quarters > 96:
                break
            if quarters == 96 and tod[j].end == tod[i].start:
                schemes.append(tod[i : j + 1])
                break
    if not schemes:
        return []

    def rank(sc: list[TodEnergy]) -> tuple[int, Decimal, int]:
        named = sum(1 for t in sc if (t.start, t.end) in approved)
        gap = abs(sum((t.energy.value for t in sc), Decimal(0)) - month_energy)
        return (-named, gap, -schemes.index(sc))

    return min(schemes, key=rank)


def _tod_rules(session: Session, bill: Bill, month: date, on: date) -> tuple[str | None, list[RuleView]]:
    """The bill's DISCOM and the approved ToD rules in force for it on `on` that apply to the
    calendar month `month` (a rule scoped to the summer months is not a winter month's rule)."""
    from ..rulebook import repository
    from ..rulebook.service import UnknownJurisdiction, jurisdiction_chain

    discom = recognise.discom_of(bill).discom
    if not discom:
        return None, []
    try:
        chain = jurisdiction_chain(session, discom)
    except UnknownJurisdiction:
        return discom, []
    rules = [
        r
        for r in repository.approved_rules(session, "tod_slots", chain, on)
        if not r.scope.get("months") or month.month in r.scope["months"]
    ]
    return discom, rules


def _slots_in(rules: list[RuleView], month: int) -> list[tuple[RuleView, dict[str, Any]]]:
    return [
        (r, sl)
        for r in rules
        for sl in r.body.get("slots") or []
        if not sl.get("months") or month in sl["months"]
    ]


def _approved_windows(
    session: Session, bill: Bill, month: date, on: date | None = None
) -> set[tuple[str, str]]:
    """The ToD windows the approved rules name for the bill's DISCOM in that month (the rules in
    force on `on`: the end of the supply period, as the bill replay takes it; else the month)."""
    _, rules = _tod_rules(session, bill, month, on or month)
    return {(str(sl.get("start")), str(sl.get("end"))) for _, sl in _slots_in(rules, month.month)}


def _name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def hours_by_name(
    labels: list[str], rules: list[RuleView], month: int
) -> dict[str, tuple[str, str, RuleView]]:
    """The hours of bill ToD slots that print a name and no hours ('TOD-1', 'Peak'), from approved
    ToD rules that name a slot the same way (compared without case, spaces or punctuation). All or
    nothing: empty unless every label is the name of exactly one window in those rules. A name the
    order does not state, or states for two different windows, is never guessed."""
    named: dict[str, dict[tuple[str, str], RuleView]] = {}
    for r, sl in _slots_in(rules, month):
        key = _name_key(str(sl.get("name") or ""))
        if key:
            named.setdefault(key, {}).setdefault((str(sl.get("start")), str(sl.get("end"))), r)
    out: dict[str, tuple[str, str, RuleView]] = {}
    for label in labels:
        hit = named.get(_name_key(label)) or {}
        if len(hit) != 1:
            return {}
        [((start, end), rule)] = hit.items()
        out[label] = (start, end, rule)
    return out


def _no_hours_reason(discom: str | None, rules: list[RuleView], month: date) -> str:
    """Why the rulebook does not give the hours of a bill's named slots, in words for the client."""
    if not discom:
        return "the DISCOM was not recognised, so no approved ToD rule could be consulted for them"
    if not rules:
        return f"no approved ToD rule for {discom.upper()} is in force for {month:%b %Y}"
    names = list(dict.fromkeys(str(sl.get("name")) for _, sl in _slots_in(rules, month.month)))
    labels = list(dict.fromkeys(r.label for r in rules if r.label))
    which = f" ({'; '.join(labels)})" if labels else ""
    return (
        "the order's hours for these names are not in the rulebook: the approved ToD rule for "
        f"{discom.upper()}{which} names its slots {', '.join(names)}"
    )


def _day(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text.strip()[:10]) if text else None
    except ValueError:
        return None


def supply_month(start: date | None, end: date | None) -> date | None:
    """The calendar month a supply period belongs to: the month holding most of its days (both ends
    counted); a tie goes to the later month, the one the closing reading falls in. A period with one
    end stated is taken as the thirty days up to (or from) it. None when no end is stated, the end
    precedes the start, or the period is longer than a quarter (a misread date, not a period)."""
    if start is None and end is None:
        return None
    if start is None:
        assert end is not None
        start = end - timedelta(days=29)
    if end is None:
        end = start + timedelta(days=29)
    if end < start or (end - start).days > 92:
        return None
    days: dict[date, int] = {}
    first = start.replace(day=1)
    while first <= end:
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        days[first] = (min(end, last) - max(start, first)).days + 1
        first = last + timedelta(days=1)
    return max(days, key=lambda k: (days[k], k))


def month_from_bill(session: Session, bill: Bill) -> MonthFacts:
    """A month's facts from one accepted bill. The month is the one the energy was used in: the
    supply period's (period.from to period.to). Only a bill that states no usable period is filed
    under its bill month, and says so (`month_basis`), since a bill month is usually the month after."""
    v = bill_store.values(session, bill)
    start = _day(v["period.from"].value) if "period.from" in v else None
    end = _day(v["period.to"].value) if "period.to" in v else None
    month = supply_month(start, end)
    basis: Literal["supply_period", "bill_month"] = "supply_period"
    if month is None:
        month = parse_month(v["period.bill_month"].value) if "period.bill_month" in v else bill.bill_month
        basis, end = "bill_month", None
    if month is None:
        raise ValueError(f"bill {bill.id} has no supply period and no bill month")
    # the tariff in force for a bill is the one on the last day of its supply, as the bill replay
    # takes it (an order that starts mid-month covers that month's bill)
    on = end or month
    if "energy.kwh" in v and v["energy.kwh"].dec:
        energy, unit = from_bill(v["energy.kwh"], v["energy.kwh"].dec), "kWh"
    elif "energy.kvah" in v and v["energy.kvah"].dec:
        energy, unit = from_bill(v["energy.kvah"], v["energy.kvah"].dec), "kVAh"
    else:
        raise ValueError(f"bill {bill.id} has no energy")
    tod, unmapped = [], []
    tod_units: set[str] = set()
    read = [
        (i, slot, bv)
        for i, slot in enumerate(bill_store.tod_slots(bill))
        if (bv := v.get(f"energy.tod.{i}.kwh")) is not None and bv.dec is not None
    ]
    # slots that print a name and no hours: the hours are the approved rule's, when it names the
    # same slots (invariant 2: read through approved_rules only); otherwise the client types them
    bare = [slot for _, slot, _ in read if slot and window_of(slot) is None]
    discom: str | None = None
    rules: list[RuleView] = []
    reason: str | None = None
    if bare:
        discom, rules = _tod_rules(session, bill, month, on)
        printed = v["tariff.category"].value if "tariff.category" in v else None
        cat = defaults.category_from_printed(defaults.state_of(discom) or "", printed) if discom else None
        if cat is not None:
            code = norm_code(str(cat.value))
            rules = [
                r
                for r in rules
                if not r.scope.get("category") or code in {norm_code(str(a)) for a in r.scope["category"]}
            ]
    by_name = hours_by_name(bare, rules, month.month)
    for i, slot, bv in read:
        assert bv.dec is not None
        tod_units.add(bv.unit or "kWh")
        w = window_of(slot)
        if w is not None:
            tod.append(TodEnergy(start=w[0], end=w[1], energy=from_bill(bv, bv.dec), slot=slot))
        elif slot in by_name:
            a, b, rule = by_name[slot]
            tod.append(
                TodEnergy(start=a, end=b, energy=from_bill(bv, bv.dec), slot=slot, hours_ref=rule.ref())
            )
        else:
            unmapped.append(slot or f"slot {i + 1}")
    if unmapped:
        tod = []  # a partial split would mislead; the month's total is used until the client types the hours
        if bare:
            reason = _no_hours_reason(discom, rules, month)
    # the slots and the month in one unit: a bill whose slots are in kVAh (UPPCL) is a kVAh month
    # when the bill prints its kVAh, though it also prints a kWh register
    if tod and tod_units == {"kVAh"} and unit == "kWh":
        if "energy.kvah" in v and v["energy.kvah"].dec:
            energy, unit = from_bill(v["energy.kvah"], v["energy.kvah"].dec), "kVAh"
        else:
            tod = []
    elif tod and tod_units == {"kWh"} and unit == "kVAh":
        if "energy.kwh" in v and v["energy.kwh"].dec:
            energy, unit = from_bill(v["energy.kwh"], v["energy.kwh"].dec), "kWh"
        else:
            tod = []
    elif len(tod_units) > 1:
        tod = []
    if tod and sum((t.energy.value for t in tod), Decimal(0)) > energy.value * Decimal("1.001"):
        # no complete day: the month's total is used and the day's shape comes from the pattern
        tod = one_scheme(tod, energy.value, _approved_windows(session, bill, month, on))
    return MonthFacts(
        month=month,
        month_basis=basis,
        unmapped_tod_reason=reason,
        energy=energy,
        energy_unit=unit,
        tod=tod,
        bill_id=bill.id,
        unmapped_tod_slots=unmapped,
        max_kva=from_bill(v["demand.max_kva"], v["demand.max_kva"].dec) if "demand.max_kva" in v else None,
        power_factor=_bill_power_factor(v),
        bill_inr=from_bill(v["totals.current_charges"], v["totals.current_charges"].dec)
        if "totals.current_charges" in v and v["totals.current_charges"].dec
        else None,
    )


def _bill_power_factor(v: dict[str, bill_store.BillValue]) -> Fact[Decimal] | None:
    """The month's power factor: as the bill prints it, else kWh / kVAh where the bill prints both
    (Stage 17: a bill without a printed PF left the profile's peak at MD × 1, while the avoided cost
    divides that peak by the bills' kWh / kVAh, overstating billing demand by the kVAh factor)."""
    if "energy.power_factor" in v and v["energy.power_factor"].dec:
        return from_bill(v["energy.power_factor"], v["energy.power_factor"].dec)
    kwh, kvah = v.get("energy.kwh"), v.get("energy.kvah")
    if kwh is None or kvah is None or not kwh.dec or not kvah.dec or not (0 < kwh.dec <= kvah.dec):
        return None
    pf = kwh.dec / kvah.dec
    if pf < Decimal("0.3"):
        return None
    return Fact(value=pf, provenance="bill", ref=kvah.ref.model_copy(update={"note": PF_FROM_KVAH}))


PF_FROM_KVAH = "kWh / kVAh as billed"  # the note of a power factor worked from the bill's two registers


def from_bills(session: Session, bill_ids: list[str], overrides: dict[str, Any] | None = None) -> SiteIntake:
    """A SiteIntake prefilled from accepted bills of one connection; what no bill prints is a
    default (an assumption) or comes from `overrides` (the client's typed values)."""
    bills = [session.get(Bill, i) for i in bill_ids]
    if not bills or any(b is None for b in bills):
        raise bill_store.BillNotFound(", ".join(bill_ids))
    ready = [b for b in bills if b is not None]
    not_ready = [b.id for b in ready if not b.accepted]
    if not_ready:
        raise ValueError(f"bills not accepted yet (confirm their fields first): {', '.join(not_ready)}")
    latest = max(ready, key=lambda b: b.bill_month or date.min)
    v = bill_store.values(session, latest)
    o = dict(overrides or {})

    found = recognise.discom_of(latest)
    if "discom" in o:
        discom = stated(o.pop("discom"))
    elif found.discom and found.ref is not None:
        discom = Fact(value=found.discom, provenance="bill", ref=found.ref)  # its printed name
    elif found.discom:
        discom = Fact(
            value=found.discom,
            provenance="assumption",
            ref=defaults.assumption(f"DISCOM from billx's layout profile '{bill_store.profile_id(latest)}'"),
        )
    else:
        raise ValueError("billx did not recognise the DISCOM; say which DISCOM supplies the site")
    st = defaults.state_of(discom.value) or ""
    printed = v["tariff.category"].value if "tariff.category" in v else None
    if "category" in o:
        category = stated(o.pop("category"))
    else:
        mapped = defaults.category_from_printed(st, printed)
        if mapped is None and printed and st not in defaults.CATEGORY_PATTERNS:
            # no approved schedule to map to in this state: the bill's own words are the category
            category = from_bill(v["tariff.category"], str(printed).strip()[:64])
        elif mapped is None:
            raise ValueError(
                f"the bill's category {printed!r} is not one ergPLAN knows for {st}; "
                "say which category applies"
            )
        else:
            category = assumed(mapped)
    voltage = (
        stated(Decimal(str(o.pop("voltage_kv"))))
        if "voltage_kv" in o
        else assumed(defaults.voltage_kv(category.value))
    )
    if "contract_demand_kva" in o:
        cd = stated(Decimal(str(o.pop("contract_demand_kva"))))
    elif "demand.contract_kva" in v and v["demand.contract_kva"].dec:
        cd = from_bill(v["demand.contract_kva"], v["demand.contract_kva"].dec)
    else:
        raise ValueError("contract demand is not on the bill; type it")
    shape_id = str(o.pop("industry_shape")) if "industry_shape" in o else None
    shape_fact = stated(shape_id) if shape_id else assumed(defaults.industry_shape())
    weekly_off = (
        stated(list(o.pop("weekly_off")))
        if "weekly_off" in o
        else assumed(defaults.weekly_off(shape_fact.value))
    )
    name = v["consumer.name"].value if "consumer.name" in v else None
    client_name = (
        stated(o.pop("client_name"))
        if "client_name" in o
        else (from_bill(v["consumer.name"], name) if name else stated("Unnamed client"))
    )
    months = {m.month: m for m in (month_from_bill(session, b) for b in ready)}
    data: dict[str, Any] = dict(
        client_name=client_name,
        site_name=stated(o.pop("site_name", None) or f"{discom.value.upper()} connection"),
        state=Fact(value=st, provenance="assumption", ref=defaults.assumption(f"state of {discom.value}")),
        discom=discom,
        category=category,
        voltage_kv=voltage,
        contract_demand_kva=cd,
        consumer_number=from_bill(v["consumer.account_number"], v["consumer.account_number"].value)
        if "consumer.account_number" in v
        else None,
        industry_shape=shape_fact,
        weekly_off=weekly_off,
        rate_block=(
            stated(o.pop("rate_block"))
            if "rate_block" in o
            else assumed(rb)
            if (rb := defaults.rate_block(st)) is not None
            else None
        ),
        months=[months[k] for k in sorted(months)][-12:],
    )
    if "open_access" in o:
        data["open_access"] = stated(o.pop("open_access"))
    else:
        data["open_access"] = _open_access_fact(session, latest)
    for key, got in bill_tariff(v).items():
        if key not in o:
            data[key] = got
    for k, val in o.items():
        data[k] = stated(val) if k in SiteIntake.model_fields and not isinstance(val, (dict, list)) else val
    return SiteIntake.model_validate(data)


def bill_tariff(v: dict[str, bill_store.BillValue]) -> dict[str, Fact[Any]]:
    """The tariff a bill shows: its energy charges over the units it billed, and its demand (fixed)
    charges over the demand it billed on (the recorded maximum demand, or the contract demand where
    that is higher: the usual minimum). Each is the bill's own figures divided, cited to the bill."""
    out: dict[str, Fact[Any]] = {}
    energy, units = v.get("charges.energy"), v.get("energy.billed_units")
    if energy and units and energy.dec and units.dec and units.dec > 0 and energy.dec > 0:
        rate = (energy.dec / units.dec).quantize(Decimal("0.0001"))
        out["energy_charge_inr_per_unit"] = from_bill(energy, rate)
        kvah, kwh = v.get("energy.kvah"), v.get("energy.kwh")
        d_kvah = abs(units.dec - kvah.dec) if kvah and kvah.dec else None
        d_kwh = abs(units.dec - kwh.dec) if kwh and kwh.dec else None
        on_kvah = d_kvah is not None and (d_kwh is None or d_kvah < d_kwh)
        out["billing_basis"] = from_bill(units, "kvah" if on_kvah else "kwh")
    demand, md, cd = v.get("charges.demand"), v.get("demand.max_kva"), v.get("demand.contract_kva")
    kva = max((x.dec for x in (md, cd) if x and x.dec), default=None)
    if demand and demand.dec and demand.dec > 0 and kva and kva > 0:  # a credit gives no rate
        per_kva = (demand.dec / kva).quantize(Decimal("0.01"))
        out["fixed_charge_inr_per_kva_month"] = from_bill(demand, per_kva)
    return out


def _open_access_fact(session: Session, bill: Bill) -> Fact[str] | None:
    """The open access the bill shows, with the page it rests on: the kind when the bill names
    one ownership (captive, group captive, third party), else 'open_access' (the client says which),
    or 'green_tariff'. None when the bill shows none (absence is not evidence of none)."""
    r = recognise.recognise(session, bill)
    if r.open_access == "none_seen" or not r.evidence:
        return None
    owned = [k for k in r.open_access_kinds if k in recognise.OWNERSHIPS]
    value = (
        owned[0]
        if len(owned) == 1
        else ("green_tariff" if r.open_access == "green_tariff" else "open_access")
    )
    return Fact(value=value, provenance="bill", ref=r.evidence[0].ref)


def recorded_ownership(conn: Connection) -> str | None:
    """The ownership of the open access the connection already takes (captive, group captive or
    third party), when the intake recorded one; the next stage settles new sources the same way
    unless the client chooses otherwise."""
    oa = (conn.provenance or {}).get("open_access") or {}
    value = oa.get("value") if isinstance(oa, dict) else None
    return value if value in recognise.OWNERSHIPS else None


# ---------------------------------------------------------------- persistence


def _prov(**facts: Fact[Any] | None) -> dict[str, Any]:
    return {
        k: {"provenance": f.provenance, "source_ref": f.ref.to_json() if f.ref else None}
        for k, f in facts.items()
        if f is not None
    }


def month_inputs(intake: SiteIntake, stated_power_factor: Decimal | None = None) -> list[MonthInput]:
    """`stated_power_factor`: the client's answer, used for a month whose bill prints none."""
    out = []
    for m in intake.months:
        pf = m.power_factor.value if m.power_factor else stated_power_factor
        kwh = m.energy.value if m.energy_unit == "kWh" else m.energy.value * (pf or Decimal("0.95"))
        out.append(
            MonthInput(
                month=m.month,
                kwh=kwh,
                max_kva=m.max_kva.value if m.max_kva else None,
                power_factor=pf,
                tod=tuple(
                    TodWindow(
                        t.start,
                        t.end,
                        t.energy.value
                        if m.energy_unit == "kWh"
                        else t.energy.value * (pf or Decimal("0.95")),
                    )
                    for t in m.tod
                ),
                shutdown_days=tuple(m.shutdown_days),
            )
        )
    return out


def synthesise(session: Session, connection_id: str, intake: SiteIntake) -> LoadProfile:
    try:
        shape(intake.industry_shape.value)
    except ShapeNotFound as e:
        raise ValueError(f"unknown industry shape {e}") from e
    # what the client states about the plant (intake.answers) shapes the day, the week, the months
    # and the peaks; the bills' energy, ToD split and recorded demand are matched as before
    stated_answers = site_answers.snapshot_for_connection(session, connection_id)
    si = site_answers.synthesis_inputs(
        {k: v["value"] for k, v in stated_answers.items()},
        intake.industry_shape.value,
        tuple(intake.weekly_off.value),
    )
    prof = synthesise_year(
        month_inputs(intake, si.power_factor),
        intake.industry_shape.value,
        weekly_off=si.weekly_off,
        week=si.week,
        shutdowns=si.shutdowns,
        large_loads=si.large_loads,
    )
    if stated_answers:
        prof.method["answers"] = stated_answers
    prof.method["intake"] = intake.model_dump(mode="json")  # what step 4 edits and rebuilds from
    prof.method["inputs"] = {
        "industry_shape": _prov(f=intake.industry_shape)["f"],
        "weekly_off": _prov(f=intake.weekly_off)["f"],
        "months": [
            {
                "month": m.month.isoformat(),
                "bill_id": m.bill_id,
                "energy_unit": m.energy_unit,
                **_prov(energy=m.energy, max_kva=m.max_kva, power_factor=m.power_factor),
            }
            for m in intake.months
        ],
    }
    version = (
        session.scalar(
            select(func.max(LoadProfile.version)).where(LoadProfile.connection_id == connection_id)
        )
        or 0
    ) + 1
    lp = LoadProfile(
        connection_id=connection_id,
        version=version,
        provenance="synthesised",
        resolution_min=15,
        year_start=prof.year_start,
        blocks=len(prof.kw),
        series_kw=encode(prof.as_decimals()),
        method=prof.method,
    )
    session.add(lp)
    session.flush()
    return lp


def profile_warnings(method: dict[str, Any]) -> list[str]:
    """What the synthesised profile could not match, in words: a month whose ToD energy alone
    averages more than its recorded maximum demand keeps the energy and peaks above the demand."""
    out = []
    for rep in method.get("months") or []:
        if rep.get("peak_excess_kw") is None:
            continue
        month = date.fromisoformat(rep["month"])
        out.append(
            f"{month:%b %Y}: the energy billed for {rep.get('peak_excess_window')} averages more than the "
            f"recorded maximum demand allows ({rep['max_kw_target']} kW = recorded kVA × power factor); "
            f"the profile keeps the bill's energy and ToD split, so its peak is {rep['max_kw']} kW, "
            f"{rep['peak_excess_kw']} kW above the recorded demand. Check the ToD energy, the maximum "
            "demand and the power factor on the bill"
        )
    return out


def build(
    session: Session, intake: SiteIntake, actor: str, connection_id: str | None = None
) -> dict[str, Any]:
    """Persist the intake: client, site, connection (new or updated), assets, the bills' link to
    the connection, and a synthesised load profile. Returns the ids and warnings."""
    if connection_id:
        conn = session.get(Connection, connection_id)
        if conn is None:
            raise LookupError(connection_id)
        site = session.get(Site, conn.site_id)
        assert site is not None
        client = session.get(Client, site.client_id)
        assert client is not None
    else:
        client = Client(name=intake.client_name.value, created_by=actor)
        session.add(client)
        session.flush()
        site = Site(client_id=client.id, name=intake.site_name.value, state=intake.state.value)
        session.add(site)
        session.flush()
        conn = Connection(site_id=site.id, discom_id=intake.discom.value)
        session.add(conn)
    client.name = intake.client_name.value
    site.name, site.state = intake.site_name.value, intake.state.value
    site.industry_shape = intake.industry_shape.value
    site.weekly_off = list(intake.weekly_off.value)
    site.shift_hours = {"shifts": [s.model_dump() for s in intake.shifts.value]} if intake.shifts else {}
    site.land_acres = intake.land_acres.value if intake.land_acres else None
    site.roof_sqm = intake.roof_sqm.value if intake.roof_sqm else None
    site.provenance = _prov(
        client_name=intake.client_name,
        site_name=intake.site_name,
        state=intake.state,
        industry_shape=intake.industry_shape,
        weekly_off=intake.weekly_off,
        shifts=intake.shifts,
        land_acres=intake.land_acres,
        roof_sqm=intake.roof_sqm,
    )
    conn.discom_id = intake.discom.value
    conn.tariff_category = intake.category.value
    conn.voltage_kv = intake.voltage_kv.value
    conn.contract_demand_kva = intake.contract_demand_kva.value
    conn.connection_kind = defaults.connection_kind(intake.voltage_kv.value)
    conn.consumer_number = intake.consumer_number.value if intake.consumer_number else None
    conn.rate_block = intake.rate_block.value if intake.rate_block else None
    conn.provenance = _prov(
        discom=intake.discom,
        category=intake.category,
        voltage_kv=intake.voltage_kv,
        contract_demand_kva=intake.contract_demand_kva,
        consumer_number=intake.consumer_number,
        rate_block=intake.rate_block,
        open_access=intake.open_access,
    )
    if intake.open_access is not None:
        conn.provenance["open_access"]["value"] = intake.open_access.value
    if intake.energy_charge_inr_per_unit is not None:
        e, d = intake.energy_charge_inr_per_unit, intake.fixed_charge_inr_per_kva_month
        conn.provenance["tariff"] = {
            "energy_inr_per_unit": {"value": str(e.value), "source_ref": e.ref.to_json() if e.ref else None},
            "demand_inr_per_kva_month": (
                {"value": str(d.value), "source_ref": d.ref.to_json() if d.ref else None} if d else None
            ),
            "billing_basis": intake.billing_basis.value if intake.billing_basis else "kwh",
        }
    session.flush()
    for a in intake.assets:
        session.add(
            Asset(site_id=site.id, kind=a.kind, params=a.params, provenance={"provenance": a.provenance})
        )
    for m in intake.months:
        if m.bill_id:
            b = session.get(Bill, m.bill_id)
            if b is not None:
                b.connection_id = conn.id
    lp = synthesise(session, conn.id, intake)
    # a measured 15-minute file replaces the synthesis: it stays the profile in use
    lp = measured_upload.carry_forward(session, conn.id) or lp
    return {
        "client_id": client.id,
        "site_id": site.id,
        "connection_id": conn.id,
        "load_profile_id": lp.id,
        "load_profile_version": lp.version,
        "warnings": intake.warnings() + profile_warnings(lp.method),
    }
