"""The transition plan (Stage 15): from today's bill to a stated target, in phases the client can act
on — rooftop first, then open access, then storage, then (where the target asks for it) the further
step that reaches the target or the most the rules allow.

This module is pure and computes nothing the engines do not compute. Every phase is an ordinary run
(sized by the optimizer, settled again by the judge, pinned, so the plan replays); what is here is

- the ask (`Target`) and the order of the runs (`decide`): which run to make next, given the runs
  that have finished, and what each is allowed to size (`candidates`);
- the page (`build`): the phases in order, each with its capacities, capital or tariff, the year's
  cost and saving, the share of consumption no longer drawn from the DISCOM, the limits that hold it
  where it is, the state's rule in words with its source, and what must be signed or applied for.
  Differences between phases and shares are the only arithmetic, each a derived SourcedValue that
  carries the refs of the two figures it was taken from.

A step the client must take is cited to a rule only where an approved rule row states it; anything
else is general practice and is marked as an assumption, never as a rule (rule 13).

`exchange_phase` is the place where Stage 18 adds purchases on the exchange as a phase of their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.sourced import RefKind, SourcedValue, SourceRef, derive
from ..core.units import Unit
from ..engine.gaps import GAPS
from ..optimizer.scenarios import CandidateSpec, FinanceSpec, ScenarioSpec, unproven_note
from ..rulebook.models import RuleView
from .statebook import settlement_points

ZERO = Decimal(0)
PCT = Decimal("0.01")
ROUTES = ("captive", "group_captive", "third_party")
ROUTE_WORDS = {
    "captive": "Captive: your own plant",
    "group_captive": "Group captive: 26% of the plant's equity, the rest through a tariff",
    "third_party": "Third party: a power purchase agreement with a generator",
}
ROOFTOP = ("rooftop_pv",)
OPEN_ACCESS = ("oa_solar_intra", "oa_wind_intra")
STORAGE_SITE, STORAGE_PLANT = "btm_bess", "oa_bess"
PLAN_KINDS = (*ROOFTOP, *OPEN_ACCESS, STORAGE_SITE, STORAGE_PLANT)
BATTERIES = (STORAGE_SITE, STORAGE_PLANT)
# a share is "reached" within this many percentage points (the figures are quantised to 0.01)
REACHED = Decimal("0.05")
# curve points are not drawn closer together than this many percentage points
APART = Decimal("0.5")
SHARE_LABEL = "Share of the year's consumption not drawn from the DISCOM"


class Target(BaseModel):
    """What the client asks the plan to reach, and what they state about the sources."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["lowest_cost", "share"] = "lowest_cost"
    # kind share: the share of the year's consumption that should not come from the DISCOM
    share_pct: Decimal | None = Field(default=None, gt=0, le=100)
    # how the open-access plant is taken; None: each route is sized and the cheapest one whose
    # settlement the state's approved rules cover is taken
    ownership: Literal["captive", "group_captive", "third_party"] | None = None
    resolution_min: Literal[15, 60] = 60
    as_of: date | None = None
    finance: FinanceSpec | None = None
    # what the client states about a source of the plan (its price, the most they would take, its
    # capital cost …): one entry per kind at most; anything left out is ergPLAN's assumption
    sources: list[CandidateSpec] = Field(default_factory=list, max_length=len(PLAN_KINDS))
    # further points of the cost curve between the cheapest plan and the most the rules allow
    curve_points: int = Field(default=2, ge=0, le=6)

    @model_validator(mode="after")
    def _consistent(self) -> Target:
        if self.kind == "share" and self.share_pct is None:
            raise ValueError("a share target needs share_pct")
        if self.kind == "lowest_cost" and self.share_pct is not None:
            raise ValueError("share_pct is for a share target; leave it out for the lowest cost")
        kinds = [c.kind for c in self.sources]
        if len(set(kinds)) != len(kinds):
            raise ValueError("each source once")
        other = [k for k in kinds if k not in PLAN_KINDS]
        if other:
            raise ValueError(
                "the transition plan sizes rooftop solar, open-access solar and wind and batteries; "
                f"not {other[0]}"
            )
        return self


@dataclass(frozen=True)
class Step:
    """One run of the plan as the decision and the page see it."""

    key: str
    role: str  # rooftop | open_access | storage | most | target | curve
    route: str | None = None
    level_pct: Decimal | None = None
    status: str = "queued"  # the run's: queued | running | done | failed | failed_resettle
    summary: dict[str, Any] | None = None
    solver: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    run_id: str | None = None
    scenario_id: str | None = None
    resolution_min: int = 60

    @property
    def pending(self) -> bool:
        return self.status in ("queued", "running")

    @property
    def done(self) -> bool:
        # a run the judge did not agree with is never shown (invariant 6)
        return self.status == "done" and self.summary is not None


@dataclass(frozen=True)
class NewStep:
    key: str
    role: str
    spec: ScenarioSpec
    route: str | None = None
    level_pct: Decimal | None = None


@dataclass(frozen=True)
class Book:
    """What the state's approved rule book says that the page restates (read by the service)."""

    state: str
    discom: str
    # route → the approved settlement rule that covers open-access solar or wind taken that way
    settlement: dict[str, RuleView | None] = field(default_factory=dict)
    # approved regulatory limits that apply to the connection: words and the figure with its source
    limits: tuple[tuple[str, SourcedValue], ...] = ()
    # approved rooftop (net-metering) rows that apply to the connection
    net_metering: tuple[RuleView, ...] = ()


# ---------------------------------------------------------------------------- reading a run


def sv(x: Any) -> SourcedValue:
    return x if isinstance(x, SourcedValue) else SourcedValue.model_validate(x)


def _j(x: SourcedValue | None) -> dict[str, Any] | None:
    return x.model_dump(mode="json") if x is not None else None


def share_of(summary: dict[str, Any]) -> SourcedValue:
    """The share of the year's consumption not drawn from the DISCOM, from the settled year."""
    load, grid = sv(summary["settlement"]["load_kwh"]), sv(summary["settlement"]["grid_kwh"])
    pct = (load.value - grid.value) / load.value * 100 if load.value > 0 else ZERO
    return derive(pct.quantize(PCT), Unit.PERCENT, [load, grid], label=SHARE_LABEL)


def sizes_kw(summary: dict[str, Any] | None) -> dict[str, Decimal]:
    """Source kind → kW sized (a battery: its power)."""
    return {z["kind"]: Decimal(z["kw"]["value"]) for z in (summary or {}).get("sizes", [])}


def floor_mw(kw: Decimal) -> Decimal:
    """A capacity already built, as the least a later phase must keep: in MW, rounded down a watt
    below the figure shown so that a later run is never asked for more than the earlier one sized."""
    return max(ZERO, ((kw - Decimal("0.001")) / 1000).quantize(Decimal("0.000001"), rounding=ROUND_FLOOR))


def after(summary: dict[str, Any]) -> Decimal:
    return Decimal(summary["annual"]["after"]["value"])


# ---------------------------------------------------------------------------- the order of the runs


def candidates(
    upto: str, route: str | None, built: dict[str, Decimal], stated: list[CandidateSpec]
) -> list[CandidateSpec]:
    """The sources a phase may size: its own and every earlier phase's. What an earlier phase built
    stays (`built`, kW by kind, becomes each source's minimum); a later phase may add to it."""
    kinds: list[str] = list(ROOFTOP)
    if upto in ("open_access", "storage") and route is not None:
        kinds += OPEN_ACCESS
    if upto == "storage":
        kinds.append(STORAGE_SITE)
        if route is not None:
            kinds.append(STORAGE_PLANT)  # it stores the open-access plant's output
    by_kind = {c.kind: c for c in stated}
    out = []
    for k in kinds:
        base: dict[str, Any] = by_kind[k].model_dump() if k in by_kind else {"kind": k}
        if k in OPEN_ACCESS:
            base["ownership"] = route
        base["min_mw"] = max(Decimal(str(base.get("min_mw") or 0)), floor_mw(built.get(k, ZERO)))
        out.append(CandidateSpec.model_validate(base))
    return out


def _spec(target: Target, cands: list[CandidateSpec], **extra: Any) -> ScenarioSpec:
    return ScenarioSpec(
        candidates=cands,
        as_of=target.as_of,
        resolution_min=target.resolution_min,
        finance=target.finance,
        **extra,
    )


def chosen_route(steps: list[Step], book_routes: dict[str, RuleView | None]) -> Step | None:
    """The open-access run the plan goes on with: the cheapest for the year among the routes whose
    settlement an approved rule covers; where no route's is, the cheapest of all (the page says the
    settlement rule is not yet sourced). Regulatory traceability wins over a lower figure (rule 13)."""
    done = [s for s in steps if s.role == "open_access" and s.done]
    covered = [s for s in done if book_routes.get(s.route or "") is not None]
    pool = covered or done
    return min(pool, key=lambda s: (after(s.summary or {}), s.route or "")) if pool else None


def ladder(steps: list[Step], book_routes: dict[str, RuleView | None]) -> list[Step]:
    """The least-cost phases that finished, in order: rooftop, open access (the chosen route), storage."""
    by_role = {s.role: s for s in steps if s.role in ("rooftop", "storage")}
    seq = [by_role.get("rooftop"), chosen_route(steps, book_routes), by_role.get("storage")]
    return [s for s in seq if s is not None and s.done]


def curve_levels(low: Decimal, high: Decimal, points: int, skip: Decimal | None) -> list[Decimal]:
    """Shares strictly between the cheapest plan's and the most the rules allow, evenly spaced, to
    one decimal; none closer than `APART` to an end or to the target's own level."""
    if points <= 0 or high - low <= 2 * APART:
        return []
    out: list[Decimal] = []
    for i in range(1, points + 1):
        level = (low + (high - low) * i / (points + 1)).quantize(Decimal("0.1"))
        near = [low, high, *out, *([skip] if skip is not None else [])]
        if all(abs(level - x) >= APART for x in near):
            out.append(level)
    return out


def decide(
    target: Target, routes: list[str], steps: list[Step], book_routes: dict[str, RuleView | None]
) -> list[NewStep] | None:
    """The runs to make next. An empty list: runs are still being solved, wait. None: the plan has
    every run it will have. Deterministic in its arguments, so a plan resumed anywhere goes the same
    way."""
    if any(s.pending for s in steps):
        return []
    roles = {s.role for s in steps}
    stated = target.sources
    if "rooftop" not in roles:
        return [NewStep("rooftop", "rooftop", _spec(target, candidates("rooftop", None, {}, stated)))]
    roof = next(s for s in steps if s.role == "rooftop")
    if "open_access" not in roles:
        built = sizes_kw(roof.summary) if roof.done else {}
        return [
            NewStep(
                f"open_access:{r}",
                "open_access",
                _spec(target, candidates("open_access", r, built, stated)),
                route=r,
            )
            for r in routes
        ]
    oa = chosen_route(steps, book_routes)
    route = oa.route if oa is not None else None
    if "storage" not in roles:
        base = oa or (roof if roof.done else None)
        built = sizes_kw(base.summary) if base is not None else {}
        return [NewStep("storage", "storage", _spec(target, candidates("storage", route, built, stated)))]
    rungs = ladder(steps, book_routes)
    if not rungs:
        return None  # nothing could be sized: the page says why
    top = rungs[-1]
    built = sizes_kw(top.summary)
    low = share_of(top.summary or {}).value
    asked = target.share_pct if target.kind == "share" else None
    # the most the rules allow is worked out when the cost curve is asked for, or when the cheapest
    # plan stops short of the target (to say how far the rules let the plan go, and what holds it)
    wanted = target.curve_points > 0 or (asked is not None and low < asked - REACHED)
    if "most" not in roles and not wanted:
        return None
    if "most" not in roles:
        return [
            NewStep(
                "most",
                "most",
                _spec(target, candidates("storage", route, built, stated), most_renewable=True),
            )
        ]
    if roles & {"target", "curve"}:
        return None
    most = next(s for s in steps if s.role == "most")
    high = share_of(most.summary or {}).value if most.done else None
    # a search for the most that ended on its time limit may have stopped short of the true most:
    # a target above what it reached is then still tried
    sure = most.done and _proven(most)[0]
    new: list[NewStep] = []
    level: Decimal | None = None
    if asked is not None and low < asked - REACHED and (high is None or not sure or asked < high - REACHED):
        level = asked
        new.append(
            NewStep(
                "target",
                "target",
                _spec(target, candidates("storage", route, built, stated), min_share_pct=asked),
                level_pct=asked,
            )
        )
    if high is not None:
        for lv in curve_levels(low, high, target.curve_points, level):
            new.append(
                NewStep(
                    f"curve:{lv}",
                    "curve",
                    _spec(target, candidates("storage", route, built, stated), min_share_pct=lv),
                    level_pct=lv,
                )
            )
    return new or None


def exchange_phase(*_: Any) -> NoReturn:
    """Purchases on the power exchange as a phase of the plan (after open access, where the exchange
    can really be scheduled for the connection)."""
    raise NotImplementedError("Stage 18: the exchange phase of the transition plan")


# ---------------------------------------------------------------------------- what must be done


def _practice(state: str) -> SourceRef:
    return SourceRef(
        kind=RefKind.ASSUMPTION,
        note=f"general practice: no approved rule in the {state} rule book states this step",
    )


def _act(what: str, whom: str, state: str, rule: SourceRef | None = None) -> dict[str, Any]:
    """A step to take. `rule`: the approved rule row that states it; without one the step is general
    practice and says so (never shown as a rule)."""
    return {
        "what": what,
        "with_whom": whom,
        "basis": "rule" if rule is not None else "assumption",
        "ref": (rule or _practice(state)).to_json(),
    }


def actions(key: str, route: str | None, added: set[str], book: Book) -> list[dict[str, Any]]:
    """What must be signed or applied for to take a phase, for the sources the phase adds."""
    d, st = book.discom.upper(), book.state
    out: list[dict[str, Any]] = []
    if "rooftop_pv" in added:
        out += [
            _act(
                "Apply for connectivity of the rooftop plant (the rooftop or net-metering application, with "
                "the single-line diagram and the plant's size)",
                f"the DISCOM ({d})",
                st,
            ),
            _act(
                "Sign the supply and installation contract for the plant if you pay for it yourself, or a "
                "rooftop power purchase agreement if a developer owns it",
                "the installer or developer",
                st,
            ),
            _act(
                "Get the electrical safety approval and the change of meter before the plant is switched on",
                f"the state's electrical inspector and the DISCOM ({d})",
                st,
            ),
        ]
    if added & set(OPEN_ACCESS):
        if route == "captive":
            out.append(
                _act(
                    "Build or buy the plant as your own. To be treated as captive you must hold at least 26% "
                    "of its equity and use at least 51% of what it generates each year (the captive tests of "
                    "the Electricity Rules, 2005, which are not in this state's approved rule book here)",
                    "the developer or contractor, and your auditor for the yearly captive certificate",
                    st,
                )
            )
        elif route == "group_captive":
            out.append(
                _act(
                    "Subscribe to at least 26% of the plant's equity with the other captive users and sign "
                    "the share subscription and shareholders' agreements; together the captive users must "
                    "use at least 51% of what it generates each year (the captive tests of the Electricity "
                    "Rules, 2005, which are not in this state's approved rule book here)",
                    "the generator's project company",
                    st,
                )
            )
        out.append(
            _act(
                "Sign the power purchase agreement for the energy"
                if route != "captive"
                else "Sign the operation and maintenance contract for the plant",
                "the generator" if route != "captive" else "the plant's operator",
                st,
            )
        )
        out += [
            _act(
                "Apply for open access for the capacity of this phase and get the no-objection and the "
                "technical feasibility for your connection",
                f"the state load despatch centre or transmission utility (the nodal agency) and {d}",
                st,
            ),
            _act(
                "Apply for grid connectivity of the plant at its substation (the generator's task; ask for "
                "the approval)",
                "the state transmission utility or the DISCOM where the plant connects",
                st,
            ),
            _act(
                "Sign the wheeling agreement, and the banking agreement where the rule allows banking",
                f"the DISCOM ({d})",
                st,
            ),
            _act(
                "Fit 15-minute interval (ABT) meters at your connection if it does not have them",
                f"the DISCOM ({d})",
                st,
            ),
        ]
        rule = book.settlement.get(route or "")
        if rule is not None and rule.get("/banking/requires_day_ahead_schedule") is True:
            out.append(
                _act(
                    "Give a schedule of the plant's injection a day ahead: the settlement rule requires it "
                    "for the energy to be set off and banked",
                    "the state load despatch centre, through the generator",
                    st,
                    rule.ref("/banking/requires_day_ahead_schedule"),
                )
            )
    if STORAGE_SITE in added:
        out += [
            _act("Order the battery and its installation at your site", "the battery supplier", st),
            _act(
                "Tell the DISCOM of the change to your installation and get the electrical safety approval "
                "for the battery",
                f"the DISCOM ({d}) and the state's electrical inspector",
                st,
            ),
        ]
    if STORAGE_PLANT in added:
        out.append(
            _act(
                "Add the battery at the plant to the plant's contracts and to its open-access schedule (it "
                "delivers the plant's own energy later in the day)",
                "the generator and the nodal agency",
                st,
            )
        )
    return out


def rule_lines(key: str, route: str | None, sized: set[str], book: Book) -> list[dict[str, Any]]:
    """The state's rules that shape a phase, in words, each with the rule row it restates."""
    out: list[dict[str, Any]] = []
    if key == "rooftop":
        for r in book.net_metering:
            regime = str(r.body.get("regime", "")).replace("_", " ")
            cap = r.body.get("max_pct_of_sanctioned_load")
            text = f"Rooftop solar here is under {regime}" + (
                f", up to {cap}% of the sanctioned load" if cap not in (None, "") else ""
            )
            out.append({"text": text + f" ({r.label or 'rooftop rule'}).", "ref": r.ref("/regime").to_json()})
        out.append(
            {
                "text": "The rooftop plant is sized for use at your site in the hours it generates: nothing "
                "is counted for energy sent out to the grid.",
                "ref": SourceRef(
                    kind=RefKind.ASSUMPTION, note="ergPLAN's treatment of rooftop solar: no export"
                ).to_json(),
            }
        )
        return out
    if sized & set(OPEN_ACCESS):
        out += settlement_points(book.settlement.get(route or ""))
    for words, value in book.limits:
        out.append({"text": words + ".", "ref": value.source_refs[0].to_json(), "value": _j(value)})
    return out


# ---------------------------------------------------------------------------- the page

PHASE_TITLE = {
    "rooftop": "Rooftop solar at your site",
    "open_access": "Open-access solar and wind",
    "storage": "Storage",
    "target": "The further step to your target",
    "most": "As far as the rules allow",
}
PHASE_WHAT = {
    "rooftop": "Solar on your own roof or land, used at the site as it generates. It needs no open access, "
    "so it is the quickest step to take.",
    "open_access": "Solar and wind from a plant elsewhere in the state, brought to your meter through the "
    "grid under open access and settled under the state's banking rule.",
    "storage": "Batteries, at your site or at the plant, that move renewable energy into the hours the "
    "plants do not cover and trim the demand you are billed for.",
    "target": "More of the same sources than the cheapest plan takes, sized so that the share you asked "
    "for is reached at the least cost.",
    "most": "The same sources sized for the most the rules in force allow, whatever it costs: the far "
    "end of the cost curve.",
}


def _diff(
    cur: SourcedValue, prev: SourcedValue | None, label: str, places: str | None = None
) -> SourcedValue:
    v = cur.value - (prev.value if prev is not None else ZERO)
    if places is not None:
        v = v.quantize(Decimal(places))
    return derive(v, cur.unit, [cur, *([prev] if prev is not None else [])], label=label)


def _total(values: list[SourcedValue], unit: Unit, label: str, anchor: SourcedValue) -> SourcedValue:
    """A sum of a run's figures (the anchor gives a zero sum its source: the run's own settlement)."""
    return derive(sum((v.value for v in values), ZERO), unit, values or [anchor], label=label)


def _proven(step: Step) -> tuple[bool, str | None]:
    status = str(step.solver.get("status") or "optimal")
    gap = step.solver.get("gap")
    words = unproven_note(status, float(gap) if isinstance(gap, int | float) else None)
    return words is None, words


def _sources(summary: dict[str, Any], prev: dict[str, Any] | None) -> list[dict[str, Any]]:
    assets = {a["id"]: a for a in (summary.get("owned_capital") or {}).get("assets", [])}
    before = {z["kind"]: z for z in (prev or {}).get("sizes", [])}
    out = []
    for z in summary["sizes"]:
        was = before.get(z["kind"])
        kw = sv(z["kw"])
        kwh = sv(z["kwh"]) if z.get("kwh") else None
        asset = assets.get(z["id"])
        tariff = next(
            (
                sv(lines[0])
                for lines in list((z.get("waterfall") or {}).values())[:1]
                if lines and lines[0].get("label") == "PPA tariff"
            ),
            None,
        )
        row: dict[str, Any] = {
            "id": z["id"],
            "kind": z["kind"],
            "label": z["label"],
            "ownership": z.get("ownership") if z["kind"] in OPEN_ACCESS else None,
            "capacity": _j(kw),
            "added": _j(_diff(kw, sv(was["kw"]) if was else None, "Added in this phase")),
            "energy": _j(kwh),
            "added_energy": _j(
                _diff(kwh, sv(was["kwh"]) if was and was.get("kwh") else None, "Added in this phase")
            )
            if kwh is not None
            else None,
            "delivered_kwh": z.get("delivered_kwh"),
            "yearly_cost": z.get("energy_cost"),
            # capital of the client's own in this source (plant, battery or equity outlay), and the
            # tariff per kWh where its energy is bought: one, the other or both
            "capital": asset["capex"] if asset else None,
            "capital_words": asset["words"] if asset else None,
            "tariff": _j(tariff),
            "gaps": [GAPS.get(g, g) for g in z.get("gaps", [])],
        }
        out.append(row)
    return out


def _energy(summary: dict[str, Any]) -> dict[str, Any]:
    """Where the year's renewable energy goes under the state's settlement: the run's own figures,
    summed over its sources."""
    st = summary["settlement"]
    grid = sv(st["grid_kwh"])
    plants = [z for z in summary["sizes"] if z["kind"] not in BATTERIES]

    def tot(key: str, label: str) -> dict[str, Any] | None:
        return _j(_total([sv(z[key]) for z in plants if z.get(key)], Unit.KWH, label, grid))

    return {
        "load_kwh": st["load_kwh"],
        "grid_kwh": st["grid_kwh"],
        "delivered_kwh": tot("delivered_kwh", "Renewable energy at your meter"),
        "setoff_kwh": tot("setoff_kwh", "Set off against consumption as it arrives"),
        "banked_kwh": tot("banked_kwh", "Banked (after the charge in kind)"),
        "unbanked_kwh": tot("unbanked_kwh", "Surplus that could not be banked"),
        "withdrawn_kwh": st["withdrawn_kwh"],
        "lapsed_kwh": st["lapsed_kwh"],
    }


def _phase(
    order: int, key: str, step: Step, prev: Step | None, route: str | None, book: Book
) -> dict[str, Any]:
    s = step.summary or {}
    p = prev.summary if prev is not None else None
    annual = s["annual"]
    cost, saving, capital = sv(annual["after"]), sv(annual["saving"]), sv(annual["capex"])
    share = share_of(s)
    sources = _sources(s, p)
    added = {
        r["kind"]
        for r in sources
        if Decimal(r["added"]["value"]) >= Decimal("0.5")
        or (r["added_energy"] is not None and Decimal(r["added_energy"]["value"]) >= Decimal("0.5"))
    }
    sized = {r["kind"] for r in sources if Decimal(r["capacity"]["value"]) > 0}
    proven, proven_words = _proven(step)
    taken = bool(added)
    return {
        "key": key,
        "order": order,
        "title": PHASE_TITLE[key],
        "what": PHASE_WHAT[key],
        # taken: the phase adds capacity. adds_nothing: at today's costs the cheapest plan takes none
        # of this phase's sources, so its figures are those of the phase before
        "status": "taken" if taken else "adds_nothing",
        "status_words": None
        if taken
        else (
            "At the costs and rules of today the cheapest plan takes none of this phase's sources: it "
            "adds nothing to the phase before. The figures below are that phase's."
        ),
        "route": route if key != "rooftop" else None,
        "route_words": ROUTE_WORDS.get(route or "") if key != "rooftop" else None,
        "run_id": step.run_id,
        "scenario_id": step.scenario_id,
        "resolution_min": step.resolution_min,
        # a run that ended on its time limit is never shown as the optimum: it says so, with its gap
        "proven": proven,
        "proven_words": proven_words,
        "sources": sources,
        "annual_cost": _j(cost),
        "annual_saving": _j(saving),
        "added_saving": _j(
            _diff(saving, sv(p["annual"]["saving"]) if p else None, "Saving this phase adds, a year", "0.01")
        ),
        "capital": _j(capital),
        "added_capital": _j(
            _diff(capital, sv(p["annual"]["capex"]) if p else None, "Capital this phase adds", "0.01")
        ),
        "share": _j(share),
        "added_share": _j(_diff(share, share_of(p) if p else None, "Share this phase adds")),
        "payback_years": s.get("payback_years"),
        "irr_pct": s.get("irr_pct"),
        "energy": _energy(s),
        "binding": [
            {"name": b["name"], "words": b["words"], "refs": b["refs"]} for b in s.get("binding", [])
        ],
        "rules": rule_lines(key, route, sized, book),
        "actions": actions(key, route, added, book),
        "notes": list(s.get("notes", [])),
    }


def _point(role: str, label: str, step: Step) -> dict[str, Any]:
    s = step.summary or {}
    proven, words = _proven(step)
    return {
        "role": role,
        "label": label,
        "share": _j(share_of(s)),
        "annual_cost": s["annual"]["after"],
        "annual_saving": s["annual"]["saving"],
        "capital": s["annual"]["capex"],
        "sizes": [
            {"label": z["label"], "kind": z["kind"], "capacity": z["kw"], "energy": z.get("kwh")}
            for z in s["sizes"]
            if Decimal(z["kw"]["value"]) > 0
        ],
        "run_id": step.run_id,
        "proven": proven,
        "proven_words": words,
        "cheapest": False,
    }


def _pct(v: Decimal) -> str:
    return f"{v.normalize():f}%" if v != v.to_integral() else f"{v.to_integral()}%"


def _why_failed(step: Step) -> str:
    err = (step.error or "").lower()
    if step.status == "failed_resettle":
        return "its settlement did not agree with the judge's, so it is not shown"
    if "infeasible" in err:
        return "no size of these sources satisfies the rules and limits in force together"
    if "time limit" in err:
        return "the search ran out of time before it found a plan"
    return "it could not be worked out"


def build(target: Target, steps: list[Step], book: Book, plan_status: str) -> dict[str, Any]:
    """The transition plan's page from its runs. Works at any point: phases whose runs have not
    finished are simply not there yet (`status` says the plan is still running)."""
    routes = book.settlement
    rungs = ladder(steps, routes)
    oa = chosen_route(steps, routes)
    route = oa.route if oa is not None else None
    most = next((s for s in steps if s.role == "most"), None)
    reach = next((s for s in steps if s.role == "target"), None)
    asked = target.share_pct if target.kind == "share" else None

    shown: list[tuple[str, Step]] = [(s.role, s) for s in rungs]
    low = share_of(rungs[-1].summary or {}) if rungs else None
    high = share_of(most.summary or {}) if most is not None and most.done else None
    beyond = (
        asked is not None
        and low is not None
        and high is not None
        and low.value < asked - REACHED
        and asked >= high.value - REACHED
    )
    most_proven = _proven(most)[0] if most is not None and most.done else None
    if reach is not None and reach.done:
        shown.append(("target", reach))
    elif (
        beyond
        and most is not None
        and high is not None
        and low is not None
        and high.value > low.value + REACHED
    ):
        # the target is above the most the rules allow: the plan is built to the most (when the
        # cheapest plan is already there, there is no further phase to show)
        shown.append(("most", most))

    phases: list[dict[str, Any]] = []
    prev: Step | None = None
    for i, (key, step) in enumerate(shown, start=1):
        phases.append(_phase(i, key, step, prev, route, book))
        prev = step

    first = next((s for s in steps if s.done), None)
    today = None
    if first is not None and first.summary is not None:
        load = sv(first.summary["settlement"]["load_kwh"])
        today = {
            "annual_cost": first.summary["annual"]["before"],
            "load_kwh": _j(load),
            "share": _j(derive(ZERO.quantize(PCT), Unit.PERCENT, [load], label=SHARE_LABEL)),
        }

    # the cost curve: today, each phase, the further points and the most the rules allow
    curve: list[dict[str, Any]] = []
    for key, step in shown:
        if key != "most":
            curve.append(_point("phase" if key != "target" else "target", PHASE_TITLE[key], step))
    for s in steps:
        if s.role == "curve" and s.done:
            curve.append(_point("curve", f"The cheapest plan that reaches {_pct(s.level_pct or ZERO)}", s))
    if most is not None and most.done:
        curve.append(_point("most", "The most the rules allow", most))
    curve.sort(key=lambda c: Decimal(c["share"]["value"]))
    least_cost = [c for c in curve if c["role"] == "phase"]
    if least_cost:
        min(least_cost, key=lambda c: Decimal(c["annual_cost"]["value"]))["cheapest"] = True

    # the target: what was asked, what is reached, the most the rules allow and what holds it there
    final = shown[-1][1] if shown else None
    reached = share_of(final.summary or {}) if final is not None else None
    reached_in = next(
        (
            ph["key"]
            for ph in phases
            if asked is not None and Decimal(ph["share"]["value"]) >= asked - REACHED
        ),
        None,
    )
    asked_sv = (
        SourcedValue(
            value=asked,
            unit=Unit.PERCENT,
            label="Your target",
            source_refs=(SourceRef(kind=RefKind.CLIENT_STATED, note="target share of renewable energy"),),
        )
        if asked is not None
        else None
    )
    words: list[str] = []
    done_all = plan_status != "running"
    if not shown:
        words.append(
            "Nothing could be sized for this connection yet."
            if done_all
            else "The plan is being worked out: its first phase has not finished."
        )
    elif asked is None:
        if low is not None:
            words.append(
                f"The cheapest plan replaces {_pct(low.value)} of the energy you draw from the DISCOM today."
            )
        if high is not None and low is not None and high.value > low.value + REACHED:
            words.append(
                (
                    f"The most the rules in force allow is {_pct(high.value)}"
                    if most_proven
                    else f"The rules in force allow at least {_pct(high.value)} (the search for the most "
                    "ended on its time limit, so more may be possible)"
                )
                + "; the cost curve shows what each further step costs."
            )
    elif reached_in is not None and reached_in not in ("target", "most"):
        words.append(
            f"Your target of {_pct(asked)} is reached in phase "
            f"{next(ph['order'] for ph in phases if ph['key'] == reached_in)} of the cheapest plan."
        )
    elif reach is not None and reach.done and low is not None:
        words.append(
            f"The cheapest plan stops at {_pct(low.value)}. Reaching your target of {_pct(asked)} takes the "
            "further step shown last: more capacity than is cheapest, at the least cost that reaches it."
        )
    elif beyond and high is not None and most_proven:
        words.append(
            f"Your target of {_pct(asked)} cannot be reached under the rules in force: the most this "
            f"connection can reach is {_pct(high.value)}. "
            + (
                "The plan is built to that."
                if shown[-1][0] == "most"
                else "The cheapest plan already reaches it."
            )
        )
    elif beyond and high is not None:
        words.append(
            f"Your target of {_pct(asked)} was not reached: the search for the most the rules allow got to "
            f"{_pct(high.value)} before its time limit, so it is not shown that {_pct(asked)} cannot be "
            "reached. A longer search may go further."
        )
    elif done_all and reached is not None:
        words.append(
            f"Your target of {_pct(asked)} was not reached: the plan gets to {_pct(reached.value)}"
            + ("; how far the rules would let it go could not be worked out." if high is None else ".")
        )
    target_out = {
        "kind": target.kind,
        "asked": _j(asked_sv),
        "reached": _j(reached),
        "met": (reached is not None and reached.value >= asked - REACHED) if asked is not None else None,
        "reached_in_phase": reached_in,
        "most": _j(high),
        # False: the search for the most ended on its time limit, so the true most may be higher
        "most_proven": most_proven,
        # what holds the share at the most the rules allow, each with its source
        "most_binding": [
            {"name": b["name"], "words": b["words"], "refs": b["refs"]}
            for b in ((most.summary or {}).get("binding", []) if most is not None and most.done else [])
        ],
        "most_run_id": most.run_id if most is not None and most.done else None,
        "words": " ".join(words),
    }

    # the routes compared for the open-access phase
    alternatives = []
    for s in steps:
        if s.role != "open_access":
            continue
        covered = routes.get(s.route or "") is not None
        row: dict[str, Any] = {
            "route": s.route,
            "label": ROUTE_WORDS.get(s.route or "", s.route),
            "chosen": oa is not None and s.key == oa.key,
            "settlement_rule_approved": covered,
            "run_id": s.run_id if s.done else None,
            "annual_cost": None,
            "annual_saving": None,
            "capital": None,
            "words": "",
        }
        if s.done and s.summary is not None:
            a = s.summary["annual"]
            row.update(annual_cost=a["after"], annual_saving=a["saving"], capital=a["capex"])
            if row["chosen"]:
                row["words"] = "The route the plan takes: the lowest cost for the year" + (
                    " among the routes whose settlement an approved rule covers."
                    if any(routes.get(x.route or "") is None for x in steps if x.role == "open_access")
                    else "."
                )
            elif not covered and oa is not None and routes.get(oa.route or "") is not None:
                row["words"] = (
                    "Not taken: no approved settlement rule covers this route here, so its figures assume "
                    "no banking and are not yet sourced."
                )
            else:
                row["words"] = "Not taken: its cost for the year is higher."
        elif s.pending:
            row["words"] = "Being worked out."
        else:
            row["words"] = f"Not shown: {_why_failed(s)}."
        alternatives.append(row)

    not_worked_out = [
        {
            "key": s.key,
            "words": f"{PHASE_TITLE.get(s.role, 'A further point of the cost curve')}"
            + (f" ({ROUTE_WORDS[s.route].split(':')[0].lower()})" if s.route in ROUTE_WORDS else "")
            + (f" at {_pct(s.level_pct)}" if s.level_pct is not None else "")
            + f": {_why_failed(s)}.",
        }
        for s in steps
        if not s.pending and not s.done
    ]
    prov = (final.summary or {}).get("provenance") if final is not None else None
    return {
        "status": plan_status,
        "pending_runs": sum(1 for s in steps if s.pending),
        "runs": len(steps),
        "resolution_min": target.resolution_min,
        "state": book.state,
        "discom": book.discom,
        "today": today,
        "target": target_out,
        "phases": phases,
        "curve": curve,
        "alternatives": alternatives,
        "not_worked_out": not_worked_out,
        "provenance": prov,
    }
