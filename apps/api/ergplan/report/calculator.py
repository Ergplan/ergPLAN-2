"""The landed-cost calculator (Stage 19): for any consumer a visitor describes (state, DISCOM,
category, supply voltage, contract demand), the landed cost at the meter of solar and wind by route
(captive from an own plant, captive with 26% equity, third party) and of exchange purchase (IEX
day-ahead, green day-ahead or real-time), line by line and per ToD slot, beside the grid tariff and
the saving. A table puts every state side by side for one consumer definition.

It computes nothing of its own: every figure is `platform.routers.landed.landed()` on approved rules
(invariant 2), laid out per slot by the state rule book's own `report.statebook._route_table` (the
banking charge beside the landed cost, the saving on the grid tariff, the averages over the day's
hours), and the exchange verdict is Stage 18's `platform.exchange_plan.verdict_for` on approved
rules. What is judged here is only whether a route may be shown: the state's minimum contract
demand for open access, and the exchange verdict. Nothing is stored (the router runs it in a
read-only transaction).

Reading taken for the IEX route (invariants 1 and 2, rule 13): the exchange's landed cost is shown
to a client or visitor only where the state's approved verdict offers the purchase (feasible, or
feasible with conditions). Where the verdict is "not yet sourced" or "not practical" the route says
so with its reasons and their sources, and the figures are withheld; an analyst sees them, marked as
the analyst's figure that a client does not see.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy.orm import Session

from ..core.sourced import RefKind, SourcedValue, SourceRef, derive
from ..core.units import Unit
from ..db.models import Jurisdiction
from ..engine import landed_cost
from ..engine import scope as escope
from ..engine.gaps import GAPS
from ..intake import defaults
from ..platform import exchange_plan
from ..platform.routers import landed as landed_api
from ..rulebook import repository
from ..rulebook.service import jurisdiction_chain
from . import statebook

ZERO = Decimal(0)
TECHS: tuple[tuple[str, str], ...] = (("solar", "Solar"), ("wind", "Wind"))
SOURCE_OF = {
    ("solar", "intra_state"): "oa_solar_intra",
    ("wind", "intra_state"): "oa_wind_intra",
    ("solar", "ists"): "oa_solar_ists",
    ("wind", "ists"): "oa_wind_ists",
}
IEX_OF = {"dam": "iex_dam", "gdam": "iex_gdam", "rtm": "iex_rtm"}
MARKET_WORDS = {"dam": "day-ahead", "gdam": "green day-ahead", "rtm": "real-time"}
# the route order on the screen: own plant, 26% equity, third party (the rule book's ROUTES)
ROUTE_ORDER = ("captive", "group_captive", "third_party")
ALL_KINDS: list[JsonValue] = landed_api.ALL_KINDS
OWNER = "rules.body + price_series + calculator input"
INPUT_KINDS: list[JsonValue] = ["assumption", "client_stated", "derived"]
MIN_CD = "min_contract_demand_for_oa_kva"


class CalculatorError(ValueError):
    pass


# ---------------------------------------------------------------- input


class PlantIn(BaseModel):
    """What the visitor states about one technology's plant; anything left out is ergPLAN's
    benchmark, shown as an assumption."""

    model_config = ConfigDict(extra="forbid")

    ppa_tariff_inr_per_kwh: Decimal | None = Field(default=None, gt=0, le=50)
    capex_inr_per_kw: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    cuf_pct: Decimal | None = Field(default=None, gt=0, le=100)
    om_pct_of_capex: Decimal | None = Field(default=None, ge=0, le=20)
    life_years: int | None = Field(default=None, ge=1, le=40)
    degradation_pct: Decimal | None = Field(default=None, ge=0, le=5)
    capacity_mw: Decimal | None = Field(default=None, gt=0, le=10_000)
    # the cost of the consumer's equity in a 26% plant, per kWh, where they know it (else it is
    # worked from the equity share and the plant's capital cost)
    equity_cost_inr_per_kwh: Decimal | None = Field(default=None, ge=0, le=50)


class CalculatorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discom: str = Field(min_length=1, max_length=40)
    state: str | None = Field(default=None, max_length=8)  # checked against the DISCOM's state
    category: str | None = Field(default=None, max_length=80)  # default: the DISCOM's HT industrial
    rate_block: str | None = Field(default=None, max_length=80)
    voltage_kv: Decimal = Field(default=Decimal(33), gt=0, le=800)
    contract_demand_kva: Decimal = Field(default=Decimal(5000), gt=0, le=10_000_000)
    month: date | None = None  # any day of the month; default this month
    as_of: date | None = None  # the rules in force on; default as the state rule book takes it
    connection: Literal["intra_state", "ists"] = "intra_state"
    solar: PlantIn = Field(default_factory=PlantIn)
    wind: PlantIn = Field(default_factory=PlantIn)
    equity_share_pct: Decimal | None = Field(default=None, gt=0, le=100)
    iex_market: Literal["dam", "gdam", "rtm"] = "dam"


# ---------------------------------------------------------------- output


class NotSourced(BaseModel):
    """A line the approved rules do not state: shown as "not yet sourced", never estimated."""

    component: str
    words: str


class CalcSlot(BaseModel):
    slot: str
    lines: list[SourcedValue] = Field(json_schema_extra={"owner": OWNER, "ref_kinds": ALL_KINDS})
    landed: SourcedValue = Field(json_schema_extra={"owner": OWNER, "ref_kinds": ALL_KINDS})
    # the banking charge falls on banked units only: beside the landed cost, not in it
    banking: SourcedValue | None = Field(
        default=None, json_schema_extra={"owner": "rules.body (settlement)", "ref_kinds": ALL_KINDS}
    )
    grid: SourcedValue | None = Field(
        default=None, json_schema_extra={"owner": "rules.body (tariff_schedule)", "ref_kinds": ALL_KINDS}
    )
    saving: SourcedValue | None = Field(
        default=None, json_schema_extra={"owner": OWNER, "ref_kinds": ALL_KINDS}
    )


class CalcRoute(BaseModel):
    route: str  # captive | group_captive | third_party | iex
    label: str
    note: str
    source: str  # the landed-cost source id
    allowed: bool  # the state's rules allow the route for this consumer, as far as they say
    shown: bool  # its figures are on the screen (see `withheld`)
    reason: str | None = None  # why not allowed, why not shown, or why it cannot be priced
    reason_values: list[SourcedValue] = Field(
        default_factory=list, json_schema_extra={"owner": "rules.body", "ref_kinds": ALL_KINDS}
    )
    withheld: str | None = None  # why an analyst sees figures a client does not
    slots: list[CalcSlot] = Field(default_factory=list)
    landed_avg: SourcedValue | None = Field(
        default=None, json_schema_extra={"owner": OWNER, "ref_kinds": ALL_KINDS}
    )
    saving_avg: SourcedValue | None = Field(
        default=None, json_schema_extra={"owner": OWNER, "ref_kinds": ALL_KINDS}
    )
    not_sourced: list[NotSourced] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    inputs: list[SourcedValue] = Field(
        default_factory=list, json_schema_extra={"owner": "calculator input", "ref_kinds": INPUT_KINDS}
    )
    rules_used: list[str] = Field(default_factory=list)


class CalcTech(BaseModel):
    tech: str
    label: str
    routes: list[CalcRoute]


class GridSlot(BaseModel):
    slot: str
    start: str
    end: str
    hours: str
    rate: SourcedValue | None = Field(
        default=None, json_schema_extra={"owner": "rules.body (tariff_schedule)", "ref_kinds": ALL_KINDS}
    )


class CalcExchange(BaseModel):
    market: str
    market_label: str
    verdict: dict[str, JsonValue]  # Stage 18's verdict: words, reasons and conditions with citations
    route: CalcRoute


class BookLink(BaseModel):
    book_id: str
    issue: int
    month: date
    title: str
    file: str  # the approved issue as a file, open to a visitor


class CalculatorOut(BaseModel):
    state: str
    state_name: str
    discom: str
    discom_name: str
    category: str
    rate_block: str | None
    voltage_kv: SourcedValue = Field(
        json_schema_extra={"owner": "calculator input", "ref_kinds": INPUT_KINDS}
    )
    contract_demand_kva: SourcedValue = Field(
        json_schema_extra={"owner": "calculator input", "ref_kinds": INPUT_KINDS}
    )
    month: date
    as_of: date
    connection: str
    slots: list[GridSlot]
    technologies: list[CalcTech]
    exchange: CalcExchange
    limits: list[landed_api.LimitOut]
    gaps: dict[str, str]
    rulebook: BookLink | None
    stored: bool = False  # a calculation is never stored


# ---------------------------------------------------------------- the consumer


def _stated(value: Decimal, unit: Unit, label: str) -> SourcedValue:
    return SourcedValue(
        value=value, unit=unit, label=label, source_refs=(SourceRef(kind=RefKind.CLIENT_STATED),)
    )


def categories(s: Session, discom: str, on: date) -> list[dict[str, str | None]]:
    """The categories of the DISCOM's approved retail schedule, with their rate blocks."""
    rows = repository.approved_rules(s, "tariff_schedule", jurisdiction_chain(s, discom), on)
    out: dict[tuple[str, str | None], dict[str, str | None]] = {}
    for r in rows:
        cat = r.body.get("category")
        if not cat:
            continue
        block = str(r.scope["rate_block"]) if r.scope.get("rate_block") else None
        out.setdefault((str(cat), block), {"category": str(cat), "rate_block": block, "label": r.label})
    return list(out.values())


def _month_and_as_of(inp: CalculatorIn) -> tuple[date, date]:
    month = (inp.month or date.today()).replace(day=1)
    if inp.as_of is not None:
        return month, inp.as_of
    today = date.today()
    return month, today if (today.year, today.month) == (month.year, month.month) else month.replace(day=15)


def consumer(s: Session, inp: CalculatorIn, on: date) -> escope.ConnectionFacts:
    discom = inp.discom.strip().lower()
    if discom not in defaults.DISCOM_STATE or s.get(Jurisdiction, discom) is None:
        raise CalculatorError(f"unknown DISCOM {inp.discom!r}")
    state = defaults.DISCOM_STATE[discom]
    if inp.state and inp.state.upper() != state:
        raise CalculatorError(f"{discom} is in {state}, not {inp.state.upper()}")
    ref_cat, ref_block = statebook.reference_category(s, discom, on)
    cats = categories(s, discom, on)
    cat = inp.category or ref_cat or "HT industrial"
    if cats and cat not in {c["category"] for c in cats}:
        raise CalculatorError(
            f"{cat!r} is not a category of {discom}'s approved schedule: "
            + ", ".join(sorted({str(c["category"]) for c in cats}))
        )
    blocks = [c["rate_block"] for c in cats if c["category"] == cat and c["rate_block"]]
    block = inp.rate_block if inp.rate_block in blocks else (ref_block if cat == ref_cat else None)
    if block is None and blocks:
        block = blocks[0]
    return escope.ConnectionFacts(
        discom=discom,
        state=state,
        category=cat,
        voltage_kv=inp.voltage_kv,
        connection_kind="ht",
        consumer_kind=defaults.connection_kind(inp.voltage_kv),
        contract_demand_kva=inp.contract_demand_kva,
        rate_block=block,
    )


def _scenario(inp: CalculatorIn) -> dict[str, landed_api.SourceScenario]:
    out: dict[str, landed_api.SourceScenario] = {}
    for tech, _label in TECHS:
        p: PlantIn = getattr(inp, tech)
        out[SOURCE_OF[(tech, inp.connection)]] = landed_api.SourceScenario(
            price_inr_per_kwh=p.ppa_tariff_inr_per_kwh,
            capex_inr_per_kw=p.capex_inr_per_kw,
            cuf_pct=p.cuf_pct,
            om_pct_of_capex=p.om_pct_of_capex,
            life_years=p.life_years,
            degradation_pct=p.degradation_pct,
            capacity_mw=p.capacity_mw,
            equity_cost_inr_per_kwh=p.equity_cost_inr_per_kwh,
            equity_share_pct=inp.equity_share_pct,
        )
    return out


# ---------------------------------------------------------------- the routes


def _route(
    out: landed_api.LandedOut,
    source_id: str,
    route: str,
    label: str,
    note: str,
    hours: dict[str, Decimal],
    grid: dict[str, SourcedValue],
) -> CalcRoute:
    t = statebook._route_table(out, source_id, hours, grid)
    gaps = list(t["gaps"])
    not_sourced = [NotSourced(component=g, words=GAPS.get(g, g.replace("_", " "))) for g in gaps]
    slots = [CalcSlot.model_validate(x) for x in t["slots"]]
    spec = landed_cost.SOURCE_BY_ID[source_id]
    if slots and spec.open_access and spec.price_kind == "ppa" and not any(x.banking for x in slots):
        not_sourced.append(
            NotSourced(
                component="banking",
                words="No banking charge in the approved rules for this route: banking is not stated, "
                "or not allowed, for it in this state.",
            )
        )
    return CalcRoute(
        route=route,
        label=label,
        note=note,
        source=source_id,
        allowed=bool(slots),
        shown=bool(slots),
        reason=t["reason"] if not slots else None,
        slots=slots,
        landed_avg=t["landed_avg"],
        saving_avg=t["saving_avg"],
        not_sourced=not_sourced,
        notes=list(t["notes"]),
        inputs=[SourcedValue.model_validate(x) for x in t["inputs"]],
        rules_used=list(t["rules_used"]),
    )


def _bar(r: CalcRoute, why: str, values: list[SourcedValue]) -> CalcRoute:
    """A route the state's rules do not allow: said why, with the rule; no figures."""
    return r.model_copy(
        update={"allowed": False, "shown": False, "reason": why, "reason_values": values, "slots": [],
                "landed_avg": None, "saving_avg": None}
    )  # fmt: skip


def calculate(s: Session, inp: CalculatorIn, *, analyst: bool = False) -> CalculatorOut:
    """The calculator's answer for one consumer. `analyst`: the exchange's figures are shown even
    where the state's verdict does not offer the purchase, marked as the analyst's figure."""
    month, as_of = _month_and_as_of(inp)
    facts = consumer(s, inp, as_of)
    factor = statebook._reference_factor()
    sources = _scenario(inp)
    by_route: dict[str, landed_api.LandedOut] = {}
    for route in ROUTE_ORDER:
        sc = landed_api.ScenarioIn(month=month.month, as_of=as_of, sources=sources)
        by_route[route] = landed_api.landed(
            s,
            facts=facts,
            factor=factor,
            contract_kva=facts.contract_demand_kva,
            assets=[],
            recorded=None,
            sc_in=sc,
            connection_id="",
            ownership_all=route,
        )
    base = by_route["third_party"]
    hours = {x.name: statebook._slot_hours(x.start, x.end) for x in base.slots}
    grid = {a.slot: a.rate for a in base.avoided}
    words = {r: (label, what) for r, label, what in statebook.ROUTES}
    # the state's minimum contract demand for open access, where its approved rules state one
    min_cd = next((x for x in base.limits if x.limit == MIN_CD), None)
    techs = []
    for tech, tech_label in TECHS:
        sid = SOURCE_OF[(tech, inp.connection)]
        routes = []
        for route in ROUTE_ORDER:
            label, what = words[route]
            r = _route(by_route[route], sid, route, label, what, hours, grid)
            cd = facts.contract_demand_kva
            if min_cd is not None and cd is not None and cd < min_cd.value.value:
                r = _bar(
                    r,
                    f"Open access is allowed in this state only from a contract demand of "
                    f"{min_cd.value.value} kVA ({min_cd.label or 'approved rule'}); this consumer's is "
                    f"{cd} kVA.",
                    [min_cd.value],
                )
            routes.append(r)
        techs.append(CalcTech(tech=tech, label=tech_label, routes=routes))
    # the exchange: Stage 18's verdict on approved rules, then the figures it allows
    iex_id = IEX_OF[inp.iex_market]
    spec = landed_cost.SOURCE_BY_ID[iex_id]
    v = exchange_plan.verdict_for(s, facts, as_of, repository.approved_rules, inp.iex_market)
    vj = exchange_plan.verdict_json(v, iex_id, spec.label)
    iex = _route(
        base,
        iex_id,
        "iex",
        f"IEX {MARKET_WORDS[inp.iex_market]}",
        "Bought on the power exchange through open access, at the month's mean clearing price per ToD "
        "slot; whether the state lets an open-access consumer buy there, and how the purchase must be "
        "scheduled, is the verdict below.",
        hours,
        grid,
    )
    if v.verdict == "not_practical":
        iex = _bar(iex, f"Not practical in this state: {v.words}{exchange_plan.cited(v.reasons)}", [])
    elif not v.offers:
        if analyst and iex.slots:
            iex = iex.model_copy(
                update={
                    "withheld": f"The analyst's figure: the state's exchange verdict is "
                    f'"{v.label}", so a client or visitor does not see this landed cost. {v.words}',
                }
            )
        else:
            iex = iex.model_copy(
                update={
                    "shown": False,
                    "slots": [],
                    "landed_avg": None,
                    "saving_avg": None,
                    # a market that cannot be priced says so first (no prices loaded)
                    "reason": "; ".join(x for x in (iex.reason, f"Not yet sourced: {v.words}") if x),
                }
            )
    sj = s.get(Jurisdiction, facts.state)
    dj = s.get(Jurisdiction, facts.discom)
    book = statebook.last_approved(s, facts.discom, None, month)
    link = (
        BookLink(
            book_id=book.id,
            issue=book.issue,
            month=book.month,
            title=str(book.content.get("title") or ""),
            file=f"/calculator/statebooks/{book.id}/file",
        )
        if book is not None and book.month == month
        else None
    )
    return CalculatorOut(
        state=facts.state,
        state_name=sj.name if sj else facts.state,
        discom=facts.discom,
        discom_name=dj.name if dj else facts.discom,
        category=facts.category,
        rate_block=facts.rate_block,
        voltage_kv=_stated(inp.voltage_kv, Unit.KV, "Supply voltage"),
        contract_demand_kva=_stated(inp.contract_demand_kva, Unit.KVA, "Contract demand"),
        month=month,
        as_of=as_of,
        connection=inp.connection,
        slots=[
            GridSlot(slot=x.name, start=x.start, end=x.end, hours=str(hours[x.name]), rate=grid.get(x.name))
            for x in base.slots
        ],
        technologies=techs,
        exchange=CalcExchange(
            market=inp.iex_market, market_label=MARKET_WORDS[inp.iex_market], verdict=vj, route=iex
        ),
        limits=base.limits,
        gaps=base.gaps,
        rulebook=link,
    )


# ---------------------------------------------------------------- every state at once


class StateCell(BaseModel):
    """One route's averages in the all-states table (over the day's hours)."""

    route: str
    tech: str  # solar | wind | iex
    shown: bool
    reason: str | None = None
    landed_avg: SourcedValue | None = Field(
        default=None, json_schema_extra={"owner": OWNER, "ref_kinds": ALL_KINDS}
    )
    saving_avg: SourcedValue | None = Field(
        default=None, json_schema_extra={"owner": OWNER, "ref_kinds": ALL_KINDS}
    )
    not_sourced: list[str] = Field(default_factory=list)


class StateRow(BaseModel):
    state: str
    state_name: str
    discom: str
    discom_name: str
    category: str | None
    error: str | None = None
    grid_avg: SourcedValue | None = Field(
        default=None, json_schema_extra={"owner": "rules.body (tariff_schedule)", "ref_kinds": ALL_KINDS}
    )
    cells: list[StateCell] = Field(default_factory=list)
    exchange_verdict: str | None = None
    rulebook: BookLink | None = None


class StatesOut(BaseModel):
    month: date
    voltage_kv: SourcedValue = Field(
        json_schema_extra={"owner": "calculator input", "ref_kinds": INPUT_KINDS}
    )
    contract_demand_kva: SourcedValue = Field(
        json_schema_extra={"owner": "calculator input", "ref_kinds": INPUT_KINDS}
    )
    iex_market: str
    rows: list[StateRow]


def discoms(s: Session, on: date) -> list[str]:
    """The DISCOMs the calculator can price: an approved open-access charge or settlement rule (the
    state rule book's own selection)."""
    out = []
    for discom in sorted(defaults.DISCOM_STATE):
        if s.get(Jurisdiction, discom) is None:
            continue
        chain = jurisdiction_chain(s, discom)
        if repository.approved_rules(s, "oa_charge", chain, on) or repository.approved_rules(
            s, "settlement", chain, on
        ):
            out.append(discom)
    return out


def _grid_avg(out: CalculatorOut) -> SourcedValue | None:
    pairs = [(Decimal(x.hours), x.rate) for x in out.slots if x.rate is not None]
    h = sum((p[0] for p in pairs), ZERO)
    if not pairs or h <= 0 or len(pairs) != len(out.slots):
        return None

    v = sum((p[0] * p[1].value for p in pairs), ZERO) / h
    return derive(v.quantize(Decimal("0.0001")), Unit.INR_PER_KWH, [p[1] for p in pairs],
                  label="Grid tariff, average over the day's hours")  # fmt: skip


def all_states(s: Session, inp: CalculatorIn, *, analyst: bool = False) -> StatesOut:
    """One consumer definition (voltage, contract demand, plant and tariff, market and month) for
    every DISCOM the calculator can price, each on its own reference HT industrial category (a
    category's name is the DISCOM's own)."""
    month, as_of = _month_and_as_of(inp)
    rows: list[StateRow] = []
    for discom in discoms(s, as_of):
        state = defaults.DISCOM_STATE[discom]
        sj, dj = s.get(Jurisdiction, state), s.get(Jurisdiction, discom)
        row = StateRow(
            state=state,
            state_name=sj.name if sj else state,
            discom=discom,
            discom_name=dj.name if dj else discom,
            category=None,
        )
        one = inp.model_copy(update={"discom": discom, "state": None, "category": None, "rate_block": None})
        try:
            out = calculate(s, one, analyst=analyst)
        except CalculatorError as e:
            rows.append(row.model_copy(update={"error": str(e)}))
            continue
        cells = [
            StateCell(
                route=r.route,
                tech=t.tech,
                shown=r.shown,
                reason=r.reason,
                landed_avg=r.landed_avg if r.shown else None,
                saving_avg=r.saving_avg if r.shown else None,
                not_sourced=[n.component for n in r.not_sourced],
            )
            for t in out.technologies
            for r in t.routes
        ]
        x = out.exchange.route
        cells.append(
            StateCell(
                route="iex",
                tech="iex",
                shown=x.shown,
                reason=x.reason,
                landed_avg=x.landed_avg if x.shown else None,
                saving_avg=x.saving_avg if x.shown else None,
                not_sourced=[n.component for n in x.not_sourced],
            )
        )
        rows.append(
            row.model_copy(
                update={
                    "category": out.category,
                    "grid_avg": _grid_avg(out),
                    "cells": cells,
                    "exchange_verdict": str(out.exchange.verdict.get("verdict_label") or ""),
                    "rulebook": out.rulebook,
                }
            )
        )
    return StatesOut(
        month=month,
        voltage_kv=_stated(inp.voltage_kv, Unit.KV, "Supply voltage"),
        contract_demand_kva=_stated(inp.contract_demand_kva, Unit.KVA, "Contract demand"),
        iex_market=inp.iex_market,
        rows=rows,
    )


ROUTE_WORDS = {
    "captive": "captive own plant",
    "group_captive": "captive 26% equity",
    "third_party": "third party",
    "iex": "IEX",
}


def _src(v: SourcedValue | None) -> str:
    """Where a figure comes from, in a CSV cell: the pages it opens, else its kinds."""
    if v is None:
        return ""
    pages = [f"{r.document_id}#page={r.page}" for r in v.source_refs if r.document_id and r.page]
    kinds = sorted({r.kind.value for r in v.source_refs})
    return "; ".join(dict.fromkeys(pages)) or ", ".join(kinds)


def to_csv(t: StatesOut) -> str:
    """The all-states table as CSV: one line per DISCOM and route, every figure with its sources;
    a route not shown carries its reason instead of a figure ("not yet sourced", not allowed)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["state", "discom", "category", "month", "voltage_kv", "contract_demand_kva", "technology", "route",
         "grid_inr_per_kwh", "landed_inr_per_kwh", "saving_inr_per_kwh", "landed_sources", "not_yet_sourced",
         "reason", "exchange_verdict", "state_rule_book"]
    )  # fmt: skip
    for r in t.rows:
        kv, cd = format(t.voltage_kv.value, "f"), format(t.contract_demand_kva.value, "f")
        head: list[Any] = [r.state, r.discom, r.category or "", t.month.isoformat(), kv, cd]
        if r.error:
            w.writerow([*head, "", "", "", "", "", "", "", r.error, "", ""])
            continue
        g = format(r.grid_avg.value, "f") if r.grid_avg else "not yet sourced"
        for c in r.cells:
            w.writerow(
                [*head, c.tech, ROUTE_WORDS.get(c.route, c.route), g,
                 format(c.landed_avg.value, "f") if c.landed_avg else "",
                 format(c.saving_avg.value, "f") if c.saving_avg else "",
                 _src(c.landed_avg), "; ".join(c.not_sourced), c.reason or "",
                 r.exchange_verdict or "", r.rulebook.file if r.rulebook else ""]
            )  # fmt: skip
    return buf.getvalue()


# ---------------------------------------------------------------- the choices


class DiscomChoice(BaseModel):
    discom: str
    name: str
    categories: list[dict[str, str | None]]
    reference_category: str | None


class StateChoice(BaseModel):
    state: str
    name: str
    discoms: list[DiscomChoice]


class OptionsOut(BaseModel):
    as_of: date
    states: list[StateChoice]
    markets: list[dict[str, str]]


def options(s: Session, on: date) -> OptionsOut:
    by_state: dict[str, list[DiscomChoice]] = {}
    for discom in discoms(s, on):
        dj = s.get(Jurisdiction, discom)
        ref, _block = statebook.reference_category(s, discom, on)
        by_state.setdefault(defaults.DISCOM_STATE[discom], []).append(
            DiscomChoice(
                discom=discom, name=dj.name if dj else discom, categories=categories(s, discom, on),
                reference_category=ref,
            )
        )  # fmt: skip
    states = []
    for st in sorted(by_state):
        sj = s.get(Jurisdiction, st)
        states.append(StateChoice(state=st, name=sj.name if sj else st, discoms=by_state[st]))
    return OptionsOut(
        as_of=on,
        states=states,
        markets=[{"market": k, "label": f"IEX {v}"} for k, v in MARKET_WORDS.items()],
    )
