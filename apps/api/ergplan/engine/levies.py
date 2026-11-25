"""FPPCA and electricity duty at the rates the client's own bills print. Pure.

Neither levy is in a tariff order (the gaps `fppca_monthly` and `electricity_duty`), yet both are on
every bill, so a cost of today that leaves them out does not reconcile with what the client pays.
Until an approved rule states them, each is carried at the average rate the connection's accepted
bills charged (the billed amount over the billed units), as a SourcedValue whose refs are the bill
lines themselves (kind `bill`): the client's own figure, never a regulation's.

What the bills cannot say is whether a levy falls on units not bought from the DISCOM (open access,
captive, rooftop). Where an approved `oa_charge` row of component `electricity_duty_oa` applies, the
landed cost uses that row. Where none does, the treatment is an assumption, carried as an
`assumption` value the result names in words (TREATMENT): nothing is silently zero, and nothing is
shown as sourced to a regulation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..core.sourced import RefKind, SourcedValue, SourceRef, derive
from ..core.units import Unit

FPPCA = "fppca"
DUTY = "electricity_duty"
CODES = (FPPCA, DUTY)
LABEL = {FPPCA: "Fuel and power purchase adjustment (FPPCA)", DUTY: "Electricity duty"}
RATE_UNITS = (Unit.INR_PER_KVAH, Unit.INR_PER_KWH)
RATE_PLACES = Decimal("0.0001")
NOT_SOURCED = "Not sourced to a regulation: no approved rule states it for this state."
# code → (share of the levy charged on units not bought from the DISCOM, the assumption in words)
TREATMENT: dict[str, tuple[Decimal, str]] = {
    FPPCA: (
        Decimal(0),
        "FPPCA is the DISCOM's fuel and power purchase surcharge on the energy it sells: assumed not "
        f"charged on open-access, captive and rooftop units. {NOT_SOURCED}",
    ),
    DUTY: (
        Decimal(100),
        "Electricity duty is assumed charged on open-access, captive and rooftop units at the rate "
        f"on your bills (the cautious side for the saving). {NOT_SOURCED}",
    ),
}


@dataclass(frozen=True)
class BillCharges:
    """One accepted bill's reading of the two levies: the billed amount of each (None when the bill
    has no such line) with the billx elements it was read from, and the bill's units."""

    bill_id: str
    amounts: dict[str, tuple[Decimal, tuple[int, ...]]]  # code → (INR, billx element indices)
    kvah: Decimal | None
    kwh: Decimal | None


@dataclass(frozen=True)
class Levy:
    code: str
    label: str
    rates: tuple[SourcedValue, ...]  # INR per kVAh and/or per kWh billed: the bills' average, kind `bill`
    bills: int  # the accepted bills that print it
    treatment: SourcedValue  # percent of the levy charged on units not bought from the DISCOM (assumption)

    def rate(self, unit: Unit) -> SourcedValue | None:
        return next((r for r in self.rates if r.unit == unit), None)

    @property
    def words(self) -> str:
        """The treatment on units not bought from the DISCOM, in words."""
        return str(self.treatment.source_refs[0].note)

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "rates": [r.model_dump(mode="json") for r in self.rates],
            "bills": self.bills,
            "treatment": self.treatment.model_dump(mode="json"),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Levy:
        return cls(
            code=str(d["code"]),
            label=str(d["label"]),
            rates=tuple(SourcedValue.model_validate(r) for r in d["rates"]),
            bills=int(d["bills"]),
            treatment=SourcedValue.model_validate(d["treatment"]),
        )


def treatment(code: str) -> SourcedValue:
    share, words = TREATMENT[code]
    return SourcedValue(
        value=share,
        unit=Unit.PERCENT,
        label=f"{LABEL[code]}: share charged on units not bought from the DISCOM",
        source_refs=(SourceRef(kind=RefKind.ASSUMPTION, note=words),),
    )


def from_bills(bills: Sequence[BillCharges]) -> tuple[Levy, ...]:
    """Each levy at the average rate the bills charged: the sum of the billed amounts over the sum of
    the billed units, across every bill that gives units (a bill without the line adds its units and
    no amount). A levy no bill prints is not returned: nothing is assumed about its rate."""
    out = []
    for code in CODES:
        with_line = [b for b in bills if code in b.amounts]
        if not with_line:
            continue
        refs = tuple(
            SourceRef(
                kind=RefKind.BILL,
                bill_id=b.bill_id,
                field_path=f"elements.{i}.amount",
                note=f"{LABEL[code]} as billed",
            )
            for b in with_line
            for i in b.amounts[code][1]
        ) or tuple(SourceRef(kind=RefKind.BILL, bill_id=b.bill_id, field_path="elements") for b in with_line)
        rates = []
        for unit, units_of in ((Unit.INR_PER_KVAH, "kvah"), (Unit.INR_PER_KWH, "kwh")):
            counted = [b for b in bills if getattr(b, units_of)]
            if not counted or not all(getattr(b, units_of) for b in with_line):
                continue  # a bill printing the levy gives no units in this basis: no honest average
            units = sum((getattr(b, units_of) for b in counted), Decimal(0))
            amount = sum((b.amounts[code][0] for b in counted if code in b.amounts), Decimal(0))
            rates.append(
                SourcedValue(
                    value=(amount / units).quantize(RATE_PLACES),
                    unit=unit,
                    source_refs=refs,
                    label=f"{LABEL[code]}, as charged on your {len(with_line)} "
                    f"bill{'' if len(with_line) == 1 else 's'}",
                )
            )
        if rates:
            out.append(Levy(code, LABEL[code], tuple(rates), len(with_line), treatment(code)))
    return tuple(out)


def per_kwh(levy: Levy, basis: str, kvah_factor: SourcedValue | None) -> SourcedValue | None:
    """The levy on one kWh bought from the DISCOM under a schedule billing in `basis`."""
    if basis == "kvah":
        r = levy.rate(Unit.INR_PER_KVAH)
        if r is None or kvah_factor is None:
            return None
        return derive(r.value * kvah_factor.value, Unit.INR_PER_KWH, [r, kvah_factor], label=r.label)
    return levy.rate(Unit.INR_PER_KWH)


def on_other_units(levy: Levy, basis: str, kvah_factor: SourcedValue | None) -> SourcedValue | None:
    """The levy on one kWh consumed that was not bought from the DISCOM (open access, captive,
    rooftop), under the stated assumption: None when the assumption is that it is not charged. The
    value carries the assumption's ref, so it is never shown as a sourced fact."""
    grid = per_kwh(levy, basis, kvah_factor)
    if grid is None or levy.treatment.value == 0:
        return None
    return derive(
        grid.value * levy.treatment.value / 100,
        Unit.INR_PER_KWH,
        [grid, levy.treatment],
        label=f"{levy.label} on units not bought from the DISCOM (assumed; not sourced to a regulation)",
    )
