"""Exchange purchase by an open-access consumer: feasibility before price (Stage 18). Pure.

A row of the `exchange_purchase` family states, field by field, what a state's regulations say
about buying on the power exchange through open access; a field no stored regulation states is
`not_stated` with the regulation to obtain, never a guess (operating rule 18). This module turns a
row and a connection's facts into a verdict, by fixed rules and no model (invariant 4):

- **not yet sourced**: no row, or the row does not state whether the purchase is permitted or how
  it must be scheduled. No exchange source is offered.
- **not practical**: the rule does not permit it, the consumer is not eligible (contract demand or
  voltage below the rule's minimum), or the rule lists a cited barrier. No exchange source is
  offered, and the reasons are given with their citations.
- **feasible with conditions**: permitted, with conditions in words (clearance to apply for, a
  uniform schedule, contract demand, standby, deviation, surcharges, lock-in, and every field the
  stored regulations do not state).
- **feasible**: permitted with nothing conditional.

It also holds what the optimizer and the judge need from the rule: the schedule the rule requires
(`Schedule`, `check_schedule`), the price of the deviation risk the rule creates
(`deviation_adder`), and one month's cost of a schedule at given block prices (`month_cost`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..core.sourced import RefKind, SourceRef
from ..rulebook.models import RuleView

FAMILY = "exchange_purchase"
ZERO = Decimal(0)

NOT_YET_SOURCED = "not_yet_sourced"
NOT_PRACTICAL = "not_practical"
WITH_CONDITIONS = "feasible_with_conditions"
FEASIBLE = "feasible"
VERDICT_WORDS = {
    NOT_YET_SOURCED: "Not yet sourced",
    NOT_PRACTICAL: "Not practical",
    WITH_CONDITIONS: "Feasible with conditions",
    FEASIBLE: "Feasible",
}

# the body's fields in the order they are read out, with what each is called in words
FIELDS: dict[str, str] = {
    "permitted": "whether an open-access consumer may buy on the exchange",
    "eligibility": "who is eligible (contract demand, voltage, metering)",
    "sldc_clearance": "the load despatch centre's no-objection or standing clearance and its validity",
    "schedule": "whether the schedule must be uniform over the day or may vary by block",
    "notice": "the day-ahead notice and whether a schedule may be revised",
    "contract_demand": "whether the DISCOM's contract demand must be reduced",
    "standby": "the charge on standby drawal from the DISCOM",
    "deviation_under": "how drawing less than the schedule is settled",
    "deviation_over": "how drawing more than the schedule is settled",
    "cross_subsidy_surcharge": "whether cross-subsidy surcharge is paid on exchange power",
    "additional_surcharge": "whether additional surcharge is paid on exchange power",
    "banking": "whether exchange power may be banked or time-shifted",
    "lock_in": "any minimum period or lock-in",
}
DECIDING = ("permitted", "schedule")  # without these two the verdict is "not yet sourced"


@dataclass(frozen=True)
class Condition:
    """One thing the client is told, in words, with where it comes from."""

    field: str
    text: str
    refs: tuple[SourceRef, ...] = ()


@dataclass(frozen=True)
class Schedule:
    """The shape of schedule the rule requires. `uniform_hours` > 0: the same quantity in every
    block of each window of that many hours, counted from 00:00 (24: the whole day)."""

    uniform_hours: Decimal = ZERO
    ref: SourceRef | None = None

    @property
    def uniform(self) -> bool:
        return self.uniform_hours > 0


@dataclass(frozen=True)
class DeviationTerms:
    """What the rule makes of a deviation from the schedule. `None` in a field: not stated, so that
    side of the risk cannot be priced (and the verdict says so)."""

    under_compensated_inr_per_kwh: Decimal | None = None  # 0: scheduled energy not drawn is lost
    over_multiple_of_grid_rate: Decimal | None = None  # 1: over-drawal at the ordinary tariff
    refs: tuple[SourceRef, ...] = ()

    @property
    def priced(self) -> bool:
        return self.under_compensated_inr_per_kwh is not None or self.over_multiple_of_grid_rate is not None


@dataclass(frozen=True)
class Verdict:
    state: str
    verdict: str
    words: str
    reasons: list[Condition] = field(default_factory=list)  # not practical / not yet sourced: why
    conditions: list[Condition] = field(default_factory=list)
    apply_for: list[Condition] = field(default_factory=list)
    not_stated: list[Condition] = field(default_factory=list)  # field → the regulation to obtain
    schedule: Schedule | None = None
    deviation: DeviationTerms | None = None
    rule_id: str | None = None
    version_id: str | None = None

    @property
    def offers(self) -> bool:
        """Whether an exchange source may be offered at all."""
        return self.verdict in (FEASIBLE, WITH_CONDITIONS)

    @property
    def label(self) -> str:
        return VERDICT_WORDS[self.verdict]


class ScheduleRefused(ValueError):
    """A schedule that the rule does not allow (not uniform where it must be)."""


def _dec(v: Any) -> Decimal | None:
    return Decimal(str(v)) if v not in (None, "") else None


def _fmt(v: Decimal) -> str:
    s = format(v.normalize(), "f")
    return s if s != "-0" else "0"


def _stated(body: dict[str, Any], name: str) -> dict[str, Any] | None:
    f = body.get(name) or {}
    return f if f.get("status") == "stated" else None


def _refs(row: RuleView, pointer: str) -> tuple[SourceRef, ...]:
    """The citation of a stated field: its own, else the row's when that is a stored page."""
    raw = (row.field_refs or {}).get(pointer)
    if raw is None and row.source_ref.get("kind") in (RefKind.REGULATION, RefKind.ORDER, RefKind.REGISTER):
        raw = row.source_ref
    return (SourceRef(**{**raw, "rule_id": row.id}),) if raw else ()


def _with(words: str, f: dict[str, Any]) -> str:
    c = str(f.get("condition") or "").strip()
    return f"{words} {c}".strip() if c else words


def schedule_of(row: RuleView) -> Schedule | None:
    f = _stated(row.body, "schedule")
    if f is None:
        return None
    refs = _refs(row, "/schedule")
    if f["shape"] == "by_block":
        return Schedule(ZERO, refs[0] if refs else None)
    return Schedule(_dec(f.get("uniform_hours")) or Decimal(24), refs[0] if refs else None)


def deviation_of(row: RuleView) -> DeviationTerms:
    under = _stated(row.body, "deviation_under")
    over = _stated(row.body, "deviation_over")
    comp: Decimal | None = None
    if under is not None:
        comp = ZERO if under["treatment"] == "not_compensated" else _dec(under.get("rate_inr_per_kwh"))
    mult: Decimal | None = None
    if over is not None:
        if over["treatment"] == "grid_tariff":
            mult = Decimal(1)
        elif over["treatment"] == "multiple_of_energy_charge":
            mult = _dec(over.get("multiple"))
    return DeviationTerms(comp, mult, _refs(row, "/deviation_under") + _refs(row, "/deviation_over"))


def verdict(
    state: str,
    row: RuleView | None,
    contract_demand_kva: Decimal | None = None,
    voltage_kv: Decimal | None = None,
) -> Verdict:
    """The verdict for a state from the rule row that applies to the connection (None: no row)."""
    if row is None:
        return Verdict(
            state,
            NOT_YET_SOURCED,
            f"Buying on the power exchange in {state} is not yet sourced: no approved rule states "
            "whether and how an open-access consumer may buy there, so no exchange source is offered.",
        )
    body = row.body
    missing = [
        Condition(name, f"The stored regulations do not state {words}. To obtain: {f.get('obtain')}")
        for name, words in FIELDS.items()
        if (f := body.get(name) or {}).get("status") != "stated"
    ]
    common: dict[str, Any] = {"not_stated": missing, "rule_id": row.id, "version_id": row.version_id}
    undecided = [c for c in missing if c.field in DECIDING]
    if undecided:
        return Verdict(
            state,
            NOT_YET_SOURCED,
            f"Buying on the power exchange in {state} is not yet sourced: the stored regulations do not "
            "state "
            + " or ".join(FIELDS[c.field] for c in undecided)
            + ", so no exchange source is offered.",
            reasons=undecided,
            **common,
        )
    reasons: list[Condition] = []
    conditions: list[Condition] = []
    apply_for: list[Condition] = []

    permitted = body["permitted"]
    if permitted["value"] == "no":
        reasons.append(
            Condition(
                "permitted",
                _with("The rule does not permit an open-access consumer to buy on the exchange.", permitted),
                _refs(row, "/permitted"),
            )
        )
    elif permitted.get("condition"):
        conditions.append(Condition("permitted", str(permitted["condition"]), _refs(row, "/permitted")))

    elig = _stated(body, "eligibility")
    if elig is not None:
        refs = _refs(row, "/eligibility")
        need_cd, need_kv = _dec(elig.get("min_contract_demand_kva")), _dec(elig.get("min_voltage_kv"))
        if need_cd is not None and contract_demand_kva is not None and contract_demand_kva < need_cd:
            reasons.append(
                Condition(
                    "eligibility",
                    f"Your contract demand of {_fmt(contract_demand_kva)} kVA is below the "
                    f"{_fmt(need_cd)} kVA the rule requires. {elig['condition']}",
                    refs,
                )
            )
        elif need_kv is not None and voltage_kv is not None and voltage_kv < need_kv:
            reasons.append(
                Condition(
                    "eligibility",
                    f"Your supply at {_fmt(voltage_kv)} kV is below the {_fmt(need_kv)} kV the rule "
                    f"requires. {elig['condition']}",
                    refs,
                )
            )
        else:
            conditions.append(Condition("eligibility", str(elig["condition"]), refs))

    for i, b in enumerate(body.get("barriers") or []):
        reasons.append(Condition("barriers", str(b["text"]), _refs(row, f"/barriers/{i}")))

    clearance = _stated(body, "sldc_clearance")
    if clearance is not None and clearance["required"] == "yes":
        what = "A standing clearance" if clearance.get("kind") == "standing_clearance" else "A no-objection"
        who = clearance.get("issued_by") or "the state load despatch centre"
        valid = f", valid for {clearance['validity_days']} days" if clearance.get("validity_days") else ""
        apply_for.append(
            Condition(
                "sldc_clearance",
                _with(f"{what} from {who}{valid}.", clearance),
                _refs(row, "/sldc_clearance"),
            )
        )

    sched = schedule_of(row)
    assert sched is not None
    if sched.uniform:
        span = (
            "the whole day" if sched.uniform_hours >= 24 else f"{_fmt(sched.uniform_hours)} hours at a time"
        )
        conditions.append(
            Condition(
                "schedule",
                _with(
                    f"The schedule must be the same in every 15-minute block over {span}: the purchase is "
                    "sized as one flat block, not shaped to your load.",
                    body["schedule"],
                ),
                _refs(row, "/schedule"),
            )
        )
    elif body["schedule"].get("condition"):
        conditions.append(Condition("schedule", str(body["schedule"]["condition"]), _refs(row, "/schedule")))

    notice = _stated(body, "notice")
    if notice is not None:
        conditions.append(Condition("notice", str(notice["condition"]), _refs(row, "/notice")))

    cd = _stated(body, "contract_demand")
    if cd is not None and (cd["treatment"] == "reduced" or cd.get("condition")):
        words = (
            "Your contract demand with the DISCOM must be reduced by the quantity scheduled."
            if cd["treatment"] == "reduced"
            else "Your contract demand with the DISCOM stays as it is."
        )
        target = apply_for if cd["treatment"] == "reduced" else conditions
        target.append(Condition("contract_demand", _with(words, cd), _refs(row, "/contract_demand")))

    standby = _stated(body, "standby")
    if standby is not None:
        basis, val = standby["basis"], standby.get("value")
        words = {
            "multiple_of_energy_charge": f"Standby drawal is charged at {val} times the energy charge.",
            "multiple_of_tariff": f"Standby drawal is charged at {val} times the tariff.",
            "inr_per_kwh": f"Standby drawal is charged at ₹{val} per kWh.",
            "temporary_tariff": "Standby drawal is charged at the temporary supply tariff.",
            "as_worded": "",
        }[basis]
        conditions.append(Condition("standby", _with(words, standby), _refs(row, "/standby")))

    dev = deviation_of(row)
    under, over = _stated(body, "deviation_under"), _stated(body, "deviation_over")
    if under is not None:
        words = (
            "Scheduled energy you do not draw is not paid back."
            if under["treatment"] == "not_compensated"
            else f"Scheduled energy you do not draw is paid at ₹{under.get('rate_inr_per_kwh')} per kWh."
            if under.get("rate_inr_per_kwh")
            else "Scheduled energy you do not draw is compensated."
        )
        conditions.append(Condition("deviation_under", _with(words, under), _refs(row, "/deviation_under")))
    if over is not None:
        words = {
            "grid_tariff": "Drawal above the schedule is billed at your ordinary tariff.",
            "multiple_of_energy_charge": f"Drawal above the schedule is billed at {over.get('multiple')} "
            "times the energy charge.",
            "as_worded": "",
        }[over["treatment"]]
        conditions.append(Condition("deviation_over", _with(words, over), _refs(row, "/deviation_over")))

    for name, noun in (
        ("cross_subsidy_surcharge", "Cross-subsidy surcharge"),
        ("additional_surcharge", "Additional surcharge"),
    ):
        f = _stated(body, name)
        if f is not None:
            verb = "is paid" if f["payable"] == "yes" else "is not paid"
            conditions.append(
                Condition(name, _with(f"{noun} {verb} on exchange power.", f), _refs(row, f"/{name}"))
            )

    banking = _stated(body, "banking")
    if banking is not None:
        words = (
            "Exchange power may be banked."
            if banking["allowed"] == "yes"
            else "Exchange power cannot be banked or shifted to another time: it is set off only in the "
            "block it is scheduled in."
        )
        conditions.append(Condition("banking", _with(words, banking), _refs(row, "/banking")))

    lock = _stated(body, "lock_in")
    if lock is not None:
        conditions.append(Condition("lock_in", str(lock["condition"]), _refs(row, "/lock_in")))

    common.update(schedule=sched, deviation=dev)
    if reasons:
        return Verdict(
            state,
            NOT_PRACTICAL,
            f"Buying on the power exchange is not practical in {state}: " + " ".join(r.text for r in reasons),
            reasons=reasons,
            conditions=conditions,
            apply_for=apply_for,
            **common,
        )
    if conditions or apply_for or missing:
        return Verdict(
            state,
            WITH_CONDITIONS,
            f"Buying on the power exchange is feasible in {state}, with conditions.",
            conditions=conditions,
            apply_for=apply_for,
            **common,
        )
    return Verdict(state, FEASIBLE, f"Buying on the power exchange is feasible in {state}.", **common)


# ---------------------------------------------------------------- the schedule the rule requires


def check_schedule(
    schedule: Schedule,
    kw_by_block: Sequence[Decimal | float],
    block_hours: Decimal | float,
    tol_kw: float = 0.01,
) -> None:
    """Refuse a schedule the rule does not allow: where it must be uniform, every block of each
    window (counted from the first block, which is 00:00) must hold the same quantity."""
    if not schedule.uniform:
        return
    per = round(float(schedule.uniform_hours) / float(block_hours))
    if per <= 1:
        return
    for start in range(0, len(kw_by_block), per):
        window = [float(x) for x in kw_by_block[start : start + per]]
        lo, hi = min(window), max(window)
        if hi - lo > tol_kw:
            span = "day" if schedule.uniform_hours >= 24 else f"{_fmt(schedule.uniform_hours)}-hour window"
            raise ScheduleRefused(
                f"the rule requires a uniform schedule over each {span}; {span} {start // per + 1} "
                f"varies between {lo:.2f} kW and {hi:.2f} kW"
            )


# ---------------------------------------------------------------- the deviation risk, priced


def deviation_adder(
    terms: DeviationTerms, landed_inr_per_kwh: Decimal, grid_inr_per_kwh: Decimal, deviation_pct: Decimal
) -> Decimal:
    """INR per scheduled kWh for the deviation risk the rule creates, when the consumer's drawal
    falls short of the schedule by `deviation_pct` of it and exceeds it by the same share:

    - short: the units are paid for at the landed cost and not drawn; the rule pays back
      `under_compensated_inr_per_kwh` (nothing when it says the energy is lost);
    - over: the units are drawn from the DISCOM at `over_multiple_of_grid_rate` × the grid rate
      instead of the grid rate, so the extra is (multiple − 1) × the grid rate.

    A side the rule does not state adds nothing here (it cannot be priced; the verdict says so)."""
    share = deviation_pct / 100
    out = ZERO
    if terms.under_compensated_inr_per_kwh is not None:
        out += share * max(landed_inr_per_kwh - terms.under_compensated_inr_per_kwh, ZERO)
    if terms.over_multiple_of_grid_rate is not None:
        out += share * max(terms.over_multiple_of_grid_rate - 1, ZERO) * grid_inr_per_kwh
    return out


@dataclass(frozen=True)
class MonthCost:
    scheduled_kwh: Decimal  # at the exchange (before losses)
    delivered_kwh: Decimal  # at the consumer's meter
    price_inr: Decimal  # paid at the clearing price
    charges_inr: Decimal  # open-access charges on the delivered energy
    deviation_inr: Decimal  # the deviation risk, priced
    total_inr: Decimal
    landed_inr_per_kwh: Decimal  # total ÷ delivered


def month_cost(
    prices_inr_per_kwh: Sequence[Decimal],
    schedule_kw: Sequence[Decimal],
    block_hours: Decimal,
    *,
    loss_pct: Decimal,
    charges_inr_per_kwh: Decimal,
    schedule: Schedule,
    terms: DeviationTerms,
    deviation_pct: Decimal,
    grid_inr_per_kwh: Sequence[Decimal],
) -> MonthCost:
    """One month of an exchange purchase, block by block:

        scheduled kWh  = kW × hours                      (bought at the block's clearing price)
        delivered kWh  = scheduled × (1 − loss % ÷ 100)
        landed (block) = price ÷ (1 − loss % ÷ 100) + charges per kWh delivered
        deviation      = delivered × deviation_adder(landed, grid rate)
        total          = Σ scheduled × price + Σ delivered × charges + Σ deviation

    The schedule is checked against the rule first: a non-uniform schedule is refused where the
    rule requires a uniform one."""
    if not (len(prices_inr_per_kwh) == len(schedule_kw) == len(grid_inr_per_kwh)):
        raise ValueError("prices, schedule and grid rates must cover the same blocks")
    check_schedule(schedule, schedule_kw, block_hours)
    keep = 1 - loss_pct / 100
    scheduled = delivered = price = charges = deviation = ZERO
    for p, kw, g in zip(prices_inr_per_kwh, schedule_kw, grid_inr_per_kwh, strict=True):
        e = kw * block_hours
        d = e * keep
        scheduled += e
        delivered += d
        price += e * p
        charges += d * charges_inr_per_kwh
        deviation += d * deviation_adder(terms, p / keep + charges_inr_per_kwh, g, deviation_pct)
    total = price + charges + deviation
    return MonthCost(
        scheduled, delivered, price, charges, deviation, total, total / delivered if delivered > 0 else ZERO
    )
