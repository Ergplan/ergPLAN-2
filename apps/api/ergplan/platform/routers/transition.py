"""The transition plan (Stage 15): from today's bill to a target (a share of renewable energy, or the
lowest cost) in phases the client can act on — rooftop, then open access, then storage — each with
its capacities, capital or tariff, the year's cost and saving, the rule that holds it and what must
be signed or applied for. Every figure is a SourcedValue read from a settled, pinned run."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ...core.sourced import SourcedValue, SourceRef
from ...db.base import session_scope
from ...db.models import Connection, TransitionPlan
from ...report import share
from ...report.transition import Target
from .. import transition as service
from ..auth import Caller, analyst, caller, owned_connection
from .runs import KINDS, BindingOut, ProvenanceOut

router = APIRouter(tags=["transition plan"])
Anyone = Annotated[Caller, Depends(caller)]

OWNER = "run_results.summary"
_CAPITAL_KINDS: list[JsonValue] = ["assumption", "client_stated"]
_RULE_KINDS: list[JsonValue] = ["order", "regulation", "register"]


def _f(owner: str = OWNER, kinds: list[JsonValue] = KINDS) -> Any:
    return Field(json_schema_extra={"owner": owner, "ref_kinds": kinds})


def _o(owner: str = OWNER, kinds: list[JsonValue] = KINDS) -> Any:
    return Field(default=None, json_schema_extra={"owner": owner, "ref_kinds": kinds})


class TransitionPhaseSourceOut(BaseModel):
    """One source of a phase: its size after the phase, what the phase adds, and its capital (the
    client's own) or tariff (energy bought by the unit), or both."""

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: str
    label: str
    ownership: str | None = None
    capacity: SourcedValue = _f()  # kW after this phase
    added: SourcedValue = _f()  # kW this phase adds
    energy: SourcedValue | None = _o()  # a battery's kWh
    added_energy: SourcedValue | None = _o()
    delivered_kwh: SourcedValue | None = _o()
    yearly_cost: SourcedValue | None = _o()
    capital: SourcedValue | None = _o("scenarios.spec + run_results.summary", _CAPITAL_KINDS)
    capital_words: str | None = None
    tariff: SourcedValue | None = _o("rules.body + scenarios.spec")
    gaps: list[str] = Field(default_factory=list)  # charges not yet sourced, in words


class TransitionPhaseEnergyOut(BaseModel):
    """Where the year's renewable energy goes under the state's settlement."""

    load_kwh: SourcedValue = _f()
    grid_kwh: SourcedValue = _f()
    delivered_kwh: SourcedValue = _f()
    setoff_kwh: SourcedValue = _f()
    banked_kwh: SourcedValue = _f()
    unbanked_kwh: SourcedValue = _f()
    withdrawn_kwh: SourcedValue = _f()
    lapsed_kwh: SourcedValue = _f()


class TransitionRuleLineOut(BaseModel):
    """A sentence restating the state's rule, with the rule row's citation (or, where no approved row
    carries one, a ref that says the sentence is ergPLAN's own)."""

    text: str
    ref: SourceRef
    value: SourcedValue | None = _o("rules.body", _RULE_KINDS)


class TransitionActionOut(BaseModel):
    """Something to sign or apply for. `basis` rule: an approved rule row states it and `ref` cites
    it. `basis` assumption: general practice that no approved rule in the state's rule book states;
    `ref` says so, and the screen never shows it as a rule."""

    what: str
    with_whom: str
    basis: Literal["rule", "assumption"]
    ref: SourceRef


class TransitionPhaseOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str  # rooftop | open_access | storage | target | most
    order: int
    title: str
    what: str
    status: Literal["taken", "adds_nothing"]
    status_words: str | None = None
    route: str | None = None
    route_words: str | None = None
    run_id: str | None
    scenario_id: str | None
    resolution_min: int
    proven: bool  # False: the search ended on its time limit; `proven_words` says so with its gap
    proven_words: str | None = None
    sources: list[TransitionPhaseSourceOut]
    annual_cost: SourcedValue = _f()
    annual_saving: SourcedValue = _f()  # against today's bill
    added_saving: SourcedValue = _f()  # against the phase before
    capital: SourcedValue = _f()
    added_capital: SourcedValue = _f()
    share: SourcedValue = _f()  # of the year's consumption not drawn from the DISCOM, after this phase
    added_share: SourcedValue = _f()
    payback_years: SourcedValue | None = _o()
    irr_pct: SourcedValue | None = _o()
    energy: TransitionPhaseEnergyOut
    binding: list[BindingOut]
    rules: list[TransitionRuleLineOut]
    actions: list[TransitionActionOut]
    notes: list[str]


class TransitionTodayOut(BaseModel):
    annual_cost: SourcedValue = _f()
    load_kwh: SourcedValue = _f()
    share: SourcedValue = _f()


class TransitionTargetOut(BaseModel):
    kind: Literal["lowest_cost", "share"]
    asked: SourcedValue | None = _o("transition_plans.target", ["client_stated"])
    reached: SourcedValue | None = _o()
    met: bool | None
    reached_in_phase: str | None
    most: SourcedValue | None = _o()  # the most the rules in force allow
    most_proven: bool | None = None  # False: that search ended on its time limit; it may be higher
    most_binding: list[BindingOut]
    most_run_id: str | None
    words: str


class TransitionCurveSizeOut(BaseModel):
    label: str
    kind: str
    capacity: SourcedValue = _f()
    energy: SourcedValue | None = _o()


class TransitionCurvePointOut(BaseModel):
    """A point of the cost curve: a share of consumption not drawn from the DISCOM and the least
    yearly cost found for it, with the mix that reaches it."""

    role: Literal["phase", "target", "curve", "most"]
    label: str
    share: SourcedValue = _f()
    annual_cost: SourcedValue = _f()
    annual_saving: SourcedValue = _f()
    capital: SourcedValue = _f()
    sizes: list[TransitionCurveSizeOut]
    run_id: str | None
    proven: bool
    proven_words: str | None = None
    cheapest: bool


class TransitionRouteOut(BaseModel):
    """A way of taking the open-access plant that the plan sized and compared."""

    route: str
    label: str
    chosen: bool
    settlement_rule_approved: bool
    run_id: str | None
    annual_cost: SourcedValue | None = _o()
    annual_saving: SourcedValue | None = _o()
    capital: SourcedValue | None = _o()
    words: str


class TransitionNotWorkedOut(BaseModel):
    key: str
    words: str


class TransitionOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    connection_id: str
    created_at: datetime
    finished_at: datetime | None
    status: Literal["running", "done", "failed"]
    pending_runs: int
    runs: int
    resolution_min: int
    state: str
    discom: str
    asked: Target
    today: TransitionTodayOut | None
    target: TransitionTargetOut
    phases: list[TransitionPhaseOut]
    curve: list[TransitionCurvePointOut]
    alternatives: list[TransitionRouteOut]
    not_worked_out: list[TransitionNotWorkedOut]
    provenance: ProvenanceOut | None  # the load profile and prices the phases were sized on


class TransitionReplayedRun(BaseModel):
    key: str
    run_id: str
    replay_run_id: str
    status: str
    identical: bool | None


class TransitionReplayOut(BaseModel):
    plan_id: str
    runs: list[TransitionReplayedRun]
    identical: bool | None  # every run identical to its original, to the paisa


@router.post("/connections/{connection_id}/transition-plan", response_model=TransitionOut, status_code=202)
def start_transition_plan(
    connection_id: str,
    body: Target,
    who: Anyone,
    wait: Annotated[bool, Query(description="solve every run in the request (tests)")] = False,
    fresh: Annotated[bool, Query(description="solve again although a plan for this target exists")] = False,
) -> TransitionOut:
    """Work out the connection's transition plan for a target. The plan's runs are solved one after
    another by the worker; the answer says how many are still to come. Asking again with the same
    target returns the same plan."""
    with session_scope() as s:
        owned_connection(s, who, connection_id)
        if s.get(Connection, connection_id) is None:
            raise HTTPException(404, "connection not found")
    try:
        d = service.transition_plan(connection_id, body, who.actor, who.role, wait=wait, fresh=fresh)
    except service.PlanNotFound as e:
        raise HTTPException(404, "connection not found") from e
    return TransitionOut.model_validate(d)


@router.get("/connections/{connection_id}/transition-plan", response_model=TransitionOut)
def latest_transition_plan(connection_id: str, who: Anyone) -> TransitionOut:
    """The connection's latest transition plan as it stands (its finished phases; `status` running
    while runs are still being solved)."""
    with session_scope() as s:
        owned_connection(s, who, connection_id)
        plan = service.latest(s, connection_id)
        if plan is None:
            raise HTTPException(404, "no transition plan for this connection yet")
        return TransitionOut.model_validate(service.view(s, plan))


@router.get("/transition-plans/{plan_id}", response_model=TransitionOut)
def get_transition_plan(plan_id: str, who: Anyone) -> TransitionOut:
    with session_scope() as s:
        plan = s.get(TransitionPlan, plan_id)
        if plan is None:
            raise HTTPException(404, "transition plan not found")
        owned_connection(s, who, plan.connection_id)
        return TransitionOut.model_validate(service.view(s, plan))


@router.post("/transition-plans/{plan_id}/replay", response_model=TransitionReplayOut, status_code=202)
def replay_transition_plan(
    plan_id: str,
    who: Annotated[Caller, Depends(analyst)],
    wait: Annotated[bool, Query()] = False,
) -> TransitionReplayOut:
    """Recompute every finished run of the plan from its pins (rule versions, load profile, price
    vintages, as-of date). `identical` says whether each matches its original to the paisa."""
    try:
        return TransitionReplayOut.model_validate(service.replay(plan_id, who.actor, who.role, wait=wait))
    except service.PlanNotFound as e:
        raise HTTPException(404, "transition plan not found") from e


@router.get("/p/{token}/transition-plan", response_model=TransitionOut)
def shared_transition_plan(token: str) -> TransitionOut:
    """The transition plan of a shared plan, logged out (the share link's connection)."""
    with session_scope() as s:
        try:
            cid = share.resolve(s, token).connection_id
        except share.LinkNotFound as e:
            raise HTTPException(404, "this link does not exist or was withdrawn") from e
        plan = service.latest(s, cid)
        if plan is None:
            raise HTTPException(404, "no transition plan for this connection yet")
        return TransitionOut.model_validate(service.view(s, plan))
