"""Exchange purchase, feasibility before price (Stage 18).

- `GET /connections/{id}/exchange` (the client): the state's verdict for this connection from
  APPROVED rules only — feasible, feasible with conditions, not practical or not yet sourced — with
  every condition's citation, and which price vintage each market's months are read from.
- `GET /exchange/sandbox/{version_id}` and `POST /exchange/sandbox/{version_id}/month-cost` (the
  analyst): the verdict a DRAFT version would give, and one month's cost of a schedule under it at
  the stored clearing prices. Drafts are read through `rulebook.sandbox` and never reach a client
  (invariant 2).
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ...core.sourced import RefKind, SourcedValue, SourceRef, derive
from ...core.units import Unit
from ...db.base import session_scope
from ...db.models import Connection, RulebookVersion
from ...engine import exchange
from ...intake import price_series
from ...rulebook import sandbox
from ...rulebook.repository import approved_rules
from .. import exchange_plan
from ..auth import Caller, analyst, caller, owned_connection
from .cost import _facts

router = APIRouter(tags=["exchange"])
Segment = Literal["dam", "rtm", "gdam", "gtam", "tam", "hpdam"]
ZERO = Decimal(0)


class ConditionOut(BaseModel):
    field: str
    text: str
    refs: list[SourceRef]


class VerdictOut(BaseModel):
    """The state's verdict on buying on the power exchange through open access."""

    kind: str | None = None
    label: str | None = None
    state: str
    verdict: Literal["feasible", "feasible_with_conditions", "not_practical", "not_yet_sourced"]
    verdict_label: str
    offered: bool
    words: str
    reasons: list[ConditionOut]
    conditions: list[ConditionOut]
    apply_for: list[ConditionOut]
    not_stated: list[ConditionOut]
    schedule: str | None
    rule_id: str | None
    version_id: str | None


class MonthVintageOut(BaseModel):
    month: int
    vintage_id: str
    filename: str
    file_sha256: str
    date_from: date
    date_to: date


class ConnectionExchangeOut(BaseModel):
    segment: str
    verdict: VerdictOut
    # the provenance of the prices a run would read (invariant 8): the vintage of each month
    prices: list[MonthVintageOut]


@router.get("/connections/{connection_id}/exchange", response_model=ConnectionExchangeOut)
def connection_exchange(
    connection_id: str,
    who: Annotated[Caller, Depends(caller)],
    segment: Segment = "dam",
    on: date | None = None,
) -> ConnectionExchangeOut:
    with session_scope(actor=who.actor) as s:
        owned_connection(s, who, connection_id)
        conn = s.get(Connection, connection_id)
        if conn is None:
            raise HTTPException(404, "connection not found")
        v = exchange_plan.verdict_for(s, _facts(s, conn), on or date.today(), approved_rules, segment)
        got = price_series.month_block_means(s, segment)
        prices = [
            MonthVintageOut(
                month=mo,
                vintage_id=u.id,
                filename=u.filename,
                file_sha256=u.file_sha256,
                date_from=u.date_from,
                date_to=u.date_to,
            )
            for mo, u in sorted((got.vintage_of if got else {}).items())
        ]
    return ConnectionExchangeOut(
        segment=segment, verdict=VerdictOut.model_validate(exchange_plan.verdict_json(v)), prices=prices
    )


# ---------------------------------------------------------------- the analyst's sandbox (drafts)


def _draft_verdict(
    s: Any, version_id: str, contract_demand_kva: Decimal | None, voltage_kv: Decimal | None
) -> tuple[exchange.Verdict, Any]:
    v = s.get(RulebookVersion, version_id)
    if v is None or v.family != exchange.FAMILY:
        raise HTTPException(404, "no exchange-purchase version with this id")
    rows = sandbox.rules(s, version_id)
    row = rows[0] if rows else None
    name = exchange_plan.state_name(s, v.jurisdiction_id)
    return exchange.verdict(name, row, contract_demand_kva, voltage_kv), row


@router.get("/exchange/sandbox/{version_id}", response_model=VerdictOut)
def sandbox_verdict(
    version_id: str,
    who: Annotated[Caller, Depends(analyst)],
    contract_demand_kva: Annotated[Decimal | None, Query(gt=0)] = None,
    voltage_kv: Annotated[Decimal | None, Query(gt=0)] = None,
) -> VerdictOut:
    """The verdict a version (a draft included) would give a consumer of this size."""
    with session_scope(actor=who.actor) as s:
        v, _ = _draft_verdict(s, version_id, contract_demand_kva, voltage_kv)
    return VerdictOut.model_validate(exchange_plan.verdict_json(v))


def _own(
    owner: str = "price_series.mcp_inr_per_mwh + rules.body + request body",
    kinds: tuple[str, ...] = ("market", "client_stated", "assumption", "regulation", "order", "register"),
) -> Any:
    return Field(json_schema_extra={"owner": owner, "ref_kinds": list(kinds)})


class MonthCostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment: Segment = "dam"
    month: int = Field(ge=1, le=12)
    kw: Decimal = Field(gt=0)  # the quantity scheduled in each block of the window
    # the window scheduled every day, from_hour:00 to to_hour:00 (0 and 24: the whole day); a window
    # that ends before it starts runs past midnight (22 and 6: 22:00 to 06:00)
    from_hour: int = Field(default=0, ge=0, le=23)
    to_hour: int = Field(default=24, ge=1, le=24)
    loss_pct: Decimal = Field(ge=0, lt=100)
    charges_inr_per_kwh: Decimal = Field(ge=0)  # open-access charges per kWh at the meter
    grid_inr_per_kwh: Decimal = Field(ge=0)  # the DISCOM energy rate the purchase is compared with
    deviation_pct: Decimal | None = Field(default=None, ge=0, le=100)


class MonthCostOut(BaseModel):
    verdict: VerdictOut
    month: int
    days: int
    vintage: MonthVintageOut
    scheduled_kwh: SourcedValue = _own()
    delivered_kwh: SourcedValue = _own()
    price_inr: SourcedValue = _own()
    charges_inr: SourcedValue = _own()
    deviation_inr: SourcedValue = _own()
    total_inr: SourcedValue = _own()
    landed_inr_per_kwh: SourcedValue = _own()
    grid_inr: SourcedValue = _own("request body", ("client_stated",))  # at the DISCOM rate stated
    words: str


def _typed(value: Decimal, unit: Unit, label: str) -> SourcedValue:
    return SourcedValue(
        value=value,
        unit=unit,
        source_refs=(SourceRef(kind=RefKind.CLIENT_STATED, note=f"{label}, as the analyst typed it"),),
        label=label,
    )


@router.post("/exchange/sandbox/{version_id}/month-cost", response_model=MonthCostOut)
def sandbox_month_cost(
    version_id: str, body: MonthCostIn, who: Annotated[Caller, Depends(analyst)]
) -> MonthCostOut:
    """One month of a schedule under a version's rule at the stored clearing prices: the same
    quantity in every block of the window, every day of the month (each block priced at the month's
    mean clearing price for that block of the day). A schedule the rule does not allow is refused."""
    if body.to_hour == body.from_hour:
        raise HTTPException(422, "the window must have a length")
    with session_scope(actor=who.actor) as s:
        v, row = _draft_verdict(s, version_id, None, None)
        if v.schedule is None or row is None:
            raise HTTPException(
                422, f"{v.words} A month cannot be costed without the schedule the rule requires."
            )
        got = price_series.month_block_means(s, body.segment)
        if got is None or body.month not in got.inr_per_kwh:
            raise HTTPException(422, f"no {body.segment.upper()} prices loaded for month {body.month}")
        used = got.vintage_of[body.month]
        by_block = got.inr_per_kwh[body.month]
    missing = [b for b in range(1, 97) if b not in by_block]
    if missing:
        raise HTTPException(
            422, f"the vintage has no price for {len(missing)} blocks of the day in this month"
        )
    # the year the vintage holds this month in (its last twelve months end at date_to)
    year = used.date_to.year if body.month <= used.date_to.month else used.date_to.year - 1
    days = calendar.monthrange(year, body.month)[1]
    lo, hi = body.from_hour * 4, body.to_hour * 4
    day_kw = [body.kw if (lo <= b < hi if lo < hi else b >= lo or b < hi) else ZERO for b in range(96)]
    pct = (
        _typed(body.deviation_pct, Unit.PERCENT, "Deviation from the schedule")
        if (body.deviation_pct is not None)
        else exchange_plan.deviation_pct()
    )
    try:
        cost = exchange.month_cost(
            [by_block[b + 1] for b in range(96)] * days,
            day_kw * days,
            Decimal("0.25"),
            loss_pct=body.loss_pct,
            charges_inr_per_kwh=body.charges_inr_per_kwh,
            schedule=v.schedule,
            terms=v.deviation or exchange.DeviationTerms(),
            deviation_pct=pct.value,
            grid_inr_per_kwh=[body.grid_inr_per_kwh] * (96 * days),
        )
    except exchange.ScheduleRefused as e:
        raise HTTPException(422, f"The schedule is refused: {e}.") from e
    market = SourcedValue(
        value=Decimal(len(by_block)),
        unit=Unit.COUNT,
        source_refs=(
            SourceRef(
                kind=RefKind.MARKET,
                vintage_id=used.id,
                note=f"mean clearing price by block of the day, month {body.month}, {used.filename} "
                f"({used.date_from} to {used.date_to})",
            ),
        ),
        label=f"IEX {body.segment.upper()} MCP",
    )
    kw = _typed(body.kw, Unit.KW, "Quantity scheduled")
    loss = _typed(body.loss_pct, Unit.PERCENT, "Losses")
    charges = _typed(body.charges_inr_per_kwh, Unit.INR_PER_KWH, "Open-access charges")
    grid = _typed(body.grid_inr_per_kwh, Unit.INR_PER_KWH, "DISCOM energy rate")
    rule = [
        SourcedValue(value=ZERO, unit=Unit.COUNT, source_refs=(r,), label="Exchange purchase rule")
        for r in (v.deviation.refs if v.deviation else ())
    ]
    q2, q4 = Decimal("0.01"), Decimal("0.0001")

    def out(value: Decimal, unit: Unit, label: str, inputs: list[SourcedValue], q: Decimal) -> SourcedValue:
        return derive(value.quantize(q), unit, inputs, label=label)

    all_in = [market, kw, loss, charges, grid, pct, *rule]
    grid_inr = cost.delivered_kwh * body.grid_inr_per_kwh
    dearer = cost.total_inr > grid_inr
    return MonthCostOut(
        verdict=VerdictOut.model_validate(exchange_plan.verdict_json(v)),
        month=body.month,
        days=days,
        vintage=MonthVintageOut(
            month=body.month,
            vintage_id=used.id,
            filename=used.filename,
            file_sha256=used.file_sha256,
            date_from=used.date_from,
            date_to=used.date_to,
        ),
        scheduled_kwh=out(cost.scheduled_kwh, Unit.KWH, "Scheduled on the exchange", [kw], Decimal("0.001")),
        delivered_kwh=out(
            cost.delivered_kwh, Unit.KWH, "Delivered at the meter", [kw, loss], Decimal("0.001")
        ),
        price_inr=out(cost.price_inr, Unit.INR, "Paid at the clearing price", [market, kw], q2),
        charges_inr=out(cost.charges_inr, Unit.INR, "Open-access charges", [kw, loss, charges], q2),
        deviation_inr=out(cost.deviation_inr, Unit.INR, "Deviation risk", [*all_in], q2),
        total_inr=out(cost.total_inr, Unit.INR, "Cost for the month", all_in, q2),
        landed_inr_per_kwh=out(cost.landed_inr_per_kwh, Unit.INR_PER_KWH, "Landed cost", all_in, q4),
        grid_inr=out(grid_inr, Unit.INR, "The same energy from the DISCOM", [kw, loss, grid], q2),
        words=(
            f"At ₹{cost.landed_inr_per_kwh.quantize(q2)} per kWh landed against "
            f"₹{body.grid_inr_per_kwh} from the DISCOM, this schedule "
            f"{'costs more than' if dearer else 'lowers'} the grid's cost for the month by "
            f"₹{abs(cost.total_inr - grid_inr).quantize(q2)}."
        ),
    )
