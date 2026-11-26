"""Runs (Stage 6): assemble the optimizer's problem for a scenario from approved rules, the load
profile and the price vintages; solve; re-settle (invariant 6); store the result with every pin
(invariant 5); replay a run from its pins. The arithmetic is in optimizer/ and settlement/ (pure);
this module reads and writes the database."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..core.settings import get_settings
from ..core.sourced import RefKind, SourcedValue, SourceRef, derive
from ..core.units import Unit
from ..db.base import session_scope
from ..db.models import (
    Asset,
    Connection,
    LoadProfile,
    PricePoint,
    PriceVintage,
    Run,
    RunResult,
    Scenario,
    Site,
)
from ..engine import avoided_cost, landed_cost, lcoe, lifecycle, tariff_eval
from ..engine import levies as elevies
from ..engine import scope as escope
from ..engine.tariff_eval import slot_classes, slot_of
from ..intake import answers as site_answers
from ..intake import generation, generation_profiles, profile_synthesis
from ..optimizer import model as om
from ..optimizer.highs_solve import Solution, SolveFailed, solve
from ..optimizer.resettle import INR, Resettled, resettle, status_quo
from ..optimizer.scenarios import CandidateSpec, ScenarioSpec, reprice, unproven_note
from ..rulebook.models import RuleView
from ..rulebook.repository import Reader
from ..rulebook.scope import matches
from ..rulebook.service import jurisdiction_chain
from .routers.cost import _facts, _kvah_factor, _rules_for, bill_levies
from .routers.landed import DEFAULT_PRICE

WORKER = "worker@ergplan"
CATEGORY = {
    "captive": "captive_generation",
    "group_captive": "captive_generation",
    "third_party": "bilateral",
}
GRID_FACTOR = (
    "0.71",
    "CEA CO2 Baseline Database, weighted average emission rate of the all-India grid (recent versions "
    "0.71–0.73 tCO2/MWh); state the version you report against",
)
ROOF_SQM_PER_KWP = Decimal(10)
STORAGE = ("btm_bess", "oa_bess")
OWNED_OA = ("captive", "group_captive")  # ownerships that put capital of the consumer's own in a plant
ZERO = Decimal(0)


class RunError(RuntimeError):
    pass


@dataclass
class CandMeta:
    spec: CandidateSpec
    cid: str
    label: str
    price: SourcedValue | None = None
    delivered: SourcedValue | None = None
    cuf: SourcedValue | None = None
    waterfalls: dict[str, list[SourcedValue]] = field(default_factory=dict)  # slot → lines (as-of month)
    chains: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # slot → steps, for the audit
    totals: list[SourcedValue] = field(default_factory=list)  # every landed total used
    capex_inr: Decimal = ZERO  # per kW (rooftop) or per kWh (battery)
    crf: SourcedValue | None = None
    life_years: int = 25
    # for the lifecycle view: the plant's O&M, life and degradation as the cost model states them
    om_pct: SourcedValue | None = None
    life_sv: SourcedValue | None = None
    degradation: SourcedValue | None = None
    inputs: list[SourcedValue] = field(default_factory=list)
    rule: RuleView | None = None
    gaps: list[str] = field(default_factory=list)
    ownership: str | None = None
    # electricity duty per kWh assumed on this source's units (engine.levies), when it was applied:
    # no approved electricity_duty_oa row covers the source
    own_duty: SourcedValue | None = None
    # capital of the consumer's own in this source: "plant" (rooftop, a captive open-access plant),
    # "battery", or "equity" (the outlay for a plant taken with 26% equity); None: bought by the unit
    owned: str | None = None
    capital_words: str | None = None  # what the capital is, in words, beside its figure
    # what the capital per kW (or per kWh) rests on: the capital cost, and the equity share
    capital_inputs: list[SourcedValue] = field(default_factory=list)


@dataclass
class LevyUse:
    """A levy taken from the client's bills, as the run used it."""

    levy: elevies.Levy
    on_discom_kwh: SourcedValue  # INR per kWh bought from the DISCOM (in the grid rate of every block)
    on_other_kwh: SourcedValue | None  # INR per kWh not bought from the DISCOM, None when not charged


@dataclass
class Assembled:
    problem: om.Problem
    cands: list[CandMeta]
    rate_cards: list[SourcedValue]
    demand_inputs: list[SourcedValue]
    profile_ref: SourcedValue
    profile: LoadProfile
    vintages: list[str]
    limits: dict[str, tuple[str, list[SourcedValue]]]  # binding name → plain words, refs
    rule: RuleView | None
    notes: list[str]
    as_of: date
    cd: SourcedValue | None
    grid_factor: SourcedValue
    spec: ScenarioSpec | None = None
    # what the run read from the client's bills, pinned so a replay reproduces it (invariant 5)
    bill_levies: tuple[elevies.Levy, ...] = ()
    bill_factors: tuple[SourcedValue, SourcedValue] | None = None  # kVAh per kWh, power factor
    levies: list[LevyUse] = field(default_factory=list)  # those in force (the rule does not state them)
    thermal_duty: SourcedValue | None = None
    # how capital is treated (pinned, invariant 5). 2: a captive open-access plant is costed from its
    # capital and O&M, a plant with 26% equity carries the equity outlay, payback and IRR are on the
    # owned capital. 1: runs made before 25 Nov 2026 (captive at a PPA tariff, no capital), kept so
    # they replay as they were solved
    capital_treatment: int = 2
    # the generation profiles supplied for the site that the run generated with, pinned by id and
    # sha256 (invariant 5): "solar:open_access" → {id, sha256, file, provenance}. Empty: library curves
    generation_profiles: dict[str, dict[str, str]] = field(default_factory=dict)


def _f(x: Decimal | float) -> float:
    return float(x)


def _sv(value: Decimal, unit: Unit, ref: SourceRef, label: str) -> SourcedValue:
    return SourcedValue(value=value, unit=unit, source_refs=(ref,), label=label)


def _stated(v: Decimal | int | None, default: SourcedValue, what: str) -> SourcedValue:
    """The client's figure for a cost or yield input when they gave one (shown as theirs), else the
    assumption."""
    if v is None:
        return default
    return SourcedValue(
        value=Decimal(str(v)),
        unit=default.unit,
        source_refs=(SourceRef(kind=RefKind.CLIENT_STATED, note=what),),
        label=default.label,
    )


def _storage_cost(cs: CandidateSpec) -> lcoe.StorageCost:
    st = lcoe.default_bess()
    return dataclasses.replace(
        st,
        capex_inr_per_kwh=_stated(cs.capex_inr_per_kwh, st.capex_inr_per_kwh, "battery capital cost per kWh"),
        om_pct_of_capex=_stated(cs.om_pct_of_capex, st.om_pct_of_capex, "battery O&M"),
        life_years=_stated(cs.life_years, st.life_years, "battery life"),
        round_trip_pct=_stated(cs.round_trip_pct, st.round_trip_pct, "battery round-trip efficiency"),
    )


CAPITAL_TREATMENT = 2
NO_TARIFF = "No tariff: your own plant (its capital and O&M are a yearly cost)"


def capital_treatment(pins: dict[str, Any] | None) -> int:
    """The capital treatment a stored run was solved with: runs made before the captive plant carried
    its capital have no `capital` pin and are treatment 1."""
    return int(((pins or {}).get("capital") or {}).get("treatment") or 1)


def own_plant(source: str, cs: CandidateSpec, cuf: SourcedValue | None) -> lcoe.GenerationCost:
    """The cost of an open-access plant of the consumer's own (captive, or the plant a 26% equity
    holding is in): the benchmark for the technology, each figure the client stated in its place,
    and the CUF the run generates with (the state's normative curve or the client's)."""
    g0 = lcoe.default_utility(source)
    return dataclasses.replace(
        g0,
        capex_inr_per_kw=_stated(cs.capex_inr_per_kw, g0.capex_inr_per_kw, "the plant's capital cost per kW"),
        om_pct_of_capex=_stated(cs.om_pct_of_capex, g0.om_pct_of_capex, "the plant's O&M"),
        cuf_pct=cuf or g0.cuf_pct,
        life_years=_stated(cs.life_years, g0.life_years, "the plant's life"),
        degradation_pct=_stated(cs.degradation_pct, g0.degradation_pct, "the plant's degradation"),
    )


def _iex_prices(
    s: Session,
    segment: str,
    vintage_id: str | None,
    months: list[int],
    slots_of: dict[int, list[str]],
    tod_for: Any,
) -> tuple[dict[tuple[int, str], SourcedValue], str, dict[int, dict[int, Decimal]]] | None:
    """Mean clearing price per (month, ToD slot) from a vintage (the latest live one unless pinned),
    and the mean clearing price per (month, 15-minute block of the day, 1–96) in INR/kWh: the
    optimizer buys block by block at the second, the slot means carry the citations."""
    q = select(PriceVintage).where(PriceVintage.segment == segment)
    q = (
        q.where(PriceVintage.id == vintage_id)
        if vintage_id
        else q.where(PriceVintage.superseded_by.is_(None))
    )
    v = s.scalars(q.order_by(PriceVintage.date_to.desc())).first()
    if v is None:
        return None
    rows = s.execute(
        select(func.extract("month", PricePoint.day), PricePoint.block, func.avg(PricePoint.mcp_inr_per_mwh))
        .where(PricePoint.vintage_id == v.id)
        .group_by(func.extract("month", PricePoint.day), PricePoint.block)
    ).all()
    by: dict[int, dict[int, Decimal]] = defaultdict(dict)
    for mo, blk, p in rows:
        by[int(mo)][int(blk)] = Decimal(str(p))
    out: dict[tuple[int, str], SourcedValue] = {}
    for mo in months:
        tod = tod_for(mo)
        for name in slots_of[mo]:
            blocks = [
                b
                for b in by.get(mo, {})
                if tod is None or (slot_of(tod, mo, (b - 1) * 15) or {}).get("name") == name
            ]
            if not blocks:
                continue
            mean = sum((by[mo][b] for b in blocks), ZERO) / len(blocks) / 1000
            out[(mo, name)] = _sv(
                mean.quantize(Decimal("0.0001")),
                Unit.INR_PER_KWH,
                SourceRef(
                    kind=RefKind.MARKET,
                    vintage_id=v.id,
                    note=f"mean clearing price, {name}, month {mo}, {v.filename} "
                    f"({v.date_from} to {v.date_to})",
                ),
                f"IEX {segment.upper()} MCP",
            )
    per_block = {mo: {b: x / 1000 for b, x in row.items()} for mo, row in by.items()}
    return out, v.id, per_block


def assemble(
    s: Session,
    sc: Scenario,
    spec: ScenarioSpec,
    reader: Reader,
    profile_id: str | None = None,
    vintage_pins: dict[str, str] | None = None,
    bill_pins: dict[str, Any] | None = None,
    treatment: int = CAPITAL_TREATMENT,
    generation_pins: dict[str, Any] | None = None,
) -> Assembled:
    """`bill_pins` (a replay): what the original run read from the client's bills — the levies and
    the kVAh factor and power factor — used instead of reading the bills again. `treatment` (a
    replay): the capital treatment the original run pinned (`capital_treatment`). `generation_pins`
    (a replay): the supplied generation profiles the original run pinned, used instead of those in
    use for the site today (an empty dict: the library curves)."""
    conn = s.get(Connection, sc.connection_id)
    if conn is None:
        raise RunError("connection not found")
    site = s.get(Site, conn.site_id)
    assert site is not None
    q = select(LoadProfile).where(LoadProfile.connection_id == conn.id)
    q = q.where(LoadProfile.id == profile_id) if profile_id else q.order_by(LoadProfile.version.desc())
    lp = s.scalars(q).first()
    if lp is None:
        raise RunError("no load profile for this connection")
    as_of = spec.as_of or date.today()
    kw = profile_synthesis.decode(lp.series_kw)
    # Stage 15: what the client stated, as the load profile recorded it (so a replay reads the same):
    # the planned expansion scales the year the sizing is run for, the contract demand intended
    # stands in for the bill's, and the roof stated bounds the rooftop plant
    growth = site_answers.growth_pct(lp.method)
    if growth is not None:
        kw = profile_synthesis.future_year(kw, growth)
    cd_intended = site_answers.contract_demand_kva(lp.method)
    cd_now = cd_intended or conn.contract_demand_kva
    roof_sqm = site_answers.roof_sqm(lp.method, site.roof_sqm)
    days = profile_synthesis.year_days(lp.year_start)
    facts = _facts(s, conn)
    if cd_intended is not None:
        facts = dataclasses.replace(facts, contract_demand_kva=cd_intended)
    chain = jurisdiction_chain(s, facts.discom)
    if bill_pins is None:
        from_bills = bill_levies(s, conn)
        factor, pf = _kvah_factor(s, conn)
    else:
        from_bills = tuple(elevies.Levy.from_json(x) for x in bill_pins.get("levies") or [])
        pinned = bill_pins.get("factors")
        factor, pf = (
            (SourcedValue.model_validate(pinned[0]), SourcedValue.model_validate(pinned[1]))
            if pinned
            else _kvah_factor(s, conn)  # a run made before the factors were pinned
        )
    rules_for = _rules_for(s, facts, as_of, reader, from_bills)
    notes: list[str] = []
    supplied = generation_profiles.for_run(s, site.id, generation_pins)
    gen_used: dict[str, dict[str, str]] = {}
    if growth is not None:
        notes.append(
            f"Sized for the year after the planned expansion: the load profile scaled by "
            f"{'+' if growth > 0 else ''}{site_answers.plain(growth)}% (the client's answer)"
        )
    if cd_intended is not None:
        notes.append(
            f"Sized on the contract demand the client intends to hold, {site_answers.plain(cd_intended)} kVA"
            + (
                f" (the bill shows {site_answers.plain(conn.contract_demand_kva)} kVA)"
                if conn.contract_demand_kva
                else ""
            )
        )

    # blocks, ToD slots and the avoided-cost rate of each block
    month_firsts = sorted({date(d.year, d.month, 1) for d in days})
    card_rate: dict[tuple[date, str], SourcedValue] = {}
    slot_meta: dict[date, list[tuple[int | None, str, frozenset[str]]]] = {}
    slots_of: dict[int, list[str]] = {}
    for mf in month_firsts:
        mr = rules_for(mf)
        if mr.schedule is None:
            raise RunError(f"no approved tariff schedule for {mf:%b %Y}")
        names = []
        if mr.tod is not None:
            for i, sl in enumerate(mr.tod.body.get("slots", [])):
                if sl.get("months") and mf.month not in sl["months"]:
                    continue
                card = avoided_cost.rate_card(mf, mr, i, factor, cd_now)
                if card is not None:
                    card_rate[(mf, str(sl["name"]))] = card.rate_inr_per_kwh
                names.append(str(sl["name"]))
        if not names:
            card = avoided_cost.rate_card(mf, mr, None, factor, cd_now)
            assert card is not None
            card_rate[(mf, "all hours")] = card.rate_inr_per_kwh
            names = ["all hours"]
        slots_of[mf.month] = names
        slot_meta[mf] = []
    # FPPCA and duty at the rates on the client's bills, where the approved tariff does not state
    # them: on DISCOM units they are in every block's grid rate (rate cards above), today and with the
    # plan alike; on units not bought from the DISCOM they follow the stated assumption
    mr0 = rules_for(month_firsts[0])
    basis0 = str(mr0.schedule.body.get("billing_basis", "kwh")) if mr0.schedule is not None else "kwh"
    levy_uses: list[LevyUse] = []
    for lv in avoided_cost.levies_in_force(mr0):
        on_grid = elevies.per_kwh(lv, basis0, factor)
        if on_grid is None:
            continue
        levy_uses.append(LevyUse(lv, on_grid, elevies.on_other_units(lv, basis0, factor)))
        notes.append(
            f"{lv.label}: not in the approved tariff, so charged at the rate on your own bills "
            f"(₹{lv.rate(tariff_eval.RATE_OF_BASIS[basis0]).value}/{'kVAh' if basis0 == 'kvah' else 'kWh'} "  # type: ignore[union-attr]
            f"billed) on every unit bought from the DISCOM, today and with the plan. {lv.words}"
        )
    duty_own = next((u.on_other_kwh for u in levy_uses if u.levy.code == elevies.DUTY), None)
    hours = spec.resolution_min / 60
    step = spec.resolution_min // 15
    if step > 1:
        notes.append(
            "hourly blocks: billing demand is each month's hourly peak, below the 15-minute peak a "
            "DISCOM meters, so demand charges today and with the plan are both a little understated; "
            "choose 15 minutes in the analysis settings for billing-grade demand"
        )
    blocks: list[om.BlockMeta] = []
    load: list[float] = []
    rate: list[float] = []
    load_d: list[Decimal] = []
    rate_d: list[Decimal] = []
    qday: list[int] = []  # first 15-minute block of the day (0–95) each block starts at
    for i in range(0, len(kw), step):
        qday.append(i % 96)
        day = days[i // 96]
        mf = date(day.year, day.month, 1)
        tod = rules_for(mf).tod
        sl = slot_of(tod, day.month, (i % 96) * 15) if tod else None
        name = str(sl["name"]) if sl else "all hours"
        cls = slot_classes(sl)
        blocks.append(om.BlockMeta(mf, name, cls, i // 4))
        load_d.append(sum(kw[i : i + step], ZERO) * Decimal("0.25"))
        load.append(_f(load_d[-1]))
        rv = card_rate.get((mf, name))
        if rv is None:
            raise RunError(f"no avoided-cost rate for {name} in {mf:%b %Y}")
        rate.append(_f(rv.value))
        rate_d.append(rv.value)

    # demand side
    demand_rate: dict[date, float] = {}
    excess_rate: dict[date, float] = {}
    demand_d: dict[date, Decimal] = {}
    excess_d: dict[date, Decimal] = {}
    floor = ZERO
    demand_inputs: list[SourcedValue] = []
    slabs_d: dict[date, list[tuple[Decimal | None, Decimal]]] = {}
    excess_abs_d: dict[date, Decimal] = {}
    min_kva = ZERO
    cd_level = cd_now or ZERO
    for mf in month_firsts:
        sch = rules_for(mf).schedule
        assert sch is not None
        dc = sch.get("/demand_charge/value")
        if dc not in (None, ""):
            slabs = tariff_eval.demand_slabs(sch)
            # the solver sees the slab rate at contract demand; the judge prices every slab exactly
            marginal = tariff_eval.marginal_demand_rate(sch, cd_level) or Decimal(str(dc))
            demand_d[mf] = marginal
            demand_rate[mf] = _f(marginal)
            for _, rate_v, ptr in slabs:
                sv = _sv(rate_v, Unit.INR_PER_KVA_MONTH, sch.ref(ptr), "Demand charge")
                if sv not in demand_inputs:
                    demand_inputs.append(sv)
            if len(slabs) > 1:
                slabs_d[mf] = [(hi, rate_v) for hi, rate_v, _ in slabs]
            ex_abs = sch.get("/demand_charge/billing_demand_rule/excess_demand_rate")
            mult = sch.get("/demand_charge/billing_demand_rule/excess_demand_penalty_multiplier")
            if ex_abs not in (None, ""):
                excess_abs_d[mf] = Decimal(str(ex_abs))
                slabs_d.setdefault(mf, [(hi, rate_v) for hi, rate_v, _ in slabs])
                excess_d[mf] = max(Decimal(str(ex_abs)) - marginal, ZERO)  # on top of the marginal rate
                excess_rate[mf] = _f(excess_d[mf])
            elif mult not in (None, ""):
                excess_d[mf] = Decimal(str(dc)) * Decimal(str(mult))
                excess_rate[mf] = _f(excess_d[mf])
        mk = sch.get("/demand_charge/billing_demand_rule/min_kva")
        if mk not in (None, ""):
            min_kva = Decimal(str(mk))
        fp = sch.get("/demand_charge/billing_demand_rule/min_pct_of_contract_demand")
        if fp not in (None, ""):
            floor = Decimal(str(fp))
    cd_kva = cd_now or ZERO
    cd_sv = (
        _sv(
            cd_kva,
            Unit.KVA,
            SourceRef(
                kind=RefKind.CLIENT_STATED,
                note="contract demand intended (the client's answer)"
                if cd_intended is not None
                else "contract demand",
            ),
            "Contract demand",
        )
        if cd_now
        else None
    )
    cds = spec.contract_demand
    grid = om.Grid(
        rate=rate,
        demand_rate=demand_rate,
        floor_pct=_f(floor),
        excess_rate=excess_rate,
        cd_kva=_f(cd_kva),
        pf=_f(pf.value),
        cd_step_kva=_f(cds.step_kva) if cds else None,
        cd_min_kva=_f(cds.min_kva or ZERO) if cds else 0.0,
        cd_max_kva=_f(cds.max_kva or cd_kva) if cds else None,
        cd_change_inr_per_kva_year=_f(cds.change_inr_per_kva_year) if cds else 0.0,
    )

    # rules for open access
    oa_rows = reader(s, "oa_charge", chain, as_of)
    waivers = reader(s, "ists_waiver", chain, as_of)
    settlements = reader(s, "settlement", chain, as_of)
    limit_rows = reader(s, "regulatory_limit", chain, as_of)
    ctx0 = escope.context(facts, date(as_of.year, as_of.month, 1))
    limits: list[om.Limit] = []
    limit_words: dict[str, tuple[str, list[SourcedValue]]] = {}
    min_oa_kw: tuple[float, SourcedValue] | None = None
    for r in limit_rows:
        if not matches(r.scope, ctx0):
            continue
        kind = str(r.body["limit"])
        v = Decimal(str(r.body["value"]))
        sv = _sv(
            v, Unit.PERCENT if r.body["unit"] == "percent" else Unit.COUNT, r.ref("/value"), r.label or kind
        )
        if kind == "min_oa_capacity_mw":
            min_oa_kw = (_f(v * 1000), sv)
            continue
        limits.append(om.Limit(kind, _f(v), r.label or kind, {}, None))
        limit_words[f"limit:{kind}:"] = (_limit_words(kind, v, r), [sv])

    cands: list[CandMeta] = []
    problem_cands: list[om.Candidate] = []
    batteries: list[om.Battery] = []
    vintages: list[str] = []
    governing: RuleView | None = None
    n = len(blocks)
    months = sorted(slots_of)
    first_of = {mf.month: mf for mf in month_firsts}

    energy_d: dict[str, list[Decimal]] = {}
    fixed_kw: dict[str, Decimal] = {}
    fixed_kwh: dict[str, Decimal] = {}

    def per_block(cid: str, by_slot: dict[tuple[int, str], Decimal]) -> list[float]:
        energy_d[cid] = [by_slot.get((b.month.month, b.slot), ZERO) for b in blocks]
        return [_f(x) for x in energy_d[cid]]

    # batteries at the open-access plant are added once the sources they store are known
    deferred_oa_bess: list[
        tuple[str, CandMeta, CandidateSpec, float, float, tuple[float, float], float, Decimal]
    ] = []
    oa_batteries: list[om.OABattery] = []

    for idx, cs in enumerate(spec.candidates):
        src = landed_cost.SOURCE_BY_ID[cs.kind]
        cid = f"{cs.kind}_{idx}" if sum(1 for x in spec.candidates if x.kind == cs.kind) > 1 else cs.kind
        meta = CandMeta(spec=cs, cid=cid, label=src.label)
        ownership = cs.ownership or ("group_captive" if cs.kind == "group_captive" else "third_party")
        meta.ownership = ownership
        lo_kw = (cs.max_mw if cs.fix_at_max and cs.max_mw else cs.min_mw) * 1000
        if cs.kind in ("btm_bess", "oa_bess"):
            st = _storage_cost(cs)
            f = lcoe.crf(st.discount_pct, st.life_years)
            yearly = st.capex_inr_per_kwh.value * (f.value + st.om_pct_of_capex.value / 100)
            rt = math.sqrt(_f(st.round_trip_pct.value) / 100)
            mx = cs.max_mw * 1000 if cs.max_mw else cd_kva * Decimal("0.5")
            hrs = (_f(cs.min_hours or Decimal(1)), _f(cs.max_hours or Decimal(4)))
            hrs = (min(hrs), max(hrs))
        if cs.kind == "oa_bess":
            deferred_oa_bess.append((cid, meta, cs, _f(mx), _f(lo_kw), hrs, rt, yearly))
            meta.capex_inr, meta.crf, meta.life_years = (
                st.capex_inr_per_kwh.value,
                f,
                int(st.life_years.value),
            )
            meta.om_pct, meta.life_sv = st.om_pct_of_capex, st.life_years
            meta.owned, meta.capital_inputs = "battery", [st.capex_inr_per_kwh]
            fixed_kwh[cid] = yearly
            energy_d[cid] = [ZERO] * n
            meta.inputs = [st.capex_inr_per_kwh, st.om_pct_of_capex, st.round_trip_pct, f]
            cands.append(meta)
            continue
        if cs.kind == "btm_bess":
            batteries.append(
                om.Battery(
                    cid,
                    max_kw=_f(mx),
                    min_kw=_f(lo_kw),
                    min_hours=hrs[0],
                    max_hours=hrs[1],
                    eff_charge=rt,
                    eff_discharge=rt,
                    inr_per_kwh_year=_f(yearly),
                )
            )
            meta.capex_inr, meta.crf, meta.life_years = (
                st.capex_inr_per_kwh.value,
                f,
                int(st.life_years.value),
            )
            meta.om_pct, meta.life_sv = st.om_pct_of_capex, st.life_years
            meta.owned, meta.capital_inputs = "battery", [st.capex_inr_per_kwh]
            fixed_kwh[cid] = yearly
            meta.inputs = [st.capex_inr_per_kwh, st.om_pct_of_capex, st.round_trip_pct, f]
            if not cs.max_mw:
                notes.append(
                    f"{meta.label}: at most half the contract demand in kW (assumption; set a limit "
                    "to change it)"
                )
            cands.append(meta)
            continue
        if cs.kind == "rooftop_pv":
            g0 = lcoe.default_rooftop()
            g = dataclasses.replace(
                g0,
                capex_inr_per_kw=_stated(
                    cs.capex_inr_per_kw, g0.capex_inr_per_kw, "rooftop capital cost per kW"
                ),
                om_pct_of_capex=_stated(cs.om_pct_of_capex, g0.om_pct_of_capex, "rooftop O&M"),
                cuf_pct=_stated(cs.cuf_pct, g0.cuf_pct, "rooftop CUF"),
                life_years=_stated(cs.life_years, g0.life_years, "rooftop life"),
                degradation_pct=_stated(cs.degradation_pct, g0.degradation_pct, "module degradation"),
            )
            f = lcoe.crf(g.discount_pct, g.life_years)
            yearly = g.capex_inr_per_kw.value * (f.value + g.om_pct_of_capex.value / 100)
            raw, cuf = generation.profile(
                generation.curve_for("solar", site.state), Decimal(1), lp.year_start
            )
            scale = g.cuf_pct.value / cuf
            own = generation_profiles.source_series("solar", "rooftop", site.state, lp.year_start, supplied)
            if own is not None:
                # the rooftop profile supplied for the site: its own shape and CUF, not scaled
                raw, scale = own.kw, Decimal(1)
                g = dataclasses.replace(g, cuf_pct=own.cuf)
                gen_used.update(own.pins)
                notes.append(f"{meta.label}: {own.note}")
            shape = _resample([v * scale * Decimal("0.25") for v in raw], step)
            if roof_sqm:
                mx = roof_sqm / ROOF_SQM_PER_KWP
                roof = _sv(
                    roof_sqm,
                    Unit.COUNT,
                    SourceRef(kind=RefKind.CLIENT_STATED, note="roof area, m²"),
                    "Roof area",
                )
                per = _sv(
                    ROOF_SQM_PER_KWP,
                    Unit.COUNT,
                    SourceRef(kind=RefKind.ASSUMPTION, note="10 m² of roof per kWp"),
                    "Roof per kWp",
                )
                limits.append(om.Limit("max_rooftop_kw", _f(mx), "roof area", {}, cid))
                limit_words[f"limit:max_rooftop_kw:{cid}"] = (
                    f"Rooftop solar limited by the roof: {roof_sqm} m² at 10 m² per kWp",
                    [roof, per],
                )
            mx2 = cs.max_mw * 1000 if cs.max_mw else (roof_sqm / ROOF_SQM_PER_KWP if roof_sqm else cd_kva)
            problem_cands.append(
                om.Candidate(
                    cid,
                    shape=shape,
                    energy_inr_per_kwh=[_f(duty_own.value) if duty_own is not None else 0.0] * n,
                    fixed_inr_per_kw_year=_f(yearly),
                    min_kw=min(_f(lo_kw), _f(mx2)),
                    max_kw=_f(mx2),
                    btm=True,
                )
            )
            meta.capex_inr, meta.crf, meta.life_years = g.capex_inr_per_kw.value, f, int(g.life_years.value)
            meta.om_pct, meta.life_sv, meta.degradation = g.om_pct_of_capex, g.life_years, g.degradation_pct
            meta.cuf = g.cuf_pct
            meta.owned, meta.capital_inputs = "plant", [g.capex_inr_per_kw]
            fixed_kw[cid] = yearly
            # rooftop units are not bought from the DISCOM: the assumed duty, when the bills print one
            energy_d[cid] = [duty_own.value if duty_own is not None else ZERO] * n
            meta.own_duty = duty_own
            meta.inputs = [g.capex_inr_per_kw, g.om_pct_of_capex, g.cuf_pct, f]
            if duty_own is not None:
                meta.inputs.append(duty_own)
            cands.append(meta)
            continue
        # open access (PPA or exchange)
        src_ctx = {**ctx0, "connection": src.connection, "source": src.source, "ownership": ownership}
        own_rule = next((r for r in settlements if matches(r.scope, src_ctx)), None)
        if governing is None and own_rule is not None:
            governing = own_rule
        meta.rule = own_rule
        cost_prices: dict[tuple[int, str], SourcedValue] = {}
        own_inputs: list[SourcedValue] = []
        if own_rule is None:
            # no approved settlement rule for this ownership in the state: the simulator sets off
            # in the same block with no banking, which the plan must say (rule 13)
            meta.gaps.append("settlement_rule")
        if src.price_kind == "iex":
            got = _iex_prices(
                s,
                src.iex_segment or "",
                (vintage_pins or {}).get(src.iex_segment or ""),
                months,
                slots_of,
                lambda mo: rules_for(first_of[mo]).tod,
            )
            if got is None:
                raise RunError(f"no {str(src.iex_segment).upper()} prices loaded")
            prices, vid, mcp_block = got
            vintages.append(vid)
            oa_shape: list[float] | None = [hours * 1.0] * n
            mx = cs.max_mw * 1000 if cs.max_mw else cd_kva
            cuf_sv = None
        else:
            if cs.price_inr_per_kwh is not None:
                p = _sv(
                    cs.price_inr_per_kwh,
                    Unit.INR_PER_KWH,
                    SourceRef(kind=RefKind.CLIENT_STATED, note=cs.stated_in),
                    "PPA tariff",
                )
            else:
                val, note = DEFAULT_PRICE[cs.kind]
                p = _sv(
                    Decimal(val),
                    Unit.INR_PER_KWH,
                    SourceRef(kind=RefKind.ASSUMPTION, note=f"{note}; change it to your quote"),
                    "PPA tariff",
                )
            meta.price = p
            prices = {(mo, nm): p for mo in months for nm in slots_of[mo]}
            raw, cuf = generation.profile(
                generation.curve_for(src.source, site.state), Decimal(1), lp.year_start
            )
            cuf_sv = _sv(
                cuf,
                Unit.PERCENT,
                SourceRef(
                    kind=RefKind.ASSUMPTION,
                    note=f"normative {src.source} curve for {site.state} (packages/profiles/cuf)",
                ),
                "CUF",
            )
            if cs.cuf_pct is not None:
                # the client's CUF: the normative curve keeps its shape and is scaled to it
                raw = [v * cs.cuf_pct / cuf for v in raw]
                cuf_sv = _sv(
                    cs.cuf_pct,
                    Unit.PERCENT,
                    SourceRef(
                        kind=RefKind.CLIENT_STATED,
                        note=f"CUF you stated; shape of the normative {src.source} curve, {site.state}",
                    ),
                    "CUF",
                )
            own = generation_profiles.source_series(
                src.source, "open_access", site.state, lp.year_start, supplied
            )
            if own is not None:
                # the developer's or the plant's own profile: its shape and CUF, not scaled to a typed CUF
                raw, cuf_sv = own.kw, own.cuf
                gen_used.update(own.pins)
                notes.append(
                    f"{meta.label}: {own.note}"
                    + ("; the CUF typed is not used" if cs.cuf_pct is not None else "")
                )
            meta.cuf = cuf_sv
            mx = cs.max_mw * 1000 if cs.max_mw else cd_kva * 3
            oa_shape = None  # after the delivered share is known
            if treatment >= 2 and ownership in OWNED_OA and src.source in lcoe.UTILITY:
                plant = own_plant(src.source, cs, cuf_sv)
                f = lcoe.crf(plant.discount_pct, plant.life_years)
                meta.crf, meta.life_years, meta.life_sv = f, int(plant.life_years.value), plant.life_years
                if ownership == "captive":
                    # the consumer's own plant: capital × capital recovery factor + O&M per kW a year,
                    # as the rooftop is costed, and no tariff per kWh; the open-access charges that a
                    # captive plant still pays stay per kWh delivered
                    yearly = lcoe.annual_cost_per_kw(plant).value
                    meta.capex_inr = plant.capex_inr_per_kw.value
                    meta.om_pct, meta.degradation = plant.om_pct_of_capex, plant.degradation_pct
                    meta.owned, meta.capital_inputs = "plant", [plant.capex_inr_per_kw]
                    meta.capital_words = (
                        "the plant's capital cost for the capacity sized (capital cost per kW × kW)"
                    )
                    own_inputs = [plant.capex_inr_per_kw, plant.om_pct_of_capex, f]
                    shown = lcoe.lcoe(plant)
                    meta.price = derive(
                        _q(shown.value, "0.0001"), Unit.INR_PER_KWH, [shown], label=lcoe.OWN_LCOE
                    )
                    free = derive(ZERO, Unit.INR_PER_KWH, own_inputs, label=NO_TARIFF)
                    cost_prices = {(mo, nm): free for mo in months for nm in slots_of[mo]}
                    prices = {(mo, nm): meta.price for mo in months for nm in slots_of[mo]}
                    if cs.price_inr_per_kwh is not None:
                        notes.append(
                            f"{meta.label}: the price you stated (₹{cs.price_inr_per_kwh}/kWh) is not used. "
                            "A captive plant is your own and pays no tariff: it is costed from its capital "
                            "cost and O&M. State those instead, or take the plant with 26% equity or from "
                            "a third party to pay a tariff"
                        )
                    notes.append(
                        f"{meta.label}: your own plant. Its capital cost (₹{plant.capex_inr_per_kw.value} "
                        f"per kW) and O&M ({plant.om_pct_of_capex.value}% of it a year) are in the plan's "
                        "cost as a yearly cost per kW (capital × capital recovery factor + O&M) and in "
                        "payback and IRR as capital at year 0; no tariff is paid for its energy, and the "
                        "open-access charges that apply to a captive plant are paid per kWh delivered. "
                        "The landed cost per kWh shown for it is its levelised cost with those charges"
                    )
                else:
                    # 26% equity: the consumer's outlay at year 0 is the equity share of the plant's
                    # capital for the capacity taken; the rest of the plant comes through the tariff.
                    # The outlay's yearly carrying cost (outlay × capital recovery factor) is what
                    # the per-kWh "equity cost" of the landed-cost card is, so no per-kWh equity line
                    # is charged here
                    share = _stated(cs.equity_share_pct, lcoe.default_equity_share(), "your equity share")
                    outlay = lcoe.equity_outlay_per_kw(plant, share)
                    yearly = outlay.value * f.value
                    meta.capex_inr = outlay.value
                    meta.om_pct = _sv(
                        ZERO,
                        Unit.PERCENT,
                        SourceRef(kind=RefKind.ASSUMPTION, note="the plant's O&M is inside the tariff"),
                        "O&M",
                    )
                    meta.owned, meta.capital_inputs = "equity", [plant.capex_inr_per_kw, share]
                    meta.capital_words = (
                        f"your equity outlay: {share.value}% of the plant's capital cost for the capacity "
                        "taken (equity share × capital cost per kW × kW; the plant's own debt and equity "
                        "split is not modelled)"
                    )
                    own_inputs = [plant.capex_inr_per_kw, share, f]
                    notes.append(
                        f"{meta.label}: captive with {share.value}% equity. Your outlay at year 0 is "
                        f"{share.value}% of the plant's capital cost for the capacity taken "
                        f"(₹{_q(outlay.value, '0.01')} per kW; the plant's own debt and equity split is not "
                        "modelled); it is in payback and IRR as capital, and in the year's cost as its "
                        "yearly carrying cost (outlay × capital recovery factor). The rest of the plant "
                        "is paid through the tariff per kWh. The equity is not charged again per kWh"
                    )
                fixed_kw[cid] = yearly
        energy: dict[tuple[int, str], Decimal] = {}
        own_capital = cid in fixed_kw
        # IEX: the landed cost is affine in the clearing price (losses scale it, charges add to it),
        # so each slot's slope and intercept price every block at its own clearing price
        affine: dict[tuple[int, str], tuple[Decimal, Decimal]] = {}
        delivered = None
        for mo in months:
            mdate = date(as_of.year if mo >= as_of.month else as_of.year + 1, mo, 15)
            ctx = escope.context(facts, mdate)
            for nm in slots_of[mo]:
                pr = prices.get((mo, nm))
                if pr is None:
                    continue

                base = landed_cost.Scenario(
                    price=pr,
                    ownership=ownership,
                    commissioned=cs.commissioned,
                    on=as_of,
                    capacity_mw=_sv(
                        Decimal(1),
                        Unit.MW,
                        SourceRef(
                            kind=RefKind.ASSUMPTION,
                            note="per MW, for capacity charges over scheduled energy",
                        ),
                        "Capacity",
                    ),
                    cuf_pct=cuf_sv,
                    duty_on_own_units=duty_own,
                )
                # banking is settled by the model and the judge, not as an upper bound (None)
                wf = landed_cost.evaluate(src, ctx, base, oa_rows, waivers, None)
                if duty_own is not None and duty_own in wf.lines:
                    meta.own_duty = duty_own  # no approved electricity_duty_oa row covers this source
                energy[(mo, nm)] = wf.total.value
                if own_capital and meta.owned == "plant":
                    # what the plant's energy costs per kWh at the meter beside its yearly cost: the
                    # same chain with no tariff (the charges, and the losses on them)
                    energy[(mo, nm)] = landed_cost.evaluate(
                        src,
                        ctx,
                        dataclasses.replace(base, price=cost_prices[(mo, nm)]),
                        oa_rows,
                        waivers,
                        None,
                    ).total.value
                if src.price_kind == "iex":
                    up = landed_cost.evaluate(
                        src,
                        ctx,
                        dataclasses.replace(
                            base, price=derive(pr.value + 1, Unit.INR_PER_KWH, [pr], label=pr.label)
                        ),
                        oa_rows,
                        waivers,
                        None,
                    )
                    slope = up.total.value - wf.total.value
                    affine[(mo, nm)] = (slope, wf.total.value - slope * pr.value)
                meta.totals.append(wf.total)
                meta.gaps += wf.gaps
                delivered = delivered or wf.delivered
                if mo == as_of.month:
                    meta.waterfalls[nm] = wf.lines
                    meta.chains[nm] = [st.to_json() for st in wf.steps]
        meta.delivered = delivered
        keep = delivered.value if delivered else Decimal(1)
        if oa_shape is None:
            oa_shape = _resample([v * keep * Decimal("0.25") for v in raw], step)
        if src.price_kind == "iex":
            iex_rates: list[Decimal] = []
            for t, b in enumerate(blocks):
                row = mcp_block.get(b.month.month, {})
                qs = [row[q + 1] for q in range(qday[t], qday[t] + step) if q + 1 in row]
                ab = affine.get((b.month.month, b.slot))
                if qs and ab:
                    iex_rates.append(ab[0] * (sum(qs, ZERO) / len(qs)) + ab[1])
                else:  # no clearing price for this block in the file: the slot's mean
                    iex_rates.append(energy.get((b.month.month, b.slot), ZERO))
            energy_d[cid] = iex_rates
            block_rates = [_f(x) for x in iex_rates]
        else:
            block_rates = per_block(cid, energy)
        problem_cands.append(
            om.Candidate(
                cid,
                shape=oa_shape,
                energy_inr_per_kwh=block_rates,
                fixed_inr_per_kw_year=_f(fixed_kw[cid]) if own_capital else 0.0,
                min_kw=min(_f(lo_kw), _f(mx)),
                max_kw=_f(mx),
                category="exchange" if src.price_kind == "iex" else CATEGORY[ownership],
                may_bank=False,  # set below once the governing rule is known
                renewable=src.price_kind != "iex",
                min_size_kw=min_oa_kw[0] if min_oa_kw and src.price_kind != "iex" else 0.0,
                dispatchable=src.price_kind == "iex",
                delivered_share=_f(keep),
            )
        )
        if min_oa_kw and src.price_kind != "iex":
            limit_words[f"min_size:{cid}"] = (
                f"Open access needs at least {Decimal(str(min_oa_kw[0])) / 1000} MW per source",
                [min_oa_kw[1]],
            )
        meta.inputs = [x for x in (meta.price, delivered, cuf_sv) if x is not None]
        if own_capital:
            meta.inputs += own_inputs
        meta.gaps = list(dict.fromkeys(meta.gaps))
        cands.append(meta)
    # batteries at the open-access plant: they store the output of the plan's own open-access plant
    # (not exchange purchases) and deliver it later as a source of their own
    charge_from = tuple(
        c.id for c in problem_cands if not c.btm and not c.dispatchable and c.category != "exchange"
    )
    for cid, meta, _cs, mx_kw, lo, hrs, rt, yearly in deferred_oa_bess:
        if not charge_from:
            notes.append(
                f"{meta.label}: left out, as the plan has no open-access solar, wind or hybrid plant "
                "for it to store"
            )
            cands.remove(meta)
            continue
        first = next(c for c in problem_cands if c.id == charge_from[0])
        problem_cands.append(
            om.Candidate(
                cid,
                shape=[hours * 1.0] * n,
                energy_inr_per_kwh=[0.0] * n,
                min_kw=min(lo, mx_kw),
                max_kw=mx_kw,
                category=first.category,
                may_bank=False,
                renewable=False,
                dispatchable=True,
                delivered_share=first.delivered_share,
            )
        )
        oa_batteries.append(
            om.OABattery(
                cid,
                charge_from,
                min_hours=hrs[0],
                max_hours=hrs[1],
                eff_charge=rt,
                eff_discharge=rt,
                inr_per_kwh_year=_f(yearly),
            )
        )
        meta.rule = next((x.rule for x in cands if x.cid == charge_from[0]), None)
        notes.append(
            f"{meta.label}: stores the output of the open-access plant and delivers it later; the energy "
            "it stores carries the plant's own landed cost (losses and charges as for the plant), and "
            "its round-trip loss is energy generated and paid for but not delivered"
        )
    # who may bank: the sources the governing rule's scope covers
    final: list[om.Candidate] = []
    for c in problem_cands:
        m = next(x for x in cands if x.cid == c.id)
        may = governing is not None and m.rule is not None and m.rule.id == governing.id
        final.append(om.Candidate(**{**c.__dict__, "may_bank": may}))
    for m in cands:
        if m.spec.max_mw:
            limit_words[f"max_kw:{m.cid}"] = (
                f"{m.label}: the most you allowed ({m.spec.max_mw} MW)",
                [
                    _sv(
                        m.spec.max_mw,
                        Unit.MW,
                        SourceRef(kind=RefKind.CLIENT_STATED, note=m.spec.stated_in),
                        "Maximum",
                    )
                ],
            )
        else:
            limit_words[f"max_kw:{m.cid}"] = (
                f"{m.label}: the default upper bound (assumption; set a maximum to change it)",
                [_sv(ZERO, Unit.MW, SourceRef(kind=RefKind.ASSUMPTION, note="default bound"), "Maximum")],
            )
    if governing is not None and "carry_cap" not in limit_words:
        limit_words["carry_cap"] = (
            "Banked energy carried to the next month is capped by the settlement rule",
            [_sv(ZERO, Unit.COUNT, governing.ref("/banking/cap"), "Carry-over ceiling")]
            if governing.get("/banking/cap") is not None
            else [],
        )
    if governing is not None and governing.get("/banking/limit") is not None:
        limit_words["banking_limit"] = (
            "The most that may be banked in a banking cycle is reached: more surplus would lapse",
            [_sv(ZERO, Unit.COUNT, governing.ref("/banking/limit"), "Banking limit")],
        )
    if cd_sv is not None and floor:
        sch = rules_for(month_firsts[0]).schedule
        assert sch is not None
        limit_words["cd_floor"] = (
            f"Billing demand is at least {floor}% of contract demand, "
            "so lowering peaks below that saves nothing",
            [
                cd_sv,
                _sv(
                    floor,
                    Unit.PERCENT,
                    sch.ref("/demand_charge/billing_demand_rule/min_pct_of_contract_demand"),
                    "Floor",
                ),
            ],
        )
    thermals = _thermals(s, site.id, hours, notes)
    if duty_own is not None and thermals:
        # captive thermal units are not bought from the DISCOM either: the same assumed duty
        thermals = [
            dataclasses.replace(u, fuel_inr_per_kwh=u.fuel_inr_per_kwh + _f(duty_own.value)) for u in thermals
        ]
    lapse = spec.lapse_price_inr_per_kwh or ZERO
    problem = om.Problem(
        blocks=blocks,
        hours=hours,
        load_kwh=load,
        grid=grid,
        candidates=final,
        batteries=batteries,
        oa_batteries=oa_batteries,
        thermals=thermals,
        rule=governing,
        limits=limits,
        lapse_inr_per_kwh=_f(lapse),
        exact=om.Exact(
            load_kwh=load_d,
            rate=rate_d,
            demand_rate=demand_d,
            excess_rate=excess_d,
            floor_pct=floor,
            cd_kva=cd_kva,
            pf=pf.value,
            energy=energy_d,
            fixed_kw=fixed_kw,
            fixed_kwh=fixed_kwh,
            fuel={u.id: Decimal(repr(u.fuel_inr_per_kwh)) for u in thermals},
            lapse=lapse,
            cd_change=cds.change_inr_per_kva_year if cds else ZERO,
            demand_slabs=slabs_d,
            excess_abs=excess_abs_d,
            min_kva=min_kva,
        ),
    )
    profile_ref = _sv(
        sum(kw, ZERO) * Decimal("0.25"),
        Unit.KWH,
        SourceRef(
            kind=RefKind.MEASURED if lp.provenance == "measured" else RefKind.DERIVED,
            note=f"load profile v{lp.version} ({lp.provenance}), year from {lp.year_start}"
            + (
                f", scaled by the stated expansion of {site_answers.plain(growth)}%"
                if growth is not None
                else ""
            ),
        ),
        "Load profile energy",
    )
    gf = (
        _sv(
            spec.grid_factor_t_per_mwh,
            Unit.TCO2_PER_MWH,
            SourceRef(kind=RefKind.CLIENT_STATED),
            "Grid emission factor",
        )
        if spec.grid_factor_t_per_mwh
        else _sv(
            Decimal(GRID_FACTOR[0]),
            Unit.TCO2_PER_MWH,
            SourceRef(kind=RefKind.ASSUMPTION, note=GRID_FACTOR[1]),
            "Grid emission factor",
        )
    )
    return Assembled(
        problem=problem,
        cands=cands,
        rate_cards=list(dict.fromkeys(card_rate.values())),
        demand_inputs=demand_inputs,
        profile_ref=profile_ref,
        profile=lp,
        vintages=vintages,
        limits=limit_words,
        rule=governing,
        notes=notes,
        as_of=as_of,
        cd=cd_sv,
        grid_factor=gf,
        spec=spec,
        bill_levies=from_bills,
        bill_factors=(factor, pf),
        levies=levy_uses,
        thermal_duty=duty_own if thermals else None,
        capital_treatment=treatment,
        generation_profiles=gen_used,
    )


def _resample(q: list[Decimal], step: int) -> list[float]:
    """kWh per kW per 15-minute block → per block of `step` quarter-hours."""
    if step == 1:
        return [_f(x) for x in q]
    return [_f(sum(q[i : i + step], ZERO)) for i in range(0, len(q), step)]


def _limit_words(kind: str, v: Decimal, r: RuleView) -> str:
    words = {
        "re_cap_pct_of_contract_demand": f"Renewable capacity capped at {v}% of contract demand",
        "re_cap_pct_of_sanctioned_load": f"Renewable capacity capped at {v}% of sanctioned load",
        "storage_mandate_pct_of_re": f"Storage of at least {v}% of renewable capacity",
        "storage_mandate_pct_of_contract_demand": f"Storage of at least {v}% of contract demand",
        "min_contract_demand_for_oa_kva": f"Open access needs a contract demand of at least {v} kVA",
        "max_rooftop_kw": f"Rooftop solar at most {v} kW",
        "min_bess_hours": f"Storage of at least {v} hours",
    }
    return f"{words.get(kind, kind)} ({r.label or 'regulatory limit'})"


def _thermals(s: Session, site_id: str, hours: float, notes: list[str]) -> list[om.Thermal]:
    out = []
    for a in s.scalars(select(Asset).where(Asset.site_id == site_id, Asset.kind == "captive_thermal")):
        p = a.params or {}
        if not p.get("capacity_kw") or not p.get("variable_cost_inr_per_kwh"):
            notes.append("the captive thermal plant needs its capacity and variable cost to be dispatched")
            continue
        out.append(
            om.Thermal(
                a.id,
                capacity_kw=float(p["capacity_kw"]),
                tech_min_pct=float(p.get("tech_min_pct", 0)),
                ramp_kw_per_hour=float(p["ramp_kw_per_hour"]) if p.get("ramp_kw_per_hour") else None,
                min_up_blocks=max(1, round(float(p.get("min_up_hours", 0)) / hours)),
                min_down_blocks=max(1, round(float(p.get("min_down_hours", 0)) / hours)),
                must_run=bool(p.get("must_run")),
                fuel_inr_per_kwh=float(p["variable_cost_inr_per_kwh"]),
                start_inr=float(p.get("start_cost_inr", 0)),
            )
        )
    return out


# ---------------------------------------------------------------- results


def _q(x: Decimal, places: str = "0.001") -> Decimal:
    return x.quantize(Decimal(places))


def summarise(a: Assembled, sol: Solution, rs: Resettled) -> dict[str, Any]:
    """The client-facing result: every number a SourcedValue carrying the refs of its inputs."""
    p = a.problem
    before_lines = status_quo(p)
    after_lines = rs.costs
    grid_inputs = [*a.rate_cards, *a.demand_inputs, a.profile_ref, *([a.cd] if a.cd else [])]
    all_inputs = list(grid_inputs)
    for m in a.cands:
        all_inputs += m.inputs + m.totals
    if a.rule is not None:
        all_inputs.append(_sv(ZERO, Unit.COUNT, a.rule.ref("/granularity"), "Settlement rule"))
    before = sum(before_lines.values(), ZERO)
    after = sum(after_lines.values(), ZERO)
    saving = before - after
    capex = ZERO
    owned_assets: list[tuple[CandMeta, Decimal]] = []  # each source with capital of the consumer's own
    for m in a.cands:
        if m.crf is None:
            continue
        size = (
            Decimal(str(sol.bess_kwh.get(m.cid, 0.0)))
            if m.spec.kind in STORAGE
            else Decimal(str(sol.cap_kw.get(m.cid, 0.0)))
        )
        capex += _q(size) * m.capex_inr
        if _q(size) > 0:
            owned_assets.append((m, _q(size) * m.capex_inr))
    legacy = a.capital_treatment < 2
    # Treatment 2: payback and IRR are on the capital of the consumer's own (rooftop, batteries, a
    # captive plant, the equity outlay) against the saving that capital brings: the plan's saving
    # less the saving of the sources bought by the unit with no capital (`_bought`).
    # Treatment 1 (a replay of an older run): the plan's as a whole, with the note below.
    ppa = any(m.crf is None and Decimal(str(sol.cap_kw.get(m.cid, 0.0))) > 0 for m in a.cands)
    by_unit = None if legacy else _bought(a, sol, rs, after_lines)
    if legacy and ppa and capex > 0:
        sized = {
            m.cid: sol.bess_kwh.get(m.cid, 0.0) if m.spec.kind in STORAGE else sol.cap_kw.get(m.cid, 0.0)
            for m in a.cands
        }
        owned = ", ".join(m.label for m in a.cands if m.crf is not None and sized[m.cid] > 0)
        a.notes.append(
            f"payback and IRR are the plan's as a whole: the capital of the owned plant ({owned}) "
            "against the whole plan's saving, part of which the open-access purchase brings without "
            "capital; read them as the plan's return, not the owned plant's alone"
        )

    def sv(v: Decimal, unit: Unit, label: str, inputs: list[SourcedValue]) -> dict[str, Any]:
        return derive(_q(v, "0.01") if unit == Unit.INR else _q(v), unit, inputs, label=label).model_dump(
            mode="json"
        )

    grid_total = sum(om.exact_of(p).load_kwh, ZERO)
    lifecycle_json = _lifecycle(a, sol, before_lines, after_lines, grid_total, all_inputs, by_unit)
    sizes = []
    for m in a.cands:
        if m.spec.kind in STORAGE:
            power = sol.bess_kw[m.cid] if m.spec.kind == "btm_bess" else sol.cap_kw[m.cid]
            sizes.append(
                {
                    "id": m.cid,
                    "kind": m.spec.kind,
                    "label": m.label,
                    "kw": sv(Decimal(str(power)), Unit.KW, "Battery power", [*m.inputs, *grid_inputs]),
                    "kwh": sv(
                        Decimal(str(sol.bess_kwh[m.cid])),
                        Unit.KWH,
                        "Battery energy",
                        [*m.inputs, *grid_inputs],
                    ),
                }
            )
            continue
        series = sol.gen[m.cid] if m.spec.kind == "rooftop_pv" else sol.delivered[m.cid]
        delivered = sum((Decimal(f"{x:.3f}") for x in series), ZERO)
        exchange = m.spec.kind.startswith("iex_")
        # an exchange purchase has no plant: its size is the most it schedules in any block
        cap = (
            max((Decimal(f"{x:.3f}") for x in series), default=ZERO) / Decimal(repr(p.hours))
            if exchange
            else Decimal(str(sol.cap_kw[m.cid]))
        )
        setoff = sum(rs.result.setoff_kwh.get(m.cid, []), ZERO) if m.spec.kind != "rooftop_pv" else delivered
        ins = [*m.inputs, *m.totals, *grid_inputs]
        row: dict[str, Any] = {
            "id": m.cid,
            "kind": m.spec.kind,
            "label": m.label,
            "kw": sv(cap, Unit.KW, "Capacity", ins),
            "delivered_kwh": sv(delivered, Unit.KWH, "Energy at the meter", ins),
            "setoff_kwh": sv(setoff, Unit.KWH, "Set off against consumption", ins),
            "energy_cost": sv(
                after_lines.get(f"energy:{m.cid}", ZERO) + after_lines.get(f"fixed:{m.cid}", ZERO),
                Unit.INR,
                "Cost for the year",
                ins,
            ),
            "waterfall": {k: [x.model_dump(mode="json") for x in v] for k, v in m.waterfalls.items()},
            "chain": m.chains,
            "delivered_share": m.delivered.model_dump(mode="json") if m.delivered else None,
            "ownership": m.ownership,
            "gaps": m.gaps,
        }
        if exchange:
            bought = after_lines.get(f"energy:{m.cid}", ZERO)
            blocks_on = sum(1 for x in series if x > 0)
            row["exchange"] = {
                "segment": m.spec.kind.removeprefix("iex_"),
                "blocks_bought": blocks_on,
                "hours_bought": str(_q(Decimal(blocks_on) * Decimal(repr(p.hours)), "0.01")),
                "avg_landed": sv(
                    bought / delivered if delivered > 0 else ZERO,
                    Unit.INR_PER_KWH,
                    "Average landed cost of exchange energy",
                    ins,
                ),
                "share_of_load": sv(
                    delivered / grid_total * 100 if grid_total > 0 else ZERO,
                    Unit.PERCENT,
                    "Share of the year's consumption",
                    ins,
                ),
            }
        if a.generation_profiles and m.cuf is not None:
            # a run that generated with a supplied profile shows each source's CUF and where it is
            # from (the file, measured or stated; the library curve, an assumption). Not on a run
            # with library curves only, whose summary stays as it was (replays compare summaries)
            row["cuf"] = m.cuf.model_dump(mode="json")
        if m.spec.kind != "rooftop_pv":
            for key, label, src in (
                ("banked_kwh", "Banked (after the charge in kind)", rs.result.deposit_kwh),
                ("unbanked_kwh", "Surplus not banked", rs.result.unbanked_kwh),
            ):
                row[key] = sv(sum(src.get(m.cid, []), ZERO), Unit.KWH, label, ins)
        sizes.append(row)
    lapsed = sum((x["kwh"] for x in rs.result.lapsed), ZERO)
    monthly = _monthly_mix(a, sol, rs, sv, grid_inputs)
    grid_before = sum((Decimal(f"{x:.3f}") for x in p.load_kwh), ZERO)
    grid_after = sum(rs.result.grid_kwh, ZERO)
    # energy bought on the exchange outside its green segments is grid-mix power: it carries the
    # grid's emission factor like the DISCOM's supply
    exchange_grey = sum(
        (
            sum((Decimal(f"{x:.3f}") for x in sol.delivered.get(m.cid, [])), ZERO)
            for m in a.cands
            if m.spec.kind.startswith("iex_") and m.spec.kind not in ("iex_gdam", "iex_gtam")
        ),
        ZERO,
    )
    gf = a.grid_factor
    levies_json = _levies(a, sol, grid_before, grid_after, grid_inputs, all_inputs)
    binding = []
    for name in sol.binding:
        words, refs = a.limits.get(name, (name, []))
        binding.append({"name": name, "words": words, "refs": [r.model_dump(mode="json") for r in refs]})
    owned_json: dict[str, Any] = {}
    if by_unit is not None:
        owned_json["owned_capital"] = _owned_capital(
            a, owned_assets, capex, saving, by_unit, lifecycle_json, all_inputs
        )
    return {
        "sizes": sizes,
        "annual": {
            "before": sv(before, Unit.INR, "Grid cost today, for the year", grid_inputs),
            "after": sv(after, Unit.INR, "Cost with the plan, for the year (re-settled)", all_inputs),
            "saving": sv(saving, Unit.INR, "Saving for the year", all_inputs),
            "capex": sv(
                capex,
                Unit.INR,
                "Capital cost (rooftop and batteries)"
                if legacy
                else "Capital of your own (plant, batteries and equity outlay)",
                all_inputs,
            ),
        },
        # the capital of the consumer's own asset by asset, the saving it is measured against and how
        # payback and IRR are defined, in words (absent on a replay of a run made before 25 Nov 2026)
        **owned_json,
        "lines_before": {k: str(v) for k, v in before_lines.items()},
        "lines_after": {k: str(v) for k, v in after_lines.items()},
        # FPPCA and duty at the bills' rates: amounts inside the costs above, and the treatment on
        # units not bought from the DISCOM in words (an assumption, not sourced to a regulation)
        "levies": levies_json,
        "lifecycle": lifecycle_json,
        # the headline payback and IRR are the lifecycle's (escalation, degradation and replacements
        # in): on the owned capital, None when the plan has none (`owned_capital` says why)
        "payback_years": lifecycle_json.get("simple_payback_years"),
        "irr_pct": lifecycle_json.get("irr_pct"),
        "emissions": {
            "factor": gf.model_dump(mode="json"),
            "exchange_grid_kwh": sv(
                exchange_grey, Unit.KWH, "Exchange energy outside the green segments", all_inputs
            ),
            "before_t": sv(
                grid_before * gf.value / 1000, Unit.TCO2, "Grid emissions today", [a.profile_ref, gf]
            ),
            "after_t": sv(
                (grid_after + exchange_grey) * gf.value / 1000,
                Unit.TCO2,
                "Grid emissions with the plan",
                [*all_inputs, gf],
            ),
        },
        "settlement": {
            "load_kwh": sv(grid_before, Unit.KWH, "Consumption for the year", [a.profile_ref]),
            "grid_kwh": sv(grid_after, Unit.KWH, "Drawn from the DISCOM", all_inputs),
            "lapsed_kwh": sv(lapsed, Unit.KWH, "Lapsed", all_inputs),
            "withdrawn_kwh": sv(
                sum(rs.result.withdrawn_kwh, ZERO), Unit.KWH, "Withdrawn from the bank", all_inputs
            ),
            "rule": a.rule.label if a.rule else None,
            "rule_id": a.rule.id if a.rule else None,
            "rule_ref": a.rule.ref().model_dump(mode="json") if a.rule else None,
        },
        "contract_demand_kva": sv(Decimal(str(sol.cd_kva)), Unit.KVA, "Contract demand", grid_inputs),
        "binding": binding,
        "provenance": {
            "load_profile": f"v{a.profile.version} ({a.profile.provenance})",
            "prices": a.vintages,
            "profile_ref": a.profile_ref.model_dump(mode="json"),
            # the supplied generation profiles the run generated with (invariant 8), when any
            **(
                {
                    "generation": [
                        f"{k.split(':')[0]} ({'rooftop' if k.endswith(':rooftop') else 'open access'}): "
                        f"{v['file']} ({v['provenance']}), sha256 {v['sha256']}"
                        for k, v in sorted(a.generation_profiles.items())
                    ]
                }
                if a.generation_profiles
                else {}
            ),
        },
        "sensitivities": [],
        # a plan the search could not prove the cheapest says so wherever its notes are shown
        "notes": [*a.notes, *filter(None, [unproven_note(sol.status, sol.gap)])],
        "monthly": monthly,
    }


def _levies(
    a: Assembled,
    sol: Solution,
    load_kwh: Decimal,
    grid_kwh: Decimal,
    grid_inputs: list[SourcedValue],
    all_inputs: list[SourcedValue],
) -> list[dict[str, Any]]:
    """Each levy taken from the bills: its rate, what it comes to today and with the plan on the
    units bought from the DISCOM, and on the other units (rooftop, open access, captive) under the
    assumption the result names. The amounts are inside `annual.before` and `annual.after`."""
    p = a.problem
    out = []
    for u in a.levies:
        other_kwh = ZERO
        if u.on_other_kwh is not None:
            for m in a.cands:
                if m.own_duty is None:
                    continue
                series = sol.gen[m.cid] if m.spec.kind == "rooftop_pv" else sol.delivered[m.cid]
                other_kwh += sum((Decimal(f"{x:.3f}") for x in series), ZERO)
                # what a battery at the plant stored was generated and charged like the rest
                other_kwh += sum((Decimal(f"{x:.3f}") for x in sol.oa_charged.get(m.cid, [])), ZERO)
            if a.thermal_duty is not None:
                other_kwh += sum(
                    (Decimal(f"{x:.3f}") for t in p.thermals for x in sol.thermal.get(t.id, [])), ZERO
                )
        ins_other = [*all_inputs, *([u.on_other_kwh] if u.on_other_kwh is not None else [u.levy.treatment])]

        def money(v: Decimal, label: str, inputs: list[SourcedValue]) -> dict[str, Any]:
            return derive(_q(v, "0.01"), Unit.INR, inputs, label=label).model_dump(mode="json")

        out.append(
            {
                "code": u.levy.code,
                "label": u.levy.label,
                "bills": u.levy.bills,
                "rates": [r.model_dump(mode="json") for r in u.levy.rates],
                "on_discom_kwh": u.on_discom_kwh.model_dump(mode="json"),
                "on_other_kwh": u.on_other_kwh.model_dump(mode="json") if u.on_other_kwh else None,
                "charged_on_other_units": u.on_other_kwh is not None,
                "treatment": u.levy.treatment.model_dump(mode="json"),
                "treatment_words": u.levy.words,
                "sourced_to_regulation": False,
                "discom_kwh_before": derive(
                    _q(load_kwh), Unit.KWH, grid_inputs, label="Bought from the DISCOM today"
                ).model_dump(mode="json"),
                "discom_kwh_after": derive(
                    _q(grid_kwh), Unit.KWH, all_inputs, label="Bought from the DISCOM with the plan"
                ).model_dump(mode="json"),
                "other_kwh": derive(
                    _q(other_kwh),
                    Unit.KWH,
                    all_inputs,
                    label="Units not bought from the DISCOM that the levy is charged on",
                ).model_dump(mode="json"),
                "before": money(
                    load_kwh * u.on_discom_kwh.value,
                    f"{u.levy.label} today, for the year",
                    [*grid_inputs, u.on_discom_kwh],
                ),
                "after_discom": money(
                    grid_kwh * u.on_discom_kwh.value,
                    f"{u.levy.label} with the plan, on units bought from the DISCOM",
                    [*all_inputs, u.on_discom_kwh],
                ),
                "after_other": money(
                    other_kwh * (u.on_other_kwh.value if u.on_other_kwh is not None else ZERO),
                    f"{u.levy.label} with the plan, on units not bought from the DISCOM (assumed)",
                    ins_other,
                ),
            }
        )
    return out


@dataclass
class Bought:
    """The sources of a plan bought by the unit with no capital of the consumer's own (a third-party
    PPA, the exchange), and what they save in the settled year: their units valued at the grid energy
    rate of the blocks they were set off in (banked units at the rate of the blocks they were drawn
    in, shared by what each source banked), less what is paid for them."""

    labels: list[str]
    avoided: Decimal  # INR, grid energy cost their units avoid
    cost: Decimal  # INR, what is paid for them

    @property
    def saving(self) -> Decimal:
        return self.avoided - self.cost


def _bought(a: Assembled, sol: Solution, rs: Resettled, after_lines: dict[str, Decimal]) -> Bought:
    p = a.problem
    x = om.exact_of(p)
    res = rs.result
    banked_all = sum((sum(v, ZERO) for v in res.deposit_kwh.values()), ZERO)
    drawn = sum((res.withdrawn_kwh[t] * x.rate[t] for t in range(p.n)), ZERO)
    out = Bought([], ZERO, ZERO)
    for m in a.cands:
        if m.crf is not None or Decimal(str(sol.cap_kw.get(m.cid, 0.0))) <= 0:
            continue
        setoff = res.setoff_kwh.get(m.cid, [])
        out.avoided += sum((setoff[t] * x.rate[t] for t in range(len(setoff)) if setoff[t]), ZERO)
        if banked_all > 0:
            out.avoided += drawn * sum(res.deposit_kwh.get(m.cid, []), ZERO) / banked_all
        out.cost += after_lines.get(f"energy:{m.cid}", ZERO)
        out.labels.append(m.label)
    out.avoided, out.cost = _q(out.avoided, "0.01"), _q(out.cost, "0.01")
    return out


CAPITAL_KIND = {
    "plant": "your own plant",
    "battery": "your own battery",
    "equity": "equity outlay",
}


def _owned_capital(
    a: Assembled,
    assets: list[tuple[CandMeta, Decimal]],
    capex: Decimal,
    saving: Decimal,
    bought: Bought,
    lc: dict[str, Any],
    all_inputs: list[SourcedValue],
) -> dict[str, Any]:
    """The capital of the consumer's own in the plan, asset by asset; the saving payback and IRR are
    measured against; and how they are defined, in the words the client reads beside them."""

    def money(v: Decimal, label: str, inputs: list[SourcedValue]) -> dict[str, Any]:
        return derive(_q(v, "0.01"), Unit.INR, inputs, label=label).model_dump(mode="json")

    owned = ", ".join(f"{m.label} ({CAPITAL_KIND[m.owned or 'plant']})" for m, _ in assets)
    years = lc["analysis_years"]["value"]
    payback_absent = irr_absent = None
    if not assets:
        by_unit = ", ".join(bought.labels) or "the grid"
        payback_absent = irr_absent = (
            "No payback or IRR: this plan has no capital of your own. Every source in it is bought by "
            f"the unit ({by_unit}), so its saving needs no investment to pay back."
        )
        basis = payback_absent
    else:
        if bought.labels:
            basis = (
                f"Payback and IRR are on the capital of your own ({owned}) against the saving that "
                "capital brings: the plan's saving less the saving of the sources bought by the unit "
                f"with no capital ({', '.join(bought.labels)}), whose units are valued at the grid energy "
                "rate of the hours they were set off in, less what is paid for them. The plan's yearly "
                "saving is shown separately."
            )
        else:
            basis = (
                f"Payback and IRR are on the capital of your own ({owned}) against the whole plan's "
                "saving: every source in the plan carries capital of your own."
            )
        basis += (
            " The capital is paid at year 0; the saving it is measured on is each year's grid cost "
            "avoided less what is still paid per kWh and for O&M, with tariffs and O&M escalating as "
            "the assumptions say. The plan's saving for the year, shown beside it, is after the yearly "
            "cost of the same capital (capital × capital recovery factor)."
        )
        if lc.get("simple_payback_years") is None:
            payback_absent = (
                f"No payback: the saving on the capital of your own does not recover it within the "
                f"{years}-year analysis period."
            )
        if lc.get("irr_pct") is None:
            irr_absent = (
                "No IRR: the cash flows on the capital of your own have no rate of return between "
                "−99% and 1,000% a year."
            )
    return {
        "total": money(capex, "Capital of your own, year 0", all_inputs),
        "assets": [
            {
                "id": m.cid,
                "label": m.label,
                "kind": m.owned or "plant",
                "capex": money(c, f"{m.label}: {CAPITAL_KIND[m.owned or 'plant']}", m.capital_inputs),
                "words": m.capital_words
                or (
                    "the battery's capital cost for the energy sized (capital cost per kWh × kWh)"
                    if m.owned == "battery"
                    else "the plant's capital cost for the capacity sized (capital cost per kW × kW)"
                ),
            }
            for m, c in assets
        ],
        # the first year's cash saving the capital is measured on (the lifecycle's year 1): the grid
        # cost avoided less what is still paid per kWh and for O&M, before the capital itself
        "saving": money(
            Decimal(lc["years"][0]["owned_saving"]["value"]) if lc["years"] else saving - bought.saving,
            "Saving in year 1 on the capital of your own (after O&M, before the capital itself)",
            all_inputs,
        ),
        "bought_saving": money(
            bought.saving, "Saving in year 1 of the sources bought by the unit", all_inputs
        )
        if bought.labels
        else None,
        "bought": bought.labels,
        "basis": basis,
        "payback_absent": payback_absent,
        "irr_absent": irr_absent,
    }


def _lifecycle(
    a: Assembled,
    sol: Solution,
    before_lines: dict[str, Decimal],
    after_lines: dict[str, Decimal],
    load_kwh: Decimal,
    inputs: list[SourcedValue],
    bought: Bought | None = None,
) -> dict[str, Any]:
    """The settled year projected over the analysis period (engine.lifecycle): owned plant paid at
    year 0 and replaced at the end of its life, degrading; tariffs and O&M escalating; every year
    discounted. The finance inputs are the scenario's, else the stated assumptions."""
    fin_spec = a.spec.finance if a.spec is not None else None
    d = lcoe.default_finance()

    def pick(name: str, unit: Unit, label: str) -> SourcedValue:
        v = getattr(fin_spec, name, None) if fin_spec is not None else None
        if v is None:
            return d[name]
        return _sv(Decimal(v), unit, SourceRef(kind=RefKind.CLIENT_STATED, note=label), label)

    fin = lifecycle.Inputs(
        analysis_years=pick("analysis_years", Unit.COUNT, "Analysis period"),
        discount_pct=pick("discount_pct", Unit.PERCENT, "Discount rate"),
        tariff_escalation_pct=pick("tariff_escalation_pct", Unit.PERCENT, "Tariff escalation"),
        ppa_escalation_pct=pick("ppa_escalation_pct", Unit.PERCENT, "PPA escalation"),
        om_escalation_pct=pick("om_escalation_pct", Unit.PERCENT, "O&M escalation"),
    )
    avg_grid = (before_lines.get("grid_energy", ZERO) / load_kwh) if load_kwh > 0 else ZERO
    if avg_grid <= 0:
        a.notes.append(
            "no grid energy line to price the output lost to degradation, so the lifecycle ignores it"
        )
    none = _sv(
        ZERO, Unit.PERCENT, SourceRef(kind=RefKind.ASSUMPTION, note="no degradation modelled"), "Degradation"
    )
    assets: list[lifecycle.Asset] = []
    for m in a.cands:
        if m.crf is None or m.om_pct is None or m.life_sv is None:
            continue
        if m.spec.kind in STORAGE:
            size = _q(Decimal(str(sol.bess_kwh.get(m.cid, 0.0))))
            displaced = ZERO
        else:
            size = _q(Decimal(str(sol.cap_kw.get(m.cid, 0.0))))
            # an open-access plant of the consumer's own: what it delivers at the meter
            series = sol.gen.get(m.cid) or sol.delivered.get(m.cid) or []
            gen = sum((Decimal(f"{x:.3f}") for x in series), ZERO)
            displaced = gen * avg_grid
        if size <= 0:
            continue
        what = "equity outlay" if m.owned == "equity" and bought is not None else "capital cost"
        capex = derive(size * m.capex_inr, Unit.INR, [*m.inputs], label=f"{m.label}: {what}")
        assets.append(
            lifecycle.Asset(
                cid=m.cid,
                label=m.label,
                capex=capex,
                om_pct=m.om_pct,
                life_years=m.life_sv,
                degradation_pct=m.degradation or none,
                displaced_inr=_q(displaced, "0.01"),
            )
        )
    if bought is None:  # a replay of a run made before payback and IRR were on the owned capital
        return lifecycle.to_json(lifecycle.project(before_lines, after_lines, assets, fin), inputs)
    lc = lifecycle.project(before_lines, after_lines, assets, fin, bought=(bought.avoided, bought.cost))
    return lifecycle.to_json(lc, inputs, owned=True)


def _monthly_mix(
    a: Assembled, sol: Solution, rs: Resettled, sv: Any, grid_inputs: list[SourcedValue]
) -> list[dict[str, Any]]:
    """Where each month's consumption came from, as the judge settled it: each source's energy set
    off (behind the meter: generated and used; a battery: discharged), withdrawn from the bank, and
    drawn from the DISCOM; with the energy banked and bought on the exchange."""
    p = a.problem
    x = om.exact_of(p)
    months = sorted({b.month for b in p.blocks})
    idx: dict[date, list[int]] = defaultdict(list)
    for t, b in enumerate(p.blocks):
        idx[b.month].append(t)
    res = rs.result
    out = []
    for mo in months:
        ts = idx[mo]
        row: dict[str, Any] = {
            "month": mo.isoformat(),
            "load_kwh": sv(sum((x.load_kwh[t] for t in ts), ZERO), Unit.KWH, "Consumption", [a.profile_ref]),
            "grid_kwh": sv(
                sum((res.grid_kwh[t] for t in ts), ZERO), Unit.KWH, "From the DISCOM", grid_inputs
            ),
            "withdrawn_kwh": sv(
                sum((res.withdrawn_kwh[t] for t in ts), ZERO), Unit.KWH, "From the bank", grid_inputs
            ),
            "sources": {},
        }
        for m in a.cands:
            ins = [*m.inputs, a.profile_ref]
            if m.spec.kind == "btm_bess":
                v = sum((Decimal(f"{sol.discharge[m.cid][t]:.3f}") for t in ts), ZERO)
                label = "Discharged"
            elif m.spec.kind == "rooftop_pv":
                v = sum((Decimal(f"{sol.gen[m.cid][t]:.3f}") for t in ts), ZERO)
                label = "Generated and used"
            else:
                v = sum((res.setoff_kwh.get(m.cid, [ZERO] * p.n)[t] for t in ts), ZERO)
                label = "Set off against consumption"
            row["sources"][m.cid] = sv(v, Unit.KWH, label, ins)
        out.append(row)
    return out


def sensitivities(spec: ScenarioSpec, summary: dict[str, Any], a: Assembled) -> list[dict[str, Any]]:
    before = Decimal(summary["annual"]["before"]["value"])
    lines = {k: Decimal(v) for k, v in summary["lines_after"].items()}
    out = []
    for snt in spec.sensitivities:
        for pct in snt.pct:
            if snt.kind == "grid_rate":
                b = before * (100 + pct) / 100
                after = reprice(lines, "grid_energy", pct)
                label = f"Grid energy rate {pct:+}%"
            else:
                cid = next((m.cid for m in a.cands if m.spec.kind == snt.candidate), None)
                if cid is None:
                    continue
                b = before
                m = next(m for m in a.cands if m.cid == cid)
                if a.capital_treatment >= 2 and m.owned == "plant":
                    # a plant of the consumer's own has no price: its capital and O&M move instead
                    after = reprice(lines, f"fixed:{cid}", pct)
                    label = f"{snt.candidate} capital cost and O&M {pct:+}% (sizes held)"
                else:
                    after = reprice(lines, f"energy:{cid}", pct)
                    label = f"{snt.candidate} price {pct:+}% (sizes held)"
            out.append(
                {
                    "label": label,
                    "saving": derive(
                        (b - after).quantize(INR),
                        Unit.INR,
                        [
                            _sv(
                                pct,
                                Unit.PERCENT,
                                SourceRef(kind=RefKind.CLIENT_STATED, note="sensitivity"),
                                label,
                            )
                        ],
                        label="Saving for the year",
                    ).model_dump(mode="json"),
                }
            )
    return out


def digest(summary: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(summary, sort_keys=True, default=str).encode()).hexdigest()


def dispatch_blob(a: Assembled, sol: Solution, rs: Resettled) -> bytes:
    p = a.problem
    series: dict[str, list[str]] = {
        "load": [f"{x:.3f}" for x in p.load_kwh],
        "metered": [format(b.load_kwh, "f") for b in rs.blocks],
        "grid": [format(x.quantize(Decimal("0.001")), "f") for x in rs.result.grid_kwh],
        "withdrawn": [format(x.quantize(Decimal("0.001")), "f") for x in rs.result.withdrawn_kwh],
    }
    for k, v in sol.delivered.items():
        series[f"supply:{k}"] = [f"{x:.3f}" for x in v]
    for k, v in sol.gen.items():
        series[f"supply:{k}"] = [f"{x:.3f}" for x in v]
    for k, v in sol.discharge.items():
        series[f"discharge:{k}"] = [f"{x:.3f}" for x in v]
    for k, v in sol.charge.items():
        series[f"charge:{k}"] = [f"{x:.3f}" for x in v]
    for k, v in sol.soc.items():
        series[f"soc:{k}"] = [f"{x:.3f}" for x in v]
    body = {
        "year_start": a.profile.year_start.isoformat(),
        "minutes": int(p.hours * 60),
        "slots": [b.slot for b in p.blocks],
        "series": series,
    }
    return zlib.compress(json.dumps(body).encode())


def read_dispatch(blob: bytes) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(zlib.decompress(blob))
    return out


# ---------------------------------------------------------------- execution


def create_run(s: Session, sc: Scenario, actor: str, replay_of: Run | None = None) -> Run:
    spec = ScenarioSpec.model_validate(replay_of.spec if replay_of else sc.spec)
    run = Run(
        scenario_id=sc.id,
        status="queued",
        resolution_min=spec.resolution_min,
        spec=spec.model_dump(mode="json"),
        pins=dict(replay_of.pins) if replay_of else {},
        solver={},
        created_by=actor,
        replay_of=replay_of.id if replay_of else None,
    )
    s.add(run)
    s.flush()
    return run


def execute(run_id: str) -> str:
    """Solve, re-settle and store a queued run. Returns the final status."""
    st = get_settings()
    with session_scope(actor=WORKER) as s:
        run = s.get(Run, run_id)
        if run is None:
            raise RunError("run not found")
        if run.status != "running":
            run.status = "running"
            run.started_at = datetime.now(UTC)
    try:
        with session_scope(actor=WORKER) as s:
            run = s.get(Run, run_id)
            assert run is not None
            sc = s.get(Scenario, run.scenario_id)
            assert sc is not None
            spec = ScenarioSpec.model_validate(run.spec)
            pins = run.pins or {}
            reader = Reader(pins.get("rulebook_versions") if run.replay_of else None)
            a = assemble(
                s,
                sc,
                spec,
                reader,
                profile_id=pins.get("load_profile_id") if run.replay_of else None,
                vintage_pins=pins.get("price_vintages_by_segment") if run.replay_of else None,
                # a run made before the bill pins existed replays with no levies, as it was solved
                bill_pins=(pins.get("bills") or {"levies": []}) if run.replay_of else None,
                # a run made before the captive plant carried its capital replays as it was solved
                treatment=capital_treatment(pins) if run.replay_of else CAPITAL_TREATMENT,
                # a replay generates with the profiles the original pinned (none: the library curves)
                generation_pins=(pins.get("generation_profiles") or {}) if run.replay_of else None,
            )
            used_versions = sorted(reader.used)
            vint_by_seg = {
                seg: vid
                for seg, vid in s.execute(
                    select(PriceVintage.segment, PriceVintage.id).where(PriceVintage.id.in_(a.vintages))
                ).all()
            }
        sol = solve(a.problem, st.ergplan_optimizer_time_limit_s, st.ergplan_optimizer_mip_gap)
        rs = resettle(a.problem, sol, st.ergplan_resettle_tolerance_kwh)
        summary = summarise(a, sol, rs)
        summary["sensitivities"] = sensitivities(spec, summary, a)
        status = "done" if rs.agrees else "failed_resettle"
        resettle_doc = {
            "agrees": rs.agrees,
            "max_diff_kwh": str(rs.max_diff_kwh),
            "tolerance_kwh": str(st.ergplan_resettle_tolerance_kwh),
            "by_month": {
                mo.isoformat(): {q: {k: str(v) for k, v in d.items()} for q, d in row.items()}
                for mo, row in rs.by_month.items()
            },
            "solver_objective_inr": str(Decimal(f"{sol.objective:.2f}")),
            "resettled_cost_inr": str(Decimal(summary["annual"]["after"]["value"]).quantize(INR)),
        }
        with session_scope(actor=WORKER) as s:
            run = s.get(Run, run_id)
            assert run is not None
            run.pins = {
                "rulebook_versions": used_versions,
                "load_profile_id": a.profile.id,
                "load_profile_version": a.profile.version,
                "price_vintages": a.vintages,
                "price_vintages_by_segment": vint_by_seg,
                "solver": sol.solver,
                "resolution_min": run.resolution_min,
                "as_of": a.as_of.isoformat(),
                # what the run read from the client's accepted bills (the levies' rates with their
                # bill citations and the treatment assumed, the kVAh factor and the power factor)
                "bills": {
                    "levies": [lv.to_json() for lv in a.bill_levies],
                    "factors": [x.model_dump(mode="json") for x in a.bill_factors]
                    if a.bill_factors
                    else None,
                },
                # how capital is treated (`Assembled.capital_treatment`); absent on a run made
                # before 25 Nov 2026 and on its replays
                **({"capital": {"treatment": a.capital_treatment}} if a.capital_treatment >= 2 else {}),
                # the supplied generation profiles the run generated with (id and the file's sha256);
                # absent when every source took its library curve
                **({"generation_profiles": a.generation_profiles} if a.generation_profiles else {}),
            }
            run.solver = {
                "status": sol.status,
                "gap": sol.gap,
                "seconds": round(sol.seconds, 2),
                "objective_inr": f"{sol.objective:.2f}",
                "blocks": a.problem.n,
            }
            run.status = status
            run.error = None if rs.agrees else f"re-settlement differs by {rs.max_diff_kwh} kWh"
            run.finished_at = datetime.now(UTC)
            s.merge(
                RunResult(
                    run_id=run_id,
                    summary=summary,
                    resettle=resettle_doc,
                    dispatch=dispatch_blob(a, sol, rs),
                    digest=digest(summary),
                )
            )
        return status
    except (RunError, SolveFailed, ValueError) as e:
        with session_scope(actor=WORKER) as s:
            run = s.get(Run, run_id)
            assert run is not None
            run.status = "failed"
            run.error = str(e)[:2000]
            run.finished_at = datetime.now(UTC)
        return "failed"


def claim_next() -> str | None:
    """Take the oldest queued run (safe with several workers)."""
    with session_scope(actor=WORKER) as s:
        row = s.execute(
            text(
                "update runs set status = 'running', started_at = now() where id = ("
                "select id from runs where status = 'queued' order by created_at "
                "for update skip locked limit 1) returning id"
            )
        ).first()
    return str(row[0]) if row else None
